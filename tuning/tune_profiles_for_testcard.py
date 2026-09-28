"""Find the highest safe video_bitrate_kbps for SOURCE=="testcard", by
actually measuring the hardware encoder - the testcard counterpart to
tune_profiles.py (see that file's docstring for why real-hardware
measurement matters at all: nvv4l2h265enc's real muxed bitrate doesn't
match its bitrate= target, and the overshoot isn't a fixed ratio).

Why this is a separate script instead of another TEST_CLIPS entry in
tune_profiles.py: testcard mode has no clip file to encode. It's a live
pipeline - a still image plus four text overlays (callsign/freq/volume/
elapsed-time, see load_testcard_overlay_config()), driven by main()'s
polling loop rather than by decoding frames from disk. Testcard mode never
shows the scrolling marquee (that's camera/video-only - see
build_pipeline_description()), so there's no marquee state to reproduce
here either. A trial here has to reproduce that live driving loop for a
fixed wall-clock duration instead of just playing a file to its natural
end.

Why no PSNR comparison (unlike tune_profiles.py): PSNR there works because
a movie/camera clip is the same deterministic sequence of frames on every
run, so the "original bitrate" and "found bitrate" encodes can be compared
frame-for-frame against one lossless reference. A testcard's overlay text
positions (marquee sweep, elapsed-time readout) are driven by
time.monotonic() polling, not by frame number - two separate trial runs
won't have pixel-identical overlay state at the same output frame, so a
frame-exact PSNR comparison isn't meaningful here. This script only
answers the bitrate-safety question (is the configured video_bitrate_kbps
actually safely under this profile's real DVB-S2 TS capacity on real
testcard content), which is the dominant risk anyway - testcard content is
overwhelmingly static, so quality at a safe bitrate is not in serious
doubt the way it is for real motion content.

Method: same bisection search as tune_profiles.py. Each trial builds the
real testcard pipeline via datv_tx_plus.py's own build_pipeline_description()
(TX_OUTPUT="file", no Pluto/MQTT needed), runs it for
TESTCARD_TRIAL_DURATION_SECONDS of wall-clock time while driving the same
elapsed-time/tone-schedule/marquee updates main() would, then measures the
resulting .ts file's real bitrate with ffprobe. Same safety rule as
tune_profiles.py (added 2026-09-26): a trial only counts as safe if it is
under SAFETY_MARGIN of capacity AND passes the exact on-air CBR relay with
zero "dts < pcr" warnings at some muxdelay in MUXDELAY_CANDIDATES - the
average alone misses keyframe bursts.

Which image: all test cards differ a lot in detail (PNG size 175 KB to
1.15 MB), and more detail means bigger keyframes. Before tuning, every
image gets a short trial at one fixed bitrate (pick_hardest_testcard());
the one with the most relay warnings / highest real bitrate is tuned on,
so the result is safe for the easier ones too. On air the testcards carry
no banners/marquee (main() forces them off), so neither do the trials.

Each trial runs in its OWN subprocess, same reasoning as tune_profiles.py
(a real SIGSEGV was observed on this hardware after ~16 in-process
GStreamer/NVENC pipeline create/destroy cycles).

Runs only on the Jetson (needs the real nvv4l2h265enc hardware encoder and
PyGObject) - not portable to the Windows dev machine.

By default (PROFILES_TO_TUNE below) only tunes the two profiles the
testcard-at-720p question is actually about. Set it to [] to sweep every
profile in PROFILES instead. A hand-edited constant, not a command-line
argument - same convention as PROFILE/SOURCE in datv_tx_plus.py and
PROFILES_TO_TUNE in tune_profiles.py, so it's just as easy to run from an
IDE's Run button as from a terminal.

Usage: python tune_profiles_for_testcard.py
"""

import contextlib
import io
import os
import subprocess
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# This script lives in tuning/, one level below the project root that holds
# datv_tx_plus.py - put the root on sys.path so the import below finds it.
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

import datv_tx_plus as tx  # noqa: E402
# The relay check (count_relay_warnings(), MUXDELAY_CANDIDATES) is shared
# with the camera/video tuner so both judge "safe" the same way.
sys.path.insert(0, SCRIPT_DIR)
import tune_profiles as tp  # noqa: E402

WORK_DIR = os.path.join(SCRIPT_DIR, "runs")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
# ffmpeg-static/ isn't on the Jetson (checked 2026-09-23) - fall back to the
# system ffprobe rather than failing on a missing path.
_STATIC_FFPROBE = os.path.join(PROJECT_DIR, "ffmpeg-static", "ffprobe")
FFPROBE = _STATIC_FFPROBE if os.path.isfile(_STATIC_FFPROBE) else "ffprobe"

# Which profile(s) to tune - edit this directly, then just hit Run. Starts
# with the profiles the testcard-at-720p hypothesis (see dvbs2_profiles.py)
# is actually about - real motion content measured better at 960x540, but
# testcard's near-zero motion may not have the same bitrate-vs-resolution
# tradeoff. All four below (sr500 and sr333 720p pairs) were confirmed safe
# at their configured bitrates by a 2026-09-10 hardware run. Empty list =
# every profile in PROFILES.
PROFILES_TO_TUNE = ["sr333_fec23_720p", "sr333_fec34_720p",
                    "sr500_fec23_720p", "sr500_fec34_720p"]

# How long each trial plays the live testcard pipeline before stopping and
# measuring - matches the ~90s clip length tune_profiles.py's movie/camera
# trials use, for a comparable amount of real encoder output per trial.
TESTCARD_TRIAL_DURATION_SECONDS = 90.0
# Shorter trial per image when picking the hardest one - still ~7 keyframes
# at the 4 s GOP, which is what differs between images.
IMAGE_SCAN_DURATION_SECONDS = 30.0

# How close to the profile's exact DVB-S2 TS capacity a trial's real
# measured bitrate is allowed to get - same reasoning and value as
# tune_profiles.py's SAFETY_MARGIN.
SAFETY_MARGIN = 0.97

BISECTION_TOLERANCE_KBPS = 5
FFPROBE_TIMEOUT_SECONDS = 15
TRIAL_TIMEOUT_SECONDS = TESTCARD_TRIAL_DURATION_SECONDS + 120
TRIAL_RETRY_LIMIT = 2


def log(message):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), message), flush=True)


def list_testcard_images():
    testcard_dir = os.path.join(tx.SCRIPT_DIR, "testcards")
    files = sorted(name for name in os.listdir(testcard_dir)
                    if name.lower().endswith((".png", ".jpg", ".jpeg")))
    if not files:
        raise SystemExit("No test card images found in {}".format(testcard_dir))
    return [os.path.join(testcard_dir, name) for name in files]


def pick_hardest_testcard(profile_name):
    """Short trial of every testcard image at profile_name's configured
    bitrate; returns the image the encoder struggles with most (most relay
    warnings at the on-air muxdelay, then highest real bitrate)."""
    profile = tx.PROFILES[profile_name]
    capacity_bps = tx.calculate_dvbs2_ts_bitrate(profile)
    scan_path = os.path.join(WORK_DIR, "_testcard_scan_trial.ts")
    log("Finding the hardest test card ({}s each at {} kbps, profile '{}')...".format(
        IMAGE_SCAN_DURATION_SECONDS, profile["video_bitrate_kbps"], profile_name))
    scores = []
    for path in list_testcard_images():
        run_testcard_trial_subprocess(profile_name, profile["video_bitrate_kbps"], path,
                                      scan_path, IMAGE_SCAN_DURATION_SECONDS)
        real_bps = measure_bitrate_bps(scan_path)
        warnings = tp.count_relay_warnings(scan_path, capacity_bps, tx.CBR_MUXDELAY_SECONDS)
        log("  {}: real {} bit/s, {} relay warnings at {}s".format(
            os.path.basename(path), real_bps, warnings, tx.CBR_MUXDELAY_SECONDS))
        scores.append((warnings, real_bps, path))
    hardest = max(scores)[2]
    log("Hardest: {} - tuning on it.".format(os.path.basename(hardest)))
    return hardest


def run_testcard_trial(profile, video_bitrate_kbps, testcard_path, out_ts_path,
                       duration_seconds=TESTCARD_TRIAL_DURATION_SECONDS):
    """Encode testcard_path through the real pipeline at video_bitrate_kbps
    for TESTCARD_TRIAL_DURATION_SECONDS of wall-clock time, writing
    out_ts_path. Reproduces main()'s live driving loop (elapsed-time
    readout, tone schedule/banners, marquee sweep) so the encoder sees the
    same kind of motion a real transmission would, just for a fixed
    duration instead of until Ctrl+C.

    Only ever called from within _trial_worker_main(), i.e. inside the
    short-lived --_trial subprocess - never directly from the orchestrator.
    """
    trial_profile = dict(profile, video_bitrate_kbps=video_bitrate_kbps)
    tx.SOURCE = "testcard"
    tx.TX_OUTPUT = "file"
    tx.TX_OUTPUT_FILE = out_ts_path

    Gst.init(None)
    pipeline_description = tx.build_pipeline_description(
        None, trial_profile, testcard_path, top_bar_enabled=False, bottom_bar_enabled=False)
    pipeline = Gst.parse_launch(pipeline_description)
    bus = pipeline.get_bus()

    tone_source = pipeline.get_by_name("tone_source")
    freq_banner_overlay = pipeline.get_by_name("testcard_freq_banner")
    volume_banner_overlay = pipeline.get_by_name("testcard_volume_banner")
    elapsed_overlay = pipeline.get_by_name("testcard_elapsed_ms")

    start_freq, _, start_is_rest = tx.TESTCARD_TONE_SCHEDULE[0]
    if freq_banner_overlay is not None:
        freq_banner_overlay.set_property(
            "text", tx.format_freq_banner(start_freq, start_is_rest))
    if volume_banner_overlay is not None:
        volume_banner_overlay.set_property(
            "text", tx.format_volume_banner(tx.TESTCARD_MELODY_VOLUMES[0], start_is_rest))

    poll_interval_seconds = min(
        tx.TESTCARD_MELODY_TICK_SECONDS, tx.TESTCARD_TIME_OVERLAY_TICK_SECONDS)

    trial_start = time.monotonic()
    last_elapsed_update = 0.0
    last_tone_change = trial_start
    tone_index = 0
    melody_play_index = 0
    pending_tone_changes = []

    pipeline.set_state(Gst.State.PLAYING)
    try:
        while True:
            message = bus.timed_pop_filtered(
                int(poll_interval_seconds * Gst.SECOND), Gst.MessageType.ERROR)
            if message is not None:
                error, debug = message.parse_error()
                raise RuntimeError("GStreamer error: {} ({})".format(error, debug))

            now = time.monotonic()
            if now - trial_start > duration_seconds:
                return

            if (elapsed_overlay is not None
                    and now - last_elapsed_update >= tx.TESTCARD_TIME_OVERLAY_TICK_SECONDS):
                elapsed_overlay.set_property(
                    "text", "[{:.0f}ms]".format((now - trial_start) * 1000))
                last_elapsed_update = now

            if (tone_source is not None
                    and now - last_tone_change >= tx.TESTCARD_TONE_SCHEDULE[tone_index][1]):
                tone_index = (tone_index + 1) % len(tx.TESTCARD_TONE_SCHEDULE)
                if tone_index == 0:
                    melody_play_index = (melody_play_index + 1) % len(tx.TESTCARD_MELODY_VOLUMES)
                new_freq, _, new_is_rest = tx.TESTCARD_TONE_SCHEDULE[tone_index]
                new_volume = 0.0 if new_is_rest else tx.TESTCARD_MELODY_VOLUMES[melody_play_index]
                if freq_banner_overlay is not None:
                    freq_banner_overlay.set_property(
                        "text", tx.format_freq_banner(new_freq, new_is_rest))
                if volume_banner_overlay is not None:
                    volume_banner_overlay.set_property(
                        "text", tx.format_volume_banner(new_volume, new_is_rest))
                pending_tone_changes.append(
                    (new_freq, new_volume, now + tx.TESTCARD_AUDIO_DELAY_SECONDS))
                last_tone_change = now

            while pending_tone_changes and now >= pending_tone_changes[0][2]:
                freq_to_apply, volume_to_apply, _ = pending_tone_changes.pop(0)
                tone_source.set_property("freq", freq_to_apply)
                tone_source.set_property("volume", volume_to_apply)
    finally:
        pipeline.set_state(Gst.State.NULL)


def run_testcard_trial_subprocess(profile_name, video_bitrate_kbps, testcard_path, out_ts_path,
                                  duration_seconds=TESTCARD_TRIAL_DURATION_SECONDS):
    """Runs one trial in a brand-new subprocess - see module docstring."""
    command = [sys.executable, os.path.abspath(__file__), "--_trial",
               profile_name, str(video_bitrate_kbps), testcard_path, out_ts_path,
               str(duration_seconds)]
    last_summary = None
    for attempt in range(1, TRIAL_RETRY_LIMIT + 1):
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=TRIAL_TIMEOUT_SECONDS + 30)
        if result.returncode == 0:
            return
        if result.returncode < 0:
            outcome = "killed by signal {} (likely a hardware/driver crash)".format(
                -result.returncode)
        else:
            outcome = "exited with code {}".format(result.returncode)
        last_summary = "{} on attempt {}/{}".format(outcome, attempt, TRIAL_RETRY_LIMIT)
        log("  trial subprocess {} - tail of its output:".format(last_summary))
        for line in result.stdout.strip().splitlines()[-8:]:
            log("    | " + line)
    raise RuntimeError("Encode trial failed after {} attempts - {}".format(
        TRIAL_RETRY_LIMIT, last_summary))


def measure_bitrate_bps(ts_path):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=bit_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        timeout=FFPROBE_TIMEOUT_SECONDS)
    return int(result.stdout.strip())


def find_max_safe_video_bitrate_kbps(profile_name, testcard_path):
    profile = tx.PROFILES[profile_name]
    capacity_bps = tx.calculate_dvbs2_ts_bitrate(profile)
    ceiling_bps = capacity_bps * SAFETY_MARGIN
    audio_kbps = profile["audio_bitrate_kbps"]

    lo = (capacity_bps / 1000.0 - audio_kbps) * 0.5
    hi = capacity_bps / 1000.0 - audio_kbps
    log("Profile '{}': capacity={:.0f} bit/s, safety ceiling={:.0f} bit/s, "
        "searching video_bitrate_kbps in [{:.0f}, {:.0f}]".format(
            profile_name, capacity_bps, ceiling_bps, lo, hi))

    trial_path = os.path.join(WORK_DIR, "_testcard_bisect_trial.ts")
    best_kbps = None
    best_muxdelay = None
    trial_num = 0
    while hi - lo > BISECTION_TOLERANCE_KBPS:
        trial_num += 1
        mid = round((lo + hi) / 2.0)
        log("  trial {}: encoding at video_bitrate_kbps={:.0f} "
            "(bracket [{:.0f}, {:.0f}])...".format(trial_num, mid, lo, hi))
        trial_start = time.monotonic()
        run_testcard_trial_subprocess(profile_name, mid, testcard_path, trial_path)
        real_bps = measure_bitrate_bps(trial_path)
        fits = real_bps <= ceiling_bps
        # Same rule as tune_profiles.py: no relay check if the average
        # already doesn't fit.
        muxdelay, tried = tp.find_min_clean_muxdelay(trial_path, capacity_bps) if fits else (None, {})
        safe = fits and muxdelay is not None
        if not fits:
            verdict = "OVER ceiling"
        elif muxdelay is None:
            verdict = "relay timing FAILS up to {}s ({})".format(
                tp.MUXDELAY_CANDIDATES[-1], tp.format_tried(tried))
        else:
            verdict = "safe, clean from muxdelay {}s ({})".format(muxdelay, tp.format_tried(tried))
        log("  trial {}: video_bitrate_kbps={:.0f} -> real {} bit/s, {} [{:.0f}s]".format(
            trial_num, mid, real_bps, verdict, time.monotonic() - trial_start))
        if safe:
            best_kbps = mid
            best_muxdelay = muxdelay
            lo = mid
        else:
            hi = mid

    if best_kbps is None:
        raise SystemExit(
            "Even the lowest bracket ({:.0f} kbps) exceeded the safety ceiling - "
            "this profile's SR/FEC may not actually support its own resolution "
            "target on real testcard content.".format(lo))
    return best_kbps, best_muxdelay, capacity_bps


def tune_one_profile(profile_name, testcard_path):
    profile = tx.PROFILES[profile_name]
    width, height = profile["resolution"]
    original_kbps = profile["video_bitrate_kbps"]

    best_kbps, best_muxdelay, capacity_bps = find_max_safe_video_bitrate_kbps(
        profile_name, testcard_path)

    # The configuration as it goes on air today, through the relay at the
    # on-air muxdelay - same sanity check as tune_profiles.py.
    original_ts = os.path.join(WORK_DIR, "_testcard_compare_original.ts")
    run_testcard_trial_subprocess(profile_name, original_kbps, testcard_path, original_ts)
    original_real_bps = measure_bitrate_bps(original_ts)
    original_on_air_warnings = tp.count_relay_warnings(
        original_ts, capacity_bps, tx.CBR_MUXDELAY_SECONDS)

    print()
    print("=" * 62)
    print("Profile '{}' ({}x{}), DVB-S2 TS capacity = {:.0f} bit/s".format(
        profile_name, width, height, capacity_bps))
    print("-" * 62)
    print("  currently configured : {:4d} kbps -> real {:7d} bit/s ({:.1f}%), "
          "relay @ {}s: {} warnings".format(
              original_kbps, original_real_bps, original_real_bps / capacity_bps * 100.0,
              tx.CBR_MUXDELAY_SECONDS, original_on_air_warnings))
    print("  found safe optimum   : {:4.0f} kbps, relay clean from muxdelay {}s".format(
        best_kbps, best_muxdelay))
    print("=" * 62)
    if best_kbps > original_kbps:
        print("-> raise video_bitrate_kbps for '{}' to {:.0f} in PROFILES "
              "(dvbs2_profiles.py) - real spare capacity on testcard "
              "content.".format(profile_name, best_kbps))
    elif best_kbps < original_kbps:
        print("-> LOWER video_bitrate_kbps for '{}' to {:.0f} in PROFILES - "
              "the current value is not safely under this profile's real "
              "capacity on real testcard content.".format(profile_name, best_kbps))
    else:
        print("-> currently configured value is already the safe optimum.")

    return {
        "profile_name": profile_name,
        "original_kbps": original_kbps,
        "best_kbps": best_kbps,
        "best_muxdelay": best_muxdelay,
        "capacity_bps": capacity_bps,
        "original_on_air_warnings": original_on_air_warnings,
    }


def print_final_report(results, errors, run_start):
    total_min = (time.monotonic() - run_start) / 60.0

    print()
    print("#" * 62)
    print("SUMMARY - testcard profiles ({:.0f} min total)".format(total_min))
    print("#" * 62)
    for r in results:
        arrow = ("raise to" if r["best_kbps"] > r["original_kbps"] else
                  "LOWER to" if r["best_kbps"] < r["original_kbps"] else "keep at")
        print("  {:22s} {:4d} kbps -> {} {:4.0f} kbps, needs muxdelay >= {}s "
              "(today at {}s: {} warnings)".format(
                  r["profile_name"], r["original_kbps"], arrow, r["best_kbps"],
                  r["best_muxdelay"], tx.CBR_MUXDELAY_SECONDS, r["original_on_air_warnings"]))
    for profile_name, reason in errors:
        print("  {:22s} FAILED ({})".format(profile_name, reason))

    over_capacity = [r for r in results if r["best_kbps"] < r["original_kbps"]]
    has_headroom = [r for r in results if r["best_kbps"] > r["original_kbps"]]
    already_optimal = [r for r in results if r["best_kbps"] == r["original_kbps"]]

    print()
    print("FINDINGS")
    print("-" * 62)
    print("  {} of {} profiles tested successfully ({} failed).".format(
        len(results), len(results) + len(errors), len(errors)))
    if over_capacity:
        print("  {} profile(s) are configured too high on real testcard content "
              "(over the {:.0f}% safety limit, or relay not clean):".format(
                  len(over_capacity), SAFETY_MARGIN * 100))
        for r in over_capacity:
            print("    - {}: {} -> {:.0f} kbps".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
    if has_headroom:
        print("  {} profile(s) have real spare capacity on testcard content:".format(
            len(has_headroom)))
        for r in has_headroom:
            print("    - {}: {} -> {:.0f} kbps".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
    if already_optimal:
        print("  {} profile(s) already at their safe optimum: {}".format(
            len(already_optimal), ", ".join(r["profile_name"] for r in already_optimal)))

    print()
    print("ACTIONS TO BE TAKEN")
    print("-" * 62)
    changed = over_capacity + has_headroom
    if not changed:
        print("  None - every tested profile's configured bitrate is already safe.")
    else:
        print("  Edit PROFILES in dvbs2_profiles.py:")
        for r in changed:
            print("    \"{}\": video_bitrate_kbps {} -> {:.0f}".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
    if results:
        needed = max(r["best_muxdelay"] for r in results)
        if needed > tx.CBR_MUXDELAY_SECONDS:
            print("  Set CBR_MUXDELAY_SECONDS in datv_tx_plus.py: {} -> {} "
                  "(largest minimum among tested profiles).".format(
                      tx.CBR_MUXDELAY_SECONDS, needed))
        else:
            print("  CBR_MUXDELAY_SECONDS ({}s) already covers every tested profile "
                  "(testcard needs >= {}s).".format(tx.CBR_MUXDELAY_SECONDS, needed))
    if errors:
        print("  Investigate/re-run failed profile(s): {}".format(
            ", ".join(name for name, _ in errors)))


def _trial_worker_main(argv):
    """Entry point when this script is re-invoked as a --_trial subprocess
    (see run_testcard_trial_subprocess()) - never called directly by a user.
    """
    profile_name, video_bitrate_kbps, testcard_path, out_ts_path, duration_seconds = (
        argv[0], int(argv[1]), argv[2], argv[3], float(argv[4]))
    profile = tx.PROFILES[profile_name]
    run_testcard_trial(profile, video_bitrate_kbps, testcard_path, out_ts_path,
                       duration_seconds)


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    run_stamp = time.strftime("%Y-%m-%d_%H%M")

    if PROFILES_TO_TUNE:
        unknown = [name for name in PROFILES_TO_TUNE if name not in tx.PROFILES]
        if unknown:
            raise SystemExit("Unknown profile name(s) in PROFILES_TO_TUNE: {} - available: {}".format(
                ", ".join(unknown), ", ".join(tx.PROFILES)))
        profile_names = PROFILES_TO_TUNE
    else:
        profile_names = list(tx.PROFILES)

    run_start = time.monotonic()
    # One image for every profile: the hardest one at the first profile's
    # bitrate is the hardest at the others too (same resolution/encoder).
    testcard_path = pick_hardest_testcard(profile_names[0])

    results = []
    errors = []
    for i, profile_name in enumerate(profile_names, start=1):
        log("=== Profile {}/{}: '{}' (run elapsed so far: {:.0f} min) ===".format(
            i, len(profile_names), profile_name, (time.monotonic() - run_start) / 60.0))
        profile_start = time.monotonic()
        try:
            results.append(tune_one_profile(profile_name, testcard_path))
        except (Exception, SystemExit) as exc:
            log("  FAILED after {:.0f}s: {}".format(
                time.monotonic() - profile_start, exc))
            errors.append((profile_name, str(exc)))
            continue
        log("  Profile '{}' done in {:.0f} min.".format(
            profile_name, (time.monotonic() - profile_start) / 60.0))

    # Also saved on its own in results/ (versioned in git), like
    # tune_profiles.py's summaries.
    summary = io.StringIO()
    with contextlib.redirect_stdout(summary):
        print_final_report(results, errors, run_start)
    print(summary.getvalue(), end="")
    results_path = os.path.join(RESULTS_DIR, "{}_tune_testcard_{}.txt".format(
        run_stamp, "+".join(r["profile_name"] for r in results) or "none"))
    with open(results_path, "w") as f:
        f.write("Testcard image (hardest of all): {}\n".format(os.path.basename(testcard_path)))
        f.write(summary.getvalue())
    log("Summary saved to {}".format(results_path))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_trial":
        _trial_worker_main(sys.argv[2:])
    else:
        main()
