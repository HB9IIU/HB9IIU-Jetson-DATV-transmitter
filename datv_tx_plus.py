"""Like datv_tx.py (live camera+mic -> Pluto DVB-S2), but supports multiple
DVB-S2 profiles (symbol rate / FEC / resolution / bitrate combinations)
instead of one fixed configuration, plus a live telemetry overlay. Select
which profile is active by editing PROFILE below - still no command-line
arguments.

Profile table (PROFILES, FRAME, PILOTS, and the DVB-S2 capacity formula) now
lives in dvbs2_profiles.py, shared with datv_tx_plus_fft.py - see that
module for how/why. Edit profiles there, not here.

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

Includes a real hardware fix proven in datv_tx.py: nvv4l2h265enc needs an
explicit iframeinterval or the picture can freeze.

PTT is MQTT tx/mute only. An earlier version also SSHed in to directly
power the TX LO down/up via sysfs, on the assumption that tx/mute alone
wasn't reliable - never actually verified (no repro recorded), and
contradicted by DATV-Red (the reference PC-side controller for this same
firmware), which mutes over MQTT alone. That SSH path is kept in reserve
(ssh_connect()/set_tx_lo_powerdown(), unused) in case real RF measurement
ever shows MQTT-only muting is insufficient.

MQTT uses the Pluto's default credentials: root/analog.
"""

import math
import os
import sys
import re
import socket
import subprocess
import threading
import time

import cairo
import gi
import paho.mqtt.client as mqtt
import paramiko
import yaml

# Register PyGObject's cairo_t -> pycairo.Context converter before loading
# GStreamer. Older Jetson/PyGObject releases otherwise deliver cairooverlay's
# draw context as a generic GBoxed object with no drawing methods.
gi.require_foreign("cairo")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402  (must follow gi.require_version)

from dvbs2_profiles import (
    PROFILES, FRAME, PILOTS, calculate_dvbs2_ts_bitrate,
    TESTCARD_PROFILE_NAMES, CAMERA_PROFILE_NAMES, VIDEO_PROFILE_NAMES)

os.environ["TZ"] = "UTC"  # clockoverlay has no UTC option, only local time
time.tzset()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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


def draw_marquee(overlay, cr, timestamp, duration, width, height, state):
    """Draw one frame of a true, continuously looping marquee."""
    style = MARQUEE_STYLES[(width, height)]
    cr.select_font_face(MARQUEE_FONT_FAMILY, 0, 0)  # normal slant/weight
    cr.set_font_size(style["font_size"])

    if state["first_timestamp"] is None:
        extents = cr.text_extents(MARQUEE_TEXT)
        # Support both tuple and attribute-style PyCairo text extents.
        if hasattr(extents, "x_bearing"):
            state["x_bearing"] = extents.x_bearing
            state["y_bearing"] = extents.y_bearing
            state["text_width"] = extents.width
            text_height = extents.height
        else:
            state["x_bearing"] = extents[0]
            state["y_bearing"] = extents[1]
            state["text_width"] = extents[2]
            text_height = extents[3]
        # font_extents (ascent/descent), not text_extents, for the
        # background bar's height - a stable line height regardless of
        # which glyphs MARQUEE_TEXT happens to contain, rather than
        # jittering with each frame's visible substring.
        font_extents = cr.font_extents()
        if hasattr(font_extents, "ascent"):
            state["font_ascent"] = font_extents.ascent
            state["font_descent"] = font_extents.descent
        else:
            state["font_ascent"] = font_extents[0]
            state["font_descent"] = font_extents[1]
        # Render the whole text ONCE into its own image (1 px margin around
        # the ink); every frame then only copies the visible window of it.
        # Re-rendering all glyphs of a ~5700 px text with show_text() on
        # every frame kept one core busy and held the pipeline at ~19 fps
        # at 1280x720 (measured 2026-09-25) even after the strip change.
        text_surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            int(math.ceil(state["text_width"])) + 2, int(math.ceil(text_height)) + 2)
        text_cr = cairo.Context(text_surface)
        text_cr.select_font_face(MARQUEE_FONT_FAMILY, 0, 0)
        text_cr.set_font_size(style["font_size"])
        text_cr.set_source_rgba(*MARQUEE_COLOR_RGBA)
        text_cr.move_to(1 - state["x_bearing"], 1 - state["y_bearing"])
        text_cr.show_text(MARQUEE_TEXT)
        text_surface.flush()
        state["text_surface"] = text_surface
        state["text_height"] = text_height
        state["first_timestamp"] = timestamp
        log("Marquee rendered width: {:.0f}px (video width: {}px)".format(
            state["text_width"], width))

    # The marquee is drawn on its own small, opaque strip (see
    # marquee_strip_geometry()), not on the full frame: shift frame
    # coordinates into strip coordinates and start every frame from a
    # clean (black) strip. The strip's transparency is applied as a whole
    # by the compositor (sink_3::alpha), not per pixel.
    cr.save()
    cr.set_operator(cairo.OPERATOR_CLEAR)
    cr.paint()
    cr.restore()
    cr.translate(0, -state["y_offset"])

    elapsed_seconds = (timestamp - state["first_timestamp"]) / float(Gst.SECOND)
    travel_distance = width + state["text_width"]
    x = width - ((elapsed_seconds * style["speed_px_per_second"]) % travel_distance)
    # Full-width band, not just behind the letters - a classic ticker strip
    # the text scrolls through, so the backdrop doesn't jump around with the
    # text's own changing width/position.
    bar_top = style["y_px"] - style["bg_padding_top_px"]
    bar_height = (state["font_ascent"] + state["font_descent"]
                  + style["bg_padding_top_px"] + style["bg_padding_bottom_px"])
    # Text centred vertically on its actual ink (tallest letter to lowest
    # descender) within the band's VISIBLE part. Placing its top at y_px
    # left ~4 px above and ~11 px below it at 1280x720 (2026-09-25): the
    # band's height comes from the font's line height, which is taller than
    # the ink, and the band starts above the frame (y_px < padding top).
    visible_top = max(0, bar_top)
    y_top = visible_top + (bar_top + bar_height - visible_top - state["text_height"]) / 2.0

    if MARQUEE_BG_RGBA is not None:
        cr.set_source_rgba(*MARQUEE_BG_RGBA)
        cr.rectangle(0, bar_top, width, bar_height)
        cr.fill()

    # The pre-rendered text image has a 1 px margin, so its top-left sits
    # 1 px up/left of the text's visible top-left edge (x, y_top). Cairo
    # only touches the part that lands inside the strip.
    cr.set_source_surface(state["text_surface"], x - 1, y_top - 1)
    cr.paint()

# ---- Settings - edit these directly ----


# Pick a symbol rate + FEC - the actual profile name (and therefore
# resolution/bitrate) is resolved automatically below from these two plus
# SOURCE, via TESTCARD/CAMERA/VIDEO_PROFILE_NAMES. No
# profile-name string to type or memorize here anymore, and no risk of the
# old "multiple uncommented PROFILE = lines, last one silently wins"
# confusion, since there's only one assignment each for SR/FEC/SOURCE.
SR = 500       # symbol rate in kS/s: 333 or 500
FEC = "2/3"    # DVB-S2 FEC: "2/3" or "3/4"

SOURCE = "video"  # "camera" (live cam+mic), "video" (pick+loop a pre-processed video), "testcard" (pick+loop a static image from testcards/), or "clock" (funnyClock's live SBB station clock)

# (SR, FEC) -> name of the entry to use in PROFILES, one table per SOURCE
# family - see dvbs2_profiles.py's TESTCARD/CAMERA/VIDEO_PROFILE_NAMES
# for the reasoning (shared with datv_engine.py,
# which needs the exact same lookup for the web UI's SR/FEC selectors -
# kept in one place so the two can't silently drift apart again).
_PROFILE_NAMES_BY_SOURCE = {
    "testcard": TESTCARD_PROFILE_NAMES,
    "camera": CAMERA_PROFILE_NAMES,
    "video": VIDEO_PROFILE_NAMES,
    # A test card with moving hands: same 720p profiles/bitrates as testcard.
    "clock": TESTCARD_PROFILE_NAMES,
}
try:
    PROFILE = _PROFILE_NAMES_BY_SOURCE[SOURCE][(SR, FEC)]
except KeyError:
    raise SystemExit(
        "No profile for SR={} FEC={} SOURCE={} - check SR/FEC/SOURCE above "
        "match a real entry in TESTCARD/CAMERA/VIDEO_PROFILE_NAMES "
        "in dvbs2_profiles.py.".format(SR, FEC, SOURCE))

TX_OUTPUT = "pluto"  # "pluto" (transmit) or "file" (write the muxed TS to TX_OUTPUT_FILE for local inspection, no Pluto/MQTT needed)
TX_OUTPUT_FILE = "debug_output.ts"
CAMERA_DEVICE = "/dev/video0"  # overwritten by select_camera_device() when SOURCE == "camera"
CAMERA_IS_CSI = False  # ditto - selects the nvarguscamerasrc branch instead of v4l2src+JPEG
# nvvidconv's flip-method enum (2026-09-07: this board's imx219 is mounted
# physically upside down): 0=none, 1=ccw-90, 2=rotate-180, 3=cw-90,
# 4=h-flip, 5=upper-right-diagonal, 6=v-flip, 7=upper-left-diagonal. Only
# applied on the CSI path - the USB webcam path doesn't use nvvidconv at
# capture time, and isn't mounted upside down anyway.
CSI_FLIP_METHOD = 2
AUDIO_DEVICE = "plughw:2,0"  # fallback only - overwritten by select_audio_device() in main() when SOURCE == "camera"
# WebRTC's adaptive AGC (webrtcdsp) on the camera mic input - a real debug_output.ts
# recording (2026-09-07) came out too quiet to use even with the ALSA capture level
# already near its max (56/60, 93%, checked via `amixer -c 2`), so the fix has to be
# in the pipeline, not the mixer. Only applies to SOURCE == "camera": pre-recorded
# video-file audio is already mixed/mastered and doesn't need this.
MIC_AGC = True
CALLSIGN = "HB9IIU"
FREQUENCY_HZ = 2405000000
# Writes directly to the Pluto's AD9361 out_voltage0_hardwaregain
# attenuation register via MQTT tx/gain. Real confirmed range (Pluto
# firmware source pluto-ori/mqtthandlecommand.cpp, and DATV-Red's own
# working gain slider: min:-89 max:0 step:-0.25) is -89 dB (near-off) to
# 0 dB (MAXIMUM power), 0.25 dB steps - NOT "0 = zero RF output" as this
# comment previously claimed (2026-09-07 raw hardware test result that
# contradicts both independent sources above and should be re-verified on
# real hardware rather than trusted either way).
GAIN_DB = -24

# Web-only override for MARQUEE_TEXT (camera_banner_marquee.yaml's
# marquee.text / video_banner_marquee.yaml's per-video text) - None means
# "use whichever YAML text the config loader picked". Set by
# datv_web_worker.py from the Setup page's saved overlay settings
# (overlay_settings.py on the Flask side); left None for direct/interactive
# runs of this script, which always just use the YAML text as-is.
MARQUEE_TEXT_OVERRIDE = None
# Same idea as MARQUEE_TEXT_OVERRIDE, for the top banner's title text
# (camera_banner_marquee.yaml's top_banner.text / video_banner_marquee.yaml's
# per-video text) instead. bottom_banner has no equivalent - it's always the
# live callsign/clock/telemetry overlay, never free text.
TITLE_TEXT_OVERRIDE = None

# When SOURCE == "testcard": testcard mode never uses the title/bars/clock/
# telemetry overlay system above (see main() - top_bar_enabled/
# bottom_bar_enabled are forced False, no prompt) - a still test-card
# image is its own "chrome" and
# doesn't need it. The four things still burned in - callsign, freq
# banner, volume banner, elapsed-time readout - have their
# position/font/color loaded from TESTCARD_OVERLAY_CONFIG_PATH (see
# load_testcard_overlay_config() below) instead of being fixed constants
# here: different test card images (and the same image at a different
# profile resolution) need different placement to land on a clean part of
# the artwork, and with up to 10 test cards x 3 resolutions that's better
# edited in one external file than hardcoded per combination in Python.
TESTCARD_OVERLAY_CONFIG_PATH = os.path.join(SCRIPT_DIR, "testcard_overlays.yaml")
TESTCARD_TIME_OVERLAY_TICK_SECONDS = 0.05  # how often the readout refreshes

# Scrolling banner: cairooverlay draws the text at an unrestricted pixel x
# coordinate. The video frame clips it, so even text much wider than the
# picture enters completely from the right and exits completely to the left.
# Motion is based on video timestamps and therefore remains smooth without a
# Python-side polling tick.
# Startup default only - for SOURCE in ("camera", "video") this is
# overwritten by ask_banner_and_marquee_settings()'s prompt in main()
# before build_pipeline_description() ever reads it. Only matters as-is
# for TESTCARD tuning trials, which never build a marquee regardless (see
# build_pipeline_description()'s testcard branch).
MARQUEE_ENABLED = True

CAMERA_BANNER_MARQUEE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "camera_banner_marquee.yaml")
VIDEO_BANNER_MARQUEE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "video_banner_marquee.yaml")


def _parse_resolution_key(key):
    width_str, height_str = key.split("x")
    return int(width_str), int(height_str)


def _resolve_banner_marquee_config(config):
    """Turn a raw {top_banner, bottom_banner, marquee} dict - either
    camera_banner_marquee.yaml loaded directly, or video_banner_marquee.yaml
    after merging a video's override onto its "default" (see
    _deep_merge_banner_marquee()) - into the flat, (width, height)-keyed
    structures build_pipeline_description()/draw_marquee() actually read
    (OVERLAY_STYLES/MARQUEE_STYLES shape, plus the flat TITLE_TEXT/
    MARQUEE_* scalars).
    """
    top_banner_cfg = config["top_banner"]
    bottom_banner_cfg = config["bottom_banner"]
    marquee_cfg = config["marquee"]

    overlay_styles = {}
    for key, top_res_cfg in top_banner_cfg.items():
        if key == "text":
            continue
        resolution = _parse_resolution_key(key)
        bottom_res_cfg = bottom_banner_cfg[key]
        overlay_styles[resolution] = {
            "title_font_size": top_res_cfg["font_size"],
            "top_bar_height": top_res_cfg["bar_height"],
            "top_bar_alpha": top_res_cfg["bar_alpha"],
            "bottom_bar_height": bottom_res_cfg["bar_height"],
            "bottom_bar_alpha": bottom_res_cfg["bar_alpha"],
            "bottom_bar_text_margin": bottom_res_cfg["text_margin"],
            "bottom_text_font_size": bottom_res_cfg["font_size"],
        }

    marquee_styles = {}
    for key, res_cfg in marquee_cfg.items():
        if key in ("text", "font_family", "color_rgba", "bg_rgba"):
            continue
        resolution = _parse_resolution_key(key)
        marquee_styles[resolution] = {
            "y_px": res_cfg["y_px"],
            "font_size": res_cfg["font_size"],
            "speed_px_per_second": res_cfg["speed_px_per_second"],
            "bg_padding_top_px": res_cfg["bg_padding_top_px"],
            "bg_padding_bottom_px": res_cfg["bg_padding_bottom_px"],
        }

    bg_rgba = marquee_cfg.get("bg_rgba")
    return {
        "title_text": top_banner_cfg["text"],
        "overlay_styles": overlay_styles,
        "marquee_text": marquee_cfg["text"],
        "marquee_font_family": marquee_cfg["font_family"],
        "marquee_color_rgba": tuple(marquee_cfg["color_rgba"]),
        "marquee_bg_rgba": tuple(bg_rgba) if bg_rgba is not None else None,
        "marquee_styles": marquee_styles,
    }


def load_camera_banner_marquee_config():
    """TITLE_TEXT/OVERLAY_STYLES/MARQUEE_TEXT/MARQUEE_FONT_FAMILY/
    MARQUEE_COLOR_RGBA/MARQUEE_BG_RGBA/MARQUEE_STYLES for SOURCE ==
    "camera" - everything about the top/bottom banner and scrolling
    marquee EXCEPT whether each is actually on (that's MARQUEE_ENABLED /
    top_bar_enabled/bottom_bar_enabled, asked interactively - see
    ask_banner_and_marquee_settings() and main()). Loaded once here, at
    import time, from CAMERA_BANNER_MARQUEE_CONFIG_PATH
    (camera_banner_marquee.yaml - see that file's own comments) instead of
    being hardcoded in this file, same reasoning as
    TESTCARD_OVERLAY_CONFIG_PATH: geometry/text/color is easier to tune by
    editing one external file than by editing Python, and doesn't need a
    code change to adjust. SOURCE == "video" has its own separate loader,
    load_video_banner_marquee_config() below - camera has no per-item
    (per-file) concept to key off, unlike video/testcard.
    """
    with open(CAMERA_BANNER_MARQUEE_CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    return _resolve_banner_marquee_config(config)


def _deep_merge_banner_marquee(default_cfg, override_cfg):
    """Field-by-field fallback to default_cfg for anything not present in
    override_cfg - same merge semantics as load_testcard_overlay_config(),
    just one level deeper: top_banner/bottom_banner/marquee's own
    resolution blocks (and text/font_family/color_rgba/bg_rgba scalars)
    each fall back individually, not as an all-or-nothing block.
    """
    merged = {}
    for section_name, default_section in default_cfg.items():
        override_section = override_cfg.get(section_name, {})
        merged_section = dict(default_section)
        for key, value in override_section.items():
            if isinstance(value, dict) and isinstance(merged_section.get(key), dict):
                merged_section[key] = dict(merged_section[key], **value)
            else:
                merged_section[key] = value
        merged[section_name] = merged_section
    return merged


def load_video_banner_marquee_config(source_path):
    """Same idea as load_camera_banner_marquee_config(), but for SOURCE ==
    "video": looked up fresh here (not cached at import time, since the
    active video isn't known until select_video_file() runs in main())
    from VIDEO_BANNER_MARQUEE_CONFIG_PATH (video_banner_marquee.yaml),
    keyed by the video's filename without extension - falls back to that
    file's "default" section field-by-field for anything not overridden,
    exactly like load_testcard_overlay_config(). Returns all resolutions
    at once (same shape as load_camera_banner_marquee_config()) since
    OVERLAY_STYLES/MARQUEE_STYLES are indexed by (width, height) downstream
    - build_pipeline_description() picks out the one it needs.
    """
    with open(VIDEO_BANNER_MARQUEE_CONFIG_PATH) as f:
        all_config = yaml.safe_load(f)

    video_name = os.path.splitext(os.path.basename(source_path))[0]
    override = all_config.get(video_name) or {}
    merged = _deep_merge_banner_marquee(all_config["default"], override)
    return _resolve_banner_marquee_config(merged)


_camera_banner_marquee_config = load_camera_banner_marquee_config()
TITLE_TEXT = _camera_banner_marquee_config["title_text"]
OVERLAY_STYLES = _camera_banner_marquee_config["overlay_styles"]
MARQUEE_TEXT = _camera_banner_marquee_config["marquee_text"]
MARQUEE_FONT_FAMILY = _camera_banner_marquee_config["marquee_font_family"]
MARQUEE_COLOR_RGBA = _camera_banner_marquee_config["marquee_color_rgba"]
MARQUEE_BG_RGBA = _camera_banner_marquee_config["marquee_bg_rgba"]
MARQUEE_STYLES = _camera_banner_marquee_config["marquee_styles"]

# -----------------------------------------

MQTT_PORT = 1883
MQTT_USERNAME = "root"
MQTT_PASSWORD = "analog"
SSH_USERNAME = "root"
SSH_PASSWORD = "analog"
TX_LO_POWERDOWN_PATH = "/sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown"
FPS = 25
# nvv4l2h265enc quality settings, modelled on a captured OBS + Easy DATV
# stream (2026-09-24, SR500 3/4) that looked clearly better on air: a
# keyframe every 4 s (not every 1 s - each keyframe costs ~5x a P-frame)
# and 4 reference frames. idrinterval matches so every keyframe is a clean
# IDR a receiver can lock onto. All options confirmed on the Nano via
# gst-inspect-1.0 (num-B-Frames is Xavier-only, so none).
ENCODER_KEYFRAME_INTERVAL = 4 * FPS
ENCODER_PRESET_LEVEL = 4  # 1=UltraFast (encoder default) ... 4=Slow
ENCODER_REF_FRAMES = 4
PLUTO_TS_PORT = 8282
IIOD_PORT = 30431
USB_DEFAULT_IP = "192.168.2.1"
FORCE_USB = True  # Skip Ethernet/mDNS entirely and connect via the Pluto's USB interface
CPU_THERMAL_ZONE_PATH = "/sys/devices/virtual/thermal/thermal_zone1/temp"  # Jetson CPU-therm
TELEMETRY_UPDATE_SECONDS = 2.0
# When SOURCE == "testcard": a little looping melody instead of a music
# soundtrack file or a plain line-up sweep. Deliberately a live
# audiotestsrc, not a file: an earlier attempt played a real soundtrack
# file on a loop (seeking back to 0 on EOS), but every loop point injected
# a segment discontinuity that mpegtsmux couldn't handle, corrupting the
# muxed TS from that point on (confirmed on real hardware - see git
# history). A live sine generator has no file, no EOS, nothing to seek -
# each note is just a (frequency_hz, duration_seconds) step, and its
# frequency is changed in place via set_property() in main()'s poll loop
# once the current note's duration has elapsed, sidestepping the whole
# problem. Volume does NOT vary note-to-note within a play-through - it's
# one fixed level for the whole tune, cycling to the next level in
# TESTCARD_MELODY_VOLUMES only once per full repeat (see main()).
#
# Two melodies below, same idiom as the PROFILE selector further up this
# file: both stay defined, only the last assignment is active - swap which
# one is on top to switch. The first is a small original, deliberately
# silly "bouncing clown" tune, kept as a fallback/reference. The second
# (active) is a precise transcription of the BBC World Service's "Lincoln-
# shire Poacher" interval signal (first two bars, in G major) - unlike the
# earlier from-memory attempt at a different famous tune, this one was
# given as an exact frequency/duration table, not reconstructed from
# memory, so it should be accurate as transcribed.
TESTCARD_TONE_MELODY = [
    (392, 0.5),  # G4
    (494, 0.5),  # B4
    (587, 0.5),  # D5
    (494, 0.5),  # B4
    (392, 0.5),  # G4
    (330, 0.5),  # E4
    (392, 1.5),  # G4, held - the "punchline"
]
TESTCARD_TONE_MELODY = [
    (294, 0.20),  # D4
    (392, 0.40),  # G4
    (392, 0.20),  # G4
    (392, 0.20),  # G4
    (370, 0.20),  # F#4
    (330, 0.20),  # E4
    (294, 0.40),  # D4
    (262, 0.20),  # C4
    (247, 0.40),  # B3
    (294, 0.20),  # D4
]
# Volume (audiotestsrc's own linear 0.0-1.0 scale; 0.126 ~= -18dBFS, this
# project's usual calibration-tone level) for one entire play-through of
# the tune - cycles to the next entry only when the tune restarts, so each
# repeat is audibly quieter/louder than the last rather than varying
# within a single play.
TESTCARD_MELODY_VOLUMES = [0.05, 0.25, 0.50, 0.75, 1.00]
TESTCARD_MELODY_REST_SECONDS = 1.5  # silent gap after the whole tune, before it repeats
# The actual runtime playback sequence: TESTCARD_TONE_MELODY followed by a
# single silent rest before the tune loops back to the start. Each entry
# is (freq, duration, is_rest) - the volume applied at each step is
# decided separately in main() (see TESTCARD_MELODY_VOLUMES above);
# is_rest drives the banner text: blank during the rest, "<freq> Hz  Vol
# <volume>" during a note.
TESTCARD_TONE_SCHEDULE = [
    (freq, duration, False) for freq, duration in TESTCARD_TONE_MELODY
]
TESTCARD_TONE_SCHEDULE.append(
    (TESTCARD_TONE_MELODY[-1][0], TESTCARD_MELODY_REST_SECONDS, True))
TESTCARD_MELODY_TICK_SECONDS = 0.1  # poll granularity - keeps short notes/rests on schedule
# The video path (hardware H.265 encode, mux, the CBR relay's pacing
# buffer) carries noticeably more latency than the lightweight audio path
# (audiotestsrc -> voaacenc), so a same-instant update of the banner text
# and the actual tone frequency arrives at the receiver with the banner
# ahead of the tone it's supposed to label (confirmed on real hardware:
# the display looked "behind" by about a full tone). Delaying the actual
# frequency change relative to the banner text compensates for that. This
# is a rough starting guess, not a measured value - increase it if the
# display still looks ahead of what you hear, decrease it if the tone now
# arrives audibly before the label changes.
TESTCARD_AUDIO_DELAY_SECONDS = 1.0
# When SOURCE == "video", detect the file ending by polling its own position
# vs. duration instead of waiting for a pipeline-level EOS message: this
# pipeline mixes the file's decoded video with two always-live videotestsrc
# bars in a compositor, and an aggregator-style element only forwards EOS
# once *all* its sink pads have seen it - the live bars never do, so a real
# end-of-file EOS never reaches the bus, and the transmission would
# otherwise hang forever on a frozen last frame instead of stopping
# (confirmed on real hardware/receiver on 2026-09-06). Must be bigger than
# TELEMETRY_UPDATE_SECONDS so the poll loop always catches it in time.
VIDEO_END_MARGIN_SECONDS = 3.0
TS_BITRATE_WAIT_SECONDS = 30.0
PLUTO_CONFIG_RETRY_SECONDS = 2.0
CBR_RELAY_PORT = 18282
# The CBR relay's -muxdelay: how far ahead of PCR each frame's DTS is
# scheduled. 0 made nearly every PES late ("dts < pcr, TS is invalid",
# ~40/s on air). Offline on the Jetson's ffmpeg 3.4 (2026-09-22): 0 -> 369
# warnings per 20s, 0.1 -> 42, 0.2+ -> 0 at sr333; sr500_fec34 still showed
# ~2.6/s on air at 0.5 (2026-09-23). tuning/tune_profiles.py measures the
# minimum per profile - costs this much extra end-to-end latency.
# 1.0 (2026-09-23): a recorded 8.4 min on-air sr500_fec34 stream of a
# detailed room scene replayed offline gave 2326 warnings at 0.5, 15 at
# 0.7, 0 at 1.0 (live had 2182 at 0.5 - so oversized keyframes, not live
# timing). The tuner's benchmark clip was clean at 0.5, i.e. too easy.
CBR_MUXDELAY_SECONDS = 1.0

# Diagnostic: when True, every transmission also saves the exact VBR TS it
# sends to the CBR relay into tuning/runs/on_air/ (~30 MB per 5 min at
# sr500), so it can be pushed through the relay offline afterwards
# (tuning/tune_profiles.py's count_relay_warnings()). 2026-09-23: on air
# still showed "dts < pcr" at muxdelay 0.5s where the offline benchmark
# clip was clean - replaying the real stream tells oversized frames
# (warnings reproduce offline) from live delivery timing (they don't). It
# was oversized frames - see CBR_MUXDELAY_SECONDS.
RECORD_ON_AIR_TS = False
ON_AIR_TS_DIR = os.path.join(SCRIPT_DIR, "tuning", "runs", "on_air")
# DATV-Red (the reference PC-side controller for this firmware) waits after
# a tx/stream/mode change before resending the rest of the config - see its
# "delay restore after MODE set" node (pauseType "delay", timeout 0.5s).
# Mirrored here for the same reason: give the modulator time to settle
# after the mode switch before sending the rest of the DVB-S2 parameters.
MODE_SWITCH_SETTLE_SECONDS = 0.5


def discover_pluto_ip():
    log("📡 Looking for the Pluto...")
    candidates = []
    if FORCE_USB:
        # Skip mDNS entirely - it would still find and prefer a flaky
        # Ethernet address if that interface responds at all, defeating
        # the point of forcing USB.
        log("   FORCE_USB is set - skipping Ethernet/mDNS discovery.")
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
                log("   ✅ Found Pluto at {}".format(address))
                return address
        except OSError:
            continue
    raise SystemExit("❌ No PlutoSDR found (checked mDNS and USB default). "
                      "Is it powered on and connected?")


def mqtt_connect(ip, client_id="jetson-datv-tx-plus"):
    # A distinct client_id per process matters: MQTT brokers silently
    # disconnect whichever connection is already using a given client_id the
    # moment a second one connects with the same id - so a read-only viewer
    # (e.g. telemetry_web.py) reusing this function must pass its own id, or
    # it would kick this script's live control connection off the broker.
    client = mqtt.Client(client_id=client_id)
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
    log("   📤 {} -> {}".format(topic, payload))


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
        log("⚙️  Configuring DVB-S2 modulator (attempt {})...".format(attempt))
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
                log("   ✅ Pluto acknowledged SR={} and TS capacity={} bit/s".format(
                    reported_sr, bitrate))
                return bitrate
            time.sleep(0.05)

        log("   ⏳ Pluto controller not ready or did not acknowledge; retrying...")

    raise RuntimeError(
        "❌ Pluto MQTT controller did not acknowledge configuration within {:.0f}s; "
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
    # reuse=1 lets ffmpeg rebind this port immediately even if a just-killed
    # previous run's socket briefly lingers - without it, a quick stop/start
    # cycle can fail with "Address already in use".
    input_url = "udp://127.0.0.1:{}?fifo_size=1000000&overrun_nonfatal=1&reuse=1".format(
        CBR_RELAY_PORT)
    output_url = "udp://{}:{}?pkt_size=1316".format(pluto_ip, PLUTO_TS_PORT)
    log("🎞️  Starting CBR relay at {} bit/s...".format(ts_bitrate))
    command = build_cbr_relay_command(input_url, output_url, ts_bitrate)
    # repeat+: print every repeated warning instead of ffmpeg's untimed
    # "Last message repeated N times", so log_cbr_relay_output() can place
    # each one in time.
    command[command.index("-loglevel") + 1] = "repeat+warning"
    relay = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)
    threading.Thread(target=log_cbr_relay_output, args=(relay.stderr,), daemon=True).start()
    return relay


def log_cbr_relay_output(stderr):
    """Timestamp the relay's stderr into our own log. "dts < pcr" warnings
    are condensed to one line per second (they can come ~40/s) - 2026-09-23:
    every sr500 transmission logged ~300-390 of them whether it ran 2 or 19
    minutes, so when they happen matters more than how many."""
    dts_count = 0
    dts_window_start = None

    def flush_dts():
        # Stamped with the window's own start, since the line itself is only
        # written once the next relay line (or exit) arrives.
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
            continue
        log("   relay: " + line.rstrip())
    if dts_count:
        flush_dts()


def build_cbr_relay_command(input_url, output_url, ts_bitrate,
                            muxdelay_seconds=CBR_MUXDELAY_SECONDS):
    """The relay's ffmpeg command line - shared with tuning/tune_profiles.py,
    which runs it on a file instead of UDP so it measures exactly what goes
    on air."""
    return [
        # -nostdin: otherwise ffmpeg reads keys from the terminal it was
        # started from - typing/pasting there while on air switched it
        # to debug output and a command prompt (funnyClock, 2026-09-26).
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+nobuffer", "-probesize", "32768", "-analyzeduration", "1000000",
        "-i", input_url,
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-muxrate", str(ts_bitrate),
        "-muxpreload", "0", "-muxdelay", str(muxdelay_seconds),
        "-pcr_period", "20", "-pat_period", "0.4",
        "-streamid", "0:256", "-streamid", "1:257",
        "-mpegts_flags", "+system_b", "-flush_packets", "0",
        "-f", "mpegts", output_url,
    ]


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
    """In reserve, currently unused.

    This direct sysfs write was added on the assumption that MQTT tx/mute
    alone doesn't reliably power the TX LO down/up on this firmware - a
    claim never actually verified (no repro recorded anywhere), and
    contradicted by DATV-Red, the reference PC-side controller for this
    same firmware, which mutes over MQTT alone with no SSH/sysfs step at
    all. set_ptt() no longer calls this. Wire it back in (pass an
    ssh_connect()'d client through) if real RF measurement ever shows
    MQTT-only muting is insufficient.
    """
    value = "1" if powered_down else "0"
    ssh.exec_command("echo {} > {}".format(value, TX_LO_POWERDOWN_PATH))


def set_ptt(mqtt_client, callsign, on):
    publish(mqtt_client, callsign, "tx/mute", "0" if on else "1")
    log("🔊 PTT ON" if on else "🔇 PTT OFF")


def format_gain_db(gain_db):
    """tx/gain MQTT payload, snapped to the AD9361's real 0.25 dB step and
    formatted as a plain integer when possible (e.g. "-24", not "-24.0").

    Untested hypothesis (2026-09-10): the web UI's gain slider made GAIN_DB
    a float instead of the previous hardcoded int, and the very next
    web-UI stream attempt got zero Pluto SR acknowledgements (gain is sent
    before sr in configure_pluto() - a stall processing tx/gain would
    explain that). Sending a noisy "-24.0" instead of a clean "-24" is a
    plausible culprit if Pluto's controller parses this command strictly,
    so this formatting removes that variable - never confirmed against
    actual firmware source, re-verify on real hardware if SR acks still
    fail with a fractional gain value.
    """
    rounded = round(gain_db * 4) / 4.0
    if rounded == int(rounded):
        return str(int(rounded))
    return "{:.2f}".format(rounded).rstrip("0").rstrip(".")


def configure_pluto(mqtt_client, ip, callsign, profile):
    # Without this, the Pluto's modulator can sit in whatever tx/stream/mode
    # it defaults to (e.g. "test" - a bare, unmodulated carrier) regardless
    # of how correctly every tx/dvbs2/* parameter below is configured. This
    # is the actual mode switch that makes it modulate real DVB-S2 data at
    # all - confirmed in the reference PlutoDVB2 source (pluto-ori).
    publish(mqtt_client, callsign, "tx/stream/mode", "dvbs2-ts")
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)
    publish(mqtt_client, callsign, "tx/frequency", FREQUENCY_HZ)
    publish(mqtt_client, callsign, "tx/gain", format_gain_db(GAIN_DB))
    publish(mqtt_client, callsign, "tx/dvbs2/sr", profile["symbol_rate"])
    publish(mqtt_client, callsign, "tx/dvbs2/fecmode", "fixed")
    publish(mqtt_client, callsign, "tx/dvbs2/fec", profile["fec"])
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


def read_jetson_cpu_load_percent():
    # Load average is "how many cores' worth of work is queued", not a
    # percentage - normalize by core count so it reads like htop's overall
    # CPU%, instead of routinely exceeding 100% on this 4-core Jetson.
    return os.getloadavg()[0] / os.cpu_count() * 100.0


def read_jetson_cpu_temp_c():
    with open(CPU_THERMAL_ZONE_PATH) as f:
        return int(f.read().strip()) / 1000.0


# Previous /proc/stat sample (busy, total jiffies) - the load is the busy
# share since the last read_jetson_cpu_load_percent() call, i.e. over one
# telemetry update interval.
_cpu_load_sample = None


def read_jetson_cpu_load_percent():
    """Average load of all CPU cores since the previous call, or None on
    the first call (nothing to compare with yet)."""
    global _cpu_load_sample
    with open("/proc/stat") as f:
        # "cpu  user nice system idle iowait irq softirq steal ..."
        values = [int(v) for v in f.readline().split()[1:]]
    idle = values[3] + values[4]
    total = sum(values[:8])
    previous, _cpu_load_sample = _cpu_load_sample, (total - idle, total)
    if previous is None or total == previous[1]:
        return None
    return 100.0 * (total - idle - previous[0]) / (total - previous[1])


def format_telemetry(telemetry, profile):
    """Bottom banner's middle text, e.g.
    "Jetson CPU 42°C 37% | Tx  2405.750 MHz  SR333  FEC 3/4  484 kb/s"."""
    tx_bitrate = telemetry.get("tx/dvbs2/ts/bitrate")
    tx_bitrate_str = "{:.0f} kb/s".format(int(tx_bitrate) / 1000.0) if tx_bitrate else "--"
    cpu_load = read_jetson_cpu_load_percent()
    cpu_load_str = "{:.0f}%".format(cpu_load) if cpu_load is not None else "--%"
    return "Jetson CPU {:.0f}°C {} | Tx  {:.3f} MHz  SR{}  FEC {}  {}".format(
        read_jetson_cpu_temp_c(), cpu_load_str, FREQUENCY_HZ / 1e6,
        profile["symbol_rate"] // 1000, profile["fec"], tx_bitrate_str)


def load_testcard_overlay_config(source_path, width, height):
    """When SOURCE == "testcard": look up this test card image's
    callsign/freq/volume/elapsed-time overlay placement, font and color
    from TESTCARD_OVERLAY_CONFIG_PATH (testcard_overlays.yaml) - see that
    file's own comments for the exact format.

    Keyed by the image's filename without extension (so renaming/adding a
    file under testcards/ is enough to give it its own tuned config - see
    select_testcard_file()) and "<width>x<height>" for the active
    profile's resolution. Anything not found for that specific
    image+resolution falls back field-by-field to the file's "default"
    section, so most test cards only need a handful of overridden fields,
    not a full copy of all four overlays' settings.
    """
    with open(TESTCARD_OVERLAY_CONFIG_PATH) as f:
        all_config = yaml.safe_load(f)

    image_name = os.path.splitext(os.path.basename(source_path))[0]
    resolution_key = "{}x{}".format(width, height)
    overrides = all_config.get(image_name, {}).get(resolution_key, {})

    merged = {}
    for overlay_name, default_fields in all_config["default"].items():
        merged[overlay_name] = dict(default_fields)
        merged[overlay_name].update(overrides.get(overlay_name, {}))
    return merged


def format_freq_banner(freq, is_rest):
    """When SOURCE == "testcard": blank during a silent rest (see
    TESTCARD_TONE_SCHEDULE), otherwise the frequency currently playing.
    """
    return "" if is_rest else "{} Hz".format(freq)


def format_volume_banner(volume, is_rest):
    """When SOURCE == "testcard": blank during a silent rest (see
    TESTCARD_TONE_SCHEDULE), otherwise the volume currently playing.
    volume is audiotestsrc's own linear 0.0-1.0 scale, shown as a plain
    percentage of that max (not a perceptual/dB loudness scale).
    """
    return "" if is_rest else "Vol {:.0f}%".format(volume * 100)


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


def select_testcard_file():
    """When SOURCE == "testcard", list the static test-card images available
    in testcards/ next to this script and let the user pick one by number.
    Unlike select_video_file(), this isn't split into per-resolution
    folders - the image gets scaled to the active profile's resolution by
    the pipeline itself, same as any other still frame.
    """
    testcard_dir = os.path.join(SCRIPT_DIR, "testcards")
    print("Looking for test cards in {}...".format(testcard_dir))

    if not os.path.isdir(testcard_dir):
        raise SystemExit("No testcards folder found: {}".format(testcard_dir))

    files = sorted(name for name in os.listdir(testcard_dir)
                    if name.lower().endswith((".png", ".jpg", ".jpeg")))
    if not files:
        raise SystemExit("No test card images found in {}".format(testcard_dir))

    print("Available test cards:")
    for i, name in enumerate(files, start=1):
        print("  {}) {}".format(i, name))

    while True:
        choice = input("Select a test card [1-{}]: ".format(len(files))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            selected = files[int(choice) - 1]
            break
        print("Invalid choice '{}', try again.".format(choice))

    testcard_path = os.path.join(testcard_dir, selected)
    print("Selected: {}".format(testcard_path))
    return testcard_path


def ask_yes_no(prompt, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        choice = input("{} {}: ".format(prompt, hint)).strip().lower()
        if choice == "":
            return default
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Invalid choice '{}', try again.".format(choice))


def ask_banner_and_marquee_settings():
    """When SOURCE in ("camera", "video"): let the user independently choose
    the top title banner, the bottom callsign/clock/telemetry banner, and
    the scrolling marquee, instead of one combined on/off switch (e.g. for
    a clean recording, or to burn in only the bottom telemetry without the
    marquee). Not asked for "testcard" - it never uses any of this, see
    TESTCARD_OVERLAY_CONFIG_PATH and main().
    """
    top_bar_enabled = ask_yes_no("Enable top title banner?")
    bottom_bar_enabled = ask_yes_no("Enable bottom callsign/clock/telemetry banner?")
    marquee_enabled = ask_yes_no("Enable scrolling marquee?")
    return top_bar_enabled, bottom_bar_enabled, marquee_enabled


def select_camera_device():
    """When SOURCE == "camera", pick which /dev/videoN to capture from - the
    Jetson can have more than one camera attached at once (e.g. an onboard
    CSI camera enumerating as video0, plus a USB webcam landing on video1),
    and which index is which physical camera depends on boot/plug order,
    not something safe to hardcode as CAMERA_DEVICE above. Skips the prompt
    entirely when only one camera is present - nothing to choose between.

    Returns (device_path, is_csi). is_csi matters downstream in
    build_pipeline_description(): a CSI sensor (e.g. this board's imx219)
    can't produce JPEG through the generic v4l2src path a USB webcam uses -
    confirmed on real hardware (2026-09-07): forcing v4l2src+JPEG caps onto
    the imx219 failed immediately with "streaming stopped, reason
    not-negotiated". It needs Jetson's own nvarguscamerasrc/Argus stack
    instead. Detected via the "vi-output" name prefix Tegra's Video Input
    bridge driver uses specifically for CSI sensors - not a generic V4L2
    convention, but reliable on this hardware.
    """
    devices = []
    for entry in sorted(os.listdir("/dev")):
        if not (entry.startswith("video") and entry[len("video"):].isdigit()):
            continue
        path = os.path.join("/dev", entry)
        try:
            with open("/sys/class/video4linux/{}/name".format(entry)) as f:
                name = f.read().strip()
        except OSError:
            name = "(unknown)"
        devices.append((path, name))

    if not devices:
        raise SystemExit("No /dev/video* camera device found - is a camera connected?")

    if len(devices) == 1:
        path, name = devices[0]
        print("Using the only camera found: {} ({})".format(path, name))
    else:
        print("Multiple cameras found:")
        for i, (path, name) in enumerate(devices, start=1):
            print("  {}) {} - {}".format(i, path, name))

        while True:
            choice = input("Select a camera [1-{}]: ".format(len(devices))).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(devices):
                path, name = devices[int(choice) - 1]
                break
            print("Invalid choice '{}', try again.".format(choice))

        print("Selected: {} ({})".format(path, name))

    is_csi = name.startswith("vi-output")
    if is_csi:
        print("   (CSI camera - will capture via nvarguscamerasrc, not v4l2src)")
    return path, is_csi


def select_audio_device():
    """When SOURCE == "camera", pick which ALSA capture device to record
    from - same reasoning as select_camera_device() above: the Jetson can
    have more than one microphone attached at once (e.g. a webcam's
    built-in mic plus a separate USB mic like a Blue Yeti Nano), and which
    card/device index maps to which physical mic depends on plug order,
    not something safe to hardcode as AUDIO_DEVICE above. Skips the
    prompt entirely when only one capture device is present - nothing to
    choose between. Parses `arecord -l` (ALSA's own device-listing tool)
    rather than /proc/asound directly - it already resolves card/device
    indices to human-readable names.
    """
    result = subprocess.run(
        ["arecord", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True)
    devices = []
    for line in result.stdout.splitlines():
        match = re.match(
            r"card (\d+): \S+ \[(.*?)\], device (\d+): (.*?) \[(.*?)\]", line)
        if match:
            card, card_name, device, device_name, _ = match.groups()
            devices.append((
                "plughw:{},{}".format(card, device),
                "{} - {}".format(card_name, device_name)))

    if not devices:
        raise SystemExit(
            "No ALSA capture device found (arecord -l) - is a microphone connected?")

    if len(devices) == 1:
        alsa_id, name = devices[0]
        print("Using the only microphone found: {} ({})".format(alsa_id, name))
    else:
        print("Multiple microphones found:")
        for i, (alsa_id, name) in enumerate(devices, start=1):
            print("  {}) {} - {}".format(i, alsa_id, name))

        while True:
            choice = input("Select a microphone [1-{}]: ".format(len(devices))).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(devices):
                alsa_id, name = devices[int(choice) - 1]
                break
            print("Invalid choice '{}', try again.".format(choice))

        print("Selected: {} ({})".format(alsa_id, name))

    return alsa_id


def probe_video_codec(path):
    """Codec name of the file's first video stream ("h264", "ffv1", ...) via
    the system ffprobe, or None if it can't be determined - the caller then
    falls back to the plain software-decoding chain."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def marquee_strip_geometry(width, height):
    """(top, height) in frame pixels of the strip the marquee is drawn on -
    exactly the band draw_marquee() fills (font ascent + descent + the
    style's top/bottom padding), so the result looks the same as before.

    Why a strip (2026-09-25): drawing the marquee on the full frame needed a
    full-frame conversion to BGRA and back for every frame, in one thread -
    at 1280x720 that capped the whole pipeline at 16 fps (measured: 30 s of
    video took 46 s with the marquee, 29 s with banners only), which
    stuttered on air. The strip is blended in by the compositor like the
    top/bottom bars, so only its few thousand pixels get converted."""
    style = MARQUEE_STYLES[(width, height)]
    cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    cr.select_font_face(MARQUEE_FONT_FAMILY, 0, 0)
    cr.set_font_size(style["font_size"])
    font_extents = cr.font_extents()
    if hasattr(font_extents, "ascent"):
        ascent, descent = font_extents.ascent, font_extents.descent
    else:
        ascent, descent = font_extents[0], font_extents[1]
    top = style["y_px"] - style["bg_padding_top_px"]
    strip_height = int(math.ceil(
        ascent + descent + style["bg_padding_top_px"] + style["bg_padding_bottom_px"]))
    strip_height += strip_height % 2  # even height for the 4:2:0 conversion
    return top, strip_height


def new_marquee_state(width, height):
    """Fresh per-pipeline state for draw_marquee() - call after
    build_pipeline_description(), which sets the marquee globals it uses."""
    top, _ = marquee_strip_geometry(width, height)
    return {"first_timestamp": None, "text_width": None, "y_offset": top}


# Set by main() when SOURCE == "clock": funnyClock/clock_tx.py imported as a
# module - see load_funny_clock().
FUNNY_CLOCK = None


def load_funny_clock():
    """For SOURCE == "clock": funnyClock's live SBB station clock, reusing
    funnyClock/clock_tx.py's own drawing and tone code (dial, hands, "... Hz"
    labels, TONE_SCHEDULE, frame_wall_time()) instead of a copy, so the
    standalone script and this source can't drift apart. Only the transmit
    side is this module's own (Pluto, relay, PTT, web log/stop, PA
    interlock). The background is drawn once with this run's callsign."""
    clock_dir = os.path.join(SCRIPT_DIR, "funnyClock")
    if clock_dir not in sys.path:
        sys.path.insert(0, clock_dir)
    import clock_tx
    clock_tx.CALLSIGN = CALLSIGN
    clock_tx.render_background(clock_tx.BACKGROUND_PNG)
    clock_tx.tone_labels = clock_tx.render_tone_labels()
    return clock_tx


def track_video_file_position(pipeline):
    """For SOURCE == "video": returns a dict whose "pts" is kept updated with
    the timestamp of the last video frame that has actually left the
    decoder (video_file_queue's src pad), or None if there's no such queue.

    Used instead of query_position() on the uridecodebin for the "video
    finished" check: that reports how far the file has been *read*, and
    with small hardware-decoded H.264 files the reader buffers far ahead -
    a 30 s test clip reported "almost at the end" after 2 s (2026-09-25),
    which would cut a transmission short by that much. Big FFV1 files hid
    this, because their read-ahead was only a fraction of a second."""
    queue = pipeline.get_by_name("video_file_queue")
    if queue is None:
        return None
    state = {"pts": None}

    def on_buffer(pad, info):
        buffer = info.get_buffer()
        if buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
            state["pts"] = buffer.pts
        return Gst.PadProbeReturn.OK

    queue.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    return state


def camera_capture_size(width, height):
    """Camera capture mode for a given output resolution - always 1280x720
    (the C920's MJPEG / imx219's NV12 mode at 30 fps), scaled down from
    there. 1280x720 is the highest profile resolution: a 1920x1080 capture
    for 1600x900 was tried (2026-09-24) and the Nano's CPU couldn't keep up.
    Shared with tuning/record_benchmark_clip.py."""
    return 1280, 720


def build_pipeline_description(ip, profile, source_path=None,
                                top_bar_enabled=True, bottom_bar_enabled=True):
    # Compositor/bars branch is needed if either bar OR the marquee wants to
    # show - the marquee's cairooverlay is chained onto this same branch's
    # output (see below), so without this it silently never appears when
    # both banners are off, regardless of MARQUEE_ENABLED (a real bug hit
    # 2026-09-10). A bar that's off still goes through the branch (alpha=0,
    # its textoverlay entries just skipped), see the comment above
    # OVERLAY_STYLES for why that's pad-renumbering-safe. Testcard is
    # excluded even though MARQUEE_ENABLED may still be True there (main()
    # never touches it for testcard) - testcard has its own separate
    # overlay system and must never enter this branch, see its SOURCE
    # branch below.
    overlay_enabled = (top_bar_enabled or bottom_bar_enabled
                        or (SOURCE in ("camera", "video") and MARQUEE_ENABLED))
    width, height = profile["resolution"]
    if SOURCE == "video":
        # Overwrite the same globals load_camera_banner_marquee_config()
        # populated at import time - safe because SOURCE is fixed for the
        # whole process lifetime (a hand-edited setting, never changes
        # mid-run - see datv_tx_plus.py's "Settings" section), so nothing
        # downstream (draw_marquee() included) needs to know which file the
        # values actually came from.
        global TITLE_TEXT, OVERLAY_STYLES, MARQUEE_TEXT, MARQUEE_FONT_FAMILY
        global MARQUEE_COLOR_RGBA, MARQUEE_BG_RGBA, MARQUEE_STYLES
        video_banner_marquee = load_video_banner_marquee_config(source_path)
        TITLE_TEXT = video_banner_marquee["title_text"]
        OVERLAY_STYLES = video_banner_marquee["overlay_styles"]
        MARQUEE_TEXT = video_banner_marquee["marquee_text"]
        MARQUEE_FONT_FAMILY = video_banner_marquee["marquee_font_family"]
        MARQUEE_COLOR_RGBA = video_banner_marquee["marquee_color_rgba"]
        MARQUEE_BG_RGBA = video_banner_marquee["marquee_bg_rgba"]
        MARQUEE_STYLES = video_banner_marquee["marquee_styles"]
    if MARQUEE_TEXT_OVERRIDE:
        # Applies after the video branch above (which would otherwise be
        # the last writer) and covers camera too, even though camera never
        # enters that branch: a `global MARQUEE_TEXT` statement anywhere in
        # a function makes the name global for the whole function body,
        # regardless of which branch actually runs, so this assignment
        # writes the module-level MARQUEE_TEXT either way. draw_marquee()
        # only ever reads that global at render time, so overwriting it
        # here, once, before Gst.parse_launch() is enough for either SOURCE.
        MARQUEE_TEXT = MARQUEE_TEXT_OVERRIDE
    if TITLE_TEXT_OVERRIDE:
        # Same reasoning as MARQUEE_TEXT_OVERRIDE just above - the existing
        # `global TITLE_TEXT` statement in the SOURCE == "video" branch
        # above already makes TITLE_TEXT global for this whole function
        # regardless of which branch actually ran, so this covers camera
        # too.
        TITLE_TEXT = TITLE_TEXT_OVERRIDE
    if (width, height) not in OVERLAY_STYLES:
        raise SystemExit(
            "No OVERLAY_STYLES entry for {}x{} - add one (see the comment "
            "above OVERLAY_STYLES).".format(width, height))
    overlay_style = OVERLAY_STYLES[(width, height)]
    if SOURCE in ("camera", "video") and MARQUEE_ENABLED and (width, height) not in MARQUEE_STYLES:
        raise SystemExit(
            "No MARQUEE_STYLES entry for {}x{} - add one (see the comment "
            "above MARQUEE_STYLES).".format(width, height))
    # valignment=bottom anchors to Pango's logical text box, which reserves
    # descender space these strings never use (no g/j/p/q/y - all caps and
    # digits), leaving dead space under the glyphs. Anchoring from the top
    # instead avoids that, so compute the exact pixel offset here. That
    # descender reservation scales with font size - the original "-6" fudge
    # was measured by eye at the baseline 960x540/11pt combination, so scale
    # it proportionally for other font sizes rather than guessing a fresh
    # constant per resolution.
    bottom_text_font_size = overlay_style["bottom_text_font_size"]
    descender_fudge = round(bottom_text_font_size * 6 / 11)
    bottom_bar_text_ypad = (height - overlay_style["bottom_bar_height"]
                             + overlay_style["bottom_bar_text_margin"] - descender_fudge)
    if TX_OUTPUT == "pluto":
        # Do not send this VBR mux directly to Pluto. Feed the local FFmpeg
        # relay instead; it adds null packets and forwards a correctly paced
        # CBR transport stream to Pluto's normal UDP port 8282.
        mux_sink = "udpsink host=127.0.0.1 port={} sync=true".format(CBR_RELAY_PORT)
    else:
        mux_sink = "filesink location={}".format(TX_OUTPUT_FILE)

    parts = ["mpegtsmux name=mux alignment=7 !"]
    if TX_OUTPUT == "pluto" and RECORD_ON_AIR_TS:
        os.makedirs(ON_AIR_TS_DIR, exist_ok=True)
        record_path = os.path.join(ON_AIR_TS_DIR, "{}_{}x{}_{}kbps.ts".format(
            time.strftime("%Y-%m-%d_%H%M%S"), width, height, profile["video_bitrate_kbps"]))
        log("🧪 Recording the relay's input to {}".format(record_path))
        # The file branch never blocks the on-air branch (own queue,
        # sync=false) - both get the identical muxed packets.
        parts += [
            "tee name=ts_tee",
            "ts_tee. ! queue !", mux_sink,
            "ts_tee. ! queue ! filesink location={} sync=false async=false".format(record_path),
        ]
    else:
        parts.append(mux_sink)

    if overlay_enabled:
        top_bar_visible = top_bar_enabled
        bottom_bar_visible = bottom_bar_enabled
        # Named junction point the source (+ the two bar videotestsrcs
        # below) link into. Source chain must link to comp. FIRST, before
        # the bar sources - compositor names request pads sink_0/1/2 in
        # link order, and the sink_0/1/2 properties below assume
        # sink_0=source, sink_1=top bar, sink_2=bottom bar. Pad layout
        # stays fixed regardless of visibility - a hidden bar is alpha=0,
        # not a removed pad, so nothing here needs renumbering.
        video_sink = "comp."
        parts += [
            "compositor name=comp",
            "sink_0::xpos=0 sink_0::ypos=0",
            "sink_1::xpos=0 sink_1::ypos=0 sink_1::alpha={}".format(
                overlay_style["top_bar_alpha"] if top_bar_visible else 0.0),
            "sink_2::xpos=0 sink_2::ypos={} sink_2::alpha={}".format(
                height - overlay_style["bottom_bar_height"],
                overlay_style["bottom_bar_alpha"] if bottom_bar_visible else 0.0),
        ]
        if SOURCE in ("camera", "video") and MARQUEE_ENABLED:
            # sink_3 = the marquee strip, linked last below the bars. One
            # alpha for the whole (opaque) strip, like the bars: the band's
            # own transparency. Per-pixel alpha would switch the compositor's
            # whole output to BGRA (measured 2026-09-25: I420 without the
            # strip, BGRA 1280x720 with it) - a full-frame conversion there
            # and back on every frame, the very cost the strip is meant to
            # avoid. Side effect: the text is as transparent as the band.
            parts.append("sink_3::xpos=0 sink_3::ypos={} sink_3::alpha={}".format(
                marquee_strip_geometry(width, height)[0],
                MARQUEE_BG_RGBA[3] if MARQUEE_BG_RGBA is not None else 1.0))
        parts += [
            # Pinned to the video's own format, so a new input can never
            # silently switch the compositor to a full-frame RGB format.
            "! video/x-raw,format=I420 !",
            "videoconvert !",
        ]
        if top_bar_visible:
            parts += [
                "textoverlay text=\"{}\" halignment=center".format(TITLE_TEXT),
                "valignment=top ypad=0 shaded-background=false font-desc=\"Sans {}\" !".format(
                    overlay_style["title_font_size"]),
            ]
        if bottom_bar_visible:
            parts += [
                "textoverlay text=\"{}\" halignment=left xpad=10".format(CALLSIGN),
                "valignment=top ypad={} shaded-background=false font-desc=\"Sans {}\" !".format(
                    bottom_bar_text_ypad, bottom_text_font_size),
                "clockoverlay time-format=\"%H:%M:%S UTC\" halignment=right xpad=10",
                "valignment=top ypad={} shaded-background=false font-desc=\"Sans {}\" !".format(
                    bottom_bar_text_ypad, bottom_text_font_size),
                "textoverlay name=telemetry_overlay text=\"\" halignment=center",
                "valignment=top ypad={} shaded-background=false font-desc=\"Sans {}\" !".format(
                    bottom_bar_text_ypad, bottom_text_font_size),
            ]
    else:
        # No compositor/bars/overlays at all - the source's video branch
        # links straight into the encode chain via this named junction.
        video_sink = "video_in."
        parts += ["videoconvert name=video_in !"]

    parts += [
        "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 !",
        "queue !",
        "nvv4l2h265enc bitrate={} insert-sps-pps=true iframeinterval={} idrinterval={}".format(
            profile["video_bitrate_kbps"] * 1000, ENCODER_KEYFRAME_INTERVAL,
            ENCODER_KEYFRAME_INTERVAL),
        "preset-level={} num-Ref-Frames={} maxperf-enable=true !".format(
            ENCODER_PRESET_LEVEL, ENCODER_REF_FRAMES),
        "h265parse config-interval=1 !",
        "queue ! mux.",
    ]

    if SOURCE == "camera":
        capture_width, capture_height = camera_capture_size(width, height)
        if CAMERA_IS_CSI:
            # nvarguscamerasrc doesn't take a /dev/videoN path (CAMERA_DEVICE is
            # unused here) - it addresses sensors by Argus sensor-id, and this
            # board has exactly one CSI port, so sensor-id=0 is always correct.
            # Its native output is NVMM-memory NV12, not JPEG - nvvidconv brings
            # it down to plain system-memory video/x-raw so the rest of this
            # chain (videorate/videoscale/videoconvert/compositor) is identical
            # to the USB path below, matching how the *encoder* end of this
            # pipeline already converts the other way (system memory -> NVMM)
            # a few lines down.
            parts += [
                "nvarguscamerasrc sensor-id=0 !",
                "video/x-raw(memory:NVMM),width={},height={},framerate=30/1,format=NV12 !".format(
                    capture_width, capture_height),
                "nvvidconv flip-method={} !".format(CSI_FLIP_METHOD),
                "videorate ! video/x-raw,framerate={}/1 !".format(FPS),
                "videoscale ! video/x-raw,width={},height={} !".format(width, height),
                "videoconvert ! {}".format(video_sink),
            ]
        else:
            parts += [
                "v4l2src device={} do-timestamp=true !".format(CAMERA_DEVICE),
                "image/jpeg,width={},height={},framerate=30/1 !".format(
                    capture_width, capture_height),
                "jpegdec !",
                "videorate ! video/x-raw,framerate={}/1 !".format(FPS),
                "videoscale ! video/x-raw,width={},height={} !".format(width, height),
                "videoconvert ! {}".format(video_sink),
            ]
        parts += [
            "alsasrc device={} !".format(AUDIO_DEVICE),
            "audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
        ]
        if MIC_AGC:
            # echo-cancel defaults to on in webrtcdsp, but that's for cancelling a
            # local speaker's playback picked back up by the mic - nothing here
            # plays audio locally, so it's disabled. gain-control (adaptive AGC)
            # and noise-suppression stay on their defaults.
            parts.append("webrtcdsp echo-cancel=false !")
        parts += [
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    elif SOURCE == "video":
        video_codec = probe_video_codec(source_path)
        if video_codec == "h264":
            # uridecodebin picks the hardware decoder by itself (nvv4l2decoder
            # ranks above avdec_h264), which outputs NVMM (GPU) memory -
            # nvvidconv brings it back to system memory and scales in
            # hardware in the same step. See preprocess_videos.py for why
            # video files are H.264: the old software-decoded FFV1 files
            # stuttered on air at 1280x720.
            log("🎞️  Video file: {} (H.264, hardware decoding)".format(
                os.path.basename(source_path)))
            video_chain = [
                "filesrc. ! queue name=video_file_queue ! nvvidconv !",
                "video/x-raw,width={},height={},format=I420 !".format(width, height),
                "videorate ! video/x-raw,framerate={}/1 !".format(FPS),
                "videoconvert ! {}".format(video_sink),
            ]
        else:
            # Older lossless FFV1 files (and anything else): software
            # decoding, exactly as before.
            log("🎞️  Video file: {} ({}, software decoding - may stutter at 1280x720; "
                "convert it again to get H.264)".format(
                    os.path.basename(source_path), video_codec or "unknown codec"))
            video_chain = [
                "filesrc. ! queue name=video_file_queue ! videoconvert ! videorate ! video/x-raw,framerate={}/1 !".format(FPS),
                "videoscale ! video/x-raw,width={},height={} !".format(width, height),
                "videoconvert ! {}".format(video_sink),
            ]
        parts += [
            # Pads are created dynamically once the file's streams are known,
            # but gst_parse_launch defers "filesrc." links until then - the
            # same idiom as `gst-launch-1.0 uridecodebin ... name=d d. ! ...`.
            "uridecodebin uri={} name=filesrc".format(Gst.filename_to_uri(source_path)),
        ]
        parts += video_chain
        parts += [
            "filesrc. ! queue ! audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    elif SOURCE == "clock":
        if (width, height) != (FUNNY_CLOCK.WIDTH, FUNNY_CLOCK.HEIGHT):
            raise SystemExit("The SBB clock is drawn for {}x{}, profile is {}x{}".format(
                FUNNY_CLOCK.WIDTH, FUNNY_CLOCK.HEIGHT, width, height))
        parts += [
            # Same chain as funnyClock's own pipeline_description(): static
            # background, hands + tone label drawn per frame by
            # FUNNY_CLOCK.draw_hands (connected in main()).
            "filesrc location={} ! pngdec ! imagefreeze !".format(FUNNY_CLOCK.BACKGROUND_PNG),
            "videoconvert ! videorate ! video/x-raw,framerate={}/1 !".format(FPS),
            "videoconvert ! video/x-raw,format=BGRA !",
            "cairooverlay name=clock_hands !",
            "videoconvert ! {}".format(video_sink),

            # Live tone, switched in place by main() (freq/volume only).
            "audiotestsrc name=clock_tone wave=sine freq=440 volume=0 is-live=true !",
            "audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    elif SOURCE == "testcard":
        overlay_config = load_testcard_overlay_config(source_path, width, height)
        callsign_cfg = overlay_config["callsign"]
        freq_cfg = overlay_config["freq"]
        volume_cfg = overlay_config["volume"]
        elapsed_cfg = overlay_config["elapsed"]
        parts += [
            # Same dynamic-pad idiom as the "video" branch above, but a still
            # image has no audio stream to pull from - imagefreeze turns the
            # single decoded frame into a continuous live stream.
            "uridecodebin uri={} name=filesrc".format(Gst.filename_to_uri(source_path)),

            "filesrc. ! queue ! imagefreeze ! videoconvert ! videorate ! video/x-raw,framerate={}/1 !".format(FPS),
            "videoscale ! video/x-raw,width={},height={} !".format(width, height),
            "videoconvert !",
            # Independent of the title/bars/clock/telemetry overlay system -
            # testcard mode never uses that (top_bar_enabled/
            # bottom_bar_enabled are forced False in main()).
            # x-absolute/y-absolute are 0-1-of-frame fractions,
            # hence the /width and /height here. Position/font/color come from
            # TESTCARD_OVERLAY_CONFIG_PATH (testcard_overlays.yaml), not
            # fixed constants - see load_testcard_overlay_config().
            "textoverlay name=testcard_callsign text=\"{}\" halignment=absolute".format(CALLSIGN),
            "x-absolute={} valignment=absolute y-absolute={}".format(
                callsign_cfg["x"] / width, callsign_cfg["y"] / height),
            "shaded-background=false color={} draw-shadow=false draw-outline=false font-desc=\"{} {}\" !".format(
                callsign_cfg["color"], callsign_cfg["font"], callsign_cfg["size"]),
            # Text is empty here - set to the starting frequency/volume and
            # then updated on every rotation in main().
            "textoverlay name=testcard_freq_banner text=\"\" halignment=absolute",
            "x-absolute={} valignment=absolute y-absolute={}".format(
                freq_cfg["x"] / width, freq_cfg["y"] / height),
            "shaded-background=false color={} draw-shadow=false draw-outline=false font-desc=\"{} {}\" !".format(
                freq_cfg["color"], freq_cfg["font"], freq_cfg["size"]),
            "textoverlay name=testcard_volume_banner text=\"\" halignment=absolute",
            "x-absolute={} valignment=absolute y-absolute={}".format(
                volume_cfg["x"] / width, volume_cfg["y"] / height),
            "shaded-background=false color={} draw-shadow=false draw-outline=false font-desc=\"{} {}\" !".format(
                volume_cfg["color"], volume_cfg["font"], volume_cfg["size"]),
            # Text set/updated in main() from time.monotonic() - START_TIME -
            # see TESTCARD_TIME_OVERLAY_TICK_SECONDS above for why this
            # isn't the built-in timeoverlay element.
            "textoverlay name=testcard_elapsed_ms text=\"\" halignment=absolute",
            "x-absolute={} valignment=absolute y-absolute={}".format(
                elapsed_cfg["x"] / width, elapsed_cfg["y"] / height),
            "shaded-background=false color={} draw-shadow=false draw-outline=false font-desc=\"{} {}\" !".format(
                elapsed_cfg["color"], elapsed_cfg["font"], elapsed_cfg["size"]),
        ]
        parts += [
            # testcard mode never shows the scrolling marquee - it's a
            # camera/video-only feature (see MARQUEE_ENABLED above).
            "{}".format(video_sink),

            # name=tone_source so main() can step it through the melody live
            # via set_property() - see TESTCARD_TONE_SCHEDULE above.
            "audiotestsrc name=tone_source wave=sine freq={} volume={} is-live=true !".format(
                TESTCARD_TONE_SCHEDULE[0][0], TESTCARD_MELODY_VOLUMES[0]),
            "audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "voaacenc bitrate={} !".format(profile["audio_bitrate_kbps"] * 1000),
            "aacparse !",
            "queue ! mux.",
        ]
    else:
        raise ValueError("Unknown SOURCE {!r}".format(SOURCE))

    if overlay_enabled:
        parts += [
            "videotestsrc pattern=black is-live=true !",
            "video/x-raw,width={},height={},framerate={}/1 !".format(
                width, overlay_style["top_bar_height"], FPS),
            "videoconvert ! comp.",

            "videotestsrc pattern=black is-live=true !",
            "video/x-raw,width={},height={},framerate={}/1 !".format(
                width, overlay_style["bottom_bar_height"], FPS),
            "videoconvert ! comp.",
        ]
        if SOURCE in ("camera", "video") and MARQUEE_ENABLED:
            # Linked last, so it becomes sink_3 and sits on top of the
            # picture and the bars (compositor z-order = pad order). Drawn
            # in BGRx (opaque), converted to I420 here - only this strip's
            # pixels - and blended with the pad alpha set above.
            marquee_top, marquee_height = marquee_strip_geometry(width, height)
            parts += [
                "videotestsrc pattern=black is-live=true !",
                "video/x-raw,format=BGRx,width={},height={},framerate={}/1 !".format(
                    width, marquee_height, FPS),
                "cairooverlay name=marquee_overlay !",
                "videoconvert ! video/x-raw,format=I420 ! comp.",
            ]

    return " ".join(parts)


def print_banner():
    line = "=" * 62
    print(line)
    print("   🛰️   {}  —  DATV-Plus QO-100 DVB-S2 Transmitter".format(CALLSIGN))
    print("   📡  {:.3f} MHz".format(FREQUENCY_HZ / 1e6))
    print(line)


def main():
    print_banner()
    profile = PROFILES[PROFILE]
    width, height = profile["resolution"]
    log("🎛️  Profile '{}': SR={} FEC={} {}x{} video={}kbps audio={}kbps".format(
        PROFILE, profile["symbol_rate"], profile["fec"], width, height,
        profile["video_bitrate_kbps"], profile["audio_bitrate_kbps"]))

    if SOURCE == "camera":
        global CAMERA_DEVICE, CAMERA_IS_CSI, AUDIO_DEVICE
        CAMERA_DEVICE, CAMERA_IS_CSI = select_camera_device()
        AUDIO_DEVICE = select_audio_device()
    if SOURCE == "video":
        source_path = select_video_file(profile)
    elif SOURCE == "testcard":
        source_path = select_testcard_file()
    else:
        source_path = None
    if SOURCE == "clock":
        global FUNNY_CLOCK
        log("🕐 Drawing the SBB clock background...")
        FUNNY_CLOCK = load_funny_clock()
    global MARQUEE_ENABLED
    if SOURCE in ("camera", "video"):
        top_bar_enabled, bottom_bar_enabled, MARQUEE_ENABLED = ask_banner_and_marquee_settings()
    else:
        # testcard never uses any of this - see TESTCARD_OVERLAY_CONFIG_PATH
        # above (top/bottom banner) and build_pipeline_description() (never
        # adds a marquee for testcard regardless of MARQUEE_ENABLED).
        top_bar_enabled = False
        bottom_bar_enabled = False

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

            log("🔇 Muting RF before configuring (safety)...")
            set_ptt(mqtt_client, CALLSIGN, on=False)

            log("⏳ Waiting for Pluto MQTT control on {} ({})...".format(
                pluto_ip, CALLSIGN))
            ts_bitrate = configure_pluto_until_ready(
                mqtt_client, pluto_ip, CALLSIGN, profile, telemetry)
            cbr_relay = start_cbr_relay(pluto_ip, ts_bitrate)

        pipeline_description = build_pipeline_description(
            pluto_ip, profile, source_path, top_bar_enabled, bottom_bar_enabled)
        log("🎬 Starting video stream...")
        gst_pipeline = Gst.parse_launch(pipeline_description)
        # Only exists when bottom_bar_enabled (see build_pipeline_description).
        telemetry_overlay = gst_pipeline.get_by_name("telemetry_overlay")
        # Only exists when (top_bar_enabled or bottom_bar_enabled) and MARQUEE_ENABLED.
        marquee_overlay = gst_pipeline.get_by_name("marquee_overlay")
        if marquee_overlay is not None:
            marquee_overlay.connect(
                "draw", draw_marquee, width, height, new_marquee_state(width, height))
        # Only exists when SOURCE == "video" or "testcard" (see
        # build_pipeline_description). For "testcard" the duration query
        # below never reports a positive duration (a still image/imagefreeze
        # has none), so the EOS watchdog naturally never fires and the card
        # loops until Ctrl+C - no separate code path needed.
        video_source = gst_pipeline.get_by_name("filesrc")
        video_position = track_video_file_position(gst_pipeline)
        # Only exist when SOURCE == "testcard" (see build_pipeline_description).
        tone_source = gst_pipeline.get_by_name("tone_source")
        freq_banner_overlay = gst_pipeline.get_by_name("testcard_freq_banner")
        volume_banner_overlay = gst_pipeline.get_by_name("testcard_volume_banner")
        if freq_banner_overlay is not None or volume_banner_overlay is not None:
            start_freq, _, start_is_rest = TESTCARD_TONE_SCHEDULE[0]
            if freq_banner_overlay is not None:
                freq_banner_overlay.set_property("text", format_freq_banner(start_freq, start_is_rest))
            if volume_banner_overlay is not None:
                volume_banner_overlay.set_property(
                    "text", format_volume_banner(TESTCARD_MELODY_VOLUMES[0], start_is_rest))
        # Only exists when SOURCE == "testcard" (see build_pipeline_description).
        elapsed_overlay = gst_pipeline.get_by_name("testcard_elapsed_ms")
        # Only exist when SOURCE == "clock" (see build_pipeline_description).
        clock_hands = gst_pipeline.get_by_name("clock_hands")
        clock_tone = gst_pipeline.get_by_name("clock_tone")
        if clock_hands is not None:
            FUNNY_CLOCK.frame_clock["pipeline"] = gst_pipeline
            clock_hands.connect("draw", FUNNY_CLOCK.draw_hands)
        clock_playing = None
        bus = gst_pipeline.get_bus()
        gst_pipeline.set_state(Gst.State.PLAYING)

        if to_pluto:
            log("🔊 Keying up...")
            set_ptt(mqtt_client, CALLSIGN, on=True)
            print()
            log("🚀 TRANSMITTING on {:.3f} MHz, SR={} FEC={}. Press Ctrl+C to stop.".format(
                FREQUENCY_HZ / 1e6, profile["symbol_rate"], profile["fec"]))
        else:
            print()
            log("💾 Writing to '{}'. Press Ctrl+C to stop.".format(TX_OUTPUT_FILE))
        # The testcard tone/time readout need a faster wake-up than plain
        # telemetry. The marquee is rendered from video timestamps in the
        # streaming thread and does not depend on this polling interval.
        poll_interval_seconds = TELEMETRY_UPDATE_SECONDS
        if tone_source is not None:
            poll_interval_seconds = min(poll_interval_seconds, TESTCARD_MELODY_TICK_SECONDS)
        if elapsed_overlay is not None:
            poll_interval_seconds = min(poll_interval_seconds, TESTCARD_TIME_OVERLAY_TICK_SECONDS)
        if clock_tone is not None:
            # Same 50 ms tick as funnyClock's own loop - tones start on time.
            poll_interval_seconds = min(poll_interval_seconds, 0.05)
        last_telemetry_update = 0.0
        last_elapsed_update = 0.0
        last_tone_change = time.monotonic()
        tone_index = 0
        melody_play_index = 0  # which TESTCARD_MELODY_VOLUMES entry the current play-through uses
        # FIFO of (freq, volume, apply_time) tuples between the banner text
        # switching to a new note and the actual audiotestsrc freq/volume
        # change being applied TESTCARD_AUDIO_DELAY_SECONDS later - see that
        # constant. A queue, not a single pending slot: if the delay is
        # close to or longer than a note's duration, more than one change
        # can be in flight at once, and a single slot would silently
        # drop/skip notes instead of just delaying them.
        pending_tone_changes = []
        while True:
            message = bus.timed_pop_filtered(
                int(poll_interval_seconds * Gst.SECOND),
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError("GStreamer error: {} ({})".format(error, debug))
                if message.type == Gst.MessageType.WARNING:
                    warning, debug = message.parse_warning()
                    log("⚠️  GStreamer warning from {}: {} ({})".format(
                        message.src.get_name(), warning, debug))
                    continue  # not fatal - keep running, just surface it
                log("🔚 EOS on the bus from {} - transmission ends here.".format(
                    message.src.get_name()))
                break
            if video_source is not None and video_position is not None:
                ok_dur, duration = video_source.query_duration(Gst.Format.TIME)
                position = video_position["pts"]
                if (ok_dur and position is not None and duration > 0
                        and position >= duration - VIDEO_END_MARGIN_SECONDS * Gst.SECOND):
                    log("🏁 Video finished; transmission ends here.")
                    break
            now = time.monotonic()
            if (telemetry_overlay is not None
                    and now - last_telemetry_update >= TELEMETRY_UPDATE_SECONDS):
                telemetry_overlay.set_property("text", format_telemetry(telemetry, profile))
                last_telemetry_update = now
            if (elapsed_overlay is not None
                    and now - last_elapsed_update >= TESTCARD_TIME_OVERLAY_TICK_SECONDS):
                # Same "[Xms]" format and time.monotonic() - START_TIME
                # reference as log() - see TESTCARD_TIME_OVERLAY_* above.
                elapsed_overlay.set_property(
                    "text", "[{:.0f}ms]".format((now - START_TIME) * 1000))
                last_elapsed_update = now
            if (tone_source is not None
                    and now - last_tone_change >= TESTCARD_TONE_SCHEDULE[tone_index][1]):
                tone_index = (tone_index + 1) % len(TESTCARD_TONE_SCHEDULE)
                if tone_index == 0:
                    # Wrapped past the rest back to the first note - the
                    # tune is repeating, so move on to the next volume
                    # level (only changes once per full play-through, not
                    # note-to-note - see TESTCARD_MELODY_VOLUMES above).
                    melody_play_index = (melody_play_index + 1) % len(TESTCARD_MELODY_VOLUMES)
                new_freq, _, new_is_rest = TESTCARD_TONE_SCHEDULE[tone_index]
                new_volume = 0.0 if new_is_rest else TESTCARD_MELODY_VOLUMES[melody_play_index]
                # Banners switch right on schedule; the actual tone follows
                # TESTCARD_AUDIO_DELAY_SECONDS later, to compensate for the
                # video path's extra encode/mux latency vs. audio.
                if freq_banner_overlay is not None:
                    freq_banner_overlay.set_property(
                        "text", format_freq_banner(new_freq, new_is_rest))
                if volume_banner_overlay is not None:
                    volume_banner_overlay.set_property(
                        "text", format_volume_banner(new_volume, new_is_rest))
                pending_tone_changes.append(
                    (new_freq, new_volume, now + TESTCARD_AUDIO_DELAY_SECONDS))
                last_tone_change = now
            if clock_tone is not None:
                # Same switching as funnyClock's own main loop.
                freq = FUNNY_CLOCK.tone_now()
                if freq != clock_playing:
                    if freq:
                        clock_tone.set_property("freq", freq)
                    clock_tone.set_property("volume", FUNNY_CLOCK.TONE_VOLUME if freq else 0.0)
                    clock_playing = freq
            while pending_tone_changes and now >= pending_tone_changes[0][2]:
                freq_to_apply, volume_to_apply, _ = pending_tone_changes.pop(0)
                tone_source.set_property("freq", freq_to_apply)
                tone_source.set_property("volume", volume_to_apply)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        log("🛑 Stopping...")
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
                set_ptt(mqtt_client, CALLSIGN, on=False)
                log("✅ Stopped. PTT OFF.")
            except Exception as exc:
                log("⚠️  WARNING: could not confirm PTT OFF ({}) - "
                    "check the Pluto directly!".format(exc))
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        else:
            log("✅ Stopped. Wrote '{}'.".format(TX_OUTPUT_FILE))


if __name__ == "__main__":
    main()
