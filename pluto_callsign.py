"""Persists the callsign app.py uses to build every Pluto MQTT topic
(cmd/pluto/<callsign>/..., dt/pluto/<callsign>/...), so a value set from the
web UI's Setup page survives an app.py restart - same small-JSON-file
pattern as overlay_settings.py/usb_video_key.py's registry.

The Pluto's own firmware stores its side of this independently, in its
U-Boot environment (see pluto_mqtt_ctrl.cpp's `fw_printenv -n call` and
mqtt_setcall.sh's `fw_setenv call $param` in the firmware source) - nothing
here talks to that directly. This file only remembers what app.py itself
last set/used; app.py's /api/pluto/callsign POST handler is what actually
publishes to cmd/pluto/call to change the Pluto's own stored value.
"""

import json
import os

SETTINGS_FILENAME = "pluto_callsign.json"
DEFAULT_CALLSIGN = "HB9IIU"

_project_dir = None


def init(project_dir):
    global _project_dir
    _project_dir = project_dir


def _settings_path():
    return os.path.join(_project_dir, SETTINGS_FILENAME)


def load():
    try:
        with open(_settings_path()) as settings_file:
            saved = json.load(settings_file)
        callsign = str(saved["callsign"]).strip()
        if callsign:
            return callsign
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return DEFAULT_CALLSIGN


def save(callsign):
    # Write-to-temp-then-replace so a crash mid-write never leaves a
    # truncated/corrupt settings file behind - same as overlay_settings.py.
    tmp_path = _settings_path() + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump({"callsign": callsign}, settings_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _settings_path())
