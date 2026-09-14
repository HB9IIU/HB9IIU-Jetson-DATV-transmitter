"""Background watcher: converts raw videos dropped into a USB video key's
"original videos" folder into the same lossless, pre-resized
preprocessed_<W>x<H>/ .mkv files preprocess_videos.py already produces for
the SD card's own demo video - so a user can just copy a video file onto
the key and have it show up in the picker a while later, no manual step
needed.

Reuses preprocess_videos.py's own ffmpeg detection and conversion/
validity-check functions directly (see its own docstring for why
FFV1/lossless, and why "does the output file already exist and play" is
the record of "already converted") rather than duplicating them - only
the *location* differs here (the USB key's own mount, not this project's
own folder next to app.py).

Runs as a simple polling background thread - not filesystem-event-driven,
consistent with usb_video_key.py's own pivot away from a fragile external
dependency (see its docstring). A video drop-and-convert is not a
latency-sensitive operation, so POLL_INTERVAL_SECONDS is deliberately
relaxed, and conversion is skipped entirely whenever a stream is live (an
FFV1 encode competing with the Jetson Nano's own live GStreamer encode/TX
pipeline for CPU is asking for dropped frames on air).
"""

import os
import threading
import time

import usb_video_key
from preprocess_videos import (
    FFMPEG, VIDEO_EXTENSIONS, convert, is_valid_output, output_path_for,
)

POLL_INTERVAL_SECONDS = 10
# Same resolutions preprocess_videos.py's own RESOLUTIONS covers - kept in
# sync by hand, see the comment there.
TARGET_RESOLUTIONS = ((640, 360), (960, 540))


def _convert_pending():
    usb_root = usb_video_key.mounted_root()
    if usb_root is None:
        return

    source_dir = os.path.join(usb_root, usb_video_key.ORIGINAL_VIDEOS_FOLDER_NAME)
    if not os.path.isdir(source_dir):
        return

    for name in sorted(os.listdir(source_dir)):
        # Hidden dotfiles: macOS drops "._real name.ext" AppleDouble sidecar
        # stubs next to every real file when copying from/via a Mac - same
        # extension as the real video, but not decodable, and ffmpeg would
        # otherwise retry-and-fail on them forever (real failure hit
        # 2026-09-14). ".DS_Store" etc are also caught by this, harmlessly.
        if name.startswith("."):
            continue
        source_path = os.path.join(source_dir, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(source_path) or ext not in VIDEO_EXTENSIONS:
            continue

        for width, height in TARGET_RESOLUTIONS:
            output_dir = os.path.join(usb_root, "preprocessed_{}x{}".format(width, height))
            out_path = output_path_for(source_path, output_dir)
            if is_valid_output(out_path):
                continue
            print("[video_conversion] Converting {} -> {}x{}".format(name, width, height))
            if convert(source_path, width, height, out_path):
                print("[video_conversion] Done: {}".format(out_path))
            else:
                print("[video_conversion] FAILED: {} @ {}x{}".format(name, width, height))


def _poll_loop(is_stream_active):
    while True:
        try:
            if not is_stream_active():
                _convert_pending()
        except Exception as exc:  # a bad/corrupt source file must not kill the watcher
            print("[video_conversion] error: {}".format(exc))
        time.sleep(POLL_INTERVAL_SECONDS)


def init(is_stream_active=lambda: False):
    """Start the watcher - a no-op if ffmpeg wasn't found at all (see
    preprocess_videos.py's own FFMPEG detection). is_stream_active is a
    callable app.py supplies so this module doesn't need to import
    DatvEngine itself just to check whether a transmission is live."""
    if FFMPEG is None:
        return
    watcher = threading.Thread(target=_poll_loop, args=(is_stream_active,), daemon=True)
    watcher.start()
