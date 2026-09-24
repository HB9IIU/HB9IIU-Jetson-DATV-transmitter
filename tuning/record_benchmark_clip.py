"""Record the fixed camera+mic benchmark clip that tune_profiles.py replays.

Why a recording instead of the live camera: every tuning trial has to see
exactly the same frames, otherwise one trial gets more motion than the
next and their measured bitrates aren't comparable. So the camera is
recorded once, and that recording becomes the benchmark.

What it does:
  1. Captures WARMUP_SECONDS + DURATION_SECONDS from the USB camera exactly
     as it delivers it on air (MJPEG 1280x720@30, copied - no re-encode) plus
     the mic as raw PCM, into clips/camera_master_raw.mkv. The warmup part
     doubles as an on-screen countdown and lets the camera's auto exposure/
     focus settle; it's cut off afterwards.
  2. Derives one lossless (FFV1) clip per resolution the tuners use, scaled
     and frame-rated the same way the on-air pipeline does (FPS from
     datv_tx_plus.py), named the way tune_profiles.py's TEST_CLIPS_CAMERA
     expects.

An existing set of clips is never overwritten - it's moved into
clips/old_<timestamp>/ first, so an earlier benchmark stays available for
comparison.

Runs only on the Jetson (needs the camera and ALSA mic). Needs an
interactive terminal when more than one camera is attached (the onboard CSI
imx219 plus the C920) - run it over `ssh -t`, see tuning/README.md. The
camera must be free: stop any transmission/preview in the web UI first.

Usage (from the project root, with the app's venv - plain python3 lacks
paho-mqtt, which datv_tx_plus imports):
    .venv/bin/python3 tuning/record_benchmark_clip.py
"""

import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# This script lives in tuning/, one level below the project root that holds
# datv_tx_plus.py - put the root on sys.path so the import below finds it.
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

import datv_tx_plus as tx  # noqa: E402

CLIPS_DIR = os.path.join(SCRIPT_DIR, "clips")
MASTER_PATH = os.path.join(CLIPS_DIR, "camera_master_raw.mkv")

DURATION_SECONDS = 90
WARMUP_SECONDS = 10
# Same resolutions (and file names) as tune_profiles.py's TEST_CLIPS_CAMERA.
# 640x360 only matters for the video-mode profiles now.
RESOLUTIONS = [(640, 360), (960, 540), (1280, 720)]
# Matches the on-air camera capture mode (datv_tx_plus.camera_capture_size()).
CAPTURE_SIZE = "{}x{}".format(*tx.camera_capture_size(1280, 720))
CAPTURE_FPS = 30

# System ffmpeg (3.4): the Ubuntu build has the v4l2 and alsa inputs this
# needs, which a static build may not.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def clip_path(width, height):
    return os.path.join(CLIPS_DIR, "camera_clip_{}x{}_{}s.mkv".format(
        width, height, DURATION_SECONDS))


def archive_existing_clips():
    existing = [p for p in [MASTER_PATH] + [clip_path(w, h) for w, h in RESOLUTIONS]
                if os.path.exists(p)]
    if not existing:
        return
    archive_dir = os.path.join(CLIPS_DIR, "old_{}".format(time.strftime("%Y-%m-%d_%H%M%S")))
    os.makedirs(archive_dir)
    for path in existing:
        os.rename(path, os.path.join(archive_dir, os.path.basename(path)))
    print("Previous clips moved to {}".format(archive_dir))


def record_master(camera_device, audio_device):
    total_seconds = WARMUP_SECONDS + DURATION_SECONDS
    command = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg", "-video_size", CAPTURE_SIZE,
        "-framerate", str(CAPTURE_FPS), "-thread_queue_size", "512", "-i", camera_device,
        "-f", "alsa", "-ac", "1", "-ar", "48000", "-thread_queue_size", "512",
        "-i", audio_device,
        "-t", str(total_seconds),
        "-c:v", "copy", "-c:a", "pcm_s16le",
        MASTER_PATH,
    ]
    process = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)

    # Wall-clock cues, not ffmpeg's own clock - close enough for a human
    # performance (device start-up adds well under a second), and the
    # warmup is cut off by timestamp afterwards anyway.
    start = time.monotonic()
    print()
    for remaining in range(WARMUP_SECONDS, 0, -1):
        if process.poll() is not None:
            break
        print("   Get ready... {}".format(remaining), flush=True)
        time.sleep(max(0.0, start + (WARMUP_SECONDS - remaining + 1) - time.monotonic()))
    if process.poll() is None:
        print("\n   >>> RECORDING - {} seconds, go! <<<\n".format(DURATION_SECONDS), flush=True)
    next_mark = 10
    while process.poll() is None:
        elapsed = time.monotonic() - start - WARMUP_SECONDS
        if elapsed >= next_mark and next_mark < DURATION_SECONDS:
            print("   ... {}s / {}s".format(next_mark, DURATION_SECONDS), flush=True)
            next_mark += 10
        time.sleep(0.2)

    stderr = process.stderr.read()
    if process.returncode != 0:
        if "busy" in stderr.lower():
            stderr += ("\nThe camera is in use - stop any transmission or camera "
                       "preview in the web UI, then run this again.")
        raise SystemExit("Recording failed (ffmpeg exit {}):\n{}".format(
            process.returncode, stderr.strip()))
    print("\n   Done - thanks, you can relax now.\n")


def derive_clip(width, height):
    out_path = clip_path(width, height)
    print("Deriving {}x{}@{}fps -> {}".format(width, height, tx.FPS, os.path.basename(out_path)))
    result = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", str(WARMUP_SECONDS), "-i", MASTER_PATH, "-t", str(DURATION_SECONDS),
         "-vf", "scale={}:{},fps={}".format(width, height, tx.FPS),
         "-c:v", "ffv1", "-c:a", "pcm_s16le",
         out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        raise SystemExit("Deriving {}x{} failed:\n{}".format(width, height, result.stderr.strip()))
    return out_path


def describe(path):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        duration = "{:.1f}s".format(float(result.stdout.strip()))
    except ValueError:
        duration = "?s"
    size_mb = os.path.getsize(path) / 1e6
    return "{}  {}  {:.0f} MB".format(os.path.basename(path), duration, size_mb)


def main():
    os.makedirs(CLIPS_DIR, exist_ok=True)

    camera_device, is_csi = tx.select_camera_device()
    if is_csi:
        raise SystemExit("The CSI camera isn't supported here - pick the USB camera (C920).")
    audio_device = tx.select_audio_device()

    archive_existing_clips()
    record_master(camera_device, audio_device)

    outputs = [derive_clip(w, h) for w, h in RESOLUTIONS]
    print("\nBenchmark clips ready in {}:".format(CLIPS_DIR))
    for path in [MASTER_PATH] + outputs:
        print("   " + describe(path))


if __name__ == "__main__":
    main()
