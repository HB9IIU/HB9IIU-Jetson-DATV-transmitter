#!/usr/bin/env python3
"""Transmit the live SBB clock test card to the Pluto - no web UI.

Self-contained: everything it needs is in this folder (Pluto control and
the CBR relay in pluto_tx.py, the font next to it). The static background
(test card, dial, callsign) is drawn once with Pillow; cairooverlay draws
the hands and the tone label on every frame.

Needs a Jetson (nvv4l2h265enc hardware encoder) with GStreamer + PyGObject
+ pycairo, Pillow, paho-mqtt and ffmpeg. The clock shows the system's
local time. Stop any other stream first - both would drive the same Pluto.
Run:  python3 clock_tx.py   (Ctrl+C stops)
"""

import math
import os
import signal
import subprocess
import tempfile
import time

import cairo
import gi
from PIL import Image, ImageColor, ImageDraw, ImageFont

import pluto_tx as tx

# Register PyGObject's cairo_t -> pycairo.Context converter before loading
# GStreamer. Older Jetson/PyGObject releases otherwise deliver cairooverlay's
# draw context as a generic GBoxed object with no drawing methods.
gi.require_foreign("cairo")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402  (must follow gi.require_version)

# ---- Settings ----

FREQUENCY_HZ = 2405750000
SR_CHOICES = [333, 500]       # asked at startup, see ask_symbol_rate()
FEC = "2/3"
GAIN_DB = 0                   # 0 dB = maximum power
CALLSIGN = "HB9IIU"           # also the Pluto's MQTT topic callsign
LOCATOR = "JN36kl"
FPS = 25
# nvv4l2h265enc quality settings - the same as the main project's
# datv_tx_plus.py (2026-09-24, modelled on a captured OBS + Easy DATV
# stream): a keyframe every 4 s instead of every 1 s (each keyframe costs
# several P-frames' worth of bits), idrinterval matching so every keyframe
# is a clean entry point for receivers, 4 reference frames, Slow preset.
ENCODER_KEYFRAME_INTERVAL = 4 * FPS
ENCODER_PRESET_LEVEL = 4     # 1=UltraFast (encoder default) ... 4=Slow
ENCODER_REF_FRAMES = 4
STATUS_SECONDS = 30           # how often the "on air" status line is logged

# Tone per 5-second slot of every minute: (from_second, frequency_hz), each
# one playing for TONE_SECONDS from its second, silent until the next
# entry; 0 Hz = off. Before :05 it's silent. Lowest tone 300 Hz: 100 Hz
# wasn't audible on air (2026-09-26).
TONE_SECONDS = 1.0
TONE_SCHEDULE = [
    (5, 300),
    (10, 400),
    (15, 600),
    (20, 800),
    (25, 1000),
    (30, 1200),
    (35, 1000),
    (40, 800),
    (45, 600),
    (50, 400),
    (55, 300),
]
TONE_VOLUME = 0.126           # audiotestsrc linear scale, ~ -18 dBFS
# SBB second hand reaches 12 after this many seconds, then waits for the
# minute to jump.
SWEEP_SECONDS = 58.5
# Each minute: a BEEP_HZ time signal for TONE_SECONDS exactly when the
# minute hand jumps (:00), silence until :05, then TONE_SCHEDULE. (Until
# 2026-09-26 the beep ran from :58.5 while the second hand waited at 12 -
# which sounded early.)
BEEP_HZ = 1000
# Extra delay of the tone relative to its "... Hz" label. 0 since the hands
# and label are drawn for each frame's own display time (frame_wall_time()):
# the pipeline renders video ~1.1 s ahead, and drawing "now" instead made the
# label (and second hand) appear 1.15 s after the tone - measured on air
# 2026-09-26, and what the old value of 1.0 was compensating for.
TONE_AUDIO_DELAY = 0.0

WIDTH, HEIGHT = 1280, 720     # the layout below is drawn for 720p
SR = None                     # set in main() from the console prompt
PROFILE = None

BACKGROUND_PNG = os.path.join(tempfile.gettempdir(), "clock_tx_background.png")

CARD_GREY = "#6e6e6e"
RIM_COLOR = "#b4b8bc"
SBB_RED = (0xe0 / 255, 0x20 / 255, 0x20 / 255)

cx, cy = WIDTH // 2, HEIGHT // 2
radius = 300                  # white dial, in output pixels
RIM = 12                      # silver bezel width, in output pixels
SS = 3                        # supersampling factor for the static dial

GRID = 60                     # test card grid pitch, in output pixels
EDGE = 20                     # height of the black/white edge blocks

# 75% colour bars, top to bottom.
BARS = [
    (191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
    (191, 0, 191), (191, 0, 0), (0, 0, 191), (0, 0, 0),
]
GREY_STEPS = [255, 204, 153, 102, 51, 0]
GRATING_WIDTHS = [4, 3, 2, 1]  # line width per row, coarse to fine

TEXT_COLOR = "#3c4650"        # dark slate for callsign/locator, softer than black
LABEL_SIZE = 30               # "... Hz" label, in output pixels
CALLSIGN_SIZE = 54            # callsign on the dial, in output pixels (was 68)
LOCATOR_SIZE = 42             # locator on the dial, in output pixels
LABEL_Y = 0.54                # label centre below the dial centre, x radius

# Set in main(): cairo surface per tone frequency.
tone_labels = {}

FONT_PATHS = [
    # Rounded font shipped next to this script (OFL, see VarelaRound-OFL.txt).
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "VarelaRound-Regular.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Jetson
    "C:/Windows/Fonts/arialbd.ttf",
]


# ---- Static background (Pillow, drawn once) ----

def load_font(size):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_bbox(font, text):
    if hasattr(font, "getbbox"):
        return font.getbbox(text)
    # Pillow < 8 (Jetson)
    left, top = font.getoffset(text)
    right, bottom = font.getsize(text)
    return left, top, right, bottom


def centered_text(draw, x, y, text, font, fill):
    left, top, right, bottom = text_bbox(font, text)
    draw.text((x - (left + right) / 2, y - (top + bottom) / 2),
              text, font=font, fill=fill)


def build_testcard():
    # PM5544-style card: grey with a white grid, edge blocks, colour bars
    # on the left, grey steps and frequency gratings on the right.
    card = Image.new("RGB", (WIDTH, HEIGHT), CARD_GREY)
    draw = ImageDraw.Draw(card)

    for x in range(cx % GRID, WIDTH, GRID):
        draw.rectangle((x - 1, 0, x, HEIGHT), fill="white")
    for y in range(cy % GRID, HEIGHT + 1, GRID):
        draw.rectangle((0, y - 1, WIDTH, y), fill="white")

    for i, x in enumerate(range(cx % GRID - GRID, WIDTH, GRID)):
        fill = "black" if i % 2 else "white"
        draw.rectangle((x, 0, x + GRID - 1, EDGE - 1), fill=fill)
        draw.rectangle((x, HEIGHT - EDGE, x + GRID - 1, HEIGHT - 1),
                       fill=fill)

    top, bottom = cy - 4 * GRID, cy + 4 * GRID
    left_x0, left_x1 = cx - 10 * GRID, cx - 6 * GRID
    right_x0, right_x1 = cx + 6 * GRID, cx + 10 * GRID

    # Black frame behind each panel, the panel contents cover the inside.
    for x0, x1 in ((left_x0, left_x1), (right_x0, right_x1)):
        draw.rectangle((x0 - 2, top - 2, x1 + 1, bottom + 1), fill="black")

    for i, colour in enumerate(BARS):
        y = top + i * GRID
        draw.rectangle((left_x0, y, left_x1 - 1, y + GRID - 1), fill=colour)

    step = (right_x1 - right_x0) // len(GREY_STEPS)
    for i, level in enumerate(GREY_STEPS):
        x = right_x0 + i * step
        draw.rectangle((x, top, x + step - 1, cy - 1),
                       fill=(level, level, level))

    for row, width in enumerate(GRATING_WIDTHS):
        y = cy + row * GRID
        for x in range(right_x0, right_x1, 2 * width):
            draw.rectangle((x, y, x + width - 1, y + GRID - 1), fill="white")

    return card


def render_background(path):
    # The dial is drawn SS times larger in its own square box, then shrunk
    # down and pasted onto the card - that gives anti-aliased edges.
    card = build_testcard()
    half = radius + RIM + 4
    box = 2 * half * SS
    R = radius * SS
    C = box / 2

    face = card.crop((cx - half, cy - half, cx + half, cy + half))
    face = face.resize((box, box), Image.NEAREST)
    draw = ImageDraw.Draw(face)

    def disc(r, fill):
        draw.ellipse((C - r, C - r, C + r, C + r), fill=fill)

    def marker(angle, r_from, r_to, w):
        px, py = math.cos(angle), math.sin(angle)
        dx, dy = math.sin(angle), -math.cos(angle)
        a = w * R / 2
        x0, y0 = C + dx * r_from * R, C + dy * r_from * R
        x1, y1 = C + dx * r_to * R, C + dy * r_to * R
        draw.polygon([(x0 - px * a, y0 - py * a), (x1 - px * a, y1 - py * a),
                      (x1 + px * a, y1 + py * a), (x0 + px * a, y0 + py * a)],
                     fill="black")

    disc(R + (RIM + 3) * SS, "black")
    disc(R + RIM * SS, RIM_COLOR)
    disc(R, "white")

    for i in range(60):
        angle = 2 * math.pi * i / 60
        if i % 5 == 0:
            marker(angle, 0.75, 0.97, 0.065)
        else:
            marker(angle, 0.90, 0.97, 0.024)

    centered_text(draw, C, C - 0.45 * R, CALLSIGN, load_font(CALLSIGN_SIZE * SS), TEXT_COLOR)
    centered_text(draw, C, C + 0.40 * R, LOCATOR, load_font(LOCATOR_SIZE * SS), TEXT_COLOR)

    card.paste(face.resize((2 * half, 2 * half), Image.BOX),
               (cx - half, cy - half))
    card.save(path)


def render_tone_labels():
    # Cairo can't load the bundled font file, so Pillow renders each
    # "... Hz" label once to a transparent PNG that cairo then pastes.
    font = load_font(LABEL_SIZE)
    rgb = ImageColor.getrgb(TEXT_COLOR)
    labels = {}
    for freq in [f for _second, f in TONE_SCHEDULE] + [BEEP_HZ]:
        if not freq or freq in labels:
            continue
        text = "{} Hz".format(freq)
        left, top, right, bottom = text_bbox(font, text)
        # Transparent pixels in the text colour, so edges blend cleanly.
        image = Image.new("RGBA", (right - left, bottom - top), rgb + (0,))
        ImageDraw.Draw(image).text((-left, -top), text, font=font, fill=TEXT_COLOR)
        path = os.path.join(tempfile.gettempdir(), "clock_tx_label_{}.png".format(freq))
        image.save(path)
        labels[freq] = cairo.ImageSurface.create_from_png(path)
    return labels


def sound_at(wall_time):
    # Frequency to play at this wall-clock time, 0 = silence.
    second = wall_time % 60
    if second < TONE_SECONDS:
        return BEEP_HZ
    freq = 0
    for from_second, slot_freq in TONE_SCHEDULE:
        if from_second <= second < from_second + TONE_SECONDS:
            freq = slot_freq
    return freq


# ---- Hands (cairo, every frame) ----

# Set in main() once the pipeline exists; see frame_wall_time().
frame_clock = {"pipeline": None, "offset": None}


def frame_wall_time(timestamp):
    """Wall-clock time (time.time() scale) at which the frame with this
    buffer timestamp is shown - not the moment it happens to be drawn. The
    video branch (imagefreeze, not live) runs ~1.1 s ahead of the live
    audio, so drawing the clock for "now" put hands and label 1.1 s behind
    the tones. Running time + base time is the pipeline clock reading at
    which the frame plays; the offset maps that clock onto time.time()."""
    pipeline = frame_clock["pipeline"]
    clock = pipeline.get_clock() if pipeline is not None else None
    if clock is None or timestamp == Gst.CLOCK_TIME_NONE:
        return time.time()
    if frame_clock["offset"] is None:
        frame_clock["offset"] = time.time() - clock.get_time() / Gst.SECOND
    return (pipeline.get_base_time() + timestamp) / Gst.SECOND + frame_clock["offset"]


def hand(cr, angle, r_from, r_to, w_from, w_to):
    # Flat-ended bar along `angle`, tapering from w_from to w_to.
    # All sizes are fractions of the dial radius; negative r_from is a tail.
    px, py = math.cos(angle), math.sin(angle)
    dx, dy = math.sin(angle), -math.cos(angle)
    x0, y0 = cx + dx * r_from * radius, cy + dy * r_from * radius
    x1, y1 = cx + dx * r_to * radius, cy + dy * r_to * radius
    a, b = w_from * radius / 2, w_to * radius / 2
    cr.move_to(x0 - px * a, y0 - py * a)
    cr.line_to(x1 - px * b, y1 - py * b)
    cr.line_to(x1 + px * b, y1 + py * b)
    cr.line_to(x0 + px * a, y0 + py * a)
    cr.close_path()
    cr.fill()


def draw_hands(_overlay, cr, timestamp, _duration):
    now = frame_wall_time(timestamp)
    # Current tone label first, so the hands pass over it.
    label = tone_labels.get(sound_at(now))
    if label is not None:
        cr.set_source_surface(label, cx - label.get_width() / 2,
                              cy + LABEL_Y * radius - label.get_height() / 2)
        cr.paint()

    local = time.localtime(now)
    second = now % 60

    hour_angle = 2 * math.pi * (local.tm_hour % 12 + local.tm_min / 60) / 12
    minute_angle = 2 * math.pi * local.tm_min / 60
    second_angle = 2 * math.pi * min(second / SWEEP_SECONDS, 1.0)

    cr.set_source_rgb(0, 0, 0)
    hand(cr, hour_angle, -0.20, 0.64, 0.12, 0.095)
    hand(cr, minute_angle, -0.20, 0.90, 0.095, 0.07)

    cr.set_source_rgb(*SBB_RED)
    hand(cr, second_angle, -0.30, 0.60, 0.022, 0.022)
    cr.arc(cx + math.sin(second_angle) * 0.60 * radius,
           cy - math.cos(second_angle) * 0.60 * radius,
           0.10 * radius, 0, 2 * math.pi)
    cr.fill()


def pipeline_description():
    # Same encode/mux/relay chain as the main project's testcard source. The
    # tone generator is live and only ever has its freq/volume changed in
    # place (see main()), so there is no file loop to break mpegtsmux.
    return " ".join([
        "mpegtsmux name=mux alignment=7 !",
        "udpsink host=127.0.0.1 port={} sync=true".format(tx.CBR_RELAY_PORT),

        "filesrc location={} ! pngdec ! imagefreeze !".format(BACKGROUND_PNG),
        "videoconvert ! videorate ! video/x-raw,framerate={}/1 !".format(FPS),
        "videoconvert ! video/x-raw,format=BGRA !",
        "cairooverlay name=hands !",
        "videoconvert !",
        "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 !",
        "queue !",
        "nvv4l2h265enc bitrate={} insert-sps-pps=true iframeinterval={} idrinterval={}".format(
            PROFILE["video_bitrate_kbps"] * 1000, ENCODER_KEYFRAME_INTERVAL,
            ENCODER_KEYFRAME_INTERVAL),
        "preset-level={} num-Ref-Frames={} maxperf-enable=true !".format(
            ENCODER_PRESET_LEVEL, ENCODER_REF_FRAMES),
        "h265parse config-interval=1 !",
        "queue ! mux.",

        "audiotestsrc name=tone wave=sine freq={} volume=0 is-live=true !".format(
            TONE_SCHEDULE[0][1] or 440),
        "audioconvert ! audioresample ! audiorate !",
        "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
        "voaacenc bitrate={} !".format(PROFILE["audio_bitrate_kbps"] * 1000),
        "aacparse !",
        "queue ! mux.",
    ])


def ask_symbol_rate():
    while True:
        choice = input("Symbol rate? {} [{}]: ".format(
            " / ".join(str(sr) for sr in SR_CHOICES), SR_CHOICES[0])).strip()
        if not choice:
            return SR_CHOICES[0]
        if choice.isdigit() and int(choice) in SR_CHOICES:
            return int(choice)
        print("Invalid choice '{}', try again.".format(choice))


def main():
    global tone_labels, SR, PROFILE
    # Both signals stop like Ctrl+C (PTT off, relay stopped): SIGTERM from
    # kill/timeout, and SIGINT even when started in the background, where
    # the shell would otherwise leave it ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    SR = ask_symbol_rate()
    PROFILE = tx.PROFILES[(SR, FEC)]
    tx.log("🎛️  SR={} FEC={} {}x{} video={}kbps".format(
        SR, FEC, WIDTH, HEIGHT, PROFILE["video_bitrate_kbps"]))

    tx.log("🕐 Drawing the background...")
    render_background(BACKGROUND_PNG)
    tone_labels = render_tone_labels()

    Gst.init(None)
    pluto_ip = tx.discover_pluto_ip()
    mqtt_client = tx.mqtt_connect(pluto_ip, client_id="jetson-clock-tx")
    telemetry = {}
    relay = None
    pipeline = None
    try:
        tx.subscribe_telemetry(mqtt_client, CALLSIGN, telemetry)
        tx.set_ptt(mqtt_client, CALLSIGN, on=False)
        ts_bitrate = tx.configure_pluto_until_ready(
            mqtt_client, pluto_ip, CALLSIGN, PROFILE, telemetry, FREQUENCY_HZ, GAIN_DB)
        relay = tx.start_cbr_relay(pluto_ip, ts_bitrate)

        pipeline = Gst.parse_launch(pipeline_description())
        frame_clock["pipeline"] = pipeline
        pipeline.get_by_name("hands").connect("draw", draw_hands)
        tone = pipeline.get_by_name("tone")
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)

        tx.set_ptt(mqtt_client, CALLSIGN, on=True)
        tx.log("🚀 TRANSMITTING clock on {:.3f} MHz, SR={} FEC={}. Ctrl+C to stop.".format(
            FREQUENCY_HZ / 1e6, SR, FEC))

        playing = None
        on_air_since = time.monotonic()
        next_status = on_air_since + STATUS_SECONDS
        while True:
            if time.monotonic() >= next_status:
                on_air = int(time.monotonic() - on_air_since)
                tx.log("⏱️  {}m{:02d}s on air, {} relay 'dts < pcr' warnings".format(
                    on_air // 60, on_air % 60, tx.dts_warning_total))
                next_status += STATUS_SECONDS

            freq = sound_at(time.time() - TONE_AUDIO_DELAY)
            if freq != playing:
                if freq:
                    tone.set_property("freq", freq)
                tone.set_property("volume", TONE_VOLUME if freq else 0.0)
                playing = freq

            message = bus.timed_pop_filtered(
                int(0.05 * Gst.SECOND), Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                raise RuntimeError("GStreamer error: {} ({})".format(error, debug))
            tx.log("🔚 EOS - transmission ends here.")
            break
    except KeyboardInterrupt:
        pass
    finally:
        tx.log("🛑 Stopping...")
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if relay is not None:
            relay.terminate()
            try:
                relay.wait(timeout=3)
            except subprocess.TimeoutExpired:
                relay.kill()
            tx.log("📊 Relay 'dts < pcr' warnings this run: {}".format(tx.dts_warning_total))
        try:
            tx.set_ptt(mqtt_client, CALLSIGN, on=False)
            tx.log("✅ Stopped. PTT OFF.")
        except Exception as exc:
            tx.log("⚠️  WARNING: could not confirm PTT OFF ({}) - "
                   "check the Pluto directly!".format(exc))
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
