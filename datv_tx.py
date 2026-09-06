"""One script to run a full DATV transmission: configure the Pluto, start
the video stream, and key PTT on - all in one process. Press Ctrl+C to
stop; this safely keys PTT off and stops the video before exiting.

No arguments - edit the settings below directly if you need to change
anything. The Pluto's IP is found automatically (mDNS, falling back to
its USB default address), so it doesn't need to be hardcoded.

PTT is MQTT tx/mute only. An earlier version also SSHed in to directly
power the TX LO down/up via sysfs, on the assumption that tx/mute alone
wasn't reliable - never actually verified (no repro recorded), and
contradicted by DATV-Red (the reference PC-side controller for this same
firmware), which mutes over MQTT alone. That SSH path is kept in reserve
(ssh_connect()/set_tx_lo_powerdown(), unused) in case real RF measurement
ever shows MQTT-only muting is insufficient.

MQTT uses the Pluto's default credentials: root/analog.
"""

import socket
import subprocess
import time

import paho.mqtt.client as mqtt
import paramiko

START_TIME = time.monotonic()


def log(message=""):
    # Piped/redirected stdout (SSH, a log file, systemd) is fully buffered
    # by default, so this would otherwise sit invisible until the buffer
    # filled or the process exited cleanly - and be lost entirely on a
    # SIGTERM/SIGKILL. flush=True forces it out immediately. (Can't use
    # sys.stdout.reconfigure(line_buffering=True) instead - that needs
    # Python 3.7+, and this runs under the Jetson's 3.6 venv.)
    print("[{:.0f}ms] {}".format((time.monotonic() - START_TIME) * 1000, message),
          flush=True)

# ---- Settings - edit these directly ----
CAMERA_DEVICE = "/dev/video0"  # Logitech C920 USB webcam
CALLSIGN = "HB9IIU"
FREQUENCY_HZ = 2405000000
SYMBOL_RATE = 333000
FEC = "4/5"
FRAME = "long"
PILOTS = False
GAIN_DB = 0
VIDEO_BITRATE_KBPS = 380
AUDIO_DEVICE = "plughw:2,0"  # Logitech C920 built-in microphone
AUDIO_BITRATE_KBPS = 48
# -----------------------------------------

MQTT_PORT = 1883
MQTT_USERNAME = "root"
MQTT_PASSWORD = "analog"
SSH_USERNAME = "root"
SSH_PASSWORD = "analog"
TX_LO_POWERDOWN_PATH = "/sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown"
FPS = 25
PLUTO_TS_PORT = 8282
IIOD_PORT = 30431
USB_DEFAULT_IP = "192.168.2.1"
# DATV-Red (the reference PC-side controller for this firmware) waits after
# a tx/stream/mode change before resending the rest of the config - see its
# "delay restore after MODE set" node (pauseType "delay", timeout 0.5s).
# Mirrored here for the same reason: give the modulator time to settle
# after the mode switch before sending the rest of the DVB-S2 parameters.
MODE_SWITCH_SETTLE_SECONDS = 0.5


def discover_pluto_ip():
    log("📡 Looking for the Pluto...")
    candidates = []
    try:
        result = subprocess.run(
            ["avahi-browse", "-r", "-t", "-p", "_iio._tcp"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            fields = line.split(";")
            if len(fields) >= 8 and fields[0] == "=" and fields[2] == "IPv4":
                candidates.append(fields[7])
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.append(USB_DEFAULT_IP)

    for address in candidates:
        try:
            with socket.create_connection((address, IIOD_PORT), timeout=1.0):
                log("   ✅ Found Pluto at {}".format(address))
                return address
        except OSError:
            continue
    raise SystemExit("❌ No PlutoSDR found (checked mDNS and USB default). "
                      "Is it powered on and connected?")


def mqtt_connect(ip):
    client = mqtt.Client(client_id="jetson-datv-tx")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.connect(ip, MQTT_PORT, keepalive=5)
    # Start the network loop immediately so QoS 1 publishes actually get
    # flushed/acked instead of merely sitting in the local client queue.
    client.loop_start()
    return client


def publish(client, callsign, subtopic, payload):
    topic = "cmd/pluto/{}/{}".format(callsign, subtopic)
    client.publish(topic, payload=str(payload), qos=1)
    log("   📤 {} -> {}".format(topic, payload))


def ssh_connect(ip):
    """In reserve, currently unused - see set_tx_lo_powerdown()."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # look_for_keys/allow_agent default to True, which makes paramiko hunt
    # through local SSH keys and an agent before trying the password below -
    # pure overhead here since this always authenticates by password.
    ssh.connect(ip, username=SSH_USERNAME, password=SSH_PASSWORD, timeout=8,
                look_for_keys=False, allow_agent=False)
    return ssh


def set_tx_lo_powerdown(ssh, powered_down):
    """In reserve, currently unused - see the module docstring."""
    value = "1" if powered_down else "0"
    ssh.exec_command("echo {} > {}".format(value, TX_LO_POWERDOWN_PATH))


def set_ptt(mqtt_client, callsign, on):
    publish(mqtt_client, callsign, "tx/mute", "0" if on else "1")
    log("🔊 PTT ON" if on else "🔇 PTT OFF")


def configure_pluto(mqtt_client, ip, callsign):
    # Without this, the Pluto's modulator can sit in whatever tx/stream/mode
    # it defaults to (e.g. "test" - a bare, unmodulated carrier) regardless
    # of how correctly every tx/dvbs2/* parameter below is configured. This
    # is the actual mode switch that makes it modulate real DVB-S2 data at
    # all - confirmed in the reference PlutoDVB2 source (pluto-ori).
    publish(mqtt_client, callsign, "tx/stream/mode", "dvbs2-ts")
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)
    # Full DVB-S2 field set as used by DATV-Red's own real preset (profiles/p1.json
    # device.tx.dvbs2), not just the subset we guessed was enough.
    publish(mqtt_client, callsign, "tx/frequency", FREQUENCY_HZ)
    publish(mqtt_client, callsign, "tx/gain", GAIN_DB)
    publish(mqtt_client, callsign, "tx/dvbs2/sr", SYMBOL_RATE)
    publish(mqtt_client, callsign, "tx/dvbs2/fecmode", "fixed")
    publish(mqtt_client, callsign, "tx/dvbs2/fec", FEC)
    publish(mqtt_client, callsign, "tx/dvbs2/frame", FRAME)
    publish(mqtt_client, callsign, "tx/dvbs2/pilots", "1" if PILOTS else "0")
    publish(mqtt_client, callsign, "tx/dvbs2/constel", "qpsk")
    publish(mqtt_client, callsign, "tx/dvbs2/gainvariable", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/fecrange", 10)
    publish(mqtt_client, callsign, "tx/dvbs2/tssourcemode", "0")
    # tx/dvbs2/digitalgain=0 was suspected of disconnecting the broker and
    # resetting SR, based on one earlier run. 5/5 repeat runs on 2026-09-06
    # (via pluto_mqtt_diagnostic.py) passed cleanly, and every real DATV-Red
    # profile (p1-p7) ships digitalgain=0 too, so it's back in the sequence.
    publish(mqtt_client, callsign, "tx/dvbs2/digitalgain", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/firfilter", "1")
    publish(mqtt_client, callsign, "tx/dvbs2/tssourceaddress",
            "{}:{}".format(ip, PLUTO_TS_PORT))


def build_video_pipeline(ip):
    return [
        "gst-launch-1.0", "-e",
        "mpegtsmux", "name=mux", "alignment=7", "!",
        "udpsink", "host={}".format(ip), "port={}".format(PLUTO_TS_PORT), "sync=true",

        "v4l2src", "device={}".format(CAMERA_DEVICE), "do-timestamp=true", "!",
        "image/jpeg,width=1280,height=720,framerate=30/1", "!",
        "jpegdec", "!",
        "videorate", "!", "video/x-raw,framerate={}/1".format(FPS), "!",
        "videoconvert", "!",
        "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV12", "!",
        "queue", "!",
        "nvv4l2h265enc", "bitrate={}".format(VIDEO_BITRATE_KBPS * 1000), "insert-sps-pps=true",
        "iframeinterval={}".format(FPS), "!",
        "h265parse", "config-interval=1", "!",
        "queue", "!", "mux.",

        "alsasrc", "device={}".format(AUDIO_DEVICE), "!",
        "audioconvert", "!", "audioresample", "!", "audiorate", "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=1", "!",
        "voaacenc", "bitrate={}".format(AUDIO_BITRATE_KBPS * 1000), "!",
        "aacparse", "!",
        "queue", "!", "mux.",
    ]


def print_banner():
    line = "=" * 62
    print(line)
    print("   🛰️   {}  —  DATV Transmitter (live cam+mic)".format(CALLSIGN))
    print("   📡  {:.3f} MHz".format(FREQUENCY_HZ / 1e6))
    print(line)


def main():
    print_banner()
    pluto_ip = discover_pluto_ip()
    mqtt_client = mqtt_connect(pluto_ip)
    process = None
    try:
        log("🔇 Muting RF before configuring (safety)...")
        set_ptt(mqtt_client, CALLSIGN, on=False)

        log("⚙️  Configuring DVB-S2 modulator on {} ({})...".format(pluto_ip, CALLSIGN))
        configure_pluto(mqtt_client, pluto_ip, CALLSIGN)

        pipeline = build_video_pipeline(pluto_ip)
        log("🎬 Starting video stream...")
        process = subprocess.Popen(pipeline)

        log("🔊 Keying up...")
        set_ptt(mqtt_client, CALLSIGN, on=True)

        print()
        log("🚀 TRANSMITTING on {:.3f} MHz, SR={} FEC={}. Press Ctrl+C to stop.".format(
            FREQUENCY_HZ / 1e6, SYMBOL_RATE, FEC))
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print()
        log("🛑 Stopping...")
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        set_ptt(mqtt_client, CALLSIGN, on=False)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        log("✅ Stopped. PTT OFF.")


if __name__ == "__main__":
    main()
