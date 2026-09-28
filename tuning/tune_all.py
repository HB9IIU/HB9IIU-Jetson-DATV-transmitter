"""Run every tuning in a row, unattended: the camera profiles against the
camera clip, the video profiles against the movie clip, then the testcard
profiles - the three runs that otherwise need hand edits of TEST_CLIPS /
PROFILES_TO_TUNE between them. Nothing is transmitted (file output only).

Each stage writes its own summary to results/. Refuses to start - and
stops before the next stage - while a transmission is running: both would
share the one hardware encoder and disturb each other.

Takes about 4 1/4 hours (roughly 25 min per camera/video profile, 15 min
per testcard profile plus a few minutes picking the hardest test card). Start it so it survives closing the SSH window - see
README.md.

Usage: .venv/bin/python3 -u tuning/tune_all.py
"""

import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import tune_profiles as tp  # noqa: E402
import tune_profiles_for_testcard as tt  # noqa: E402

tx = tp.tx


def log(message):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), message), flush=True)


def transmission_running():
    # Brackets: stop pgrep matching a shell whose command line holds the
    # pattern itself.
    return subprocess.run(["pgrep", "-f", "[d]atv_web_worker|[c]lock_tx.py"],
                          stdout=subprocess.DEVNULL).returncode == 0


def run_clip_stage(label, clips, profile_names):
    """One tune_profiles.py run with the given clips/profiles, as if those
    two constants had been edited by hand."""
    log("##### {}: {} #####".format(label, ", ".join(profile_names)))
    tp.TEST_CLIPS = clips
    tp.PROFILES_TO_TUNE = profile_names
    console = sys.stdout
    try:
        tp.main()
    finally:
        # tp.main() tees stdout into its run's log file - undo that so the
        # next stage doesn't keep writing into it.
        sys.stdout = console


def run_testcard_stage(profile_names):
    log("##### Testcard: {} #####".format(", ".join(profile_names)))
    tt.PROFILES_TO_TUNE = profile_names
    tt.main()


def main():
    names = list(tx.PROFILES)
    camera = [n for n in names if n.endswith("_camera")]
    testcard = [n for n in names if n.endswith("_720p")]
    video = [n for n in names if n not in camera and n not in testcard]
    stages = [
        ("Camera", lambda: run_clip_stage("Camera", tp.TEST_CLIPS_CAMERA, camera)),
        ("Video", lambda: run_clip_stage("Video", tp.TEST_CLIPS_MOVIE, video)),
        ("Testcard", lambda: run_testcard_stage(testcard)),
    ]

    run_start = time.time()
    log("Tuning everything: {} camera, {} video, {} testcard profiles (about 4 hours).".format(
        len(camera), len(video), len(testcard)))
    done = []
    for label, stage in stages:
        if transmission_running():
            log("A transmission is running - stop it in the web page. "
                "Skipping {} and everything after it.".format(label))
            break
        stage()
        done.append(label)

    log("ALL DONE in {:.0f} min ({}). Summaries in {}:".format(
        (time.time() - run_start) / 60.0, ", ".join(done) or "nothing", tp.RESULTS_DIR))
    for name in sorted(os.listdir(tp.RESULTS_DIR)):
        if os.path.getmtime(os.path.join(tp.RESULTS_DIR, name)) >= run_start:
            log("  " + name)


if __name__ == "__main__":
    main()
