#!/usr/bin/python3
import ctypes
import json
import math
import signal
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)
APP_DIR = Path(__file__).resolve().parent
TESTCARD = APP_DIR / "testcards" / "normalized" / "hb9iiu-orbital.png"
SOUNDTRACK_DIR = APP_DIR / "soundtracks" / "normalized"


class TextExtents(ctypes.Structure):
    _fields_ = [("x_bearing", ctypes.c_double), ("y_bearing", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double),
                ("x_advance", ctypes.c_double), ("y_advance", ctypes.c_double)]


CAIRO = ctypes.CDLL("libcairo.so.2")
CAIRO.cairo_set_source_rgba.argtypes = [ctypes.c_void_p] + [ctypes.c_double] * 4
CAIRO.cairo_rectangle.argtypes = [ctypes.c_void_p] + [ctypes.c_double] * 4
CAIRO.cairo_fill.argtypes = [ctypes.c_void_p]
CAIRO.cairo_select_font_face.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
CAIRO.cairo_set_font_size.argtypes = [ctypes.c_void_p, ctypes.c_double]
CAIRO.cairo_text_extents.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(TextExtents)]
CAIRO.cairo_move_to.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
CAIRO.cairo_show_text.argtypes = [ctypes.c_void_p, ctypes.c_char_p]


def quoted(value):
    return '"{}"'.format(str(value).replace("\\", "\\\\").replace('"', '\\"'))


def build_pipeline(s):
    encoder = "nvv4l2h264enc" if s["codec"] == "h264" else "nvv4l2h265enc"
    parser = "h264parse" if s["codec"] == "h264" else "h265parse"
    parts = [
        "mpegtsmux name=mux alignment=7 !",
        "udpsink host={} port={} sync=true async=false buffer-size=1048576".format(s["host"], s["port"]),
        "filesrc location={} ! pngdec ! imagefreeze ! videoconvert !".format(quoted(TESTCARD)),
        "videoscale add-borders=true ! video/x-raw,width={},height={},framerate={}/1,pixel-aspect-ratio=1/1 !".format(s["width"], s["height"], s["fps"]),
        "cairooverlay name=overlay ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 !",
        "{} bitrate={} insert-sps-pps=true iframeinterval={} !".format(encoder, s["bitrate_kbps"] * 1000, s["fps"]),
        "{} config-interval=1 ! queue ! mux.".format(parser),
    ]
    if s["audio_mode"] == "manual":
        soundtrack = SOUNDTRACK_DIR / s["soundtrack"]
        decode = "wavparse !" if soundtrack.suffix.lower() == ".wav" else "mpegaudioparse ! mpg123audiodec !"
        volume = math.pow(10.0, s["volume_db"] / 20.0)
        parts.extend([
            "multifilesrc location={} loop=true ! {}".format(quoted(soundtrack), decode),
            "audioconvert ! audioresample ! audiorate ! audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "volume volume={:.6f} ! voaacenc bitrate={} ! aacparse ! queue ! mux.".format(volume, s["audio_bitrate_kbps"] * 1000),
        ])
    return " ".join(parts)


class OrbitalStream:
    def __init__(self, settings):
        self.settings = settings
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.parse_launch(build_pipeline(settings))
        self.pipeline.get_by_name("overlay").connect("draw", self.draw)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

    def draw(self, overlay, cr, timestamp, duration):
        context = ctypes.c_void_p(hash(cr))
        width, height = self.settings["width"], self.settings["height"]
        scale = height / 720.0
        seconds = 0.0 if timestamp == Gst.CLOCK_TIME_NONE else timestamp / float(Gst.SECOND)

        # Full-width attribution ticker, matching the established live ticker style.
        banner = "GENERATED WITH CHATGPT  •  RUNNING ON NVIDIA JETSON NANO  •  HB9IIU  •  QO-100 DATV"
        banner_size = max(15, int(22 * scale))
        band_height = max(38, int(48 * scale))
        band_y = height - max(int(66 * scale), band_height)
        CAIRO.cairo_set_source_rgba(context, 0.01, 0.025, 0.04, 0.84)
        CAIRO.cairo_rectangle(context, 0, band_y, width, band_height)
        CAIRO.cairo_fill(context)
        CAIRO.cairo_select_font_face(context, b"Sans", 0, 1)
        CAIRO.cairo_set_font_size(context, banner_size)
        banner_ext = TextExtents()
        CAIRO.cairo_text_extents(context, banner.encode(), ctypes.byref(banner_ext))
        travel = width + banner_ext.width + int(100 * scale)
        # 50 px/s at 720p is exactly 2 pixels per 25-fps frame. Scaling the
        # speed with the picture keeps the same smooth apparent motion at 360p.
        banner_speed = 50.0 * width / 1280.0
        banner_x = width - ((seconds * banner_speed) % travel)
        banner_y = band_y + (band_height + banner_size * 0.72) / 2.0
        CAIRO.cairo_set_source_rgba(context, 1.0, 1.0, 1.0, 1.0)
        CAIRO.cairo_move_to(context, banner_x, banner_y)
        CAIRO.cairo_show_text(context, banner.encode())

        # Broadcast-style UTC module in the open right-hand area.
        now = datetime.utcnow()
        time_text = now.strftime("%H:%M:%S")
        date_text = now.strftime("%Y-%m-%d")
        panel_x = int(width * 0.785)
        panel_y = int(height * 0.345)
        panel_w = int(width * 0.17)
        panel_h = max(72, int(height * 0.14))
        CAIRO.cairo_set_source_rgba(context, 0.01, 0.03, 0.05, 0.86)
        CAIRO.cairo_rectangle(context, panel_x, panel_y, panel_w, panel_h)
        CAIRO.cairo_fill(context)
        CAIRO.cairo_set_source_rgba(context, 0.22, 0.82, 1.0, 0.95)
        CAIRO.cairo_rectangle(context, panel_x, panel_y, max(3, int(4 * scale)), panel_h)
        CAIRO.cairo_fill(context)

        label_size = max(11, int(13 * scale))
        CAIRO.cairo_select_font_face(context, b"Sans", 0, 1)
        CAIRO.cairo_set_source_rgba(context, 0.45, 0.9, 1.0, 1.0)
        CAIRO.cairo_set_font_size(context, label_size)
        CAIRO.cairo_move_to(context, panel_x + int(18 * scale), panel_y + int(23 * scale))
        CAIRO.cairo_show_text(context, b"UTC")

        time_size = max(22, int(32 * scale))
        CAIRO.cairo_set_source_rgba(context, 1.0, 1.0, 1.0, 1.0)
        CAIRO.cairo_set_font_size(context, time_size)
        CAIRO.cairo_move_to(context, panel_x + int(18 * scale), panel_y + int(59 * scale))
        CAIRO.cairo_show_text(context, time_text.encode())

        date_size = max(11, int(14 * scale))
        CAIRO.cairo_set_source_rgba(context, 0.65, 0.76, 0.82, 1.0)
        CAIRO.cairo_set_font_size(context, date_size)
        CAIRO.cairo_move_to(context, panel_x + int(19 * scale), panel_y + int(84 * scale))
        CAIRO.cairo_show_text(context, date_text.encode())

    def on_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print("ERROR: {}".format(error), file=sys.stderr, flush=True)
            if debug:
                print(debug, file=sys.stderr, flush=True)
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            self.loop.quit()

    def stop(self, *_args):
        self.pipeline.send_event(Gst.Event.new_eos())

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        print("Orbital live-UTC pipeline ready", flush=True)
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    OrbitalStream(json.loads(sys.argv[1])).run()
