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
    # Local test: the MiniTiouner receives the Pluto's 2.4 GHz uplink
    # directly (no dish/satellite), so auto-tune sends the uplink frequency
    # with offset 0. The MiniTiouner tunes up to ~2450 MHz.
    "local_test": False,
    # Added to the uplink in local test mode only. OpenTuner has no ppm
    # calibration for the MiniTiouner's crystal (MiniTioune does: 23 ppm on
    # HB9IIU's unit) - harmless at the ~740 MHz IF via the satellite, but
    # ~55 kHz at 2.4 GHz direct, too far to lock. +67 kHz (55 + the 12 kHz
    # MiniTioune found) locked at D11 on 2026-09-29. OpenTuner's "Freq
    # Carrier Offset" while hunting is NOT a usable measure (-182 made it worse).
    "local_correction_khz": 0,
    # Receive-chain error via the satellite (mostly the LNB's 9750 MHz
    # oscillator), added to every satellite tune so stations sit at ~0
    # carrier offset. Measure it on the beacon (exactly 10491.500 MHz):
    # MiniTioune found it at 10491.452 on 2026-09-29 (-48 kHz, with its
    # 23 ppm tuner calibration); OpenTuner, without that calibration,
    # sees ~-30 kHz. Not used in local test mode.
    "rx_correction_khz": 0,
    # Keep rx_correction_khz up to date by itself whenever OpenTuner is
    # locked on the beacon - see app.py's _auto_calibrate().
    "auto_calibrate": True,
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


def save(enabled, target_ip, port, lnb_offset_khz, local_test=False, local_correction_khz=0,
         rx_correction_khz=0, auto_calibrate=True):
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
    local_correction_khz = int(local_correction_khz)
    rx_correction_khz = int(rx_correction_khz)
    if abs(local_correction_khz) > 1000 or abs(rx_correction_khz) > 1000:
        raise ValueError("corrections must be within +/-1000 kHz")
    settings = {
        "enabled": bool(enabled),
        "target_ip": target_ip,
        "port": port,
        "lnb_offset_khz": lnb_offset_khz,
        "local_test": bool(local_test),
        "local_correction_khz": local_correction_khz,
        "rx_correction_khz": rx_correction_khz,
        "auto_calibrate": bool(auto_calibrate),
    }
    # Write-to-temp-then-replace, same as overlay_settings.py.
    tmp_path = _settings_path() + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump(settings, settings_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _settings_path())
    return settings


def set_rx_correction(rx_correction_khz):
    """Auto-calibration's way in - changes only this one setting."""
    settings = load()
    settings["rx_correction_khz"] = rx_correction_khz
    return save(**settings)


def build_message(downlink_khz, symbol_rate_ksps, lnb_offset_khz):
    return "[GlobalMsg],Freq={},Offset={},Doppler=0,Srate={}".format(
        int(downlink_khz), lnb_offset_khz, int(symbol_rate_ksps))


def send_tune(tx_frequency_hz, symbol_rate_ksps, settings=None):
    """Tunes OpenTuner to the downlink of our own uplink frequency - or, in
    local test mode, straight to the uplink itself (offset 0)."""
    settings = settings or load()
    uplink_khz = int(round(tx_frequency_hz / 1000.0))
    if settings["local_test"]:
        return send_downlink(uplink_khz + int(settings["local_correction_khz"]), symbol_rate_ksps,
                             settings, lnb_offset_khz=0)
    return send_downlink(uplink_khz + QO100_DOWNLINK_OFFSET_KHZ, symbol_rate_ksps, settings)


def send_beacon(settings=None):
    """Setup page's test button - the beacon is always on air, so OpenTuner
    should visibly lock, not just change frequency."""
    return send_downlink(QO100_BEACON_DOWNLINK_KHZ, QO100_BEACON_SYMBOL_RATE_KSPS, settings)


def send_downlink(downlink_khz, symbol_rate_ksps, settings=None, lnb_offset_khz=None):
    """Sends one Quick Tune message. Returns (message, destination) - raises
    OSError if the send itself fails (no route, bad IP...). A satellite tune
    (no lnb_offset_khz given) gets the RX correction; local test passes its
    own offset (0) and correction instead."""
    settings = settings or load()
    if lnb_offset_khz is None:
        lnb_offset_khz = settings["lnb_offset_khz"]
        downlink_khz = downlink_khz + int(settings["rx_correction_khz"])
    message = build_message(downlink_khz, symbol_rate_ksps, lnb_offset_khz)
    target_ip = settings["target_ip"] or "255.255.255.255"
    destination = (target_ip, int(settings["port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(message.encode("ascii"), destination)
    finally:
        sock.close()
    return message, destination
