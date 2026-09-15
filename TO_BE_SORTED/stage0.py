"""Stage 0: encode a test pattern on the Jetson's hardware encoder and
send it over the network as MPEG-TS/UDP, so it can be watched in VLC.

No web UI, no camera switching, no RF yet — just proving the Jetson can
encode and the stream arrives somewhere.
"""

import argparse
import subprocess
from typing import List


def build_pipeline(host: str, port: int, bitrate_kbps: int) -> List[str]:
    return [
        "gst-launch-1.0", "-e",
        "videotestsrc", "is-live=true", "!",
        "video/x-raw,width=1280,height=720,framerate=25/1", "!",
        "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        "nvv4l2h265enc", f"bitrate={bitrate_kbps * 1000}", "insert-sps-pps=true", "!",
        "h265parse", "config-interval=1", "!",
        "mpegtsmux", "alignment=7", "!",
        "udpsink", f"host={host}", f"port={port}", "sync=true",
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.5",
                        help="Destination IP that will run VLC to view the stream")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--bitrate-kbps", type=int, default=3000)
    args = parser.parse_args()

    pipeline = build_pipeline(args.host, args.port, args.bitrate_kbps)
    print("Running:", " ".join(pipeline))
    print(f"In VLC on {args.host}: Media > Open Network Stream > udp://@:{args.port}")
    subprocess.run(pipeline, check=True)


if __name__ == "__main__":
    main()
