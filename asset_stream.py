#!/usr/bin/env python3
"""Loop and transcode one generated MKV asset into an MPEG transport stream."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
child = None
stopping = False


def stop(_signum=None, _frame=None):
    global stopping
    stopping = True
    if child is not None and child.poll() is None:
        child.send_signal(signal.SIGINT)


def command(settings):
    asset = APP_DIR / "generated-assets" / settings["generated_asset"]
    encoder = "nvv4l2h264enc" if settings["codec"] == "h264" else "nvv4l2h265enc"
    parser = "h264parse" if settings["codec"] == "h264" else "h265parse"
    return [
        "gst-launch-1.0", "-e",
        "filesrc", "location={}".format(asset), "!", "matroskademux", "!",
        "queue", "max-size-time=3000000000", "!", "decodebin", "!",
        "videoconvert", "!", "videoscale", "add-borders=true", "!", "videorate", "!",
        "video/x-raw,width={},height={},framerate={}/1,pixel-aspect-ratio=1/1".format(
            settings["width"], settings["height"], settings["fps"]), "!",
        "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        encoder, "bitrate={}".format(settings["bitrate_kbps"] * 1000),
        "insert-sps-pps=true", "iframeinterval={}".format(settings["gop_frames"]), "!",
        parser, "config-interval=1", "!", "queue", "!",
        "mpegtsmux", "alignment=7", "!",
        "udpsink", "host={}".format(settings["host"]), "port={}".format(settings["port"]),
        "sync=true", "async=false", "buffer-size=1048576",
    ]


def main():
    global child
    settings = json.loads(sys.argv[1])
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        child = subprocess.Popen(command(settings))
        return_code = child.wait()
        child = None
        if stopping:
            return 0
        if return_code != 0:
            return return_code
        print("MKV finished; restarting from the beginning", flush=True)
        time.sleep(0.15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
