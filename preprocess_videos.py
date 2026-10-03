"""Pre-process source videos from "original videos/" into copies resized
to the DATV profile resolution and frame rate, so datv_tx_plus.py
(SOURCE="video") never has to scale or drop frames on the fly.

Why H.264 (since 2026-09-25, was lossless FFV1): the Jetson has to decode
these files in real time while also scaling, drawing overlays and
encoding. FFV1 can only be decoded in software - a detailed 1280x720 file
took ~2.5 of the Nano's 4 CPU cores just to decode, and videos stuttered on
air. H.264 is decoded by the Jetson's hardware decoder (nvv4l2decoder) at
almost no CPU cost. At CRF 16 the intermediate is visually lossless, far
above what survives the final low-bitrate DVB-S2 H.265 encode. Audio is
copied untouched (no re-encode needed, resolution doesn't affect audio).

Naming convention / "only convert once" trick: each output keeps the
source's own filename (extension stripped) as a .mkv in the matching
preprocessed_WxH/ folder, e.g. "original videos/Bunny.avi" -> resolution
960x540 -> "preprocessed_960x540/Bunny.mkv". That exact path's existence
*is* the record of "already converted" - no separate manifest/database
needed, and re-running this script only converts whatever is still missing.

Run manually: python preprocess_videos.py
"""

import os
import shutil
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(SCRIPT_DIR, "original videos")

# DATV profile resolutions (see PROFILES in datv_tx_plus.py) -> their
# pre-processed output folder. Add an entry here if a new profile
# resolution shows up later - no other code needs to change.
#
# 1280x720 only since 2026-09-24: with the new encoder settings every
# profile (SR333/SR500, camera/video/testcard) runs at 720p - see
# dvbs2_profiles.py.
RESOLUTIONS = {
    (1280, 720): os.path.join(SCRIPT_DIR, "preprocessed_1280x720"),
}

VIDEO_EXTENSIONS = {".avi", ".mp4", ".m4v", ".mov", ".webm", ".mkv"}

# Same as datv_tx_plus.FPS (not imported - that module pulls in GStreamer).
# Converting to the on-air frame rate here means the pipeline's videorate
# never has to decode frames only to throw them away (a 30 fps source
# wastes 1 decode in 6 at 25 fps).
TARGET_FPS = 25
# Codec the "already converted?" check requires - older lossless FFV1
# outputs don't count, so re-running this script replaces them.
OUTPUT_VIDEO_CODEC = "h264"


def h264_output_args(width, height):
    """ffmpeg output options for one preprocessed video - shared with
    fileUploader/engine.py, video_conversion.py and
    testcard_movies_generator.py so every video source ends up in the same
    hardware-decodable format. High profile 8-bit 4:2:0 is what the Jetson
    Nano's nvv4l2decoder handles; veryfast keeps conversion time down on
    the Nano's CPU (at a fixed CRF a faster preset mostly costs file size,
    not quality); -g 50 = a keyframe every 2 s.

    Other aspect ratios (e.g. 4:3) are fitted inside width x height with
    black bars and square pixels (setsar=1). A plain scale=WxH kept the
    shape via a non-square SAR (3:4 for 4:3) instead, which the transmitter's
    compositor then rescaled in software every frame - stuttering video and
    audio and an empty right quarter on air (ON1AVO, 2026-10-03)."""
    return [
        "-vf", "scale={w}:{h}:force_original_aspect_ratio=decrease,"
               "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}".format(
                   w=width, h=height, fps=TARGET_FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", str(2 * TARGET_FPS),
    ]

# ffmpeg-static/ffmpeg is a self-contained static build (currently 7.0.2,
# with AV1/libdav1d decode support) placed next to this script - checked
# first because the Jetson's system ffmpeg (3.4.8, Ubuntu 18.04's package)
# can't decode AV1 sources at all ("Decoder (codec av1) not found"). Kept
# separate from /usr/bin/ffmpeg rather than replacing it, to avoid any risk
# of an apt-based upgrade destabilizing the NVIDIA multimedia stack the
# GStreamer hardware encoder pipeline depends on.
#
# ffmpeg isn't always on PATH on the Windows machine either (confirmed
# earlier - `where ffmpeg` found nothing), so fall back to the known WinGet
# install location there.
FFMPEG_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "ffmpeg-static", "ffmpeg"),
    shutil.which("ffmpeg"),
    r"C:\Users\danie\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe",
]
FFMPEG = next((p for p in FFMPEG_CANDIDATES if p and os.path.isfile(p)), None)

# ffprobe lives next to whichever ffmpeg got picked above (both the static
# build and a normal system install ship it alongside ffmpeg); fall back to
# PATH if that exact pairing isn't there.
if FFMPEG is not None:
    _ffprobe_name = "ffprobe.exe" if FFMPEG.lower().endswith(".exe") else "ffprobe"
    _ffprobe_candidate = os.path.join(os.path.dirname(FFMPEG), _ffprobe_name)
    FFPROBE = _ffprobe_candidate if os.path.isfile(_ffprobe_candidate) else shutil.which("ffprobe")
else:
    FFPROBE = None


def find_source_videos():
    """Return the list of video file paths directly inside SOURCE_DIR."""
    print("[1/3] Scanning '{}' for source videos...".format(SOURCE_DIR))
    if not os.path.isdir(SOURCE_DIR):
        raise SystemExit("Source folder not found: {}".format(SOURCE_DIR))

    videos = []
    for name in sorted(os.listdir(SOURCE_DIR)):
        path = os.path.join(SOURCE_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(path):
            continue
        if ext in VIDEO_EXTENSIONS:
            print("  found:   {}".format(name))
            videos.append(path)
        else:
            print("  ignored: {} (not a recognized video extension)".format(name))

    print("  -> {} source video(s) found".format(len(videos)))
    return videos


def output_path_for(source_path, output_dir):
    """Expected pre-processed path for a given source at a given resolution -
    see module docstring for the naming convention this relies on."""
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(output_dir, base_name + ".mkv")


def is_valid_output(path):
    """A previous run's output only counts as done if it's a real, playable
    file - ffmpeg can create/truncate the output file before failing partway
    through (e.g. no decoder for the source codec), which plain
    os.path.exists() can't tell apart from a genuinely completed
    conversion, silently leaving a broken file in place forever. It must
    also already be H.264 (OUTPUT_VIDEO_CODEC) - an older FFV1 output gets
    converted again."""
    if not os.path.exists(path):
        return False
    if FFPROBE is None:
        return os.path.getsize(path) > 0  # best effort without ffprobe
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name:format=duration",
         "-of", "default=noprint_wrappers=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    try:
        duration = float(fields.get("duration", ""))
    except ValueError:
        return False
    return duration > 0 and fields.get("codec_name") == OUTPUT_VIDEO_CODEC


def plan_conversions(source_videos):
    """Check every (source, resolution) pair and split into already-done vs
    still-needed, printing the reasoning for each one along the way."""
    print("\n[2/3] Checking which (source, resolution) pairs are missing...")
    todo = []
    for source_path in source_videos:
        source_name = os.path.basename(source_path)
        for (width, height), output_dir in RESOLUTIONS.items():
            out_path = output_path_for(source_path, output_dir)
            if is_valid_output(out_path):
                print("  [skip]    {} @ {}x{} -> already exists: {}".format(
                    source_name, width, height, out_path))
            else:
                print("  [pending] {} @ {}x{} -> will create: {}".format(
                    source_name, width, height, out_path))
                todo.append((source_path, width, height, out_path))

    print("  -> {} conversion(s) needed".format(len(todo)))
    return todo


def convert(source_path, width, height, out_path):
    """Resize + frame-rate pass to hardware-decodable H.264, audio copied
    untouched (see module docstring)."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = ([FFMPEG, "-y", "-i", source_path]
           + h264_output_args(width, height)
           + ["-c:a", "copy", out_path])
    print("  command: {}".format(" ".join(cmd)))

    # capture_output=/text= need Python 3.7+; the Jetson's venv is 3.6, so
    # use the equivalent stdout/stderr=PIPE + universal_newlines instead.
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        print("  FFMPEG FAILED (exit code {})".format(result.returncode))
        print("  ---- last part of ffmpeg stderr ----")
        print(result.stderr[-2000:])  # ffmpeg's actual error is at the end
        print("  -------------------------------------")
        return False

    print("  done: {} ({} bytes)".format(out_path, os.path.getsize(out_path)))
    return True


def main():
    print("=== DATV source video pre-processing ===")
    print("ffmpeg: {}".format(FFMPEG))
    if FFMPEG is None:
        raise SystemExit(
            "ffmpeg not found (checked PATH and the known WinGet install "
            "location). Install it or fix FFMPEG_CANDIDATES in this script.")

    source_videos = find_source_videos()
    if not source_videos:
        print("\nNothing to do - no source videos found in '{}'.".format(SOURCE_DIR))
        return

    todo = plan_conversions(source_videos)
    if not todo:
        print("\nNothing to do - every source is already converted at every resolution.")
        return

    print("\n[3/3] Converting {} missing file(s)...".format(len(todo)))
    ok_count = 0
    for i, (source_path, width, height, out_path) in enumerate(todo, start=1):
        print("\n({}/{}) {} -> {}x{}".format(
            i, len(todo), os.path.basename(source_path), width, height))
        if convert(source_path, width, height, out_path):
            ok_count += 1

    print("\n=== Done: {}/{} conversion(s) succeeded ===".format(ok_count, len(todo)))


if __name__ == "__main__":
    main()
