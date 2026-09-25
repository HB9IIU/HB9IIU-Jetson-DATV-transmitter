"""Combine one testcard image + one soundtrack into a real video file, for
each DATV profile resolution, so it can be picked up by datv_tx_plus.py's
existing SOURCE == "video" path (see select_video_file(), which scans the
same preprocessed_WxH/ folders this script writes into).

Why this exists instead of a live "static image + separate audio" pipeline:
looping two independently-clocked sources (an image and an audio track) in
lockstep inside GStreamer proved unreliable - two different looping
techniques were tried against that setup and both failed the same way.
Pre-rendering them into one real video file sidesteps the problem entirely:
a single file has one uridecodebin carrying both streams already in sync,
which is exactly the "video" source path already used for real videos, and
already loops correctly (see loop_media_element() in datv_tx_plus.py).

Video is encoded with the same H.264 settings as preprocess_videos.py
(h264_output_args()) so the Jetson decodes it in hardware - FFV1 had to be
decoded in software, even for a still image, and stuttered on air. Audio is
stream-copied from the already-normalized soundtracks_normalized/*.m4a
untouched.

Usage: run this script, pick one testcard and one soundtrack (or none) when
prompted, and it creates one file per resolution:
    preprocessed_1280x720/<testcard>_<soundtrack>.mkv
Run it again with a different combination whenever you want another one
available - existing files for a combination already generated are skipped.

Run manually: python testcard_movies_generator.py
"""

import os
import shutil
import subprocess

from preprocess_videos import h264_output_args, is_valid_output

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTCARDS_DIR = os.path.join(SCRIPT_DIR, "testcards")
SOUNDTRACKS_DIR = os.path.join(SCRIPT_DIR, "soundtracks_normalized")

# DATV profile resolutions (see PROFILES in datv_tx_plus.py) -> their
# pre-processed output folder - the same ones preprocess_videos.py fills,
# so datv_tx_plus.py's picker doesn't need to know these came from here.
RESOLUTIONS = {
    (1280, 720): os.path.join(SCRIPT_DIR, "preprocessed_1280x720"),
}

# A still image has no natural duration - only matters when there's no
# soundtrack to match length to, since the result loops seamlessly anyway.
NO_AUDIO_DURATION_SECONDS = 10

FFMPEG_CANDIDATES = [
    shutil.which("ffmpeg"),
    r"C:\Users\danie\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe",
]
FFMPEG = next((p for p in FFMPEG_CANDIDATES if p and os.path.isfile(p)), None)


def select_testcard():
    print("Looking for testcard images in {}...".format(TESTCARDS_DIR))
    if not os.path.isdir(TESTCARDS_DIR):
        raise SystemExit("Testcards folder not found: {}".format(TESTCARDS_DIR))

    files = sorted(name for name in os.listdir(TESTCARDS_DIR) if name.lower().endswith(".png"))
    if not files:
        raise SystemExit("No testcard images found in {}".format(TESTCARDS_DIR))

    print("Available testcards:")
    for i, name in enumerate(files, start=1):
        print("  {}) {}".format(i, name))

    while True:
        choice = input("Select a testcard [1-{}]: ".format(len(files))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            selected = files[int(choice) - 1]
            break
        print("Invalid choice '{}', try again.".format(choice))

    path = os.path.join(TESTCARDS_DIR, selected)
    print("Selected: {}".format(path))
    return path


def select_soundtrack():
    print("Looking for soundtracks in {}...".format(SOUNDTRACKS_DIR))
    if not os.path.isdir(SOUNDTRACKS_DIR):
        print("  no soundtracks folder found - continuing without sound.")
        return None

    files = sorted(name for name in os.listdir(SOUNDTRACKS_DIR) if name.lower().endswith(".m4a"))
    if not files:
        print("  no processed soundtracks found - continuing without sound.")
        return None

    print("Available soundtracks:")
    print("  0) No soundtrack")
    for i, name in enumerate(files, start=1):
        print("  {}) {}".format(i, name))

    while True:
        choice = input("Select a soundtrack [0-{}]: ".format(len(files))).strip()
        if choice.isdigit() and 0 <= int(choice) <= len(files):
            choice = int(choice)
            break
        print("Invalid choice '{}', try again.".format(choice))

    if choice == 0:
        print("Selected: no soundtrack")
        return None

    path = os.path.join(SOUNDTRACKS_DIR, files[choice - 1])
    print("Selected: {}".format(path))
    return path


def output_name(testcard_path, soundtrack_path):
    """Naming convention - same "does it already exist" skip trick as
    preprocess_videos.py, just keyed on the (testcard, soundtrack) pair
    instead of a single source file."""
    testcard_base = os.path.splitext(os.path.basename(testcard_path))[0]
    soundtrack_base = (
        os.path.splitext(os.path.basename(soundtrack_path))[0]
        if soundtrack_path else "silent")
    return "{}_{}.mkv".format(testcard_base, soundtrack_base)


def generate(testcard_path, soundtrack_path, width, height, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = [FFMPEG, "-y", "-loop", "1", "-i", testcard_path]

    if soundtrack_path is not None:
        cmd += ["-i", soundtrack_path, "-shortest", "-c:a", "copy"]
    else:
        cmd += ["-t", str(NO_AUDIO_DURATION_SECONDS)]

    cmd += h264_output_args(width, height) + [out_path]
    print("  command: {}".format(" ".join(cmd)))

    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        print("  FFMPEG FAILED (exit code {})".format(result.returncode))
        print("  ---- last part of ffmpeg stderr ----")
        print(result.stderr[-2000:])
        print("  -------------------------------------")
        return False

    print("  done: {} ({} bytes)".format(out_path, os.path.getsize(out_path)))
    return True


def main():
    print("=== Testcard movie generator ===")
    print("ffmpeg: {}".format(FFMPEG))
    if FFMPEG is None:
        raise SystemExit(
            "ffmpeg not found (checked PATH and the known WinGet install "
            "location). Install it or fix FFMPEG_CANDIDATES in this script.")

    testcard_path = select_testcard()
    soundtrack_path = select_soundtrack()
    name = output_name(testcard_path, soundtrack_path)

    print("\nGenerating '{}' at {} resolution(s)...".format(name, len(RESOLUTIONS)))
    ok_count = 0
    for (width, height), output_dir in RESOLUTIONS.items():
        out_path = os.path.join(output_dir, name)
        print("\n{}x{} -> {}".format(width, height, out_path))
        # is_valid_output(), not os.path.exists(): an older FFV1 file of
        # the same name gets regenerated as H.264 instead of skipped.
        if is_valid_output(out_path):
            print("  [skip] already exists")
            ok_count += 1
            continue
        if generate(testcard_path, soundtrack_path, width, height, out_path):
            ok_count += 1

    print("\n=== Done: {}/{} resolution(s) succeeded ===".format(ok_count, len(RESOLUTIONS)))


if __name__ == "__main__":
    main()
