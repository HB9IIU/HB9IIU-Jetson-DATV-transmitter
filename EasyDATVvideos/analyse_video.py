"""Analyse a recorded DATV video (e.g. an Easy DATV .TS captured from
MiniTioune) and print its encoder fingerprint: codec, resolution, frame
rate, bitrates, GOP length, frame types, reference frames and audio.

Run: python analyse_video.py  (analyses every video in this folder)
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXTENSIONS = {".ts", ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm"}


def find_tool(name):
    """ffmpeg/ffprobe: the project's static build first (the Jetson's system
    ffmpeg 3.4.8 is too old for the trace_headers check), then PATH, then
    the WinGet install."""
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = [os.path.join(SCRIPT_DIR, "..", "ffmpeg-static", exe),
                  shutil.which(name)]
    candidates += glob.glob(os.path.join(os.path.expanduser("~"), "AppData", "Local", "Microsoft",
                                         "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", exe))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise SystemExit("{} not found - install ffmpeg or put it on PATH".format(name))


FFPROBE = find_tool("ffprobe")
FFMPEG = find_tool("ffmpeg")


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, errors="replace")


def find_videos():
    videos = sorted(f for f in os.listdir(SCRIPT_DIR)
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS)
    if not videos:
        raise SystemExit("No video files found in " + SCRIPT_DIR)
    return [os.path.join(SCRIPT_DIR, f) for f in videos]


def packets(path, stream):
    """(pts_time, size, is_keyframe) for every packet of one stream."""
    out = run([FFPROBE, "-v", "error", "-select_streams", stream,
               "-show_entries", "packet=pts_time,size,flags", "-of", "csv=p=0", path]).stdout
    result = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or parts[0] in ("", "N/A"):
            continue
        result.append((float(parts[0]), int(parts[1]), "K" in parts[2]))
    return sorted(result)


def bitrate_kbps(pkts):
    if len(pkts) < 2:
        return None
    duration = pkts[-1][0] - pkts[0][0]
    return sum(p[1] for p in pkts) * 8 / duration / 1000 if duration > 0 else None


def frame_types(path):
    out = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "frame=pict_type", "-of", "csv=p=0", path]).stdout
    return Counter(line.strip(",") for line in out.splitlines() if line.strip(","))


def header_fields(path):
    """Parameter-set fields from the first keyframe via the trace_headers
    bitstream filter (reference frames, AMP, SAO, ...)."""
    err = run([FFMPEG, "-hide_banner", "-v", "trace", "-i", path, "-map", "0:v:0",
               "-c", "copy", "-bsf:v", "trace_headers", "-frames:v", "1", "-f", "null", "-"]).stderr
    fields = {}
    for m in re.finditer(r"\]\s+\d+\s+([a-z0-9_\[\]]+)\s+[01]+\s+=\s+(-?\d+)", err):
        fields.setdefault(m.group(1), int(m.group(2)))
    return fields


def encoder_string(path):
    """x264/x265 write their settings as plain text into the stream; hardware
    encoders don't."""
    with open(path, "rb") as f:
        data = f.read(8 * 1024 * 1024)
    m = re.search(rb"(x26[45] \(build|x264 - core)[ -~]{0,300}", data)
    return m.group(0).decode("ascii", "replace") if m else None


def main():
    for path in find_videos():
        analyse(path)


def analyse(path):
    print("\nAnalysing {} ...".format(os.path.basename(path)))

    info = json.loads(run([FFPROBE, "-v", "error", "-show_format", "-show_streams",
                           "-of", "json", path]).stdout or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        print("  No video stream found - skipped")
        return

    vpkts = packets(path, "v:0")
    keys = [p[0] for p in vpkts if p[2]]
    gaps = [b - a for a, b in zip(keys, keys[1:])]
    fps_num, fps_den = (video.get("r_frame_rate") or "0/1").split("/")
    fps = float(fps_num) / float(fps_den) if float(fps_den) else 0
    types = frame_types(path)
    hdr = header_fields(path)
    encoder = encoder_string(path)

    refs = hdr.get("sps_max_dec_pic_buffering_minus1[0]")  # HEVC
    if refs is None:
        refs = hdr.get("max_num_ref_frames")  # H.264

    def flag(name):
        return {0: "off", 1: "on"}.get(hdr.get(name), "?")

    duration = float(info.get("format", {}).get("duration") or 0)
    lines = [
        ("File", os.path.basename(path)),
        ("Duration", "{:.1f} s".format(duration)),
        ("Mux bitrate", "{:.0f} kbit/s".format(os.path.getsize(path) * 8 / duration / 1000) if duration else "?"),
        ("", ""),
        ("Video codec", "{} {}".format(video.get("codec_name", "?").upper(), video.get("profile", ""))),
        ("Resolution", "{}x{}".format(video.get("width"), video.get("height"))),
        ("Frame rate", "{:.2f} fps".format(fps)),
        ("Pixel format", video.get("pix_fmt", "?")),
        ("Video bitrate", "{:.0f} kbit/s".format(bitrate_kbps(vpkts) or 0)),
        ("Keyframes", "{} in file".format(len(keys))),
        ("GOP (keyframe gap)", "{:.2f} s = {:.0f} frames".format(sum(gaps) / len(gaps), sum(gaps) / len(gaps) * fps)
         if gaps else "only one keyframe - recording shorter than the GOP"),
        ("Frame types", ", ".join("{}={}".format(t, n) for t, n in sorted(types.items())) or "?"),
        ("B-frames", "yes" if types.get("B") else "no"),
        ("Reference frames", refs if refs is not None else "?"),
    ]
    if video.get("codec_name") == "hevc":
        lines += [("AMP", flag("amp_enabled_flag")),
                  ("SAO", flag("sample_adaptive_offset_enabled_flag")),
                  ("Temporal MVP", flag("sps_temporal_mvp_enabled_flag"))]
    lines.append(("Encoder string", encoder or "none (probably a hardware encoder)"))
    lines.append(("", ""))
    if audio:
        lines += [
            ("Audio codec", "{} {}".format(audio.get("codec_name", "?").upper(), audio.get("profile", ""))),
            ("Sample rate", "{} Hz".format(audio.get("sample_rate", "?"))),
            ("Channels", audio.get("channels", "?")),
            ("Audio bitrate", "{:.0f} kbit/s".format(bitrate_kbps(packets(path, "a:0")) or 0)),
        ]
    else:
        lines.append(("Audio", "none"))

    print("\n" + "=" * 50)
    for label, value in lines:
        print("  {:<20} {}".format(label, value) if label else "")
    print("=" * 50)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
