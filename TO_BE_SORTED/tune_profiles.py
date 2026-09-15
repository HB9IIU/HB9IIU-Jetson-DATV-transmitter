"""Find the highest safe video_bitrate_kbps for every DATV-Plus profile, by
actually measuring the hardware encoder instead of trusting its bitrate=
target.

Why this exists: nvv4l2h265enc's control-rate already defaults to
constant_bitrate, yet real hardware testing (2026-09-07) showed a 90s
real-motion clip encoded at sr500_fec34's configured 600kbps video / 32kbps
audio produced a REAL muxed bitrate of 728550 bit/s - already over that
profile's exact 726038 bit/s DVB-S2 TS capacity (calculate_dvbs2_ts_bitrate()
in datv_tx_plus.py), not under it as the configured numbers suggest. Worse,
the real overshoot turned out NOT to be a fixed ratio across profiles (some
profiles run ~15% over their target, others ~30-35%) - so a single guessed
correction factor would have been wrong for at least some profiles. The only
trustworthy way to pick a safe video_bitrate_kbps is to actually encode a
real clip and measure what comes out, per profile.

Method: bisection search on video_bitrate_kbps. Each trial encodes a fixed
local test clip through the real hardware H.265 encoder (datv_tx_plus.py's
own build_pipeline_description(), TX_OUTPUT="file", no Pluto/MQTT needed),
then measures the resulting .ts file's real bitrate with ffprobe. A trial is
"safe" if that real bitrate stays under SAFETY_MARGIN of the profile's exact
TS capacity. Converges on the highest safe bitrate, then reports a PSNR
comparison (via ffmpeg's psnr filter - libvmaf isn't available on this
Jetson's system ffmpeg or on the two static builds tried on 2026-09-07: one
had no libvmaf, the other needed a newer glibc than this L4T image has)
between the originally configured bitrate and the discovered one, against a
lossless reference scaled/frame-rated to match the profile (so the
comparison isolates encoding loss, not resize loss).

Each trial runs in its OWN subprocess (this same script, re-invoked with a
hidden --_trial flag), not inside this long-lived orchestrator process.
Real hardware testing (2026-09-07) crashed the whole run outright (SIGSEGV,
exit code 139) after ~16 in-process GStreamer/NVENC pipeline create/destroy
cycles - a native resource leak in the encoder driver that no Python
try/except can catch, since it kills the interpreter itself. A fresh
process per trial gives the hardware driver a clean slate every time, which
should prevent that leak from ever accumulating; each subprocess is also
retried a couple of times before being given up on, as insurance against
whatever crash still gets through.

Runs only on the Jetson (needs the real nvv4l2h265enc hardware encoder and
PyGObject) - not portable to the Windows dev machine.

Each trial runs at roughly real-clip-speed: the pipeline's two bar
videotestsrcs are is-live=true, which paces the whole pipeline to wall-clock
time even for file output. A ~90s test clip means ~90s per trial, so a full
bisection (a handful of trials) plus the two final PSNR comparison runs
takes on the order of 15-20 minutes per profile.

By default (PROFILES_TO_TUNE below left empty) loops through every profile
in datv_tx_plus.py's PROFILES automatically. With 8 profiles at ~15-20
minutes each, a full run is a multi-hour "start it and walk away" job. One
profile's failure (after exhausting retries) is logged and skipped, not
fatal to the rest.

Set PROFILES_TO_TUNE to a list of profile names to tune just those instead
- e.g. after changing a single profile's resolution, a quick sanity check
on that one profile alone (~15-20 minutes) is enough; you don't need to
re-run the other 7. A hand-edited constant rather than a command-line
argument, same convention as PROFILE/SOURCE in datv_tx_plus.py and
TEST_CLIPS below - so it's just as easy to run from an IDE's Run button as
from a terminal.

Usage: python tune_profiles.py
"""

import os
import re
import subprocess
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import datv_tx_plus as tx

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(SCRIPT_DIR, "tuning")
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg-static", "ffmpeg")
FFPROBE = os.path.join(SCRIPT_DIR, "ffmpeg-static", "ffprobe")

# Which profile(s) to tune - edit this directly, then just hit Run (see
# module docstring for why this is a constant, not a command-line
# argument). Empty list = every profile in PROFILES (multi-hour).
PROFILES_TO_TUNE = ["sr500_fec34"]

# Two content types tested so far turned out to matter (2026-09-07 finding:
# real encoder overshoot depends on motion complexity, not just SR/FEC) - a
# fast-motion movie clip vs. calmer "person talking at a desk" camera
# content could plausibly want different safe bitrates. Add a same-length
# excerpt per resolution (ffmpeg -ss N -t 90 -c copy on the matching
# preprocessed_WxHx file) for whichever content type you're testing, then
# point TEST_CLIPS at the right dict below - it's a one-line edit, not a
# command-line argument, same convention as PROFILE/SOURCE in
# datv_tx_plus.py.
TEST_CLIPS_MOVIE = {
    (640, 360): os.path.join(WORK_DIR, "test_clip_640x360_90s.mkv"),
    (960, 540): os.path.join(WORK_DIR, "test_clip_960x540_90s.mkv"),
    (1280, 720): os.path.join(WORK_DIR, "test_clip_1280x720_90s.mkv"),
}
TEST_CLIPS_CAMERA = {
    (640, 360): os.path.join(WORK_DIR, "camera_clip_640x360_90s.mkv"),
    (960, 540): os.path.join(WORK_DIR, "camera_clip_960x540_90s.mkv"),
    (1280, 720): os.path.join(WORK_DIR, "camera_clip_1280x720_90s.mkv"),
}
TEST_CLIPS = TEST_CLIPS_CAMERA

# How close to the profile's exact DVB-S2 TS capacity a trial's real
# measured bitrate is allowed to get. Not 1.0: the CBR relay downstream can
# only pad UNDER capacity with null packets, never trim OVER capacity, so a
# trial that measures exactly at capacity has zero real margin for the next
# clip's motion being slightly worse than this test clip's.
SAFETY_MARGIN = 0.97

BISECTION_TOLERANCE_KBPS = 5
FFPROBE_TIMEOUT_SECONDS = 15
# Generous relative to real trial length (encoding paces to wall-clock time
# - see module docstring) - this is a safety net against a genuinely stuck
# subprocess, not the expected run time.
TRIAL_TIMEOUT_SECONDS = 600
TRIAL_RETRY_LIMIT = 2


def log(message):
    # A wall-clock timestamp, not elapsed-since-start: this is a multi-hour
    # unattended run, so "what time did the last line print" is what a human
    # checking in on it actually needs, not a stopwatch reading.
    print("[{}] {}".format(time.strftime("%H:%M:%S"), message), flush=True)


def run_encode_trial(profile, video_bitrate_kbps, clip_path, out_ts_path):
    """Encode clip_path through the real pipeline at video_bitrate_kbps,
    writing out_ts_path, reusing datv_tx_plus.py's own pipeline builder with
    TX_OUTPUT="file" so no Pluto/MQTT is involved. Overlay is off - a
    telemetry/clock overlay burned into the picture would bias any quality
    comparison, and it's irrelevant to a bitrate-vs-capacity measurement.

    Only ever called from within _trial_worker_main(), i.e. inside the
    short-lived --_trial subprocess - never directly from the orchestrator.
    """
    trial_profile = dict(profile, video_bitrate_kbps=video_bitrate_kbps)
    # build_pipeline_description() reads SOURCE/TX_OUTPUT/TX_OUTPUT_FILE as
    # module globals (they're settings meant to be hand-edited for a full
    # transmit run, not function parameters) - setting them here is the same
    # thing a human editing datv_tx_plus.py before a debug run would do. Safe
    # to mutate: this process only ever runs one trial before exiting.
    # SOURCE must always be "video" here regardless of whatever
    # datv_tx_plus.py currently has on disk (e.g. left on "camera" from an
    # unrelated live-testing session) - a real bug caught 2026-09-07: without
    # this, a trial silently ignored clip_path and tried to open the
    # physical camera/mic instead of decoding the test clip.
    tx.SOURCE = "video"
    tx.TX_OUTPUT = "file"
    tx.TX_OUTPUT_FILE = out_ts_path

    Gst.init(None)
    pipeline_description = tx.build_pipeline_description(
        None, trial_profile, clip_path, top_bar_enabled=False, bottom_bar_enabled=False)
    pipeline = Gst.parse_launch(pipeline_description)
    video_source = pipeline.get_by_name("filesrc")
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)

    start = time.monotonic()
    try:
        while True:
            message = bus.timed_pop_filtered(
                int(1.0 * Gst.SECOND), Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError("GStreamer error: {} ({})".format(error, debug))
                return
            ok_dur, duration = video_source.query_duration(Gst.Format.TIME)
            ok_pos, position = video_source.query_position(Gst.Format.TIME)
            if (ok_dur and ok_pos and duration > 0
                    and position >= duration - int(1.0 * Gst.SECOND)):
                return
            if time.monotonic() - start > TRIAL_TIMEOUT_SECONDS:
                raise RuntimeError(
                    "trial exceeded {}s safety timeout".format(TRIAL_TIMEOUT_SECONDS))
    finally:
        pipeline.set_state(Gst.State.NULL)


def run_encode_trial_subprocess(profile_name, video_bitrate_kbps, clip_path, out_ts_path):
    """Runs one encode trial in a brand-new subprocess instead of inside
    this orchestrator - see the module docstring for why (a real SIGSEGV
    crash was observed after ~16 in-process pipeline cycles). Retries a
    couple of times before giving up, since a fresh process should already
    avoid most of the problem; this is just insurance for whatever gets
    through anyway (or a genuine one-off hardware hiccup).
    """
    command = [sys.executable, os.path.abspath(__file__), "--_trial",
               profile_name, str(video_bitrate_kbps), clip_path, out_ts_path]
    last_summary = None
    for attempt in range(1, TRIAL_RETRY_LIMIT + 1):
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=TRIAL_TIMEOUT_SECONDS + 30)
        if result.returncode == 0:
            return
        # A negative returncode means the subprocess was killed by a signal
        # (e.g. -11 = SIGSEGV) rather than exiting normally - worth saying
        # plainly, since "exit code -11" alone doesn't read as "it crashed".
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


def ensure_reference(clip_path, width, height, fps):
    """One lossless reference per (clip, resolution), reused across every
    bitrate trial for that profile - it doesn't depend on video_bitrate_kbps,
    only on the resolution/framerate the profile encodes at. Matches the
    exact scale+framerate the real pipeline applies before the H.265
    encoder, so the PSNR comparison isolates *encoding* loss, not a
    resize/frame-rate-conversion difference between reference and trial.
    """
    # Keyed by clip_path's own name, not just resolution/fps: without this,
    # switching TEST_CLIPS (e.g. movie -> camera) would silently reuse
    # whichever reference was built first, comparing new trials against
    # unrelated footage and producing meaningless PSNR numbers - a real bug
    # caught before it could do that (2026-09-07, right as camera clips were
    # being added).
    clip_stem = os.path.splitext(os.path.basename(clip_path))[0]
    ref_path = os.path.join(
        WORK_DIR, "reference_{}_{}x{}_{}fps.mkv".format(clip_stem, width, height, fps))
    if os.path.exists(ref_path):
        return ref_path
    log("Building PSNR reference at {}x{}@{}fps (one-time)...".format(width, height, fps))
    subprocess.run(
        [FFMPEG, "-y", "-i", clip_path,
         "-vf", "scale={}:{},fps={}".format(width, height, fps),
         "-c:v", "ffv1", "-an", ref_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True)
    return ref_path


def measure_psnr(ts_path, reference_path):
    """Average PSNR (dB) of ts_path's video against reference_path, via
    ffmpeg's built-in psnr filter - no libvmaf available on this Jetson (see
    module docstring), so this is the metric available without a from-source
    ffmpeg build."""
    result = subprocess.run(
        [FFMPEG, "-i", ts_path, "-i", reference_path,
         "-lavfi", "psnr", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        timeout=TRIAL_TIMEOUT_SECONDS)
    match = re.search(r"average:(\d+\.?\d*)", result.stderr)
    if not match:
        raise RuntimeError("Could not parse PSNR from ffmpeg output:\n" + result.stderr[-2000:])
    return float(match.group(1))


def find_max_safe_video_bitrate_kbps(profile_name):
    profile = tx.PROFILES[profile_name]
    width, height = profile["resolution"]
    clip_path = TEST_CLIPS.get((width, height))
    if clip_path is None or not os.path.exists(clip_path):
        raise SystemExit(
            "No test clip for {}x{} - add one to TEST_CLIPS (see module "
            "docstring for how the existing ones were made).".format(width, height))

    capacity_bps = tx.calculate_dvbs2_ts_bitrate(profile)
    ceiling_bps = capacity_bps * SAFETY_MARGIN
    audio_kbps = profile["audio_bitrate_kbps"]

    lo = (capacity_bps / 1000.0 - audio_kbps) * 0.5
    hi = capacity_bps / 1000.0 - audio_kbps
    log("Profile '{}': capacity={:.0f} bit/s, safety ceiling={:.0f} bit/s, "
        "searching video_bitrate_kbps in [{:.0f}, {:.0f}]".format(
            profile_name, capacity_bps, ceiling_bps, lo, hi))

    trial_path = os.path.join(WORK_DIR, "_bisect_trial.ts")
    best_kbps = None
    trial_num = 0
    while hi - lo > BISECTION_TOLERANCE_KBPS:
        trial_num += 1
        mid = round((lo + hi) / 2.0)
        log("  trial {}: encoding at video_bitrate_kbps={:.0f} "
            "(bracket [{:.0f}, {:.0f}])...".format(trial_num, mid, lo, hi))
        trial_start = time.monotonic()
        run_encode_trial_subprocess(profile_name, mid, clip_path, trial_path)
        real_bps = measure_bitrate_bps(trial_path)
        safe = real_bps <= ceiling_bps
        log("  trial {}: video_bitrate_kbps={:.0f} -> real {} bit/s ({}) [{:.0f}s]".format(
            trial_num, mid, real_bps, "safe" if safe else "OVER ceiling",
            time.monotonic() - trial_start))
        if safe:
            best_kbps = mid
            lo = mid
        else:
            hi = mid

    if best_kbps is None:
        raise SystemExit(
            "Even the lowest bracket ({:.0f} kbps) exceeded the safety ceiling - "
            "this profile's SR/FEC may not actually support its own resolution "
            "target on real content.".format(lo))
    return best_kbps, capacity_bps, clip_path


def tune_one_profile(profile_name):
    profile = tx.PROFILES[profile_name]
    width, height = profile["resolution"]
    original_kbps = profile["video_bitrate_kbps"]

    best_kbps, capacity_bps, clip_path = find_max_safe_video_bitrate_kbps(profile_name)

    log("Building before/after PSNR comparison...")
    reference_path = ensure_reference(clip_path, width, height, tx.FPS)

    original_ts = os.path.join(WORK_DIR, "_compare_original.ts")
    best_ts = os.path.join(WORK_DIR, "_compare_best.ts")
    run_encode_trial_subprocess(profile_name, original_kbps, clip_path, original_ts)
    run_encode_trial_subprocess(profile_name, best_kbps, clip_path, best_ts)

    original_real_bps = measure_bitrate_bps(original_ts)
    best_real_bps = measure_bitrate_bps(best_ts)
    original_psnr = measure_psnr(original_ts, reference_path)
    best_psnr = measure_psnr(best_ts, reference_path)

    print()
    print("=" * 62)
    print("Profile '{}' ({}x{}), DVB-S2 TS capacity = {:.0f} bit/s".format(
        profile_name, width, height, capacity_bps))
    print("-" * 62)
    print("  currently configured : {:4d} kbps -> real {:7d} bit/s, PSNR {:.2f} dB".format(
        original_kbps, original_real_bps, original_psnr))
    print("  found safe optimum   : {:4.0f} kbps -> real {:7d} bit/s, PSNR {:.2f} dB".format(
        best_kbps, best_real_bps, best_psnr))
    print("=" * 62)
    if best_kbps > original_kbps:
        print("-> raise video_bitrate_kbps for '{}' to {:.0f} in PROFILES "
              "(datv_tx_plus.py) for better picture quality at the same "
              "safe margin.".format(profile_name, best_kbps))
    elif best_kbps < original_kbps:
        print("-> LOWER video_bitrate_kbps for '{}' to {:.0f} in PROFILES - "
              "the current value is not safely under this profile's real "
              "capacity on real motion content.".format(profile_name, best_kbps))
    else:
        print("-> currently configured value is already the safe optimum.")

    return {
        "profile_name": profile_name,
        "original_kbps": original_kbps,
        "best_kbps": best_kbps,
        "capacity_bps": capacity_bps,
        "original_real_bps": original_real_bps,
        "best_real_bps": best_real_bps,
        "original_psnr": original_psnr,
        "best_psnr": best_psnr,
    }


def print_final_report(results, errors, run_start):
    total_min = (time.monotonic() - run_start) / 60.0

    print()
    print("#" * 62)
    print("SUMMARY - all profiles ({:.0f} min total)".format(total_min))
    print("#" * 62)
    for r in results:
        arrow = ("raise to" if r["best_kbps"] > r["original_kbps"] else
                  "LOWER to" if r["best_kbps"] < r["original_kbps"] else "keep at")
        print("  {:22s} {:4d} kbps -> {} {:4.0f} kbps".format(
            r["profile_name"], r["original_kbps"], arrow, r["best_kbps"]))
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
        overshoots = [(r["original_real_bps"] - r["capacity_bps"]) / r["capacity_bps"] * 100
                      for r in over_capacity]
        print("  {} profile(s) are configured OVER their real DVB-S2 capacity "
              "on real motion content:".format(len(over_capacity)))
        for r, pct in zip(over_capacity, overshoots):
            print("    - {}: real output {:.1f}% over capacity ({} vs {} bit/s)".format(
                r["profile_name"], pct, r["original_real_bps"], int(r["capacity_bps"])))
        print("  Overshoot is NOT a fixed ratio across profiles (ranged {:.0f}%-{:.0f}% "
              "here) - confirms this needs per-profile measurement, not one guessed "
              "correction factor.".format(min(overshoots), max(overshoots)))
    if has_headroom:
        print("  {} profile(s) have real spare capacity and could go higher for "
              "better quality:".format(len(has_headroom)))
        for r in has_headroom:
            print("    - {}: {} -> {:.0f} kbps".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
    if already_optimal:
        print("  {} profile(s) already at their safe optimum: {}".format(
            len(already_optimal), ", ".join(r["profile_name"] for r in already_optimal)))
    if results:
        psnr_costs = [r["original_psnr"] - r["best_psnr"] for r in over_capacity]
        if psnr_costs:
            print("  Quality cost of the corrections: {:.2f} dB PSNR on average "
                  "(max {:.2f} dB) - consistently small.".format(
                      sum(psnr_costs) / len(psnr_costs), max(psnr_costs)))

    print()
    print("ACTIONS TO BE TAKEN")
    print("-" * 62)
    changed = over_capacity + has_headroom
    if not changed:
        print("  None - every tested profile's configured bitrate is already safe.")
    else:
        print("  Edit PROFILES in datv_tx_plus.py:")
        for r in changed:
            print("    \"{}\": video_bitrate_kbps {} -> {:.0f}".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
        print("  datv_tx_plus_fft.py has its own separate copy of PROFILES - "
              "mirror the same changes there if you use that script too.")
    if errors:
        print("  Investigate/re-run failed profile(s): {}".format(
            ", ".join(name for name, _ in errors)))


def _trial_worker_main(argv):
    """Entry point when this script is re-invoked as a --_trial subprocess
    (see run_encode_trial_subprocess()) - never called directly by a user.
    """
    profile_name, video_bitrate_kbps, clip_path, out_ts_path = (
        argv[0], int(argv[1]), argv[2], argv[3])
    profile = tx.PROFILES[profile_name]
    run_encode_trial(profile, video_bitrate_kbps, clip_path, out_ts_path)


def main():
    os.makedirs(WORK_DIR, exist_ok=True)

    if PROFILES_TO_TUNE:
        unknown = [name for name in PROFILES_TO_TUNE if name not in tx.PROFILES]
        if unknown:
            raise SystemExit("Unknown profile name(s) in PROFILES_TO_TUNE: {} - available: {}".format(
                ", ".join(unknown), ", ".join(tx.PROFILES)))
        profile_names = PROFILES_TO_TUNE
    else:
        profile_names = list(tx.PROFILES)

    results = []
    errors = []
    run_start = time.monotonic()
    for i, profile_name in enumerate(profile_names, start=1):
        log("=== Profile {}/{}: '{}' (run elapsed so far: {:.0f} min) ===".format(
            i, len(profile_names), profile_name, (time.monotonic() - run_start) / 60.0))
        profile_start = time.monotonic()
        try:
            results.append(tune_one_profile(profile_name))
        except (Exception, SystemExit) as exc:
            # Broad on purpose: this loop is meant to survive a bad profile
            # (exhausted trial retries, a disk hiccup, an unparseable PSNR
            # result) without losing the other 7 - the whole point of an
            # unattended multi-hour run is that one failure shouldn't cost
            # you everything that already succeeded.
            log("  FAILED after {:.0f}s: {}".format(
                time.monotonic() - profile_start, exc))
            errors.append((profile_name, str(exc)))
            continue
        log("  Profile '{}' done in {:.0f} min.".format(
            profile_name, (time.monotonic() - profile_start) / 60.0))

    print_final_report(results, errors, run_start)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_trial":
        _trial_worker_main(sys.argv[2:])
    else:
        main()
