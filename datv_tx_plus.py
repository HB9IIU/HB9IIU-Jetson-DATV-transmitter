"""Like datv_tx.py (live camera+mic -> Pluto DVB-S2), but supports multiple
DVB-S2 profiles (symbol rate / FEC / resolution / bitrate combinations)
instead of one fixed configuration, plus a live telemetry overlay. Select
which profile is active by editing PROFILE below - still no command-line
arguments.

Profile values (resolution/bitrate per symbol rate/FEC) come from real
quality testing done with a local test movie file, not guesses: 960x540
was found to look noticeably better than 1280x720 at the same bitrate
(fewer compression mosaics on motion), and lower symbol rates get a lower
resolution to match their smaller bitrate budget. Add more profiles
(e.g. "sr333_fec45") to the PROFILES dict below the same way - no other
code needs to change.

Overlays on the video:
- UTC clock (top-right) and callsign (top-left). The Jetson's system
  clock is local time (Europe/Zurich), so TZ is forced to UTC for this
  process specifically (os.environ + time.tzset()) rather than changing
  the system-wide timezone.
- Live telemetry (bottom), updated every couple of seconds: Pluto
  temperature and actual TX bitrate (both via MQTT, from the Pluto's own
  ad9361-phy temp sensor and its live DVB-S2 stats), plus the Jetson's
  own CPU load and CPU temperature (read locally, not from the Pluto).

Why this needed a bigger change than datv_tx.py: a live-updating overlay
can't be done with a static `gst-launch` command string run as a
subprocess - the text has to be pushed into a *running* pipeline. So this
script builds and controls the GStreamer pipeline directly in Python via
PyGObject (the same GStreamer install, just used as a library instead of
a spawned CLI process). PyGObject isn't pip-installable in the project's
venv (it needs system packages), so the venv's pyvenv.cfg has
`include-system-site-packages = true` to let it see the system-installed
gi/GStreamer bindings alongside its own pip-installed paho-mqtt/paramiko.

Includes the same real hardware bugs/fixes proven in datv_tx.py:
- tx/mute over MQTT alone doesn't reliably power up the TX local
  oscillator on this firmware; PTT is done via direct sysfs control too.
- nvv4l2h265enc needs an explicit iframeinterval or the picture can freeze.

MQTT and SSH both use the Pluto's default credentials: root/analog.
"""

import os
import math
import socket
import subprocess
import time

import gi
import paho.mqtt.client as mqtt
import paramiko

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402  (must follow gi.require_version)

os.environ["TZ"] = "UTC"  # clockoverlay has no UTC option, only local time
time.tzset()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- Profiles - add more here later (e.g. "sr333_fec45") the same way ----
PROFILES = {
    "sr250_fec23": {"symbol_rate": 250000, "fec": "2/3", "resolution": (640, 360),
                    "video_bitrate_kbps": 220, "audio_bitrate_kbps": 32},
    "sr250_fec34": {"symbol_rate": 250000, "fec": "3/4", "resolution": (640, 360),
                    "video_bitrate_kbps": 250, "audio_bitrate_kbps": 32},
    "sr333_fec23": {"symbol_rate": 333000, "fec": "2/3", "resolution": (960, 540),
                    "video_bitrate_kbps": 305, "audio_bitrate_kbps": 32},
    "sr333_fec34": {"symbol_rate": 333000, "fec": "3/4", "resolution": (960, 540),
                    "video_bitrate_kbps": 300, "audio_bitrate_kbps": 32},
    "sr500_fec23": {"symbol_rate": 500000, "fec": "2/3", "resolution": (960, 540),
                    "video_bitrate_kbps": 550, "audio_bitrate_kbps": 32},
    "sr500_fec34": {"symbol_rate": 500000, "fec": "3/4", "resolution": (960, 540),
                    "video_bitrate_kbps": 600, "audio_bitrate_kbps": 32},
    "sr500_fec34_720p": {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                          "video_bitrate_kbps": 600, "audio_bitrate_kbps": 32},
    "sr500_fec23_720p": {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                          "video_bitrate_kbps": 550, "audio_bitrate_kbps": 32},
}

# ---- Settings - edit these directly ----
PROFILE = "sr333_fec34"
SOURCE = "video"  # "camera" (live cam+mic) or "video" (pick+loop a pre-processed video)
TX_OUTPUT = "pluto"  # "pluto" (transmit) or "file" (write the muxed TS to TX_OUTPUT_FILE for local inspection, no Pluto/MQTT needed)
TX_OUTPUT_FILE = "debug_output.ts"
CAMERA_DEVICE = "/dev/video0"  # Logitech C920 USB webcam
AUDIO_DEVICE = "plughw:2,0"  # Logitech C920 built-in microphone
CALLSIGN = "HB9IIU"
FREQUENCY_HZ = 2405000000
FRAME = "long"
PILOTS = True
GAIN_DB = -10 # 0 was confirmed to produce zero RF output in a raw hardware test; -10 produced a visible signal
TITLE_TEXT = "Jetson Nano - Standalone Hardware H.265 DVB-S2 Encoder"
TOP_BAR_HEIGHT = 45
TOP_BAR_ALPHA = 0.5
BOTTOM_BAR_HEIGHT = 30  # = text block height (~18px for "Sans 11" at 96 DPI) + 2x6px margin
BOTTOM_BAR_ALPHA = 0.5
BOTTOM_BAR_TEXT_MARGIN = 6  # top/bottom gap around bottom-bar text, in px
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
FORCE_USB = True  # Skip Ethernet/mDNS entirely and connect via the Pluto's USB interface
CPU_THERMAL_ZONE_PATH = "/sys/devices/virtual/thermal/thermal_zone1/temp"  # Jetson CPU-therm
TELEMETRY_UPDATE_SECONDS = 2.0
TS_BITRATE_WAIT_SECONDS = 30.0
PLUTO_CONFIG_RETRY_SECONDS = 2.0
CBR_RELAY_PORT = 18282


def discover_pluto_ip():
    print("Looking for the Pluto...")
    candidates = []
    if FORCE_USB:
        # Skip mDNS entirely - it would still find and prefer a flaky
        # Ethernet address if that interface responds at all, defeating
        # the point of forcing USB.
        print("  FORCE_USB is set - skipping Ethernet/mDNS discovery.")
    else:
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
    client = mqtt.Client(client_id="jetson-datv-tx-plus")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.connect(ip, MQTT_PORT, keepalive=5)
    # Start the network loop immediately. Previously it was started only
    # after all configuration publishes, so those QoS messages merely sat
    # in the local client queue during Pluto startup.
    client.loop_start()
    return client


def publish(client, callsign, subtopic, payload):
    topic = "cmd/pluto/{}/{}".format(callsign, subtopic)
    client.publish(topic, payload=str(payload), qos=1)
    print("  {} -> {}".format(topic, payload))


def subscribe_telemetry(mqtt_client, callsign, telemetry):
    prefix = "dt/pluto/{}/".format(callsign)

    def on_message(_client, _userdata, message):
        key = message.topic[len(prefix):]
        telemetry[key] = message.payload.decode("utf-8", "replace")

    mqtt_client.on_message = on_message
    # Subscribe before sending any configuration. The complete state tree
    # provides acknowledgements for the requested configuration. This
    # PlutoDVB2 build does NOT publish a tx/dvbs2/ts/bitrate topic, so the
    # TS capacity is calculated locally after the SR acknowledgement.
    # MQTT publish success alone only proves that
    # the broker received a command, not that pluto_mqtt_ctrl was listening.
    result, _mid = mqtt_client.subscribe(prefix + "#", qos=1)
    if result != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("Could not subscribe to Pluto telemetry")
    time.sleep(0.25)


def calculate_dvbs2_ts_bitrate(profile):
    """Return exact DVB-S2 QPSK normal-frame TS capacity in bit/s.

    This is the ETSI frame calculation used by the dvbs2rate utility. It
    deliberately replaces the nonexistent PlutoDVB2 MQTT
    ``tx/dvbs2/ts/bitrate`` telemetry topic. For example, 500 kS/s, 3/4,
    long frame and pilots on gives the previously verified 726038 bit/s.
    """
    fec_parameters = {
        "2/3": (2, 3, 10),
        "3/4": (3, 4, 12),
    }
    try:
        fec_num, fec_den, bch = fec_parameters[profile["fec"]]
    except KeyError:
        raise ValueError("Unsupported DVB-S2 FEC for TS calculation: {}".format(
            profile["fec"]))

    fec_frame_bits = 64800.0
    modulation_bits = 2.0  # QPSK
    data_symbols = fec_frame_bits / modulation_bits
    pilot_symbols = 36.0 if PILOTS else 0.0
    pilot_blocks = math.ceil(data_symbols / 90.0 / 16.0 - 1.0)
    frame_symbols = data_symbols + 90.0 + pilot_blocks * pilot_symbols
    useful_bits = fec_frame_bits * fec_num / fec_den - 16.0 * bch - 80.0
    return int(profile["symbol_rate"] / frame_symbols * useful_bits)


def configure_pluto_until_ready(mqtt_client, ip, callsign, profile, telemetry):
    """Configure Pluto only after its MQTT controller is demonstrably ready.

    The broker can accept commands before ``pluto_mqtt_ctrl`` has subscribed
    after boot. These command topics are not retained, so a one-shot publish
    can be silently lost. Keep resending the complete configuration while RF
    is hardware-muted, and proceed only when Pluto reports both the requested
    symbol rate. This firmware has no TS-bitrate telemetry topic; capacity is
    calculated locally from the acknowledged modulation parameters.
    """
    deadline = time.monotonic() + TS_BITRATE_WAIT_SECONDS
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        telemetry.pop("tx/dvbs2/sr", None)
        print("Configuring DVB-S2 modulator (attempt {})...".format(attempt))
        configure_pluto(mqtt_client, ip, callsign, profile)

        attempt_deadline = min(
            deadline, time.monotonic() + PLUTO_CONFIG_RETRY_SECONDS)
        while time.monotonic() < attempt_deadline:
            reported_sr = telemetry.get("tx/dvbs2/sr")
            try:
                sr_matches = int(reported_sr) == profile["symbol_rate"]
            except (TypeError, ValueError):
                sr_matches = False
            if sr_matches:
                bitrate = calculate_dvbs2_ts_bitrate(profile)
                # Reuse the overlay's existing field with our exact local value.
                telemetry["tx/dvbs2/ts/bitrate"] = str(bitrate)
                print("  Pluto acknowledged SR={} and TS capacity={} bit/s".format(
                    reported_sr, bitrate))
                return bitrate
            time.sleep(0.05)

        print("  Pluto controller not ready or did not acknowledge; retrying...")

    raise RuntimeError(
        "Pluto MQTT controller did not acknowledge configuration within {:.0f}s; "
        "RF remains muted".format(TS_BITRATE_WAIT_SECONDS))


def start_cbr_relay(pluto_ip, ts_bitrate):
    """Convert GStreamer's variable-rate TS into the fixed rate Pluto needs.

    The Jetson's older mpegtsmux has no ``bitrate`` property and therefore
    cannot insert null TS packets itself. With a static/easy-to-compress
    picture we measured about 615 kbit/s leaving GStreamer while PlutoDVB2
    required exactly 726038 bit/s for the active 500 kS/s, FEC 3/4 profile.
    That underfed Pluto's input buffer and caused audio to arrive late and
    intermittently at the receiver. FFmpeg remuxes without re-encoding and
    ``-muxrate`` fills unused capacity with null packets at Pluto's exact
    telemetry-reported rate.
    """
    input_url = "udp://127.0.0.1:{}?fifo_size=1000000&overrun_nonfatal=1".format(
        CBR_RELAY_PORT)
    output_url = "udp://{}:{}?pkt_size=1316".format(pluto_ip, PLUTO_TS_PORT)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+nobuffer", "-probesize", "32768", "-analyzeduration", "1000000",
        "-i", input_url,
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-muxrate", str(ts_bitrate),
        "-muxpreload", "0", "-muxdelay", "0",
        "-pcr_period", "20", "-pat_period", "0.4",
        "-streamid", "0:256", "-streamid", "1:257",
        "-mpegts_flags", "+system_b", "-flush_packets", "0",
        "-f", "mpegts", output_url,
    ]
    print("Starting CBR relay at {} bit/s...".format(ts_bitrate))
    return subprocess.Popen(command)


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


def configure_pluto(mqtt_client, ip, callsign, profile):
    # Without this, the Pluto's modulator can sit in whatever tx/stream/mode
    # it defaults to (e.g. "test" - a bare, unmodulated carrier) regardless
    # of how correctly every tx/dvbs2/* parameter below is configured. This
    # is the actual mode switch that makes it modulate real DVB-S2 data at
    # all - confirmed in the reference PlutoDVB2 source (pluto-ori), never
    # previously sent by this script.
    publish(mqtt_client, callsign, "tx/stream/mode", "dvbs2-ts")
    publish(mqtt_client, callsign, "tx/frequency", FREQUENCY_HZ)
    publish(mqtt_client, callsign, "tx/gain", GAIN_DB)
    publish(mqtt_client, callsign, "tx/dvbs2/sr", profile["symbol_rate"])
    publish(mqtt_client, callsign, "tx/dvbs2/fecmode", "fixed")
    publish(mqtt_client, callsign, "tx/dvbs2/fec", profile["fec"])
    publish(mqtt_client, callsign, "tx/dvbs2/frame", FRAME)
    publish(mqtt_client, callsign, "tx/dvbs2/pilots", "1" if PILOTS else "0")
    publish(mqtt_client, callsign, "tx/dvbs2/constel", "qpsk")
    publish(mqtt_client, callsign, "tx/dvbs2/gainvariable", "0")
    publish(mqtt_client, callsign, "tx/dvbs2/fecrange", 10)
    publish(mqtt_client, callsign, "tx/dvbs2/tssourcemode", "0")
    # Do not publish tx/dvbs2/digitalgain on PlutoDVB2 0.5.16.7. A direct,
    # RF-muted MQTT diagnostic proved that this command disconnects the broker
    # client and resets the modulator SR to its 1000000 boot value.
    publish(mqtt_client, callsign, "tx/dvbs2/firfilter", "1")
    publish(mqtt_client, callsign, "tx/dvbs2/tssourceaddress",
            "{}:{}".format(ip, PLUTO_TS_PORT))


def read_jetson_cpu_load_percent():
    # Load average is "how many cores' worth of work is queued", not a
    # percentage - normalize by core count so it reads like htop's overall
    # CPU%, instead of routinely exceeding 100% on this 4-core Jetson.
    return os.getloadavg()[0] / os.cpu_count() * 100.0


def read_jetson_cpu_temp_c():
    with open(CPU_THERMAL_ZONE_PATH) as f:
        return int(f.read().strip()) / 1000.0


def format_telemetry(telemetry):
    pluto_temp = telemetry.get("temperature_ad")
    pluto_temp_str = "{:.1f}°C".format(int(pluto_temp) / 1000.0) if pluto_temp else "--"
    tx_bitrate = telemetry.get("tx/dvbs2/ts/bitrate")
    tx_bitrate_str = "{:.0f}kb/s".format(int(tx_bitrate) / 1000.0) if tx_bitrate else "--"
    return "Pluto {} {} | Jetson CPU {:.1f}°C {:.0f}%".format(
        pluto_temp_str, tx_bitrate_str,
        read_jetson_cpu_temp_c(), read_jetson_cpu_load_percent())


def select_video_file(profile):
    """When SOURCE == "video", list the pre-processed videos available for
    the active profile's resolution (see preprocess_videos.py, which fills
    preprocessed_WxH/ folders next to this script) and let the user pick one
    by number.
    """
    width, height = profile["resolution"]
    video_dir = os.path.join(SCRIPT_DIR, "preprocessed_{}x{}".format(width, height))
    print("Looking for pre-processed videos in {}...".format(video_dir))

    if not os.path.isdir(video_dir):
        raise SystemExit(
            "No pre-processed videos folder for {}x{}: {}\n"
            "Run preprocess_videos.py first.".format(width, height, video_dir))

    files = sorted(name for name in os.listdir(video_dir) if name.lower().endswith(".mkv"))
    if not files:
        raise SystemExit(
            "No pre-processed videos found in {}\n"
            "Run preprocess_videos.py first.".format(video_dir))

    print("Available videos for {}x{}:".format(width, height))
    for i, name in enumerate(files, start=1):
        print("  {}) {}".format(i, name))

    while True:
        choice = input("Select a video [1-{}]: ".format(len(files))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            selected = files[int(choice) - 1]
            break
        print("Invalid choice '{}', try again.".format(choice))

    video_path = os.path.join(video_dir, selected)
    print("Selected: {}".format(video_path))
    return video_path


def build_pipeline_description(ip, profile, source_path=None):
    width, height = profile["resolution"]
    # valignment=bottom anchors to Pango's logical text box, which reserves
    # descender space these strings never use (no g/j/p/q/y - all caps and
    # digits), leaving dead space under the glyphs. Anchoring from the top
    # instead avoids that, so compute the exact pixel offset here.
    bottom_bar_text_ypad = height - BOTTOM_BAR_HEIGHT + BOTTOM_BAR_TEXT_MARGIN-6
    if TX_OUTPUT == "pluto":
        # Do not send this VBR mux directly to Pluto. Feed the local FFmpeg
        # relay instead; it adds null packets and forwards a correctly paced
        # CBR transport stream to Pluto's normal UDP port 8282.
        mux_sink = "udpsink host=127.0.0.1 port={} sync=true".format(CBR_RELAY_PORT)
    else:
        mux_sink = "filesink location={}".format(TX_OUTPUT_FILE)

    parts = [
        "mpegtsmux name=mux alignment=7 !",
        mux_sink,

        "compositor name=comp",
        "sink_0::xpos=0 sink_0::ypos=0",
        "sink_1::xpos=0 sink_1::ypos=0 sink_1::alpha={}".format(TOP_BAR_ALPHA),
        "sink_2::xpos=0 sink_2::ypos={} sink_2::alpha={}".format(
            height - BOTTOM_BAR_HEIGHT, BOTTOM_BAR_ALPHA),
        "!",
        "videoconvert !",
        "textoverlay text=\"{}\" halignment=center".format(TITLE_TEXT),
        "valignment=top ypad=0 shaded-background=false font-desc=\"Sans 14\" !",
        "textoverlay text=\"{}\" halignment=left xpad=10".format(CALLSIGN),
        "valignment=top ypad={} shaded-background=false font-desc=\"Sans 11\" !".format(
            bottom_bar_text_ypad),
        "clockoverlay time-format=\"%H:%M:%S UTC\" halignment=right xpad=10",
        "valignment=top ypad={} shaded-background=false font-desc=\"Sans 11\" !".format(
            bottom_bar_text_ypad),
        "textoverlay name=telemetry_overlay text=\"\" halignment=center",
        "valignment=top ypad={} shaded-background=false font-desc=\"Sans 11\" !".format(
            bottom_bar_text_ypad),
        "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 !",
        "queue !",
        "nvv4l2h265enc bitrate={} insert-sps-pps=true iframeinterval={} !".format(
            profile["video_bitrate_kbps"] * 1000, FPS),
        "h265parse config-interval=1 !",
        "queue ! mux.",
    ]

    # Source chain must link to comp. FIRST, before the bar sources below -
    # compositor names request pads sink_0/1/2 in link order, and the
    # sink_0/1/2 properties above assume sink_0=source, sink_1=top bar,
    # sink_2=bottom bar.
    if SOURCE == "camera":
        parts += [
            "v4l2src device={} do-timestamp=true !".format(CAMERA_DEVICE),
            "image/jpeg,width=1280,height=720,framerate=30/1 !",
            "jpegdec !",
            "videorate ! video/x-raw,framerate={}/1 !".format(FPS),
            "videoscale ! video/x-raw,width={},height={} !".format(width, height),
            "videoconvert ! comp.",

            "alsasrc device={} !".format(AUDIO_DEVICE),
            "audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    elif SOURCE == "video":
        parts += [
            # Pads are created dynamically once the file's streams are known,
            # but gst_parse_launch defers "filesrc." links until then - the
            # same idiom as `gst-launch-1.0 uridecodebin ... name=d d. ! ...`.
            "uridecodebin uri={} name=filesrc".format(Gst.filename_to_uri(source_path)),

            "filesrc. ! queue ! videoconvert ! videorate ! video/x-raw,framerate={}/1 !".format(FPS),
            "videoscale ! video/x-raw,width={},height={} !".format(width, height),
            "videoconvert ! comp.",

            "filesrc. ! queue ! audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    else:
        raise ValueError("Unknown SOURCE {!r}".format(SOURCE))

    parts += [
        "videotestsrc pattern=black is-live=true !",
        "video/x-raw,width={},height={},framerate={}/1 !".format(width, TOP_BAR_HEIGHT, FPS),
        "videoconvert ! comp.",

        "videotestsrc pattern=black is-live=true !",
        "video/x-raw,width={},height={},framerate={}/1 !".format(width, BOTTOM_BAR_HEIGHT, FPS),
        "videoconvert ! comp.",
    ]

    return " ".join(parts)


def main():
    profile = PROFILES[PROFILE]
    width, height = profile["resolution"]
    print("Profile '{}': SR={} FEC={} {}x{} video={}kbps audio={}kbps".format(
        PROFILE, profile["symbol_rate"], profile["fec"], width, height,
        profile["video_bitrate_kbps"], profile["audio_bitrate_kbps"]))

    source_path = select_video_file(profile) if SOURCE == "video" else None

    Gst.init(None)

    to_pluto = TX_OUTPUT == "pluto"
    pluto_ip = discover_pluto_ip() if to_pluto else None
    mqtt_client = mqtt_connect(pluto_ip) if to_pluto else None
    telemetry = {}
    cbr_relay = None
    gst_pipeline = None
    try:
        if to_pluto:
            # Listen for state acknowledgements before issuing commands.
            subscribe_telemetry(mqtt_client, CALLSIGN, telemetry)

            print("Muting RF before configuring (safety)...")
            set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=False)

            print("Waiting for Pluto MQTT control on {} ({})...".format(
                pluto_ip, CALLSIGN))
            ts_bitrate = configure_pluto_until_ready(
                mqtt_client, pluto_ip, CALLSIGN, profile, telemetry)
            cbr_relay = start_cbr_relay(pluto_ip, ts_bitrate)

        pipeline_description = build_pipeline_description(pluto_ip, profile, source_path)
        print("Starting video stream...")
        gst_pipeline = Gst.parse_launch(pipeline_description)
        telemetry_overlay = gst_pipeline.get_by_name("telemetry_overlay")
        bus = gst_pipeline.get_bus()
        gst_pipeline.set_state(Gst.State.PLAYING)

        if to_pluto:
            print("Keying up...")
            set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=True)
            print()
            print("TRANSMITTING on {:.3f} MHz. Press Ctrl+C to stop.".format(FREQUENCY_HZ / 1e6))
        else:
            print()
            print("Writing to '{}'. Press Ctrl+C to stop.".format(TX_OUTPUT_FILE))
        while True:
            message = bus.timed_pop_filtered(
                int(TELEMETRY_UPDATE_SECONDS * Gst.SECOND),
                Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError("GStreamer error: {} ({})".format(error, debug))
                break  # EOS
            if telemetry_overlay is not None:
                telemetry_overlay.set_property("text", format_telemetry(telemetry))
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print("Stopping...")
        if gst_pipeline is not None:
            gst_pipeline.set_state(Gst.State.NULL)
        if cbr_relay is not None:
            cbr_relay.terminate()
            try:
                cbr_relay.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cbr_relay.kill()
        if to_pluto:
            try:
                set_ptt(mqtt_client, pluto_ip, CALLSIGN, on=False)
                print("Stopped. PTT OFF.")
            except Exception as exc:
                print("  WARNING: could not confirm PTT OFF ({}) - "
                      "check the Pluto directly!".format(exc))
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        else:
            print("Stopped. Wrote '{}'.".format(TX_OUTPUT_FILE))


if __name__ == "__main__":
    main()
