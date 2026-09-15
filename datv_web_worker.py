"""Thin command-line adapter for the proven datv_tx_plus.py implementation.

This file contains no replacement GStreamer logic. It supplies web-selected
values to datv_tx_plus.py and then runs that module's original main() unchanged.
One dispatcher handles all three SOURCE modes (testcard/camera/video) - each
just patches the specific interactive prompt(s) that mode would otherwise
block on (input() has no real terminal to read from in this subprocess).
"""

import argparse
import os
import re

import datv_tx_plus as tx
import usb_video_key


def _patch(name, value):
    # datv_tx_plus.py is the reference implementation and is actively
    # edited elsewhere - if a rename ever drops one of these names, fail
    # loudly here instead of silently creating an unused new module
    # attribute and leaving the original behavior underneath us (e.g. an
    # interactive input() prompt that would hang this subprocess forever).
    if not hasattr(tx, name):
        raise SystemExit(
            "datv_tx_plus.{} not found - web worker patch is out of date".format(name))
    setattr(tx, name, value)


def _bool_arg(value):
    return value.lower() in ("1", "true", "yes", "y")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=("testcard", "camera", "video"))
    parser.add_argument("--profile", required=True, choices=sorted(tx.PROFILES))
    parser.add_argument("--gain", required=True, type=float)
    parser.add_argument("--frequency", required=True, type=int)
    # Sourced from app.py's own PLUTO_CALLSIGN global, so the web MQTT
    # client (app.py) and this worker's own MQTT client (datv_tx_plus.py)
    # always agree on the topic prefix - datv_tx_plus.py's own CALLSIGN
    # default only applies to a direct/interactive run of that script.
    parser.add_argument("--callsign", required=True)
    # testcard mode never uses these three (ask_banner_and_marquee_settings
    # is never called for it - see datv_tx_plus.py's main()), so they're
    # harmless no-ops there and just need a default rather than being
    # required.
    parser.add_argument("--top-banner", type=_bool_arg, default=True)
    parser.add_argument("--bottom-banner", type=_bool_arg, default=True)
    parser.add_argument("--marquee", type=_bool_arg, default=True)
    # Empty string (the default) means "no override" - datv_tx_plus.py's own
    # YAML-loaded TITLE_TEXT/MARQUEE_TEXT is left alone. See
    # TITLE_TEXT_OVERRIDE/MARQUEE_TEXT_OVERRIDE there.
    parser.add_argument("--top-banner-text", default="")
    parser.add_argument("--marquee-text", default="")
    # Only one of these three is actually required, depending on --source -
    # validated below instead of via argparse's required= (which can't
    # express "required only if --source is X").
    parser.add_argument("--testcard")
    parser.add_argument("--video")
    parser.add_argument("--camera-device")
    parser.add_argument("--camera-is-csi", type=_bool_arg, default=False)
    parser.add_argument("--audio-device")
    args = parser.parse_args()

    # Needed before mounted_root() below - usb_video_key.py's _supported
    # flag (and therefore every list_candidates()/mounted_root() call)
    # stays False until init() has set _project_dir, same as app.py's own
    # usb_video_key.init(PROJECT_DIR) call at Flask startup. This is a
    # separate process, so that call never reaches here on its own.
    usb_video_key.init(tx.SCRIPT_DIR)

    _patch("PROFILE", args.profile)
    _patch("GAIN_DB", args.gain)
    _patch("FREQUENCY_HZ", args.frequency)
    _patch("SOURCE", args.source)
    _patch("TX_OUTPUT", "pluto")
    _patch("CALLSIGN", args.callsign)
    _patch("ask_banner_and_marquee_settings",
           lambda: (args.top_banner, args.bottom_banner, args.marquee))
    if args.top_banner_text:
        _patch("TITLE_TEXT_OVERRIDE", args.top_banner_text)
    if args.marquee_text:
        _patch("MARQUEE_TEXT_OVERRIDE", args.marquee_text)

    if args.source == "testcard":
        if not args.testcard:
            raise SystemExit("--testcard is required for --source testcard")
        testcard = os.path.abspath(args.testcard)
        expected_testcard_dir = os.path.abspath(os.path.join(tx.SCRIPT_DIR, "testcards"))
        if os.path.dirname(testcard) != expected_testcard_dir or not os.path.isfile(testcard):
            raise SystemExit("Invalid testcard path")
        _patch("select_testcard_file", lambda: testcard)

    elif args.source == "video":
        if not args.video:
            raise SystemExit("--video is required for --source video")
        video_path = os.path.abspath(args.video)
        video_dir = os.path.dirname(video_path)
        folder_name = os.path.basename(video_dir)
        # Same two allowed roots as datv_engine.py's own start_video() check
        # (SD card, plus the USB video key when mounted) - app.py and
        # datv_engine.py already validated this exact path before launching
        # this subprocess, but re-checking here is cheap and this is what
        # actually reaches the GStreamer pipeline.
        allowed_roots = {os.path.abspath(tx.SCRIPT_DIR)}
        usb_root = usb_video_key.mounted_root()
        if usb_root:
            allowed_roots.add(usb_root)
        if (os.path.dirname(video_dir) not in allowed_roots
                or not re.fullmatch(r"preprocessed_\d+x\d+", folder_name)
                or not os.path.isfile(video_path)):
            raise SystemExit("Invalid video path")
        _patch("select_video_file", lambda profile: video_path)

    elif args.source == "camera":
        if not args.camera_device or not args.audio_device:
            raise SystemExit("--camera-device and --audio-device are required for --source camera")
        _patch("select_camera_device", lambda: (args.camera_device, args.camera_is_csi))
        _patch("select_audio_device", lambda: args.audio_device)

    tx.main()


if __name__ == "__main__":
    main()
