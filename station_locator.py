"""Persists the station's QRA (Maidenhead) locator shown on the SBB clock
(SOURCE "clock" - funnyClock/clock_tx.py's LOCATOR), set from the web UI's
Setup page - same small-JSON-file pattern as pluto_callsign.py. Requested by
ON1AVO (2026-10-01), who had to edit clock_tx.py by hand.
"""

import json
import os
import re

SETTINGS_FILENAME = "station_locator.json"
DEFAULT_LOCATOR = "JN36kl"
# 4 or 6 characters: field (AA-RR), square (00-99), optional subsquare (aa-xx).
LOCATOR_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", re.IGNORECASE)

_project_dir = None


def init(project_dir):
    global _project_dir
    _project_dir = project_dir


def _settings_path():
    return os.path.join(_project_dir, SETTINGS_FILENAME)


def normalize(locator):
    """Usual written form - field upper, subsquare lower (JN36kl) - or None
    if it isn't a valid 4/6-character locator."""
    locator = str(locator).strip()
    if not LOCATOR_RE.match(locator):
        return None
    return locator[:4].upper() + locator[4:].lower()


def load():
    try:
        with open(_settings_path()) as settings_file:
            locator = normalize(json.load(settings_file)["locator"])
        if locator:
            return locator
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return DEFAULT_LOCATOR


def save(locator):
    # Write-to-temp-then-replace, same as pluto_callsign.py.
    tmp_path = _settings_path() + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump({"locator": locator}, settings_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _settings_path())
