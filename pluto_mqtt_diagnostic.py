"""Step-by-step MQTT diagnostic for PlutoDVB2.

RF is muted before any test and is never unmuted by this program. Each
configuration command is sent separately, followed by a telemetry check. The
program stops at the first command that is not acknowledged or that resets the
symbol rate, making it clear which PlutoDVB2 function is failing.
"""

import threading
import time

import paho.mqtt.client as mqtt


PLUTO_IP = "192.168.0.50"
MQTT_PORT = 1883
MQTT_USERNAME = "root"
MQTT_PASSWORD = "analog"
CALLSIGN = "HB9IIU"

COMMAND_PREFIX = "cmd/pluto/{}/".format(CALLSIGN)
TELEMETRY_PREFIX = "dt/pluto/{}/".format(CALLSIGN)
TARGET_SR = "333000"
ACK_TIMEOUT_SECONDS = 5.0

# These are exactly the MQTT settings used by datv_tx_plus.py. RF remains
# muted throughout. SR is deliberately tested again after every command so a
# command that resets the DVB-S2 modulator to its 1000000 boot value is caught.
TESTS = [
    ("tx/frequency", "2405000000"),
    ("tx/gain", "0"),
    ("tx/dvbs2/sr", TARGET_SR),
    ("tx/dvbs2/fecmode", "fixed"),
    ("tx/dvbs2/fec", "3/4"),
    ("tx/dvbs2/frame", "long"),
    ("tx/dvbs2/pilots", "1"),
    ("tx/dvbs2/constel", "qpsk"),
    ("tx/dvbs2/gainvariable", "0"),
    ("tx/dvbs2/fecrange", "10"),
    ("tx/dvbs2/tssourcemode", "0"),
    ("tx/dvbs2/digitalgain", "0"),
    ("tx/dvbs2/firfilter", "1"),
    ("tx/dvbs2/tssourceaddress", "192.168.0.50:8282"),
]


telemetry = {}
telemetry_event = threading.Event()
connected_event = threading.Event()


def on_connect(client, _userdata, _flags, result_code):
    print("MQTT connected: rc={}".format(result_code), flush=True)
    if result_code == 0:
        client.subscribe(TELEMETRY_PREFIX + "#", qos=1)
        connected_event.set()


def on_disconnect(_client, _userdata, result_code):
    print("MQTT disconnected: rc={}".format(result_code), flush=True)


def on_message(_client, _userdata, message):
    if not message.topic.startswith(TELEMETRY_PREFIX):
        return
    key = message.topic[len(TELEMETRY_PREFIX):]
    value = message.payload.decode("utf-8", "replace")
    telemetry[key] = value
    telemetry_event.set()


def values_match(key, reported, expected):
    """Accept Pluto's harmless numeric formatting/tuning quantization."""
    try:
        if key == "tx/frequency":
            # AD936x LO tuning is quantized; a small offset is normal.
            return abs(float(reported) - float(expected)) <= 1000.0
        if key in ("tx/gain", "tx/dvbs2/digitalgain"):
            return float(reported) == float(expected)
    except ValueError:
        return False
    return str(reported).lower() == str(expected).lower()


def publish_and_confirm(client, key, value):
    """Publish one setting and wait until that same setting is reported."""
    telemetry.pop(key, None)
    telemetry_event.clear()
    topic = COMMAND_PREFIX + key
    print("\nSEND {} -> {}".format(topic, value), flush=True)
    info = client.publish(topic, payload=value, qos=1)
    info.wait_for_publish(timeout=ACK_TIMEOUT_SECONDS)
    if not info.is_published():
        return False, "broker did not acknowledge the publish"

    deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reported = telemetry.get(key)
        if reported is not None:
            if values_match(key, reported, value):
                print("RECV {}{} -> {} [OK]".format(
                    TELEMETRY_PREFIX, key, reported), flush=True)
                return True, reported
        telemetry_event.wait(0.2)
        telemetry_event.clear()
    return False, "expected {}, last report was {}".format(
        value, telemetry.get(key, "nothing"))


def read_sr(client):
    """Request an SR publication by writing the already-selected SR."""
    ok, result = publish_and_confirm(client, "tx/dvbs2/sr", TARGET_SR)
    return ok, result


def main():
    client = mqtt.Client(client_id="jetson-pluto-mqtt-diagnostic")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    print("Pluto MQTT diagnostic: {}:{}".format(PLUTO_IP, MQTT_PORT))
    print("RF WILL REMAIN MUTED FOR THE ENTIRE TEST.")
    client.connect(PLUTO_IP, MQTT_PORT, keepalive=5)
    client.loop_start()

    try:
        if not connected_event.wait(5):
            raise RuntimeError("Could not connect to the Pluto MQTT broker")

        ok, reason = publish_and_confirm(client, "tx/mute", "1")
        if not ok:
            raise RuntimeError("Could not confirm RF mute: {}".format(reason))
        print("RF mute confirmed.")

        # Establish the intended SR before starting the sequence.
        ok, reason = read_sr(client)
        if not ok:
            raise RuntimeError("Initial SR test failed: {}".format(reason))

        for number, (key, value) in enumerate(TESTS, 1):
            print("\n=== TEST {}/{}: {} ===".format(number, len(TESTS), key))
            ok, reason = publish_and_confirm(client, key, value)
            if not ok:
                print("\nFAILED AT {}: {}".format(key, reason))
                break

            # Do not rewrite SR after the SR test itself: observe whether the
            # command just tested made Pluto fall back to its boot-time SR.
            time.sleep(1.2)
            reported_sr = telemetry.get("tx/dvbs2/sr")
            print("CHECK SR -> {}".format(reported_sr), flush=True)
            if key != "tx/dvbs2/sr" and reported_sr != TARGET_SR:
                print("\nRESET DETECTED AFTER {}: SR is {}, expected {}".format(
                    key, reported_sr, TARGET_SR))
                break
        else:
            print("\nALL MQTT CONFIGURATION TESTS PASSED.")
    finally:
        # A final MQTT mute is intentional. There is no PTT-ON operation here.
        client.publish(COMMAND_PREFIX + "tx/mute", payload="1", qos=1).wait_for_publish()
        print("\nFinished. RF mute command sent; PTT was never enabled.")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
