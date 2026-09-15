"""Jetson LimeSDR Mini DVB-S2 test: C920 camera + mic, via DATV-Linux's
dvbs2_tx. Reuses the real, hardware-tuned profile table from
dvbs2_profiles.py (CAMERA_VIDEO_PROFILE_NAMES) instead of redefining
resolution/bitrate numbers here - see that file's docstring for why.

Deliberately NOT using the ffmpeg CBR-relay stage (compare datv_tx_plus.py's
start_cbr_relay()) for this first step - keeping the pipeline to two
processes (GStreamer -> dvbs2_tx directly) since sr500_fec34 has comfortable
capacity margin (607 kbps used of 726 kbps available). Add the relay back
only if picture quality specifically needs it.

Run: python3 lime_cli_test.py     Stop: Ctrl+C
"""

import os
import re
import signal
import subprocess
import sys
import time

from dvbs2_profiles import (PROFILES, CAMERA_VIDEO_PROFILE_NAMES, FRAME,
                             calculate_dvbs2_ts_bitrate)

TX_BIN = os.path.expanduser("~/DATV-Linux/build/dvbs2_tx")
FREQ_HZ = 2405000000
TX_GAIN = 30
ROLLOFF = "0.20"
PILOTS = 1
GOLDCODE = 0
FPS = 25

# Edit these two to try a different symbol rate/FEC - must be a key in
# CAMERA_VIDEO_PROFILE_NAMES (dvbs2_profiles.py).
SYMBOL_RATE_KSPS = 500
FEC = "3/4"


def modcod_string(fec):
    frame_suffix = "N" if FRAME == "long" else "S"
    return "QPSK_{}_{}".format(fec, frame_suffix)


def resolve_profile():
    key = (SYMBOL_RATE_KSPS, FEC)
    try:
        profile_name = CAMERA_VIDEO_PROFILE_NAMES[key]
    except KeyError:
        raise SystemExit(
            "No camera profile for SR={}kS/s FEC={} - see "
            "CAMERA_VIDEO_PROFILE_NAMES in dvbs2_profiles.py for valid "
            "combinations.".format(SYMBOL_RATE_KSPS, FEC))
    return PROFILES[profile_name]


def cleanup_stale_processes():
    subprocess.run(["pkill", "-f", "dvbs2_tx"], check=False)
    subprocess.run(["pkill", "-f", "gst-launch-1.0"], check=False)
    time.sleep(2)


def detect_camera_device():
    output = subprocess.run(["v4l2-ctl", "--list-devices"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout
    for block in output.strip().split("\n\n"):
        if "logitech" in block.lower() or "c920" in block.lower():
            match = re.search(r"(/dev/video\d+)", block)
            if match:
                return match.group(1)
    raise SystemExit(
        "Could not find the Logitech camera via v4l2-ctl --list-devices. "
        "Run `lsusb` to check it enumerates (046d:xxxx).")


def detect_mic_device():
    output = subprocess.run(["arecord", "-l"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout
    for line in output.splitlines():
        if "c920" in line.lower():
            match = re.search(r"card (\d+)", line)
            if match:
                return "plughw:{},0".format(match.group(1))
    raise SystemExit(
        "Could not find the C920 microphone via arecord -l. "
        "Run `lsusb` to check it enumerates (046d:xxxx).")


def build_gst_args(video_device, mic_device, profile):
    width, height = profile["resolution"]
    return [
        "gst-launch-1.0", "-q",
        "mpegtsmux", "name=mux", "alignment=7", "!", "fdsink", "fd=1",

        "v4l2src", "device={}".format(video_device), "do-timestamp=true", "!",
        "image/jpeg,width=1280,height=720,framerate=30/1", "!",
        "jpegdec", "!",
        "videorate", "!", "video/x-raw,framerate={}/1".format(FPS), "!",
        "videoscale", "!", "video/x-raw,width={},height={}".format(width, height), "!",
        "videoconvert", "!",
        "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        "queue", "!",
        "nvv4l2h265enc", "bitrate={}".format(profile["video_bitrate_kbps"] * 1000),
        "insert-sps-pps=true", "iframeinterval={}".format(FPS), "!",
        "h265parse", "config-interval=1", "!",
        "queue", "!", "mux.",

        "alsasrc", "device={}".format(mic_device), "!",
        "audioconvert", "!", "audioresample", "!", "audiorate", "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=1", "!",
        "voaacenc", "bitrate={}".format(profile["audio_bitrate_kbps"] * 1000), "!",
        "aacparse", "!",
        "queue", "!", "mux.",
    ]


def build_dvbs2_tx_args():
    return [
        TX_BIN, modcod_string(FEC), str(FREQ_HZ), str(SYMBOL_RATE_KSPS * 1000),
        str(TX_GAIN), "0", ROLLOFF, str(PILOTS), str(GOLDCODE), "limesdrmini",
    ]


def main():
    if not os.path.isfile(TX_BIN):
        raise SystemExit("dvbs2_tx not found at {}".format(TX_BIN))

    cleanup_stale_processes()
    profile = resolve_profile()
    video_device = detect_camera_device()
    mic_device = detect_mic_device()
    muxrate = calculate_dvbs2_ts_bitrate(
        {"symbol_rate": SYMBOL_RATE_KSPS * 1000, "fec": FEC})

    print("Camera:  {}".format(video_device))
    print("Mic:     {}".format(mic_device))
    print("Profile: {}x{} @ {}kbps video + {}kbps audio (channel capacity "
          "{} bit/s)".format(profile["resolution"][0], profile["resolution"][1],
                              profile["video_bitrate_kbps"],
                              profile["audio_bitrate_kbps"], muxrate))
    print("MODCOD:  {}  SR={}kS/s  gain={}dB".format(
        modcod_string(FEC), SYMBOL_RATE_KSPS, TX_GAIN))

    gst_args = build_gst_args(video_device, mic_device, profile)
    tx_args = build_dvbs2_tx_args()
    print(" ".join(gst_args), "|", " ".join(tx_args))

    gst_proc = subprocess.Popen(gst_args, stdout=subprocess.PIPE)
    tx_proc = subprocess.Popen(tx_args, stdin=gst_proc.stdout,
                                start_new_session=True)
    gst_proc.stdout.close()  # so gst-launch gets SIGPIPE if dvbs2_tx dies first

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        tx_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (gst_proc, tx_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (gst_proc, tx_proc):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        cleanup_stale_processes()

    return tx_proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
