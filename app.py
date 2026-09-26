import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, url_for

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from camera_preview import stop_active_preview, stream_camera
from datv_engine import DatvEngine
from dvbs2_profiles import VIDEO_PROFILE_NAMES, PROFILES
from pluto_fft_bridge import get_latest_frame, start_background_reader
import overlay_settings
import pa_relay
import pluto_callsign
import usb_video_key

# The web GUI polls /api/stream/status and /api/telemetry every 1-2s, which
# floods the terminal with a request-log line each time under werkzeug's
# default INFO level - drowning out the real DEBUG prints below. Only
# WARNING and above (e.g. actual errors) still print.
logging.getLogger("werkzeug").setLevel(logging.INFO)

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
pluto_callsign.init(PROJECT_DIR)
# Loaded from pluto_callsign.json (falls back to pluto_callsign.DEFAULT_CALLSIGN
# on first run) rather than a hardcoded literal, so a callsign set via the
# Setup page's /api/pluto/callsign survives an app.py restart - see
# pluto_callsign.py. The Pluto's own firmware stores its side of this
# independently (U-Boot env, via fw_setenv/fw_printenv) - the two only
# agree when /api/pluto/callsign has actually been used to push this value
# to the Pluto too (see stream_start()'s "did not acknowledge" failure mode
# when they drift apart).
PLUTO_CALLSIGN = pluto_callsign.load()
# Deliberately conservative (uppercase letters/digits only, no "/" portable
# suffixes) - this value flows straight into an MQTT topic segment here and
# into `fw_setenv call $param` on the Pluto's own firmware side (see
# mqtt_setcall.sh in the firmware source), so it's worth keeping simple/safe
# rather than accepting the full range of real-world callsign formats.
PLUTO_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,10}$")
# Fallback only, used before any stream has been started - the RX WebFFT is
# centred on our own real, live TX frequency (CURRENT_TX_FREQUENCY_HZ,
# set by stream_start()) to visually confirm real RF is going out (see
# pluto_fft_bridge.py). This is a completely separate MQTT control path
# (app.py's own PLUTO_MQTT_CLIENT) from datv_tx_plus.py's own FREQUENCY_HZ,
# even though both now ultimately come from the same web UI selection.
PLUTO_TX_FREQUENCY_HZ = 2405000000
# Fixed, known-working wide capture span - tried computing a narrow
# per-symbol-rate span instead (2026-09-13) and the Pluto went quiet
# (no frames at all), so this stays wide/reliable and the zoom is done
# client-side instead (see drawLocalFft() in datv.js).
PLUTO_FFT_SPAN_HZ = 9000000
# Confirmed empirically (2026-09-13) across three different symbol rates
# (250/333/500 kS/s) - the real captured width is consistently 2x what we
# publish as rx/webfft/span, not equal to it. Every test showed the known
# signal occupying ~1/6 of our intended-1/3 zoom window, a clean, fixed
# ratio (not random measurement noise) - most likely explained by the
# Pluto's "span" meaning +/- this much from centre, not the total width.
# Used only for our own Hz-per-bin math (labels/cropping) - the MQTT value
# actually published is unchanged, whatever the firmware does with it.
PLUTO_FFT_ACTUAL_WIDTH_HZ = PLUTO_FFT_SPAN_HZ * 2
# DVB-S2 occupied bandwidth is roughly symbol_rate * (1 + rolloff) - 1.35
# matches the same rolloff-derived multiplier already used elsewhere in
# this project (see dvbs2_tx's choose_output_rf_bandwidth() for LimeSDR,
# same idea).
OCCUPIED_BANDWIDTH_FACTOR = 1.35
# The web UI should crop its display to margin + occupied-bandwidth +
# margin, margin == occupied bandwidth - i.e. 3x it - not the whole capture
# span. Computed from the live symbol rate (set_current_symbol_rate()) and
# reported to the frontend via /api/fft, rather than the frontend guessing
# a fixed percentage of the span.
FFT_ZOOM_TO_BANDWIDTH_RATIO = 3
# Set by set_pluto_rx_fft() whenever a stream starts - None until then,
# meaning /api/fft has nothing valid to report yet.
CURRENT_FFT_SPAN_HZ = None
# Set by stream_start() - the live symbol rate in kS/s (333, not 333000 -
# matches the web UI's dropdown value and dvbs2_profiles.py's
# (333, "3/4")-style profile keys), used to size the zoom window in
# /api/fft. None when no stream has been started yet.
CURRENT_SYMBOL_RATE_KSPS = None
# Set by stream_start() to the real live TX frequency (Hz) - the RX WebFFT
# must follow whatever frequency was actually selected, not a fixed
# default, now that frequency is genuinely user-selectable (2026-09-13).
# None until a stream has been started at least once.
CURRENT_TX_FREQUENCY_HZ = None
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


def set_pluto_rx_fft(enabled, frequency_hz=None):
    """Enable/disable the Pluto's own RX WebFFT service over MQTT - a
    separate, independent control path from datv_tx_plus.py's TX
    configuration (same firmware, different MQTT topics, same broker).
    Safe no-op if MQTT isn't connected yet.

    Uses a fixed, known-working wide span (PLUTO_FFT_SPAN_HZ) rather than
    asking the Pluto to narrow its own capture - tried computing a tight
    per-symbol-rate span instead (2026-09-13) and the Pluto appeared to
    reject/ignore it, going quiet with no frames at all. The zoom effect is
    done client-side instead - see drawLocalFft() in datv.js - since we
    already know our own signal sits at the exact centre of this capture
    by construction (rx/webfft/frequency is always set to our own real,
    live TX frequency - frequency_hz, required when enabling).
    """
    global CURRENT_FFT_SPAN_HZ, CURRENT_TX_FREQUENCY_HZ
    if PLUTO_MQTT_CLIENT is None:
        return
    prefix = "cmd/pluto/{}/".format(PLUTO_CALLSIGN)
    if enabled:
        # CURRENT_FFT_SPAN_HZ is the real captured width for our own math
        # (labels/cropping) - the MQTT publish just below still sends the
        # raw PLUTO_FFT_SPAN_HZ value, since that's the number the firmware
        # actually expects regardless of how we interpret its real effect.
        CURRENT_FFT_SPAN_HZ = PLUTO_FFT_ACTUAL_WIDTH_HZ
        CURRENT_TX_FREQUENCY_HZ = frequency_hz
        PLUTO_MQTT_CLIENT.publish(prefix + "rx/webfft/frequency",
                                   payload=str(frequency_hz), qos=1)
        PLUTO_MQTT_CLIENT.publish(prefix + "rx/webfft/span",
                                   payload=str(PLUTO_FFT_SPAN_HZ), qos=1)
        PLUTO_MQTT_CLIENT.publish(prefix + "rx/stream/mode", payload="webfft", qos=1)
        PLUTO_MQTT_CLIENT.publish(prefix + "rx/stream/run", payload="1", qos=1)
    else:
        PLUTO_MQTT_CLIENT.publish(prefix + "rx/stream/run", payload="0", qos=1)


def _current_zoom_span_hz():
    """Shared by /api/fft (frontend display cropping) and RELAY_CONTROLLER
    (server-side stability detection) - both need exactly the same margin +
    occupied-bandwidth + margin window (see FFT_ZOOM_TO_BANDWIDTH_RATIO's
    comment above) computed from the live symbol rate, not two independently
    maintained copies of this formula."""
    if not CURRENT_SYMBOL_RATE_KSPS:
        return None
    occupied_bandwidth_hz = CURRENT_SYMBOL_RATE_KSPS * 1000 * OCCUPIED_BANDWIDTH_FACTOR
    return occupied_bandwidth_hz * FFT_ZOOM_TO_BANDWIDTH_RATIO


_start_pluto_telemetry()
start_background_reader()
usb_video_key.init(PROJECT_DIR)
overlay_settings.init(PROJECT_DIR)
# PA-relay safety interlock - see pa_relay.py's own docstring for the full
# story. Getter callables rather than a direct import keep pa_relay.py from
# ever importing this module back (one-directional dependency).
RELAY_CONTROLLER = pa_relay.RelayController(
    get_stream_state=lambda: STREAM_ENGINE.status()["state"],
    get_latest_fft=get_latest_frame,
    get_span_hz=lambda: CURRENT_FFT_SPAN_HZ,
    get_zoom_span_hz=_current_zoom_span_hz,
)
pa_relay.start_background_monitor(RELAY_CONTROLLER)


def detect_video_devices():
    """Return the V4L2 devices currently exposed by the Jetson.

    The onboard CSI camera (imx219, labelled "vi-output...") is always
    sorted last - it's /dev/video0 by enumeration order, which would
    otherwise put it first/default-selected in the dropdown ahead of any
    USB webcam, even though the webcam is the one usually wanted.
    """
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
    devices.sort(key=lambda device: device["label"].startswith("vi-output"))
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
                    # Cache-buster for the preview URL (?v=...): previews are
                    # cached for 12 h, so an image replaced or renamed under a
                    # name used before would otherwise keep showing the old one.
                    "version": int(os.path.getmtime(os.path.join(TESTCARD_DIR, name))),
                })
    return files


def video_folder_for_sr_fec(symbol_rate, fec):
    """Which preprocessed_<W>x<H>/ folder holds videos for this SR/FEC -
    the same resolution video mode's real transmission uses (see
    VIDEO_PROFILE_NAMES in dvbs2_profiles.py), not a fixed
    hardcoded folder. Returns None for an unsupported SR/FEC combination.
    """
    try:
        profile_name = VIDEO_PROFILE_NAMES[(symbol_rate, fec)]
    except KeyError:
        return None
    width, height = PROFILES[profile_name]["resolution"]
    return "preprocessed_{}x{}".format(width, height)


def _preprocessed_roots():
    """(root_dir, value_prefix) pairs to search for prepared videos - the
    SD card (PROJECT_DIR, always) and the USB video key's mount, only
    while one is actually recognised and mounted (see
    usb_video_key.mounted_root()). value_prefix distinguishes which root a
    given video's "value" came from, so video_thumbnail()/stream_start()
    can resolve it straight back to a real path (via the "root_dir" field
    detect_preprocessed_videos() attaches to each item) without having to
    re-derive or re-scan anything from the value string itself.
    """
    roots = [(PROJECT_DIR, "")]
    usb_root = usb_video_key.mounted_root()
    if usb_root:
        roots.append((usb_root, "usb:"))
    return roots


def detect_preprocessed_videos(folder, root_dir=PROJECT_DIR, value_prefix=""):
    videos = []
    folder_path = os.path.join(root_dir, folder)
    if os.path.isdir(folder_path):
        for name in sorted(os.listdir(folder_path)):
            # "._filename.mkv" is a macOS AppleDouble sidecar file, written
            # automatically by Finder whenever it copies onto a non-HFS+
            # drive (the USB video key's FAT32/exFAT) - not a real video,
            # just metadata junk that happens to also end in .mkv.
            if name.lower().endswith(".mkv") and not name.startswith("._"):
                videos.append({
                    "root_dir": root_dir,
                    "folder": folder,
                    "file": name,
                    "value": "{}{}/{}".format(value_prefix, folder, name),
                    "label": os.path.splitext(name)[0].replace("_", " ").title(),
                })
    return videos


def all_preprocessed_videos():
    """Every prepared video across every preprocessed_<W>x<H>/ folder on
    every currently available root (SD card, plus the USB video key when
    mounted - see _preprocessed_roots()), regardless of which SR/FEC
    currently maps to that resolution - used to validate a requested video
    (thumbnail or stream start) by whitelist, the same pattern
    testcard_preview() already uses, without needing to already know the
    request's SR/FEC.
    """
    videos = []
    for root_dir, value_prefix in _preprocessed_roots():
        for name in sorted(os.listdir(root_dir)):
            if re.fullmatch(r"preprocessed_\d+x\d+", name):
                videos += detect_preprocessed_videos(name, root_dir, value_prefix)
    return videos


@app.route("/")
def index():
    default_video_folder = video_folder_for_sr_fec(DEFAULT_SYMBOL_RATE, DEFAULT_FEC)
    prepared_videos = []
    if default_video_folder:
        for root_dir, value_prefix in _preprocessed_roots():
            prepared_videos += detect_preprocessed_videos(default_video_folder, root_dir, value_prefix)
    return render_template(
        "index.html",
        video_devices=detect_video_devices(),
        audio_devices=detect_audio_inputs(),
        testcards=detect_testcards(),
        prepared_videos=prepared_videos,
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
    videos = []
    for root_dir, value_prefix in _preprocessed_roots():
        videos += [
            {
                "value": video["value"],
                "label": video["label"],
                "preview_url": url_for("video_thumbnail", video=video["value"]),
            }
            for video in detect_preprocessed_videos(folder, root_dir, value_prefix)
        ]
    return jsonify({"videos": videos})


@app.route("/setup")
def setup():
    return render_template("setup.html")


@app.route("/api/usb-key/candidates")
def usb_key_candidates():
    """Polled by the Setup page to list whatever USB drive(s) are
    currently plugged in - see usb_video_key.py for what each entry
    means (mounted_at is None if the OS hasn't auto-mounted it yet)."""
    return jsonify({"candidates": usb_video_key.list_candidates()})


@app.route("/api/usb-key/select", methods=["POST"])
def usb_key_select():
    data = request.get_json(silent=True) or {}
    serial = data.get("serial")
    if not serial:
        return jsonify({"error": "Missing serial"}), 400
    ok, error = usb_video_key.select(serial)
    if not ok:
        return jsonify({"error": error}), 409
    return jsonify({"candidates": usb_video_key.list_candidates()})


@app.route("/api/overlay-settings")
def overlay_settings_get():
    """Current top/bottom banner + marquee on/off and top banner/marquee
    text, for the Setup page to populate its form with on load.
    default_top_banner_text/default_marquee_text are included so the Setup
    page can show real text in those boxes even when the saved override is
    "" (no override saved), instead of a blank field."""
    settings = overlay_settings.load()
    settings["default_top_banner_text"] = overlay_settings.default_top_banner_text()
    settings["default_marquee_text"] = overlay_settings.default_marquee_text()
    return jsonify(settings)


@app.route("/api/overlay-settings", methods=["POST"])
def overlay_settings_post():
    data = request.get_json(silent=True) or {}
    saved = overlay_settings.save(
        data.get("top_banner", True),
        data.get("top_banner_text", ""),
        data.get("bottom_banner", True),
        data.get("marquee", True),
        data.get("marquee_text", ""),
    )
    return jsonify(saved)


@app.route("/api/stream/status")
def stream_status():
    return jsonify(STREAM_ENGINE.status())


@app.route("/api/fft")
def fft():
    """Latest RX WebFFT frame from the Pluto's own local receiver, bridged
    in-process by pluto_fft_bridge.py - bins is null when nothing recent
    has arrived (RX WebFFT not enabled, or the Pluto link is down).
    Includes the known tuning (center/span) so the frontend can label the
    frequency axis and crop to a zoomed-in view around the centre, where
    our own signal always sits by construction (rx/webfft/frequency is
    always our own TX frequency - see set_pluto_rx_fft()).
    """
    return jsonify({
        "bins": get_latest_frame(),
        "center_hz": CURRENT_TX_FREQUENCY_HZ or PLUTO_TX_FREQUENCY_HZ,
        "span_hz": CURRENT_FFT_SPAN_HZ,
        "zoom_span_hz": _current_zoom_span_hz(),
    })


@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    # The camera preview and the real transmission can't both hold the same
    # /dev/videoN open at once - stop any active preview first, regardless
    # of which source is being started, so a leftover preview never causes
    # a spurious "Device or resource busy" failure (real bug, 2026-09-13).
    stop_active_preview()
    try:
        symbol_rate = int(data.get("symbol_rate"))
        fec = str(data.get("fec"))
        gain_db = float(data.get("gain_db"))
        # Frontend sends MHz (e.g. "2405.043", matching frequencyInput's
        # value set by static/js/batc-spectrum.js's green-slot click
        # handler) - convert to whole Hz for the rest of the stack.
        frequency_hz = round(float(data.get("frequency")) * 1e6)
        # Banner/marquee on/off + marquee text come from the Setup page
        # (overlay_settings.py), not this request body - camera/video are
        # the only sources that use them (testcard never asks).
        overlay = overlay_settings.load()
        top_banner = overlay["top_banner"]
        top_banner_text = overlay["top_banner_text"]
        bottom_banner = overlay["bottom_banner"]
        marquee = overlay["marquee"]
        marquee_text = overlay["marquee_text"]

        if source == "testcard":
            testcard = data.get("testcard", "")
            if testcard not in {item["value"] for item in detect_testcards()}:
                return jsonify({"error": "Select a valid testcard"}), 400
            status_data = STREAM_ENGINE.start_testcard(
                testcard, symbol_rate, fec, gain_db, frequency_hz, PLUTO_CALLSIGN)
        elif source == "camera":
            camera_device = data.get("camera_device", "")
            audio_device = data.get("audio_device", "")
            status_data = STREAM_ENGINE.start_camera(
                camera_device, is_csi_camera(camera_device), audio_device,
                symbol_rate, fec, gain_db, frequency_hz, top_banner, top_banner_text,
                bottom_banner, marquee, marquee_text, PLUTO_CALLSIGN)
        elif source == "video":
            requested_video = data.get("video", "")
            selected_video = next(
                (item for item in all_preprocessed_videos() if item["value"] == requested_video),
                None,
            )
            if selected_video is None:
                return jsonify({"error": "Select a valid video"}), 400
            video_path = os.path.join(
                selected_video["root_dir"], selected_video["folder"], selected_video["file"])
            status_data = STREAM_ENGINE.start_video(
                video_path, symbol_rate, fec, gain_db, frequency_hz,
                top_banner, top_banner_text, bottom_banner, marquee, marquee_text, PLUTO_CALLSIGN)
        else:
            return jsonify({"error": "Unknown source"}), 400
    except (ValueError, TypeError) as exc:
        # TypeError included alongside ValueError: int(None)/float(None) -
        # a required field missing from the request body entirely (e.g. an
        # older cached frontend not yet sending it) - raises TypeError, not
        # ValueError, and was otherwise an unhandled 500 that broke the
        # page with an HTML error response instead of a clean JSON one
        # (real bug hit 2026-09-14 testing the frequency field).
        return jsonify({"error": "Missing or invalid field: {}".format(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    global CURRENT_SYMBOL_RATE_KSPS
    CURRENT_SYMBOL_RATE_KSPS = symbol_rate
    set_pluto_rx_fft(True, frequency_hz)
    return jsonify(status_data), 202


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    # Before anything else - synchronous belt-and-suspenders alongside
    # RELAY_CONTROLLER.tick()'s own backstop, so the PA relay drops within
    # this same request rather than waiting for the next monitor tick.
    RELAY_CONTROLLER.force_disengage_and_idle()
    set_pluto_rx_fft(False)
    return jsonify(STREAM_ENGINE.stop())


@app.route("/api/app/restart", methods=["POST"])
def app_restart():
    """Setup page's "Restart app" button. Stops any transmission the same
    way stream_stop() does, then exits - datv-app.service's Restart=always
    starts a fresh app.py 3s later (RestartSec), so no sudo is needed.
    With debug=True's reloader, the served process is a child of
    Werkzeug's watcher, which exits with the child's code (anything but 3),
    so the whole service really exits and systemd takes over.
    """
    # INVOCATION_ID is set by systemd for every service process - without
    # it this app.py was started by hand (PyCharm/terminal), and exiting
    # would just kill it with nothing to bring it back.
    if not os.environ.get("INVOCATION_ID"):
        return jsonify({"error": "app.py isn't running as the datv-app service - "
                                 "restart it where you started it."}), 409
    RELAY_CONTROLLER.force_disengage_and_idle()
    set_pluto_rx_fft(False)
    STREAM_ENGINE.stop()
    app.logger.warning("Restart requested from the web UI - exiting for systemd to restart.")
    # Delayed so this response still reaches the browser.
    threading.Timer(1.0, os._exit, args=(1,)).start()
    return jsonify({"restarting": True}), 202


@app.route("/api/relay/status")
def relay_status():
    return jsonify(RELAY_CONTROLLER.status())


@app.route("/api/relay/engage", methods=["POST"])
def relay_engage():
    """Operator confirmation that it's safe to power the CN0417 pre-amp -
    only succeeds once RELAY_CONTROLLER has independently decided the local
    Pluto RX spectrum has held a stable plateau (state == "ready"). See
    pa_relay.py's docstring - this is a deliberate human-in-the-loop gate
    on top of the automatic detector, not a rubber stamp."""
    ok, state = RELAY_CONTROLLER.request_engage()
    if not ok:
        return jsonify({
            "error": "Relay not ready to engage (state: {})".format(state),
            "state": state,
        }), 409
    return jsonify({"state": state}), 202


@app.route("/api/relay/disengage", methods=["POST"])
def relay_disengage():
    """Manual e-stop - always allowed, no confirmation required (removing
    power is never gated)."""
    return jsonify({"state": RELAY_CONTROLLER.request_disengage()})


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


@app.route("/api/pluto/callsign")
def pluto_callsign_get():
    return jsonify({"callsign": PLUTO_CALLSIGN})


@app.route("/api/pluto/callsign", methods=["POST"])
def pluto_callsign_set():
    """Pushes a new callsign to the Pluto and switches every MQTT topic/
    stream-start this process itself uses over to it too.

    cmd/pluto/call is the one topic the firmware's mqtt_setcall.sh listens
    on unprefixed (see the firmware source) - receiving anything there
    makes it fw_setenv the new value into its U-Boot env and reboot itself
    immediately, regardless of what it had stored before. The Setup page
    shows a "rebooting" spinner and polls /api/telemetry's pluto_connected
    until fresh telemetry arrives again under the new prefix.
    """
    global PLUTO_CALLSIGN
    if PLUTO_MQTT_CLIENT is None:
        return jsonify({"error": "MQTT not available"}), 503
    data = request.get_json(silent=True) or {}
    new_callsign = str(data.get("callsign", "")).strip().upper()
    if not PLUTO_CALLSIGN_RE.match(new_callsign):
        return jsonify({"error": "Callsign must be 3-10 letters/digits"}), 400

    old_callsign = PLUTO_CALLSIGN
    PLUTO_MQTT_CLIENT.publish("cmd/pluto/call", payload=new_callsign, qos=1)
    if new_callsign != old_callsign:
        PLUTO_MQTT_CLIENT.unsubscribe("dt/pluto/{}/#".format(old_callsign))
        PLUTO_MQTT_CLIENT.subscribe("dt/pluto/{}/#".format(new_callsign), qos=1)
    PLUTO_CALLSIGN = new_callsign
    pluto_callsign.save(new_callsign)
    # Force pluto_connected (see telemetry() below) false until real
    # telemetry arrives under the new prefix - without this, the still-
    # under-10s-old last_message from just before the switch would read as
    # "connected" for a few seconds even though the Pluto is rebooting.
    PLUTO_STATE["last_message"] = 0.0
    return jsonify({"callsign": PLUTO_CALLSIGN})


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

    video_path = os.path.join(selected["root_dir"], selected["folder"], selected["file"])
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
    # Port 80 so the web UI is reachable as plain http://jetson-nano.local
    # with no port suffix - needs cap_net_bind_service granted to the
    # python3 binary on the Jetson (setcap, one-time), rather than running
    # this whole process as root: debug=True leaves Werkzeug's interactive
    # debugger reachable, which is a real risk to run with root privileges
    # even on a LAN-only device. See `sudo setcap 'cap_net_bind_service=+ep'
    # $(readlink -f $(which python3))`.
    app.run(host="0.0.0.0", port=80, debug=True, threaded=True)


