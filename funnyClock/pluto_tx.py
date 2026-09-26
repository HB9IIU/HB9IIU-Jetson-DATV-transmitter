"""Pluto (PlutoDVB2 firmware) control + CBR relay for clock_tx.py.

Self-contained copy of the parts of the main project's datv_tx_plus.py and
dvbs2_profiles.py that clock_tx.py needs, so the funnyClock folder runs on
its own. Copied 2026-09-23 - fixes made there are NOT picked up here.

Needs: paho-mqtt, and ffmpeg on the PATH (the relay).
"""

import math
import socket
import subprocess
import threading
import time

import paho.mqtt.client as mqtt

MQTT_PORT = 1883
MQTT_USERNAME = "root"          # PlutoDVB2 defaults
MQTT_PASSWORD = "analog"
PLUTO_TS_PORT = 8282
IIOD_PORT = 30431
USB_DEFAULT_IP = "192.168.2.1"  # Pluto over USB

TS_BITRATE_WAIT_SECONDS = 30.0
PLUTO_CONFIG_RETRY_SECONDS = 2.0
# DATV-Red waits this long after a tx/stream/mode change before sending the
# rest of the DVB-S2 parameters, to let the modulator settle.
MODE_SWITCH_SETTLE_SECONDS = 0.5

CBR_RELAY_PORT = 18282
# The relay's -muxdelay: how far ahead of PCR each frame's DTS is scheduled.
# 1.0 s is what the main project's tuning proved free of "dts < pcr"
# warnings on air (oversized keyframes need the headroom).
CBR_MUXDELAY_SECONDS = 1.0

FRAME = "long"
PILOTS = True

# Testcard profiles at 1280x720, keyed by (symbol rate kS/s, FEC). Video
# bitrates measured by the main project's tune_profiles_for_testcard.py
# (2026-09-10) to stay under each channel's real TS capacity.
PROFILES = {
    (333, "2/3"): {"symbol_rate": 333000, "fec": "2/3", "resolution": (1280, 720),
                   "video_bitrate_kbps": 292, "audio_bitrate_kbps": 32},
    (333, "3/4"): {"symbol_rate": 333000, "fec": "3/4", "resolution": (1280, 720),
                   "video_bitrate_kbps": 342, "audio_bitrate_kbps": 32},
    (500, "2/3"): {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                   "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
    (500, "3/4"): {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                   "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
}

START_TIME = time.monotonic()

# Running total of the relay's "dts < pcr" warnings, for status lines.
dts_warning_total = 0


def log(message=""):
    # flush=True: stdout over SSH/pipes is otherwise block-buffered.
    print("[{:.0f}ms] {}".format((time.monotonic() - START_TIME) * 1000, message),
          flush=True)


def calculate_dvbs2_ts_bitrate(profile):
    """Exact DVB-S2 QPSK normal-frame TS capacity in bit/s (the ETSI frame
    calculation, as used by dvbs2rate). PlutoDVB2 doesn't publish it."""
    fec_parameters = {
        "2/3": (2, 3, 10),
        "3/4": (3, 4, 12),
    }
    fec_num, fec_den, bch = fec_parameters[profile["fec"]]

    fec_frame_bits = 64800.0
    modulation_bits = 2.0  # QPSK
    data_symbols = fec_frame_bits / modulation_bits
    pilot_symbols = 36.0 if PILOTS else 0.0
    pilot_blocks = math.ceil(data_symbols / 90.0 / 16.0 - 1.0)
    frame_symbols = data_symbols + 90.0 + pilot_blocks * pilot_symbols
    useful_bits = fec_frame_bits * fec_num / fec_den - 16.0 * bch - 80.0
    return int(profile["symbol_rate"] / frame_symbols * useful_bits)


def discover_pluto_ip():
    log("📡 Looking for the Pluto...")
    try:
        with socket.create_connection((USB_DEFAULT_IP, IIOD_PORT), timeout=1.0):
            log("   ✅ Found Pluto at {}".format(USB_DEFAULT_IP))
            return USB_DEFAULT_IP
    except OSError:
        raise SystemExit("❌ No PlutoSDR found at {} (USB). "
                         "Is it powered on and connected?".format(USB_DEFAULT_IP))


def mqtt_connect(ip, client_id):
    # client_id must be unique per process: the broker kicks off an existing
    # connection when a second one connects with the same id.
    if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    else:
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.connect(ip, MQTT_PORT, keepalive=5)
    client.loop_start()
    return client


def publish(client, callsign, subtopic, payload):
    topic = "cmd/pluto/{}/{}".format(callsign, subtopic)
    client.publish(topic, payload=str(payload), qos=1)
    log("   📤 {} -> {}".format(topic, payload))


def subscribe_telemetry(mqtt_client, callsign, telemetry):
    prefix = "dt/pluto/{}/".format(callsign)

    def on_message(_client, _userdata, message):
        key = message.topic[len(prefix):]
        telemetry[key] = message.payload.decode("utf-8", "replace")

    mqtt_client.on_message = on_message
    # Subscribe before configuring: the SR acknowledgement arrives here.
    result, _mid = mqtt_client.subscribe(prefix + "#", qos=1)
    if result != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("Could not subscribe to Pluto telemetry")
    time.sleep(0.25)


def set_ptt(mqtt_client, callsign, on):
    publish(mqtt_client, callsign, "tx/mute", "0" if on else "1")
    log("🔊 PTT ON" if on else "🔇 PTT OFF")


def format_gain_db(gain_db):
    # Snap to the AD9361's 0.25 dB step, "-24" rather than "-24.0".
    rounded = round(gain_db * 4) / 4.0
    if rounded == int(rounded):
        return str(int(rounded))
    return "{:.2f}".format(rounded).rstrip("0").rstrip(".")


def configure_pluto(mqtt_client, ip, callsign, profile, frequency_hz, gain_db):
    # The mode switch is what makes the modulator send DVB-S2 at all.
    publish(mqtt_client, callsign, "tx/stream/mode", "dvbs2-ts")
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)
    publish(mqtt_client, callsign, "tx/frequency", frequency_hz)
    publish(mqtt_client, callsign, "tx/gain", format_gain_db(gain_db))
    publish(mqtt_client, callsign, "tx/dvbs2/sr", profile["symbol_rate"])
    publish(mqtt_client, callsign, "tx/dvbs2/fecmode", "fixed")
    publish(mqtt_client, callsign, "tx/dvbs2/fec", profile["fec"])
    publish(mqtt_client, callsign, "tx/dvbs2/frame", FRAME)
    publish(mqtt_client, callsign, "tx/dvbs2/pilots", "1" if PILOTS else "0")
    publish(mqtt_client, callsign, "tx/dvbs2/constel", "qpsk")
    publish(mqtt_client, callsign, "tx/dvbs2/gainvariable", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/fecrange", 10)
    publish(mqtt_client, callsign, "tx/dvbs2/tssourcemode", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/digitalgain", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/firfilter", "1")
    publish(mqtt_client, callsign, "tx/dvbs2/tssourceaddress",
            "{}:{}".format(ip, PLUTO_TS_PORT))


def configure_pluto_until_ready(mqtt_client, ip, callsign, profile, telemetry,
                                frequency_hz, gain_db):
    """Resend the configuration (RF muted) until Pluto reports the requested
    symbol rate - after boot its MQTT controller can miss one-shot commands.
    Returns the channel's TS capacity in bit/s."""
    deadline = time.monotonic() + TS_BITRATE_WAIT_SECONDS
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        telemetry.pop("tx/dvbs2/sr", None)
        log("⚙️  Configuring DVB-S2 modulator (attempt {})...".format(attempt))
        configure_pluto(mqtt_client, ip, callsign, profile, frequency_hz, gain_db)

        attempt_deadline = min(deadline, time.monotonic() + PLUTO_CONFIG_RETRY_SECONDS)
        while time.monotonic() < attempt_deadline:
            reported_sr = telemetry.get("tx/dvbs2/sr")
            try:
                sr_matches = int(reported_sr) == profile["symbol_rate"]
            except (TypeError, ValueError):
                sr_matches = False
            if sr_matches:
                bitrate = calculate_dvbs2_ts_bitrate(profile)
                log("   ✅ Pluto acknowledged SR={} and TS capacity={} bit/s".format(
                    reported_sr, bitrate))
                return bitrate
            time.sleep(0.05)

        log("   ⏳ Pluto controller not ready or did not acknowledge; retrying...")

    raise RuntimeError(
        "❌ Pluto MQTT controller did not acknowledge configuration within {:.0f}s; "
        "RF remains muted".format(TS_BITRATE_WAIT_SECONDS))


def start_cbr_relay(pluto_ip, ts_bitrate):
    """ffmpeg remux (no re-encode) that pads GStreamer's variable-rate TS
    with null packets to the exact constant rate Pluto needs."""
    input_url = "udp://127.0.0.1:{}?fifo_size=1000000&overrun_nonfatal=1&reuse=1".format(
        CBR_RELAY_PORT)
    output_url = "udp://{}:{}?pkt_size=1316".format(pluto_ip, PLUTO_TS_PORT)
    log("🎞️  Starting CBR relay at {} bit/s...".format(ts_bitrate))
    command = [
        # repeat+: every warning on its own line, so they can be timed below.
        # -nostdin: otherwise ffmpeg reads keys from the terminal it was
        # started from - typing/pasting there while on air switched it
        # to debug output and a command prompt (funnyClock, 2026-09-26).
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "repeat+warning",
        "-fflags", "+nobuffer", "-probesize", "32768", "-analyzeduration", "1000000",
        "-i", input_url,
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-muxrate", str(ts_bitrate),
        "-muxpreload", "0", "-muxdelay", str(CBR_MUXDELAY_SECONDS),
        "-pcr_period", "20", "-pat_period", "0.4",
        "-streamid", "0:256", "-streamid", "1:257",
        "-mpegts_flags", "+system_b", "-flush_packets", "0",
        "-f", "mpegts", output_url,
    ]
    relay = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)
    threading.Thread(target=log_cbr_relay_output, args=(relay.stderr,), daemon=True).start()
    return relay


def log_cbr_relay_output(stderr):
    """Log the relay's stderr; "dts < pcr" warnings (can come ~40/s) are
    condensed to one line per second."""
    global dts_warning_total
    dts_count = 0
    dts_window_start = dts_window_end = None

    def flush_dts():
        log("   ⚠️  relay: {}x 'dts < pcr' from {:.0f}ms to {:.0f}ms".format(
            dts_count, (dts_window_start - START_TIME) * 1000,
            (dts_window_end - START_TIME) * 1000))

    for line in stderr:
        now = time.monotonic()
        if dts_count and now - dts_window_start >= 1.0:
            flush_dts()
            dts_count = 0
        if "dts < pcr" in line:
            if dts_count == 0:
                dts_window_start = now
            dts_window_end = now
            dts_count += 1
            dts_warning_total += 1
            continue
        log("   relay: " + line.rstrip())
    if dts_count:
        flush_dts()
