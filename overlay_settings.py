"""Top/bottom banner + marquee on/off, and top banner/marquee text, set
from the web UI's Setup page and remembered across restarts - same small-
JSON-file pattern as usb_video_key.py's registry, one level simpler (no
hardware detection here, just read/write).

Only meaningful for SOURCE in ("camera", "video") - see datv_engine.py's
start_camera()/start_video() (the only callers of load()) and
datv_tx_plus.py's TITLE_TEXT_OVERRIDE/MARQUEE_TEXT_OVERRIDE for how these
reach the actual GStreamer worker. Testcard mode never asks for any of
this. bottom_banner has no text of its own to override - it's always the
live callsign/clock/telemetry overlay, see camera_banner_marquee.yaml's
bottom_banner comment.
"""

import json
import os

import yaml

SETTINGS_FILENAME = "overlay_settings.json"
# Read fresh on every default_marquee_text() call rather than duplicating
# the text here - camera_banner_marquee.yaml's marquee.text is also
# video_banner_marquee.yaml's own "default" section text as of 2026-09-14
# (see that file's comments), so this one file is representative of both.
CAMERA_BANNER_MARQUEE_YAML = "camera_banner_marquee.yaml"
DEFAULTS = {
    "top_banner": True,
    "top_banner_text": "",
    "bottom_banner": True,
    "marquee": True,
    "marquee_text": "",
}

_project_dir = None


def init(project_dir):
    global _project_dir
    _project_dir = project_dir


def _settings_path():
    return os.path.join(_project_dir, SETTINGS_FILENAME)


def load():
    """Current settings, merged over DEFAULTS so a missing file (first run)
    or one written by an older version (missing a newer key) never raises -
    same reasoning as usb_video_key.py's _load_registry()."""
    try:
        with open(_settings_path()) as settings_file:
            saved = json.load(settings_file)
    except (OSError, ValueError):
        saved = {}
    merged = dict(DEFAULTS)
    merged.update({key: saved[key] for key in DEFAULTS if key in saved})
    return merged


def default_marquee_text():
    """The text that's actually shown when no override is saved (blank
    marquee_text) - for the Setup page to display in place of an empty
    box, so a user editing it starts from the real message instead of a
    blank field with a "leave this blank" hint."""
    path = os.path.join(_project_dir, CAMERA_BANNER_MARQUEE_YAML)
    with open(path, encoding="utf-8") as yaml_file:
        config = yaml.safe_load(yaml_file)
    return config["marquee"]["text"].strip()


def default_top_banner_text():
    """Same idea as default_marquee_text(), for the top banner's title
    text instead."""
    path = os.path.join(_project_dir, CAMERA_BANNER_MARQUEE_YAML)
    with open(path, encoding="utf-8") as yaml_file:
        config = yaml.safe_load(yaml_file)
    return config["top_banner"]["text"].strip()


def save(top_banner, top_banner_text, bottom_banner, marquee, marquee_text):
    settings = {
        "top_banner": bool(top_banner),
        "top_banner_text": str(top_banner_text).strip(),
        "bottom_banner": bool(bottom_banner),
        "marquee": bool(marquee),
        "marquee_text": str(marquee_text).strip(),
    }
    # Write-to-temp-then-replace so a crash mid-write never leaves a
    # truncated/corrupt settings file behind.
    tmp_path = _settings_path() + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump(settings, settings_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _settings_path())
    return settings
