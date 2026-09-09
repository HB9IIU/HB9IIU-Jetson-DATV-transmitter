import glob
import os
import re
import shutil
import subprocess
import time

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from camera_preview import stream_camera
from datv_engine import DatvEngine

app = Flask(__name__)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_ENGINE = DatvEngine(PROJECT_DIR)
TESTCARD_DIR = os.path.join(PROJECT_DIR, "testcards")
VIDEO_CATALOG_FOLDER = "preprocessed_1280x720"
PLUTO_IP = "192.168.2.1"
PLUTO_MQTT_PORT = 1883
PLUTO_CALLSIGN = "HB9IIU"
PLUTO_TELEMETRY = {}
PLUTO_STATE = {"connected": False, "last_message": 0.0}


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
    if mqtt is None:
        return

    client = mqtt.Client(client_id="jetson-stream-panel")
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


def detect_preprocessed_videos():
    videos = []
    folder = VIDEO_CATALOG_FOLDER
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


@app.route("/")
def index():
    return render_template(
        "index.html",
        video_devices=detect_video_devices(),
        audio_devices=detect_audio_inputs(),
        testcards=detect_testcards(),
        prepared_videos=detect_preprocessed_videos(),
    )


@app.route("/api/stream/status")
def stream_status():
    return jsonify(STREAM_ENGINE.status())


@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    data = request.get_json(silent=True) or {}
    testcard = data.get("testcard", "")
    if testcard not in {item["value"] for item in detect_testcards()}:
        return jsonify({"error": "Select a valid testcard"}), 400
    try:
        symbol_rate = int(data.get("symbol_rate"))
        fec = str(data.get("fec"))
        status_data = STREAM_ENGINE.start_testcard(testcard, symbol_rate, fec)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(status_data), 202


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    return jsonify(STREAM_ENGINE.stop())


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
    devices = detect_video_devices()
    selected = next(
        (device for device in devices if device["value"] == requested_device),
        None,
    )
    if selected is None:
        abort(404)

    is_csi = selected["label"].startswith("vi-output")
    return Response(
        stream_camera(selected["value"], is_csi),
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
        (item for item in detect_preprocessed_videos() if item["value"] == requested),
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


