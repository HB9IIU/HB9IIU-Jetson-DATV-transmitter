"""Pre-process source soundtracks from "soundtracks/" into loudness-
normalized, format-standardized copies in "soundtracks_normalized/", for use
as an optional testcard soundtrack in datv_tx_plus.py.

Why loudness normalization: source tracks come from different places (rips,
different masters, downloads) and can differ wildly in perceived volume.
The final transmission encode is a fixed, quite low bitrate (32kbps mono
AAC in the current DATV profiles), so there's no headroom in the pipeline
to gain-ride live - whatever level goes in is roughly what goes out.
Normalizing once, offline, to a consistent target loudness means switching
between tracks doesn't jump between whisper-quiet and clipping.

Why also convert format in the same pass: loudnorm is a filter, so it
requires decoding and re-encoding the file regardless - there's no way to
apply it "in place" on a compressed stream. Since that re-encode is
happening anyway, standardizing every output to the same codec/container
(AAC in .m4a) costs nothing extra, and keeps the file-picker in
datv_tx_plus.py simple (one known extension to look for, regardless of
what mix of .mp3/.wav/.flac shows up in the source folder). This re-encode
doesn't need to be lossless the way the video pre-processing did - the
final 32kbps mono AAC transmission encode is already the dominant quality
bottleneck by a wide margin, so a decent-bitrate (192kbps) intermediate
loses nothing perceptible beyond what that final pass already costs.

Naming convention / "only convert once" trick - same as preprocess_videos.py:
each output keeps the source's own filename (extension stripped) as a .m4a
in soundtracks_normalized/, e.g. "soundtracks/arise.mp3" ->
"soundtracks_normalized/arise.m4a". That exact path's existence *is* the
record of "already processed" - no separate manifest/database needed, and
re-running this script only processes whatever is still missing.

Note: tone-1khz-minus18dbfs.wav looks like a calibration reference tone
(precise level, not music) - loudness-normalizing it would defeat its
purpose. Exclude it from soundtracks/ (or just don't offer it as a
soundtrack pick) rather than relying on this script to guess which files
are calibration tones vs. music.

Run manually: python preprocess_soundtracks.py
"""

import os
import shutil
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(SCRIPT_DIR, "soundtracks")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "soundtracks_normalized")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}

# EBU R128-ish loudness target - a reasonable default for background/
# streaming-style audio, not a precise broadcast-spec requirement here.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
OUTPUT_BITRATE = "192k"

# ffmpeg isn't always on PATH (confirmed on the Jetson it resolves to
# /usr/bin/ffmpeg via PATH; the WinGet path is a fallback for local
# Windows testing).
FFMPEG_CANDIDATES = [
    shutil.which("ffmpeg"),
    r"C:\Users\danie\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe",
]
FFMPEG = next((p for p in FFMPEG_CANDIDATES if p and os.path.isfile(p)), None)


def find_source_tracks():
    """Return the list of audio file paths directly inside SOURCE_DIR."""
    print("[1/3] Scanning '{}' for source tracks...".format(SOURCE_DIR))
    if not os.path.isdir(SOURCE_DIR):
        raise SystemExit("Source folder not found: {}".format(SOURCE_DIR))

    tracks = []
    for name in sorted(os.listdir(SOURCE_DIR)):
        path = os.path.join(SOURCE_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(path):
            continue
        if ext in AUDIO_EXTENSIONS:
            print("  found:   {}".format(name))
            tracks.append(path)
        else:
            print("  ignored: {} (not a recognized audio extension)".format(name))

    print("  -> {} source track(s) found".format(len(tracks)))
    return tracks


def output_path_for(source_path):
    """Expected normalized path for a given source - see module docstring
    for the naming convention this relies on."""
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(OUTPUT_DIR, base_name + ".m4a")


def plan_conversions(source_tracks):
    """Check every source against its expected output and split into
    already-done vs still-needed, printing the reasoning along the way."""
    print("\n[2/3] Checking which tracks still need processing...")
    todo = []
    for source_path in source_tracks:
        source_name = os.path.basename(source_path)
        out_path = output_path_for(source_path)
        if os.path.exists(out_path):
            print("  [skip]    {} -> already exists: {}".format(source_name, out_path))
        else:
            print("  [pending] {} -> will create: {}".format(source_name, out_path))
            todo.append((source_path, out_path))

    print("  -> {} conversion(s) needed".format(len(todo)))
    return todo


def convert(source_path, out_path):
    """Loudness-normalize and re-encode to a consistent AAC/.m4a format."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = [
        FFMPEG, "-y",
        "-i", source_path,
        "-af", LOUDNORM_FILTER,
        "-c:a", "aac", "-b:a", OUTPUT_BITRATE,
        out_path,
    ]
    print("  command: {}".format(" ".join(cmd)))

    # stdout/stderr=PIPE + universal_newlines instead of capture_output/text
    # - the Jetson's venv is Python 3.6, which predates both those kwargs.
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
    print("=== DATV soundtrack pre-processing ===")
    print("ffmpeg: {}".format(FFMPEG))
    if FFMPEG is None:
        raise SystemExit(
            "ffmpeg not found (checked PATH and the known WinGet install "
            "location). Install it or fix FFMPEG_CANDIDATES in this script.")

    source_tracks = find_source_tracks()
    if not source_tracks:
        print("\nNothing to do - no source tracks found in '{}'.".format(SOURCE_DIR))
        return

    todo = plan_conversions(source_tracks)
    if not todo:
        print("\nNothing to do - every source track is already processed.")
        return

    print("\n[3/3] Processing {} missing track(s)...".format(len(todo)))
    ok_count = 0
    for i, (source_path, out_path) in enumerate(todo, start=1):
        print("\n({}/{}) {}".format(i, len(todo), os.path.basename(source_path)))
        if convert(source_path, out_path):
            ok_count += 1

    print("\n=== Done: {}/{} track(s) succeeded ===".format(ok_count, len(todo)))


if __name__ == "__main__":
    main()
