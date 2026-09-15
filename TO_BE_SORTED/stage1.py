"""Stage 1: same as stage0, but the video source is a real camera instead
of a generated test pattern. Detects connected cameras and asks which one
to use. The USB webcam path also includes audio from its built-in
microphone (the CSI camera has no microphone, so that path stays video-only).

[camera (+ mic for USB)] -> [Jetson hardware encoder] -> [network: UDP] -> [your PC / VLC]
"""

import argparse
import glob
import subprocess
from typing import List, Tuple

FPS = 25
AUDIO_DEVICE = "plughw:2,0"  # Logitech C920 built-in microphone
AUDIO_BITRATE_KBPS = 48


def detect_cameras() -> List[Tuple[str, str]]:
    cameras = []
    for device in sorted(glob.glob("/dev/video*")):
        name_file = "/sys/class/video4linux/{}/name".format(device.rsplit("/", 1)[-1])
        try:
            with open(name_file) as f:
                name = f.read().strip()
        except OSError:
            name = "unknown"
        cameras.append((device, name))
    return cameras


def choose_camera(cameras: List[Tuple[str, str]]) -> Tuple[str, str]:
    print("Detected cameras:")
    for index, (device, name) in enumerate(cameras):
        print("  [{}] {} ({})".format(index, device, name))
    choice = input("Which camera should stream? [0-{}]: ".format(len(cameras) - 1))
    return cameras[int(choice.strip())]


def build_csi_pipeline(host: str, port: int, bitrate_kbps: int, sensor_id: int,
                       flip_method: int, wbmode: int, saturation: float) -> List[str]:
    return [
        "gst-launch-1.0", "-e",
        "nvarguscamerasrc", f"sensor-id={sensor_id}", f"wbmode={wbmode}",
        f"saturation={saturation}", "!",
        f"video/x-raw(memory:NVMM),width=1280,height=720,framerate={FPS}/1,format=NV12", "!",
        "nvvidconv", f"flip-method={flip_method}", "!",
        "video/x-raw(memory:NVMM),format=NV12", "!",
        "queue", "!",
        "nvv4l2h265enc", f"bitrate={bitrate_kbps * 1000}", "insert-sps-pps=true",
        f"iframeinterval={FPS}", "!",
        "h265parse", "config-interval=1", "!",
        "queue", "!",
        "mpegtsmux", "alignment=7", "!",
        "udpsink", f"host={host}", f"port={port}",
        "sync=true", "async=false", "buffer-size=1048576",
    ]


def build_usb_pipeline(device: str, host: str, port: int, bitrate_kbps: int) -> List[str]:
    return [
        "gst-launch-1.0", "-e",
        "mpegtsmux", "name=mux", "alignment=7", "!",
        "udpsink", f"host={host}", f"port={port}",
        "sync=true", "async=false", "buffer-size=1048576",

        "v4l2src", f"device={device}", "do-timestamp=true", "!",
        f"image/jpeg,width=1280,height=720,framerate=30/1", "!",
        "jpegdec", "!",
        "videorate", "!", f"video/x-raw,framerate={FPS}/1", "!",
        "videoconvert", "!",
        "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        "queue", "!",
        "nvv4l2h265enc", f"bitrate={bitrate_kbps * 1000}", "insert-sps-pps=true",
        f"iframeinterval={FPS}", "!",
        "h265parse", "config-interval=1", "!",
        "queue", "!", "mux.",

        "alsasrc", f"device={AUDIO_DEVICE}", "!",
        "audioconvert", "!", "audioresample", "!", "audiorate", "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=1", "!",
        "voaacenc", f"bitrate={AUDIO_BITRATE_KBPS * 1000}", "!",
        "aacparse", "!",
        "queue", "!", "mux.",
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.5",
                        help="Destination IP that will run VLC to view the stream")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--bitrate-kbps", type=int, default=3000)
    parser.add_argument("--flip-method", type=int, default=2,
                        help="CSI camera only: nvvidconv flip-method (0=none, "
                             "2=180 degrees, 1/3=90 degrees, 4/6=horizontal/vertical flip)")
    parser.add_argument("--wbmode", type=int, default=1,
                        help="CSI camera only: white balance mode, 0=off, 1=auto, "
                             "2=incandescent, 3=fluorescent, 4=warm-fluorescent, "
                             "5=daylight, 6=cloudy-daylight, 7=twilight, 8=shade")
    parser.add_argument("--saturation", type=float, default=1.0,
                        help="CSI camera only: color saturation, 0=grayscale, 1=normal, up to 2")
    args = parser.parse_args()

    cameras = detect_cameras()
    if not cameras:
        raise SystemExit("No cameras found under /dev/video*")
    device, name = choose_camera(cameras)

    if "imx219" in name.lower():
        sensor_id = int(device.rsplit("video", 1)[-1])
        pipeline = build_csi_pipeline(args.host, args.port, args.bitrate_kbps, sensor_id,
                                      args.flip_method, args.wbmode, args.saturation)
    else:
        pipeline = build_usb_pipeline(device, args.host, args.port, args.bitrate_kbps)

    print("Streaming {} ({})".format(device, name))
    print("Running:", " ".join(pipeline))
    print(f"In VLC on {args.host}: Media > Open Network Stream > udp://@:{args.port}")
    subprocess.run(pipeline, check=True)


if __name__ == "__main__":
    main()
