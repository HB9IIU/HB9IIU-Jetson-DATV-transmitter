"""One script to run a full DATV transmission: configure the Pluto, start
the video stream, and key PTT on - all in one process. Press Ctrl+C to
stop; this safely keys PTT off and stops the video before exiting.

No arguments - edit the settings below directly if you need to change
anything. The Pluto's IP is found automatically (mDNS, falling back to
its USB default address), so it doesn't need to be hardcoded.

Includes the TX-LO-powerdown fix found by testing against real hardware
(publishing tx/mute alone does not reliably power up the TX local
oscillator on this firmware build).

MQTT and SSH both use the Pluto's default credentials: root/analog.
"""

import socket
import subprocess

import paho.mqtt.client as mqtt
import paramiko

# ---- Settings - edit these directly ----
CAMERA_DEVICE = "/dev/video1"  # Logitech C920 USB webcam
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


def discover_pluto_ip():
    print("Looking for the Pluto...")
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
                print("  Found Pluto at {}".format(address))
                return address
        except OSError:
            continue
    raise SystemExit("No PlutoSDR found (checked mDNS and USB default). "
                      "Is it powered on and connected?")


def mqtt_connect(ip):
    client = mqtt.Client(client_id="jetson-datv-tx")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.connect(ip, MQTT_PORT, keepalive=5)
    return client


def publish(client, callsign, subtopic, payload):
    topic = "cmd/pluto/{}/{}".format(callsign, subtopic)
    client.publish(topic, payload=str(payload), qos=1)
    print("  {} -> {}".format(topic, payload))


def set_tx_lo_powerdown(ip, powered_down):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, password=SSH_PASSWORD, timeout=8)
    value = "1" if powered_down else "0"
    ssh.exec_command("echo {} > {}".format(value, TX_LO_POWERDOWN_PATH))
    ssh.close()


def set_ptt(mqtt_client, ip, callsign, on):
    publish(mqtt_client, callsign, "tx/mute", "0" if on else "1")
    set_tx_lo_powerdown(ip, powered_down=not on)
    print("PTT {}".format("ON" if on else "OFF"))


def configure_pluto(mqtt_client, ip, callsign):
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
    publish(mqtt_client, callsign, "tx/dvbs2/digitalgain", 0)
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


def main():
    pluto_ip = discover_pluto_ip()
    mqtt_client = mqtt_connect(pluto_ip)
    process = None
    try:
        print("Muting RF before configuring (safety)...")
        set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=False)

        print("Configuring DVB-S2 modulator on {} ({})...".format(pluto_ip, CALLSIGN))
        configure_pluto(mqtt_client, pluto_ip, CALLSIGN)

        pipeline = build_video_pipeline(pluto_ip)
        print("Starting video stream...")
        process = subprocess.Popen(pipeline)

        print("Keying up...")
        set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=True)

        print()
        print("TRANSMITTING on {:.3f} MHz. Press Ctrl+C to stop.".format(FREQUENCY_HZ / 1e6))
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print("Stopping...")
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=False)
        mqtt_client.disconnect()
        print("Stopped. PTT OFF.")


if __name__ == "__main__":
    main()
