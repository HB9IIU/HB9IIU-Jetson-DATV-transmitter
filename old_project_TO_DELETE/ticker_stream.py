#!/usr/bin/python3
import json
import math
import signal
import sys
import ctypes
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst


Gst.init(None)
APP_DIR = Path(__file__).resolve().parent
TESTCARD = APP_DIR / "testcards" / "normalized" / "hb9iiu-test-card.png"
SOUNDTRACK_DIR = APP_DIR / "soundtracks" / "normalized"


class TextExtents(ctypes.Structure):
    _fields_ = [
        ("x_bearing", ctypes.c_double), ("y_bearing", ctypes.c_double),
        ("width", ctypes.c_double), ("height", ctypes.c_double),
        ("x_advance", ctypes.c_double), ("y_advance", ctypes.c_double),
    ]


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


def build_pipeline(settings):
    width = settings["width"]
    height = settings["height"]
    fps = settings["fps"]
    codec = settings["codec"]
    encoder = "nvv4l2h264enc" if codec == "h264" else "nvv4l2h265enc"
    parser = "h264parse" if codec == "h264" else "h265parse"
    parts = [
        "mpegtsmux name=mux alignment=7 !",
        "udpsink host={} port={} sync=true async=false buffer-size=1048576".format(
            settings["host"], settings["port"]),
        "filesrc location={} ! pngdec ! imagefreeze ! videoconvert !".format(quoted(TESTCARD)),
        "videoscale add-borders=true !",
        "video/x-raw,width={},height={},framerate={}/1,pixel-aspect-ratio=1/1 !".format(
            width, height, fps),
        "cairooverlay name=overlay ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 !",
        "{} bitrate={} insert-sps-pps=true iframeinterval={} !".format(
            encoder, settings["bitrate_kbps"] * 1000, fps),
        "{} config-interval=1 ! queue ! mux.".format(parser),
    ]
    if settings["audio_mode"] == "manual":
        soundtrack = SOUNDTRACK_DIR / settings["soundtrack"]
        parse_decode = "wavparse !" if soundtrack.suffix.lower() == ".wav" else "mpegaudioparse ! mpg123audiodec !"
        volume = math.pow(10.0, settings["volume_db"] / 20.0)
        parts.extend([
            "multifilesrc location={} loop=true ! {}".format(quoted(soundtrack), parse_decode),
            "audioconvert ! audioresample ! audiorate !",
            "audio/x-raw,format=S16LE,rate=48000,channels=1 !",
            "volume volume={:.6f} ! voaacenc bitrate={} ! aacparse ! queue ! mux.".format(
                volume, settings["audio_bitrate_kbps"] * 1000),
        ])
    return " ".join(parts)


class TickerStream:
    def __init__(self, settings):
        self.settings = settings
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.parse_launch(build_pipeline(settings))
        self.overlay = self.pipeline.get_by_name("overlay")
        self.overlay.connect("draw", self.draw)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

    def text_extents(self, context, text):
        ext = TextExtents()
        CAIRO.cairo_text_extents(context, text.encode("utf-8"), ctypes.byref(ext))
        return ext

    def draw_label(self, context, text, x, y, font_size, padding=7):
        CAIRO.cairo_select_font_face(context, b"Sans", 0, 1)
        CAIRO.cairo_set_font_size(context, font_size)
        ext = self.text_extents(context, text)
        text_width, text_height = ext.width, ext.height
        CAIRO.cairo_set_source_rgba(context, 0.02, 0.03, 0.04, 0.78)
        CAIRO.cairo_rectangle(context, x - padding, y - text_height - padding, text_width + padding * 2, text_height + padding * 2)
        CAIRO.cairo_fill(context)
        CAIRO.cairo_set_source_rgba(context, 1, 1, 1, 1)
        CAIRO.cairo_move_to(context, x, y)
        CAIRO.cairo_show_text(context, text.encode("utf-8"))
        return text_width

    def draw(self, overlay, cr, timestamp, duration):
        context = ctypes.c_void_p(hash(cr))
        width = self.settings["width"]
        height = self.settings["height"]
        font_size = max(18, int(height * 0.065))
        band_height = max(42, int(height * 0.13))
        top = self.settings["ticker_position"] == "top"
        band_y = 0 if top else height - band_height
        CAIRO.cairo_set_source_rgba(context, 0.01, 0.02, 0.03, 0.72)
        CAIRO.cairo_rectangle(context, 0, band_y, width, band_height)
        CAIRO.cairo_fill(context)

        message = self.settings["ticker_message"]
        CAIRO.cairo_select_font_face(context, b"Sans", 0, 1)
        CAIRO.cairo_set_font_size(context, font_size)
        text_width = self.text_extents(context, message).width
        seconds = 0.0 if timestamp == Gst.CLOCK_TIME_NONE else timestamp / float(Gst.SECOND)
        travel = width + text_width + 80
        x = width - ((seconds * self.settings["ticker_speed"]) % travel)
        baseline = band_y + (band_height + font_size * 0.72) / 2.0
        CAIRO.cairo_set_source_rgba(context, 1, 1, 1, 1)
        CAIRO.cairo_move_to(context, x, baseline)
        CAIRO.cairo_show_text(context, message.encode("utf-8"))

        clock = datetime.utcnow().strftime("%Y-%m-%d  %H:%M:%S UTC")
        clock_size = max(14, int(height * 0.042))
        CAIRO.cairo_select_font_face(context, b"Sans", 0, 1)
        CAIRO.cairo_set_font_size(context, clock_size)
        clock_width = self.text_extents(context, clock).width
        self.draw_label(context, clock, width - clock_width - 10, max(24, int(height * 0.06)), clock_size, 6)

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
        print("Ticker pipeline ready", flush=True)
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    TickerStream(json.loads(sys.argv[1])).run()
