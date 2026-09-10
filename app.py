import glob
import logging
import os
import re
import shutil
import subprocess
import time

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, url_for

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from camera_preview import stream_camera
from datv_engine import DatvEngine
from dvbs2_profiles import CAMERA_VIDEO_PROFILE_NAMES, PROFILES

# The web GUI polls /api/stream/status and /api/telemetry every 1-2s, which
# floods the terminal with a request-log line each time under werkzeug's
# default INFO level - drowning out the real DEBUG prints below. Only
# WARNING and above (e.g. actual errors) still print.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_ENGINE = DatvEngine(PROJECT_DIR)
TESTCARD_DIR = os.path.join(PROJECT_DIR, "testcards")
# Matches templates/index.html's hardcoded <option selected> defaults for
# #symbol-rate/#fec - used to pick which preprocessed_<W>x<H>/ folder to
# show on first page load, before any SR/FEC change (see
# static/js/datv.js's refreshPreparedVideos()) has run.
DEFAULT_SYMBOL_RATE = 333
DEFAULT_FEC = "3/4"
PLUTO_IP = "192.168.2.1"
PLUTO_MQTT_PORT = 1883
PLUTO_CALLSIGN = "HB9IIU"
PLUTO_TELEMETRY = {}
PLUTO_STATE = {"connected": False, "last_message": 0.0}
PLUTO_MQTT_CLIENT = None


def format_gain_db(gain_db):
    """tx/gain MQTT payload, snapped to the AD9361's real 0.25 dB step and
    formatted as a plain integer when possible (e.g. "-24", not "-24.0") -
    kept in sync with datv_tx_plus.py's own format_gain_db(), duplicated
    here (not imported) so this module doesn't need GStreamer/PyGObject
    just to publish one MQTT command live while a stream is already
    running under a separate process.
    """
    rounded = round(gain_db * 4) / 4.0
    if rounded == int(rounded):
        return str(int(rounded))
    return "{:.2f}".format(rounded).rstrip("0").rstrip(".")


def _read_number(path, scale=1.0):
    try:
        with open(path) as value_file:
            return float(value_file.read().strip()) * scale
    except (OSError, ValueError):
        return None


def read_cpu_temperature():
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            with open(os.path.join(zone, "type")) as type_file:
                if type_file.read().strip() == "CPU-therm":
                    return _read_number(os.path.join(zone, "temp"), 0.001)
        except OSError:
            continue
    return None


def read_cpu_load():
    try:
        return min(100.0, os.getloadavg()[0] / float(os.cpu_count() or 1) * 100.0)
    except (AttributeError, OSError):
        return None


def read_fan():
    pwm = _read_number("/sys/devices/pwm-fan/cur_pwm")
    if pwm is None:
        pwm = _read_number("/sys/devices/pwm-fan/target_pwm")
    if pwm is None:
        return {"pwm": None, "state": "Unavailable"}
    if pwm <= 0:
        state = "Off"
    elif pwm <= 80:
        state = "Low"
    elif pwm <= 120:
        state = "Medium"
    elif pwm <= 160:
        state = "High"
    else:
        state = "Full"
    return {"pwm": int(pwm), "state": state}


def _start_pluto_telemetry():
    global PLUTO_MQTT_CLIENT
    if mqtt is None:
        return

    client = mqtt.Client(client_id="jetson-stream-panel")
    PLUTO_MQTT_CLIENT = client
    client.username_pw_set("root", "analog")
    client.reconnect_delay_set(min_delay=1, max_delay=10)

    def on_connect(active_client, _userdata, _flags, result_code):
        PLUTO_STATE["connected"] = result_code == 0
        if result_code == 0:
            active_client.subscribe(
                "dt/pluto/{}/#".format(PLUTO_CALLSIGN), qos=1)

    def on_disconnect(_client, _userdata, _result_code):
        PLUTO_STATE["connected"] = False

    def on_message(_client, _userdata, message):
        prefix = "dt/pluto/{}/".format(PLUTO_CALLSIGN)
        if message.topic.startswith(prefix):
            PLUTO_TELEMETRY[message.topic[len(prefix):]] = message.payload.decode(
                "utf-8", "replace")
            PLUTO_STATE["last_message"] = time.time()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(PLUTO_IP, PLUTO_MQTT_PORT, keepalive=5)
    client.loop_start()


_start_pluto_telemetry()


def detect_video_devices():
    """Return the V4L2 devices currently exposed by the Jetson."""
    devices = []
    for path in sorted(glob.glob("/dev/video*")):
        entry = os.path.basename(path)
        if not entry[5:].isdigit():
            continue

        name_path = "/sys/class/video4linux/{}/name".format(entry)
        try:
            with open(name_path) as name_file:
                name = name_file.read().strip()
        except OSError:
            name = "Unknown camera"

        devices.append({"value": path, "label": "{} ({})".format(name, path)})
    return devices


def is_csi_camera(device_value):
    """Whether device_value (a /dev/videoN path) is the onboard CSI camera
    rather than a USB webcam - same "vi-output" label prefix check
    camera_preview() already uses, factored out so /api/stream/start can
    reuse it when passing CAMERA_IS_CSI through to datv_tx_plus.py.
    """
    device = next(
        (item for item in detect_video_devices() if item["value"] == device_value),
        None,
    )
    return bool(device) and device["label"].startswith("vi-output")


def detect_audio_inputs():
    """Return ALSA capture devices reported by ``arecord -l``."""
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    devices = []
    pattern = re.compile(
        r"^card\s+(\d+):\s*([^\[]+)\[([^\]]+)\],\s*device\s+(\d+):\s*([^\[]+)"
    )
    for line in result.stdout.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        card_number, _card_id, card_name, device_number, device_name = match.groups()
        if "ADMAIF" in "{} {}".format(card_name, device_name).upper():
            continue

        alsa_name = "plughw:{},{}".format(card_number, device_number)
        card_name = card_name.strip()
        device_name = device_name.strip()
        friendly_device = "Microphone" if device_name.lower() == "usb audio" else device_name
        label = "{} — {}".format(card_name, friendly_device)
        devices.append({"value": alsa_name, "label": label})
    return devices


def detect_testcards():
    files = []
    if os.path.isdir(TESTCARD_DIR):
        for name in sorted(os.listdir(TESTCARD_DIR)):
            if name.lower().endswith((".png", ".jpg", ".jpeg")):
                files.append({
                    "value": name,
                    "label": os.path.splitext(name)[0].replace("-", " ").replace("_", " ").title(),
                })
    return files


def video_folder_for_sr_fec(symbol_rate, fec):
    """Which preprocessed_<W>x<H>/ folder holds videos for this SR/FEC -
    the same resolution camera/video mode's real transmission uses (see
    CAMERA_VIDEO_PROFILE_NAMES in dvbs2_profiles.py), not a fixed
    hardcoded folder. Returns None for an unsupported SR/FEC combination.
    """
    try:
        profile_name = CAMERA_VIDEO_PROFILE_NAMES[(symbol_rate, fec)]
    except KeyError:
        return None
    width, height = PROFILES[profile_name]["resolution"]
    return "preprocessed_{}x{}".format(width, height)


def detect_preprocessed_videos(folder):
    videos = []
    folder_path = os.path.join(PROJECT_DIR, folder)
    if os.path.isdir(folder_path):
        for name in sorted(os.listdir(folder_path)):
            if name.lower().endswith(".mkv"):
                videos.append({
                    "folder": folder,
                    "file": name,
                    "value": "{}/{}".format(folder, name),
                    "label": os.path.splitext(name)[0].replace("_", " ").title(),
                })
    return videos


def all_preprocessed_videos():
    """Every prepared video across every preprocessed_<W>x<H>/ folder that
    exists on disk, regardless of which SR/FEC currently maps to that
    resolution - used to validate a requested video (thumbnail or stream
    start) by whitelist, the same pattern testcard_preview() already uses,
    without needing to already know the request's SR/FEC.
    """
    videos = []
    for name in sorted(os.listdir(PROJECT_DIR)):
        if re.fullmatch(r"preprocessed_\d+x\d+", name):
            videos += detect_preprocessed_videos(name)
    return videos


@app.route("/")
def index():
    default_video_folder = video_folder_for_sr_fec(DEFAULT_SYMBOL_RATE, DEFAULT_FEC)
    return render_template(
        "index.html",
        video_devices=detect_video_devices(),
        audio_devices=detect_audio_inputs(),
        testcards=detect_testcards(),
        prepared_videos=detect_preprocessed_videos(default_video_folder) if default_video_folder else [],
    )


@app.route("/api/videos")
def api_videos():
    """Prepared videos for a given SR/FEC's resolution - called by
    static/js/datv.js's refreshPreparedVideos() whenever the symbol
    rate/FEC selectors change, so the video picker always matches what
    camera/video mode will actually transmit at, instead of a fixed
    resolution that may not even have any prepared videos.
    """
    try:
        symbol_rate = int(request.args.get("symbol_rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid symbol_rate"}), 400
    fec = str(request.args.get("fec"))
    folder = video_folder_for_sr_fec(symbol_rate, fec)
    if folder is None:
        return jsonify({"error": "Unsupported SR/FEC combination"}), 400
    videos = [
        {
            "value": video["value"],
            "label": video["label"],
            "preview_url": url_for("video_thumbnail", video=video["value"]),
        }
        for video in detect_preprocessed_videos(folder)
    ]
    return jsonify({"videos": videos})


@app.route("/api/stream/status")
def stream_status():
    return jsonify(STREAM_ENGINE.status())


@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    try:
        symbol_rate = int(data.get("symbol_rate"))
        fec = str(data.get("fec"))
        gain_db = float(data.get("gain_db"))
        # Banner/marquee toggles default on - matches ask_yes_no()'s own
        # [Y/n] default in datv_tx_plus.py's interactive prompt, which this
        # replaces for a web-started stream. No dedicated UI control for
        # these yet; add one later if/when that's wanted.
        top_banner = bool(data.get("top_banner", True))
        bottom_banner = bool(data.get("bottom_banner", True))
        marquee = bool(data.get("marquee", True))

        if source == "testcard":
            testcard = data.get("testcard", "")
            if testcard not in {item["value"] for item in detect_testcards()}:
                return jsonify({"error": "Select a valid testcard"}), 400
            status_data = STREAM_ENGINE.start_testcard(testcard, symbol_rate, fec, gain_db)
        elif source == "camera":
            camera_device = data.get("camera_device", "")
            audio_device = data.get("audio_device", "")
            status_data = STREAM_ENGINE.start_camera(
                camera_device, is_csi_camera(camera_device), audio_device,
                symbol_rate, fec, gain_db, top_banner, bottom_banner, marquee)
        elif source == "video":
            status_data = STREAM_ENGINE.start_video(
                data.get("video", ""), symbol_rate, fec, gain_db,
                top_banner, bottom_banner, marquee)
        else:
            return jsonify({"error": "Unknown source"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(status_data), 202


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    return jsonify(STREAM_ENGINE.stop())


@app.route("/api/gain", methods=["POST"])
def set_gain():
    """Live TX gain change, independent of the stream subprocess - publishes
    straight over this process's own persistent MQTT connection so it takes
    effect immediately, including mid-transmission (the real ask: moving
    the slider during TX should change RF power live, like the reference
    Evariste/DATV-Red firmware slider - a one-shot value at stream start
    isn't enough).
    """
    if PLUTO_MQTT_CLIENT is None:
        return jsonify({"error": "MQTT not available"}), 503
    data = request.get_json(silent=True) or {}
    try:
        gain_db = float(data.get("gain_db"))
    except (TypeError, ValueError):
        return jsonify({"error": "gain_db must be a number"}), 400
    # -60 dB, not the AD9361's theoretical -89 dB floor: real spectrum-
    # analyzer measurement (2026-09-10) showed no further measurable RF
    # output change below -60 dB on this specific Pluto/antenna setup.
    if not -60.0 <= gain_db <= 0.0:
        return jsonify({"error": "TX gain must be between -60 and 0 dB"}), 400

    topic = "cmd/pluto/{}/tx/gain".format(PLUTO_CALLSIGN)
    payload = format_gain_db(gain_db)
    PLUTO_MQTT_CLIENT.publish(topic, payload=payload, qos=1)
    return jsonify({"gain_db": payload})


@app.route("/api/telemetry")
def telemetry():
    pluto_temperature = None
    try:
        pluto_temperature = float(PLUTO_TELEMETRY.get("temperature_ad")) / 1000.0
    except (TypeError, ValueError):
        pass
    connected = (PLUTO_STATE["connected"] and
                 time.time() - PLUTO_STATE["last_message"] < 10.0)
    return jsonify({
        "jetson_cpu_temp_c": read_cpu_temperature(),
        "jetson_cpu_load_percent": read_cpu_load(),
        "fan": read_fan(),
        "pluto_connected": connected,
        "pluto_temp_c": pluto_temperature if connected else None,
    })


@app.route("/camera-preview.mjpg")
def camera_preview():
    requested_device = request.args.get("device", "")
    if not any(device["value"] == requested_device for device in detect_video_devices()):
        abort(404)

    return Response(
        stream_camera(requested_device, is_csi_camera(requested_device)),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/testcard-preview/<path:filename>")
def testcard_preview(filename):
    allowed = {item["value"] for item in detect_testcards()}
    if filename not in allowed:
        abort(404)
    return send_from_directory(TESTCARD_DIR, filename)


@app.route("/video-thumbnail")
def video_thumbnail():
    requested = request.args.get("video", "")
    selected = next(
        (item for item in all_preprocessed_videos() if item["value"] == requested),
        None,
    )
    if selected is None:
        abort(404)

    ffmpeg = shutil.which("ffmpeg")
    bundled_ffmpeg = os.path.join(PROJECT_DIR, "ffmpeg-static", "ffmpeg")
    if not ffmpeg and os.path.isfile(bundled_ffmpeg):
        ffmpeg = bundled_ffmpeg
    if not ffmpeg:
        abort(503)

    video_path = os.path.join(PROJECT_DIR, selected["folder"], selected["file"])
    try:
        result = subprocess.run(
            [
                ffmpeg, "-loglevel", "error", "-ss", "1", "-i", video_path,
                "-frames:v", "1", "-vf",
                "scale=640:360:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        abort(503)
    if result.returncode != 0 or not result.stdout:
        abort(503)
    return Response(result.stdout, mimetype="image/jpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


