import ipaddress
import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_from_directory

try:
    import paho.mqtt.client as mqtt
    import paho.mqtt.publish as mqtt_publish
except ImportError:
    mqtt = None
    mqtt_publish = None


APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "stream.log"
PID_FILE = APP_DIR / "stream.pid"
RELAY_LOG_FILE = APP_DIR / "ts_cbr_relay.log"
RELAY_PID_FILE = APP_DIR / "ts_cbr_relay.pid"
TESTCARD_DIR = APP_DIR / "testcards" / "normalized"
SOUNDTRACK_DIR = APP_DIR / "soundtracks" / "normalized"
VIDEO_DIR = APP_DIR / "videos" / "normalized"
GENERATED_ASSET_DIR = APP_DIR / "generated-assets"

DEFAULTS = {
    "camera": "usb",
    "station": "DATV TEST",
    "image": "hb9iiu-test-card.png",
    "movie": "Acrylic paints-datv-720p25.mp4",
    "audio_mode": "none",
    "soundtrack": "test-card-classical.mp3",
    "audio_bitrate_kbps": 48,
    "volume_db": -6.0,
    "ticker_message": "HB9IIU DATV test transmission • QO-100 • 73",
    "ticker_speed": 80,
    "ticker_position": "bottom",
    "codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "bitrate_kbps": 3000,
    "host": "192.168.0.242",
    "port": 5000,
    "output_mode": "preview",
    "pluto_ip": "192.168.0.50",
    "pluto_callsign": "NOCALL",
    "pluto_frequency_mhz": 2405.250,
    "pluto_symbol_rate": 500,
    "pluto_fec": "34",
    "pluto_power_db": -79.0,
    "lnb_lo_mhz": 9750.0,
    "rf_profile": "500_quality",
    "generated_asset": "datv-scroll-reference-720p25-v1.mkv",
    "gop_frames": 50,
}

app = Flask(__name__)
lock = threading.RLock()
stream_process = None  # type: Optional[subprocess.Popen]
relay_process = None  # type: Optional[subprocess.Popen]
last_command = []
started_at = None  # type: Optional[float]
pluto_status_cache = {"at": 0.0, "data": {}}


def load_settings():
    try:
        return {**DEFAULTS, **json.loads(STATE_FILE.read_text())}
    except (OSError, ValueError, TypeError):
        return DEFAULTS.copy()


def save_settings(settings):
    STATE_FILE.write_text(json.dumps(settings, indent=2) + "\n")


def camera_devices():
    devices = []
    base = Path("/sys/class/video4linux")
    if base.exists():
        for item in sorted(base.glob("video*")):
            try:
                name = (item / "name").read_text().strip()
            except OSError:
                name = "Unknown camera"
            devices.append({"device": f"/dev/{item.name}", "name": name})
    return devices


def process_alive():
    return stream_process is not None and stream_process.poll() is None


def stop_saved_process(pid_file, expected_commands, first_signal):
    """Stop a process left behind by an earlier web-panel instance."""
    try:
        pid = int(pid_file.read_text().strip())
        command = Path("/proc/{}/cmdline".format(pid)).read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        if not any(name in command for name in expected_commands):
            return
        process_group = os.getpgid(pid)
        os.killpg(process_group, first_signal)
        for _ in range(25):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.killpg(process_group, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass


def pluto_url(ip, query):
    try:
        with urllib.request.urlopen("http://{}/requests.php?{}".format(ip, query), timeout=1.5) as response:
            return response.read().decode("ascii", "replace").strip()
    except (OSError, urllib.error.URLError):
        return None


def pluto_ptt(ip, enabled):
    result = pluto_url(ip, "PTT={}".format("on" if enabled else "off"))
    if result is None:
        raise OSError("Pluto is not reachable")


def pluto_capacity(symbol_rate, fec):
    kbch = {"12": 32208, "23": 43040, "34": 48408}[fec]
    efficiency = (32400.0 / (32400.0 + 90.0)) * 2.0 * (kbch / 64800.0)
    return int(round(symbol_rate * 1000.0 * efficiency))


def configure_pluto(settings):
    if mqtt_publish is None:
        raise OSError("Pluto MQTT support is not installed")
    ip = settings["pluto_ip"]
    values = {
        "freq": "{:.3f}".format(settings["pluto_frequency_mhz"]),
        "mode": "DVBS2", "mod": "QPSK",
        "sr": str(settings["pluto_symbol_rate"]),
        "fec": settings["pluto_fec"], "pilots": "Off",
        "frame": "LongFrame", "rolloff": "0.35", "trvlo": "0",
        "power": "{:.1f}".format(settings["pluto_power_db"]),
        "callsign": settings.get("pluto_callsign", "NOCALL"),
        "provname": "HB9IIU Jetson DATV",
    }
    messages = []
    for key, value in values.items():
        messages.append({"topic": "plutodvb/subvar/{}".format(key), "payload": value, "qos": 0, "retain": False})
    messages.append({"topic": "plutodvb/var", "payload": json.dumps(values), "qos": 0, "retain": False})
    messages.append({"topic": "plutodvb/modulator/apply", "payload": "1", "qos": 0, "retain": False})
    mqtt_publish.multiple(messages, hostname=ip, port=1883, client_id="jetson-datv-panel")
    pluto_url(ip, "gain={:.1f}".format(settings["pluto_power_db"]))


def read_pluto_status(ip):
    global pluto_status_cache
    now = time.time()
    if pluto_status_cache.get("ip") == ip and now - pluto_status_cache["at"] < 2.0:
        return pluto_status_cache["data"]
    data = {"reachable": False, "ptt": False, "buffer": "Unknown", "stats": {}}
    onair = pluto_url(ip, "onair")
    if onair is not None:
        data.update({"reachable": True, "ptt": onair == "0"})
    if mqtt is not None and data["reachable"]:
        received = {}
        client = mqtt.Client(client_id="jetson-datv-status", clean_session=True)
        client.on_connect = lambda c, _u, _f, _rc: c.subscribe("plutodvb/status/#")
        client.on_message = lambda _c, _u, msg: received.__setitem__(msg.topic, msg.payload.decode("utf-8", "replace"))
        try:
            client.connect(ip, 1883, 2)
            client.loop_start()
            time.sleep(0.3)
            client.loop_stop()
            client.disconnect()
        except OSError:
            pass
        data["buffer"] = received.get("plutodvb/status/ts/bufferstate", "Unknown")
        # Expose every status topic the firmware published, keyed by the path
        # after "plutodvb/status/" (e.g. "ts/bufferstate", "rf/temp"). The UI
        # renders these generically so new firmware fields appear automatically.
        stats = {}
        for topic, value in received.items():
            stats[topic.split("plutodvb/status/", 1)[-1]] = value
            if "temp" in topic.lower():
                data["temperature"] = value
        data["stats"] = stats
    pluto_status_cache = {"at": now, "ip": ip, "data": data}
    return data


def stop_stream():
    global stream_process, relay_process
    settings = load_settings()
    if settings.get("output_mode") == "pluto":
        try:
            pluto_ptt(settings.get("pluto_ip", "192.168.0.50"), False)
        except OSError:
            pass
    if process_alive():
        os.killpg(stream_process.pid, signal.SIGINT)
        try:
            stream_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(stream_process.pid, signal.SIGKILL)
            stream_process.wait(timeout=2)
    stream_process = None
    stop_saved_process(
        PID_FILE,
        ("gst-launch-1.0", "ticker_stream.py", "orbital_stream.py", "asset_stream.py"),
        signal.SIGINT,
    )
    if relay_process is not None and relay_process.poll() is None:
        os.killpg(relay_process.pid, signal.SIGTERM)
        try:
            relay_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(relay_process.pid, signal.SIGKILL)
            relay_process.wait(timeout=1)
    relay_process = None
    stop_saved_process(RELAY_PID_FILE, ("ts_cbr_relay.py",), signal.SIGTERM)


def validated_settings(data):
    current = load_settings()
    settings = {
        "camera": str(data.get("camera", "usb")),
        "station": str(data.get("station", "DATV TEST")).strip(),
        "image": str(data.get("image", "hb9iiu-test-card.png")),
        "movie": str(data.get("movie", "Acrylic paints-datv-720p25.mp4")),
        "audio_mode": str(data.get("audio_mode", "none")),
        "soundtrack": str(data.get("soundtrack", "test-card-classical.mp3")),
        "audio_bitrate_kbps": int(data.get("audio_bitrate_kbps", 48)),
        "volume_db": float(data.get("volume_db", -6.0)),
        "ticker_message": str(data.get("ticker_message", "HB9IIU DATV test transmission")).strip(),
        "ticker_speed": int(data.get("ticker_speed", 80)),
        "ticker_position": str(data.get("ticker_position", "bottom")),
        "codec": str(data.get("codec", "h264")),
        "width": int(data.get("width", 1280)),
        "height": int(data.get("height", 720)),
        "fps": int(data.get("fps", 30)),
        "bitrate_kbps": int(data.get("bitrate_kbps", 3000)),
        "host": str(data.get("host", "192.168.0.242")),
        "port": int(data.get("port", 5000)),
        "output_mode": str(data.get("output_mode", "preview")),
        "pluto_ip": str(data.get("pluto_ip", "192.168.0.50")),
        "pluto_callsign": str(data.get("pluto_callsign", current.get("pluto_callsign", "NOCALL"))).strip().upper(),
        "pluto_frequency_mhz": float(data.get("pluto_frequency_mhz", 2405.250)),
        "pluto_symbol_rate": int(data.get("pluto_symbol_rate", 500)),
        "pluto_fec": str(data.get("pluto_fec", "34")),
        "pluto_power_db": float(data.get("pluto_power_db", -79.0)),
        "lnb_lo_mhz": float(data.get("lnb_lo_mhz", 9750.0)),
        "rf_profile": str(data.get("rf_profile", "custom")),
        "generated_asset": str(data.get("generated_asset", current.get("generated_asset", ""))),
        "gop_frames": int(data.get("gop_frames", current.get("gop_frames", 50))),
    }
    if settings["camera"] not in {"testcard", "imagecard", "ticker", "orbital", "movie", "usb", "csi", "generated_asset"}:
        raise ValueError("Unknown source")
    if not settings["pluto_callsign"] or len(settings["pluto_callsign"]) > 16:
        raise ValueError("Callsign must contain 1 to 16 characters")
    if not settings["station"] or len(settings["station"]) > 40:
        raise ValueError("Station label must contain 1 to 40 characters")
    image_path = TESTCARD_DIR / settings["image"]
    if (Path(settings["image"]).name != settings["image"] or
            image_path.suffix.lower() != ".png" or not image_path.is_file()):
        raise ValueError("Unknown test-card image")
    movie_path = VIDEO_DIR / settings["movie"]
    if (settings["movie"].startswith(".") or
            Path(settings["movie"]).name != settings["movie"] or
            movie_path.suffix.lower() != ".mp4" or not movie_path.is_file()):
        raise ValueError("Unknown movie")
    if settings["audio_mode"] not in {"none", "manual", "movie"}:
        raise ValueError("Unknown audio mode")
    soundtrack_path = SOUNDTRACK_DIR / settings["soundtrack"]
    if (Path(settings["soundtrack"]).name != settings["soundtrack"] or
            soundtrack_path.suffix.lower() not in {".mp3", ".wav"} or
            not soundtrack_path.is_file()):
        raise ValueError("Unknown soundtrack")
    if settings["audio_bitrate_kbps"] not in {32, 48, 64, 96}:
        raise ValueError("Unsupported audio bitrate")
    if not -40.0 <= settings["volume_db"] <= 0.0:
        raise ValueError("Volume must be between -40 and 0 dB")
    if not settings["ticker_message"] or len(settings["ticker_message"]) > 240:
        raise ValueError("Ticker message must contain 1 to 240 characters")
    if not 20 <= settings["ticker_speed"] <= 240:
        raise ValueError("Ticker speed must be between 20 and 240 pixels per second")
    if settings["ticker_position"] not in {"top", "bottom"}:
        raise ValueError("Unknown ticker position")
    if settings["codec"] not in {"h264", "h265"}:
        raise ValueError("Unknown codec")
    if (settings["width"], settings["height"]) not in {
        (640, 360), (640, 480), (960, 540), (1280, 720), (1920, 1080)
    }:
        raise ValueError("Unsupported resolution")
    if settings["fps"] not in {15, 25, 30, 50}:
        raise ValueError("Unsupported frame rate")
    if not 100 <= settings["bitrate_kbps"] <= 20000:
        raise ValueError("Bitrate must be between 100 and 20000 kb/s")
    if not 1 <= settings["port"] <= 65535:
        raise ValueError("Invalid UDP port")
    try:
        ipaddress.ip_address(settings["host"])
    except ValueError:
        raise ValueError("Enter a numeric IPv4 or IPv6 destination")
    if settings["output_mode"] not in {"preview", "pluto"}:
        raise ValueError("Unknown output mode")
    try:
        ipaddress.IPv4Address(settings["pluto_ip"])
    except ipaddress.AddressValueError:
        raise ValueError("Enter a numeric Pluto IPv4 address")
    if not 70.0 <= settings["pluto_frequency_mhz"] <= 6000.0:
        raise ValueError("Pluto frequency must be between 70 and 6000 MHz")
    if settings["pluto_symbol_rate"] not in {125, 250, 333, 500}:
        raise ValueError("Symbol rate must be 125, 250, 333 or 500 kS/s")
    if settings["pluto_fec"] not in {"12", "23", "34"}:
        raise ValueError("Unsupported DVB-S2 FEC")
    if not -79.0 <= settings["pluto_power_db"] <= 0.0:
        raise ValueError("Pluto power must be between -79 and 0 dB")
    if not 0.0 <= settings["lnb_lo_mhz"] <= 20000.0:
        raise ValueError("Invalid LNB local oscillator")
    if settings["rf_profile"] not in {"500_quality", "500_robust", "333_efficient", "custom"}:
        raise ValueError("Unknown RF profile")
    if settings["camera"] == "generated_asset":
        asset_path = GENERATED_ASSET_DIR / settings["generated_asset"]
        if (settings["generated_asset"].startswith(".") or
                Path(settings["generated_asset"]).name != settings["generated_asset"] or
                asset_path.suffix.lower() != ".mkv" or not asset_path.is_file()):
            raise ValueError("Unknown generated MKV asset")
    if not 1 <= settings["gop_frames"] <= 300:
        raise ValueError("GOP must contain 1 to 300 frames")
    if settings["output_mode"] == "pluto":
        has_audio = settings["audio_mode"] == "manual" or settings["camera"] == "movie"
        payload = (settings["bitrate_kbps"] +
                   (settings["audio_bitrate_kbps"] if has_audio else 0)) * 1000
        capacity = pluto_capacity(settings["pluto_symbol_rate"], settings["pluto_fec"])
        if payload > capacity * 0.92:
            raise ValueError("Video and audio bitrates are too high for this DVB-S2 profile")
    return settings


def build_pipeline(s):
    if s["camera"] == "generated_asset":
        return ["/usr/bin/python3", str(APP_DIR / "asset_stream.py"), json.dumps(s)]
    if s["camera"] == "ticker":
        return ["/usr/bin/python3", str(APP_DIR / "ticker_stream.py"), json.dumps(s)]
    if s["camera"] == "orbital":
        return ["/usr/bin/python3", str(APP_DIR / "orbital_stream.py"), json.dumps(s)]
    caps = f"width={s['width']},height={s['height']},framerate={s['fps']}/1"
    encoder = "nvv4l2h264enc" if s["codec"] == "h264" else "nvv4l2h265enc"
    parser = "h264parse" if s["codec"] == "h264" else "h265parse"
    bitrate = str(s["bitrate_kbps"] * 1000)

    if s["camera"] == "movie":
        volume = 10 ** (s["volume_db"] / 20.0)
        movie = (VIDEO_DIR / s["movie"]).as_uri()
        return [
            "gst-launch-1.0", "-e",
            "uridecodebin", f"uri={movie}", "name=dec",
            "mpegtsmux", "name=mux", "alignment=7", "!",
            "udpsink", f"host={s['host']}", f"port={s['port']}",
            "sync=true", "async=false", "buffer-size=1048576",
            "dec.", "!", "queue", "max-size-time=3000000000", "!",
            "nvvidconv", "!", "video/x-raw,format=I420", "!",
            "videoscale", "add-borders=true", "!", "videorate", "!",
            f"video/x-raw,width={s['width']},height={s['height']},framerate={s['fps']}/1,pixel-aspect-ratio=1/1", "!",
            "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
            encoder, f"bitrate={bitrate}", "insert-sps-pps=true", f"iframeinterval={s['fps']}", "!",
            parser, "config-interval=1", "!", "queue", "!", "mux.",
            "dec.", "!", "queue", "max-size-time=3000000000", "!",
            "audioconvert", "!", "audioresample", "!", "audiorate", "!",
            "audio/x-raw,format=S16LE,rate=48000,channels=1", "!",
            "volume", f"volume={volume:.6f}", "!",
            "voaacenc", f"bitrate={s['audio_bitrate_kbps'] * 1000}", "!",
            "aacparse", "!", "queue", "!", "mux.",
        ]

    if s["camera"] == "imagecard":
        source = [
            "filesrc", f"location={TESTCARD_DIR / s['image']}", "!",
            "pngdec", "!", "imagefreeze", "!", "videoconvert", "!",
            "videoscale", "add-borders=true", "!",
            f"video/x-raw,width={s['width']},height={s['height']},framerate={s['fps']}/1,pixel-aspect-ratio=1/1", "!",
            "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        ]
    elif s["camera"] == "testcard":
        source = [
            "videotestsrc", "is-live=true", "pattern=smpte", "!",
            f"video/x-raw,{caps}", "!",
            "clockoverlay", "time-format=%Y-%m-%d %H:%M:%S UTC",
            "halignment=right", "valignment=top", "shaded-background=true",
            "font-desc=Sans Bold 24", "!",
            "textoverlay", f"text={s['station']}", "halignment=left",
            "valignment=bottom", "shaded-background=true", "font-desc=Sans Bold 32", "!",
            "timeoverlay", "text=LIVE +", "halignment=right", "valignment=bottom",
            "shaded-background=true", "font-desc=Sans Bold 20", "!",
            "videoconvert", "!", "nvvidconv", "!",
            "video/x-raw(memory:NVMM),format=NV12", "!",
        ]
    elif s["camera"] == "usb":
        # The C920 advertises 15 and 30 fps at these MJPEG sizes, but not 25.
        # Capture at 30 and let videorate produce a standards-friendly 25 fps.
        camera_fps = 30 if s["fps"] == 25 else s["fps"]
        source = [
            "v4l2src", "device=/dev/video1", "do-timestamp=true", "!",
            f"image/jpeg,width={s['width']},height={s['height']},framerate={camera_fps}/1", "!",
            "jpegdec", "!",
        ]
        if camera_fps != s["fps"]:
            source += ["videorate", "!", f"video/x-raw,framerate={s['fps']}/1", "!"]
        source += [
            "videoconvert", "!",
            "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        ]
    else:
        source = [
            "nvarguscamerasrc", "sensor-id=0", "!",
            f"video/x-raw(memory:NVMM),format=NV12,{caps}", "!",
        ]

    output = [
        "mpegtsmux", "name=mux", "alignment=7", "!",
        "udpsink", f"host={s['host']}", f"port={s['port']}",
        "sync=true", "async=false", "buffer-size=1048576",
    ]
    video = [
        *source,
        encoder, f"bitrate={bitrate}", "insert-sps-pps=true", f"iframeinterval={s['fps']}", "!",
        parser, "config-interval=1", "!", "queue", "!", "mux.",
    ]
    audio = []
    if s["audio_mode"] == "manual":
        soundtrack = SOUNDTRACK_DIR / s["soundtrack"]
        parser_element = "wavparse" if soundtrack.suffix.lower() == ".wav" else "mpegaudioparse"
        decoder_element = [] if soundtrack.suffix.lower() == ".wav" else ["mpg123audiodec", "!"]
        volume = 10 ** (s["volume_db"] / 20.0)
        audio = [
            "multifilesrc", f"location={soundtrack}", "loop=true", "!",
            parser_element, "!", *decoder_element,
            "audioconvert", "!", "audioresample", "!", "audiorate", "!",
            "audio/x-raw,format=S16LE,rate=48000,channels=1", "!",
            "volume", f"volume={volume:.6f}", "!",
            "voaacenc", f"bitrate={s['audio_bitrate_kbps'] * 1000}", "!",
            "aacparse", "!", "queue", "!", "mux.",
        ]
    return ["gst-launch-1.0", "-e", *output, *video, *audio]


@app.get("/")
def index():
    return render_template("index.html", settings=load_settings())


@app.get("/rf")
def rf_console():
    return render_template("rf.html", settings=load_settings())


@app.get("/lab")
def mkv_lab():
    return render_template("lab.html", settings=load_settings())


@app.get("/api/generated-assets")
def generated_assets():
    assets = []
    if GENERATED_ASSET_DIR.exists():
        for path in sorted(GENERATED_ASSET_DIR.glob("*.mkv")):
            if not path.is_file() or path.name.startswith("."):
                continue
            item = {"name": path.name, "size_bytes": path.stat().st_size}
            try:
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,width,height,r_frame_rate",
                    "-show_entries", "format=duration,bit_rate", "-of", "json", str(path),
                ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   universal_newlines=True, timeout=8, check=True)
                info = json.loads(probe.stdout)
                stream = (info.get("streams") or [{}])[0]
                fmt = info.get("format") or {}
                item.update({
                    "codec": stream.get("codec_name", "unknown"),
                    "width": stream.get("width"), "height": stream.get("height"),
                    "frame_rate": stream.get("r_frame_rate", "unknown"),
                    "duration": float(fmt.get("duration", 0) or 0),
                    "source_bitrate": int(fmt.get("bit_rate", 0) or 0),
                })
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            assets.append(item)
    return jsonify({"assets": assets})


@app.get("/api/testcards")
def testcards():
    cards = [p.name for p in sorted(TESTCARD_DIR.glob("*.png")) if p.is_file()]
    return jsonify({"cards": cards})


@app.get("/api/videos")
def videos():
    movies = [p.name for p in sorted(VIDEO_DIR.glob("*.mp4"))
              if p.is_file() and not p.name.startswith(".")]
    return jsonify({"videos": movies})


@app.get("/videos/<path:filename>")
def movie_file(filename):
    return send_from_directory(str(VIDEO_DIR), filename)


@app.get("/testcards/<path:filename>")
def testcard_image(filename):
    return send_from_directory(str(TESTCARD_DIR), filename)


@app.get("/api/soundtracks")
def soundtracks():
    tracks = [
        p.name for p in sorted(SOUNDTRACK_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in {".mp3", ".wav"}
    ] if SOUNDTRACK_DIR.exists() else []
    return jsonify({"tracks": tracks})


@app.get("/soundtracks/<path:filename>")
def soundtrack_audio(filename):
    return send_from_directory(str(SOUNDTRACK_DIR), filename)


@app.get("/api/status")
def status():
    with lock:
        running = process_alive()
        settings = load_settings()
        pluto = read_pluto_status(settings["pluto_ip"]) if settings.get("output_mode") == "pluto" else None
        return jsonify({
            "running": running,
            "pid": stream_process.pid if running else None,
            "uptime_seconds": round(time.time() - started_at) if running and started_at else 0,
            "return_code": None if running or stream_process is None else stream_process.returncode,
            "settings": settings,
            "pluto": pluto,
            "rf_capacity_bps": pluto_capacity(settings["pluto_symbol_rate"], settings["pluto_fec"]),
            "cameras": camera_devices(),
            "command": last_command,
        })


@app.get("/api/log")
def log():
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()[-80:]
    except OSError:
        lines = []
    return jsonify({"lines": lines})


@app.post("/api/start")
def start():
    global stream_process, relay_process, last_command, started_at
    with lock:
        try:
            settings = validated_settings(request.get_json(force=True))
            stop_stream()
            save_settings(settings)
            pipeline_settings = settings.copy()
            if settings["output_mode"] == "pluto":
                pluto_ptt(settings["pluto_ip"], False)
                configure_pluto(settings)
                relay_log_handle = RELAY_LOG_FILE.open("w")
                relay_process = subprocess.Popen(
                    ["/usr/bin/python3", str(APP_DIR / "ts_cbr_relay.py"),
                     "--listen-port", "10000", "--destination", settings["pluto_ip"],
                     "--port", "8282", "--bitrate",
                     str(pluto_capacity(settings["pluto_symbol_rate"], settings["pluto_fec"]))],
                    stdout=relay_log_handle, stderr=subprocess.STDOUT, start_new_session=True,
                )
                RELAY_PID_FILE.write_text("{}\n".format(relay_process.pid))
                pipeline_settings.update({"host": "127.0.0.1", "port": 10000})
            last_command = build_pipeline(pipeline_settings)
            log_handle = LOG_FILE.open("w")
            stream_process = subprocess.Popen(
                last_command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=dict(os.environ, TZ="UTC"),
            )
            PID_FILE.write_text(f"{stream_process.pid}\n")
            started_at = time.time()
            # Best-effort liveness heuristic: give the pipeline a moment to fail
            # fast (bad caps, missing device) before reporting success. This can
            # miss slow failures and won't catch errors printed after the window;
            # /api/status + /api/log surface anything that dies later.
            time.sleep(0.8)
            try:
                startup_log = LOG_FILE.read_text(errors="replace")
            except OSError:
                startup_log = ""
            if stream_process.poll() is not None or "ERROR:" in startup_log:
                stop_stream()
                return jsonify({"ok": False, "error": "Pipeline rejected these settings; see encoder log"}), 500
            if settings["output_mode"] == "pluto":
                if relay_process.poll() is not None:
                    stop_stream()
                    return jsonify({"ok": False, "error": "Transport-stream relay did not start"}), 500
                pluto_ptt(settings["pluto_ip"], True)
            return jsonify({"ok": True, "pid": stream_process.pid})
        except (ValueError, OSError) as exc:
            stop_stream()
            return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/stop")
def stop():
    with lock:
        stop_stream()
    return jsonify({"ok": True})


@app.post("/api/pluto/power")
def set_pluto_power():
    with lock:
        try:
            power = float(request.get_json(force=True).get("power_db"))
            if not -79.0 <= power <= 0.0:
                raise ValueError("Pluto power must be between -79 and 0 dB")
            settings = load_settings()
            ip = settings.get("pluto_ip", "192.168.0.50")
            if pluto_url(ip, "gain={:.1f}".format(power)) is None:
                raise OSError("Pluto is not reachable")
            settings["pluto_power_db"] = power
            save_settings(settings)
            return jsonify({"ok": True, "power_db": power})
        except (TypeError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/pluto/status")
def pluto_status():
    settings = load_settings()
    return jsonify({"ok": True, "pluto": read_pluto_status(settings["pluto_ip"]),
                    "settings": settings})


@app.post("/api/pluto/ptt")
def set_pluto_ptt():
    with lock:
        try:
            enabled = bool(request.get_json(force=True).get("enabled"))
            settings = load_settings()
            pluto_ptt(settings["pluto_ip"], enabled)
            pluto_status_cache["at"] = 0.0
            return jsonify({"ok": True, "ptt": enabled})
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/pluto/rf-config")
def set_pluto_rf_config():
    with lock:
        try:
            data = request.get_json(force=True)
            settings = load_settings()
            frequency = float(data.get("frequency_mhz"))
            symbol_rate = int(data.get("symbol_rate"))
            fec = str(data.get("fec"))
            power = float(data.get("power_db"))
            callsign = str(data.get("callsign", "NOCALL")).strip().upper()
            if not 2400.0 <= frequency <= 2410.0:
                raise ValueError("Uplink frequency must be between 2400 and 2410 MHz")
            if symbol_rate not in {125, 250, 333, 500}:
                raise ValueError("Symbol rate must be 125, 250, 333 or 500 kS/s")
            downlink = frequency + 8089.5
            raster = 0.25 if symbol_rate == 125 else 0.5
            slot = 10492.75 + round((downlink - 10492.75) / raster) * raster
            if not 10492.75 <= slot <= 10499.25 or abs(downlink - slot) > 0.002:
                raise ValueError("Select a permitted QO-100 channel slot on the spectrum")
            if fec not in {"12", "23", "34"}:
                raise ValueError("Unsupported FEC")
            if not -79.0 <= power <= 0.0:
                raise ValueError("Power must be between -79 and 0 dB")
            if not callsign or len(callsign) > 16:
                raise ValueError("Callsign must contain 1 to 16 characters")
            settings.update({"pluto_frequency_mhz": frequency,
                             "pluto_symbol_rate": symbol_rate,
                             "pluto_fec": fec,
                             "pluto_power_db": power,
                             "pluto_callsign": callsign})
            pluto_ptt(settings["pluto_ip"], False)
            configure_pluto(settings)
            save_settings(settings)
            pluto_status_cache["at"] = 0.0
            return jsonify({"ok": True})
        except (TypeError, ValueError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
