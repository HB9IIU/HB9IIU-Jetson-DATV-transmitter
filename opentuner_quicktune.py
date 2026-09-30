"""Auto-tunes OpenTuner (ZR6TG) to our own QO-100 downlink whenever a stream
starts, via OpenTuner's "Quick Tune Control" extra - the same one-line UDP
message M0DTS's QO-100 WB Quick Tune sends when you click a signal.

OpenTuner's MQTT client can't do this: it subscribes to cmd/opentuner/tuner1/#
but nothing acts on those messages (checked in the tomvdb/open_tuner source,
2026-09-28) - it only publishes status. Quick Tune is plain UDP instead:

    [GlobalMsg],Freq=10498750,Offset=9750000,Doppler=0,Srate=333

QuickTuneControl.cs reads fields by position (2nd = Freq kHz, 3rd = Offset kHz,
5th = Srate kS/s) and tunes the MiniTiouner to Freq - Offset, ignoring the
offset set in OpenTuner's own Minitiouner settings - so ours has to be right.
Tuner 1 listens on UDP 6789 by default (tuner 2 on 6790).

target_ip "" means broadcast to the whole LAN, so the PC's IP doesn't need to
be known. Same small-JSON-file pattern as overlay_settings.py.
"""

import json
import os
import socket

SETTINGS_FILENAME = "opentuner_settings.json"
# QO-100 NB/WB transponder: downlink = uplink + 8089.5 MHz (2400.0 -> 10489.5).
QO100_DOWNLINK_OFFSET_KHZ = 8089500
# QO-100 wideband beacon, always on air.
QO100_BEACON_DOWNLINK_KHZ = 10491500
QO100_BEACON_SYMBOL_RATE_KSPS = 1500
DEFAULTS = {
    "enabled": False,
    "target_ip": "",
    "port": 6789,
    "lnb_offset_khz": 9750000,
    # Receive-chain error via the satellite (mostly the LNB's 9750 MHz
    # oscillator), added to every satellite tune so stations sit at ~0
    # carrier offset. Measure it on the beacon (exactly 10491.500 MHz):
    # MiniTioune found it at 10491.452 on 2026-09-29 (-48 kHz, with its
    # 23 ppm tuner calibration); OpenTuner, without that calibration,
    # sees ~-30 kHz.
    "rx_correction_khz": 0,
    # Keep rx_correction_khz up to date by itself whenever OpenTuner is
    # locked on the beacon - see app.py's _auto_calibrate().
    "auto_calibrate": True,
    # Pluto TX frequency error, measured on our own signal via the
    # satellite (app.py's _measure_tx()); the Pluto is sent
    # frequency - this, so the signal lands exactly in the chosen slot.
    # Applied at stream start; tx_auto_apply stores new measurements by
    # itself instead of waiting for the On-air monitor's Apply button.
    "tx_correction_khz": 0,
    "tx_auto_apply": False,
}

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
    except (OSError, ValueError):
        saved = {}
    merged = dict(DEFAULTS)
    merged.update({key: saved[key] for key in DEFAULTS if key in saved})
    return merged


def save(enabled, target_ip, port, lnb_offset_khz, rx_correction_khz=0, auto_calibrate=True,
         tx_correction_khz=0, tx_auto_apply=False):
    """Raises ValueError on a bad IP/port/offset."""
    target_ip = str(target_ip).strip()
    if target_ip:
        socket.inet_aton(target_ip)  # OSError (a ValueError-alike) on garbage
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    lnb_offset_khz = int(lnb_offset_khz)
    if lnb_offset_khz < 0:
        raise ValueError("LNB offset can't be negative")
    rx_correction_khz = int(rx_correction_khz)
    tx_correction_khz = int(tx_correction_khz)
    if max(abs(rx_correction_khz), abs(tx_correction_khz)) > 1000:
        raise ValueError("corrections must be within +/-1000 kHz")
    settings = {
        "enabled": bool(enabled),
        "target_ip": target_ip,
        "port": port,
        "lnb_offset_khz": lnb_offset_khz,
        "rx_correction_khz": rx_correction_khz,
        "auto_calibrate": bool(auto_calibrate),
        "tx_correction_khz": tx_correction_khz,
        "tx_auto_apply": bool(tx_auto_apply),
    }
    # Write-to-temp-then-replace, same as overlay_settings.py.
    tmp_path = _settings_path() + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump(settings, settings_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _settings_path())
    return settings


def update(**changes):
    """Changes only the given settings (auto-calibration, TX Apply)."""
    settings = load()
    settings.update(changes)
    return save(**settings)


def build_message(downlink_khz, symbol_rate_ksps, lnb_offset_khz):
    return "[GlobalMsg],Freq={},Offset={},Doppler=0,Srate={}".format(
        int(downlink_khz), lnb_offset_khz, int(symbol_rate_ksps))


def send_tune(tx_frequency_hz, symbol_rate_ksps, settings=None):
    """Tunes OpenTuner to the downlink of our own uplink frequency."""
    settings = settings or load()
    uplink_khz = int(round(tx_frequency_hz / 1000.0))
    return send_downlink(uplink_khz + QO100_DOWNLINK_OFFSET_KHZ, symbol_rate_ksps, settings)


def send_beacon(settings=None):
    """Setup page's test button - the beacon is always on air, so OpenTuner
    should visibly lock, not just change frequency."""
    return send_downlink(QO100_BEACON_DOWNLINK_KHZ, QO100_BEACON_SYMBOL_RATE_KSPS, settings)


def send_downlink(downlink_khz, symbol_rate_ksps, settings=None):
    """Sends one Quick Tune message, with the RX correction added. Returns
    (message, destination) - raises OSError if the send itself fails (no
    route, bad IP...)."""
    settings = settings or load()
    downlink_khz = downlink_khz + int(settings["rx_correction_khz"])
    message = build_message(downlink_khz, symbol_rate_ksps, settings["lnb_offset_khz"])
    target_ip = settings["target_ip"] or "255.255.255.255"
    destination = (target_ip, int(settings["port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(message.encode("ascii"), destination)
    finally:
        sock.close()
    return message, destination
