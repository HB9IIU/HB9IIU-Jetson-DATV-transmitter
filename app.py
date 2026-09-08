import glob
import os
import re
import shutil
import subprocess

from flask import Flask, Response, abort, render_template, request, send_from_directory

from camera_preview import stream_camera

app = Flask(__name__)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTCARD_DIR = os.path.join(PROJECT_DIR, "testcards")
VIDEO_CATALOG_FOLDER = "preprocessed_1280x720"


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
