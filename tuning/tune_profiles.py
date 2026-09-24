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
TS capacity AND (added 2026-09-23) the .ts passes through the exact on-air
CBR relay command (datv_tx_plus.build_cbr_relay_command()) with zero
"dts < pcr" warnings at some muxdelay in MUXDELAY_CANDIDATES - the average
check alone missed short-term keyframe bursts that the relay can't
schedule in time, which is what the receiver actually suffers from. Converges on the highest safe bitrate, then reports a PSNR
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

import contextlib
import io
import os
import re
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
import overlay_settings  # noqa: E402

CLIPS_DIR = os.path.join(SCRIPT_DIR, "clips")
# PSNR references are cached here across runs; each run's trial files and
# log go in their own dated RUN_DIR subfolder (set in main()).
WORK_DIR = os.path.join(SCRIPT_DIR, "runs")
RUN_DIR = None
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")
# ffmpeg-static/ isn't on the Jetson (checked 2026-09-23) - fall back to the
# system ffmpeg/ffprobe (3.4) rather than failing on a missing path.
_STATIC_FFMPEG_DIR = os.path.join(PROJECT_DIR, "ffmpeg-static")
if os.path.isdir(_STATIC_FFMPEG_DIR):
    FFMPEG = os.path.join(_STATIC_FFMPEG_DIR, "ffmpeg")
    FFPROBE = os.path.join(_STATIC_FFMPEG_DIR, "ffprobe")
else:
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"

# Which profile(s) to tune - edit this directly, then just hit Run (see
# module docstring for why this is a constant, not a command-line
# argument). Empty list = every profile in PROFILES (multi-hour).
# 2026-09-23 overnight run: the 6 camera/video profiles against the new,
# harder room-scene benchmark clip (the testcard "_720p" profiles don't
# carry camera content, so they're left out).
PROFILES_TO_TUNE = ["sr250_fec23", "sr250_fec34", "sr333_fec23", "sr333_fec34",
                    "sr500_fec23", "sr500_fec34"]

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
    (640, 360): os.path.join(CLIPS_DIR, "test_clip_640x360_90s.mkv"),
    (960, 540): os.path.join(CLIPS_DIR, "test_clip_960x540_90s.mkv"),
    (1280, 720): os.path.join(CLIPS_DIR, "test_clip_1280x720_90s.mkv"),
}
# Produced by record_benchmark_clip.py.
TEST_CLIPS_CAMERA = {
    (640, 360): os.path.join(CLIPS_DIR, "camera_clip_640x360_90s.mkv"),
    (960, 540): os.path.join(CLIPS_DIR, "camera_clip_960x540_90s.mkv"),
    (1280, 720): os.path.join(CLIPS_DIR, "camera_clip_1280x720_90s.mkv"),
}
TEST_CLIPS = TEST_CLIPS_CAMERA

# How close to the profile's exact DVB-S2 TS capacity a trial's real
# measured bitrate is allowed to get. Not 1.0: the CBR relay downstream can
# only pad UNDER capacity with null packets, never trim OVER capacity, so a
# trial that measures exactly at capacity has zero real margin for the next
# clip's motion being slightly worse than this test clip's.
SAFETY_MARGIN = 0.97

# Encode trials with the banners/marquee exactly as on air (the Setup page's
# saved overlay_settings.json, camera_banner_marquee.yaml styling, live
# telemetry text). 2026-09-23: overlay-free trials showed 0 relay warnings
# at muxdelay 0.5s while the same profile on air (overlays on) still gave
# ~300 - burned-in text changes the encoder's bursts, so leave this on for
# bitrate/muxdelay tuning. PSNR is then measured against an overlay-free
# reference, so its absolute value drops a little; the configured-vs-optimum
# comparison stays fair (both rows carry the same overlays).
TRIAL_OVERLAYS = True

# Relay -muxdelay values tried per trial, smallest first (see
# datv_tx_plus.CBR_MUXDELAY_SECONDS). A trial only counts as safe if one of
# these gives ZERO "dts < pcr" warnings. 1.5s added 2026-09-23: the on-air
# room scene already needed 1.0s, so a harder clip may need a little more -
# capping there still keeps the extra end-to-end latency sensible rather
# than hiding a real overload behind a huge buffer (past 1.5s the tuner
# lowers the bitrate instead).
MUXDELAY_CANDIDATES = [0.2, 0.3, 0.5, 0.7, 1.0, 1.5]
RELAY_TIMEOUT_SECONDS = 120

BISECTION_TOLERANCE_KBPS = 5
FFPROBE_TIMEOUT_SECONDS = 15
# Generous relative to real trial length (encoding paces to wall-clock time
# - see module docstring) - this is a safety net against a genuinely stuck
# subprocess, not the expected run time.
TRIAL_TIMEOUT_SECONDS = 600
TRIAL_RETRY_LIMIT = 2


class _Tee:
    """Minimal stdout splitter: console plus the run's log file."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def log(message):
    # A wall-clock timestamp, not elapsed-since-start: this is a multi-hour
    # unattended run, so "what time did the last line print" is what a human
    # checking in on it actually needs, not a stopwatch reading.
    print("[{}] {}".format(time.strftime("%H:%M:%S"), message), flush=True)


def run_encode_trial(profile, video_bitrate_kbps, clip_path, out_ts_path):
    """Encode clip_path through the real pipeline at video_bitrate_kbps,
    writing out_ts_path, reusing datv_tx_plus.py's own pipeline builder with
    TX_OUTPUT="file" so no Pluto/MQTT is involved. Banners/marquee follow
    TRIAL_OVERLAYS (see there for why they matter).

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
    if TRIAL_OVERLAYS:
        # Same on/off switches and texts the web app passes to
        # datv_web_worker.py from the Setup page.
        overlay_settings.init(PROJECT_DIR)
        settings = overlay_settings.load()
        top_bar_enabled = settings["top_banner"]
        bottom_bar_enabled = settings["bottom_banner"]
        tx.MARQUEE_ENABLED = settings["marquee"]
        tx.TITLE_TEXT_OVERRIDE = settings["top_banner_text"] or None
        tx.MARQUEE_TEXT_OVERRIDE = settings["marquee_text"] or None
        # SOURCE="video" would restyle from video_banner_marquee.yaml, but
        # the benchmark clips are camera footage - keep the camera styling.
        tx.load_video_banner_marquee_config = (
            lambda source_path: tx.load_camera_banner_marquee_config())
    else:
        top_bar_enabled = bottom_bar_enabled = False
        tx.MARQUEE_ENABLED = False

    Gst.init(None)
    pipeline_description = tx.build_pipeline_description(
        None, trial_profile, clip_path, top_bar_enabled, bottom_bar_enabled)
    pipeline = Gst.parse_launch(pipeline_description)
    # Same hookups as datv_tx_plus.main() - each only exists if enabled.
    width, height = profile["resolution"]
    marquee_overlay = pipeline.get_by_name("marquee_overlay")
    if marquee_overlay is not None:
        marquee_overlay.connect("draw", tx.draw_marquee, width, height,
                                {"first_timestamp": None, "text_width": None})
    telemetry_overlay = pipeline.get_by_name("telemetry_overlay")
    # No Pluto here - fill in plausible values; the Jetson's own CPU
    # temp/load in the string are live, just like on air.
    fake_telemetry = {"temperature_ad": "45000",
                      "tx/dvbs2/ts/bitrate": str(int(tx.calculate_dvbs2_ts_bitrate(profile)))}
    last_telemetry_update = 0.0
    video_source = pipeline.get_by_name("filesrc")
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)

    start = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if (telemetry_overlay is not None
                    and now - last_telemetry_update >= tx.TELEMETRY_UPDATE_SECONDS):
                telemetry_overlay.set_property("text", tx.format_telemetry(fake_telemetry))
                last_telemetry_update = now
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


def count_relay_warnings(ts_path, capacity_bps, muxdelay_seconds):
    """Push ts_path through the exact on-air CBR relay command (file in,
    /dev/null out instead of UDP) and count its "dts < pcr" warnings.

    The relay's mux decision depends only on timestamps and bytes written,
    not on arrival timing, so a file reproduces the on-air result - checked
    2026-09-22: muxdelay 0 gave ~18 warnings/s offline vs ~40/s on air, both
    gone at 0.2+.
    """
    command = tx.build_cbr_relay_command(ts_path, os.devnull, capacity_bps, muxdelay_seconds)
    command[0] = FFMPEG
    # -y: ffmpeg otherwise refuses to "overwrite" /dev/null (UDP on air
    # never needs it).
    command.insert(1, "-y")
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=RELAY_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError("CBR relay check failed:\n" + result.stderr[-2000:])
    # ffmpeg collapses repeats into "Last message repeated N times".
    count = 0
    previous_was_dts = False
    for line in result.stderr.splitlines():
        if "dts < pcr" in line:
            count += 1
            previous_was_dts = True
            continue
        match = re.search(r"Last message repeated (\d+) times", line)
        if match and previous_was_dts:
            count += int(match.group(1))
        previous_was_dts = False
    return count


def find_min_clean_muxdelay(ts_path, capacity_bps):
    """Smallest MUXDELAY_CANDIDATES value giving zero relay warnings, or
    None if even the largest doesn't. Also returns {muxdelay: warnings} for
    every value tried, for the log."""
    tried = {}
    for muxdelay in MUXDELAY_CANDIDATES:
        tried[muxdelay] = count_relay_warnings(ts_path, capacity_bps, muxdelay)
        if tried[muxdelay] == 0:
            return muxdelay, tried
    return None, tried


def format_tried(tried):
    return ", ".join("{}s:{}".format(d, n) for d, n in tried.items())


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

    trial_path = os.path.join(RUN_DIR, "{}_bisect_trial.ts".format(profile_name))
    best_kbps = None
    best_muxdelay = None
    trial_num = 0
    while hi - lo > BISECTION_TOLERANCE_KBPS:
        trial_num += 1
        mid = round((lo + hi) / 2.0)
        log("  trial {}: encoding at video_bitrate_kbps={:.0f} "
            "(bracket [{:.0f}, {:.0f}])...".format(trial_num, mid, lo, hi))
        trial_start = time.monotonic()
        run_encode_trial_subprocess(profile_name, mid, clip_path, trial_path)
        real_bps = measure_bitrate_bps(trial_path)
        fits = real_bps <= ceiling_bps
        # No point checking relay timing if the average already doesn't fit.
        muxdelay, tried = find_min_clean_muxdelay(trial_path, capacity_bps) if fits else (None, {})
        safe = fits and muxdelay is not None
        if not fits:
            verdict = "OVER ceiling"
        elif muxdelay is None:
            verdict = "relay timing FAILS up to {}s ({})".format(
                MUXDELAY_CANDIDATES[-1], format_tried(tried))
        else:
            verdict = "safe, clean from muxdelay {}s ({})".format(muxdelay, format_tried(tried))
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
            "target on real content.".format(lo))
    return best_kbps, best_muxdelay, capacity_bps, clip_path


def tune_one_profile(profile_name):
    profile = tx.PROFILES[profile_name]
    width, height = profile["resolution"]
    original_kbps = profile["video_bitrate_kbps"]

    best_kbps, best_muxdelay, capacity_bps, clip_path = (
        find_max_safe_video_bitrate_kbps(profile_name))

    log("Building before/after PSNR comparison...")
    reference_path = ensure_reference(clip_path, width, height, tx.FPS)

    original_ts = os.path.join(RUN_DIR, "{}_compare_original.ts".format(profile_name))
    best_ts = os.path.join(RUN_DIR, "{}_compare_best.ts".format(profile_name))
    run_encode_trial_subprocess(profile_name, original_kbps, clip_path, original_ts)
    run_encode_trial_subprocess(profile_name, best_kbps, clip_path, best_ts)

    original_real_bps = measure_bitrate_bps(original_ts)
    best_real_bps = measure_bitrate_bps(best_ts)
    original_psnr = measure_psnr(original_ts, reference_path)
    best_psnr = measure_psnr(best_ts, reference_path)
    # The configuration as it goes on air today (configured bitrate at the
    # current relay muxdelay) - should reproduce what the on-air log shows,
    # which is the sanity check that this offline method is trustworthy.
    on_air_muxdelay = tx.CBR_MUXDELAY_SECONDS
    original_on_air_warnings = count_relay_warnings(original_ts, capacity_bps, on_air_muxdelay)
    original_muxdelay, original_tried = find_min_clean_muxdelay(original_ts, capacity_bps)

    def pct(bps):
        return bps / capacity_bps * 100.0

    print()
    print("=" * 70)
    print("Profile '{}' ({}x{}), DVB-S2 TS capacity = {:.0f} bit/s".format(
        profile_name, width, height, capacity_bps))
    print("-" * 70)
    print("  currently configured : {:4d} kbps -> real {:7d} bit/s ({:.1f}%), PSNR {:.2f} dB".format(
        original_kbps, original_real_bps, pct(original_real_bps), original_psnr))
    print("      relay @ on-air muxdelay {}s: {} warnings over the clip; clean from: {}".format(
        on_air_muxdelay, original_on_air_warnings,
        "{}s".format(original_muxdelay) if original_muxdelay is not None
        else "never (tried {})".format(format_tried(original_tried))))
    print("  found safe optimum   : {:4.0f} kbps -> real {:7d} bit/s ({:.1f}%), PSNR {:.2f} dB".format(
        best_kbps, best_real_bps, pct(best_real_bps), best_psnr))
    print("      relay clean from muxdelay {}s".format(best_muxdelay))
    print("=" * 70)

    return {
        "profile_name": profile_name,
        "original_kbps": original_kbps,
        "best_kbps": best_kbps,
        "best_muxdelay": best_muxdelay,
        "capacity_bps": capacity_bps,
        "original_real_bps": original_real_bps,
        "best_real_bps": best_real_bps,
        "original_psnr": original_psnr,
        "best_psnr": best_psnr,
        "original_on_air_warnings": original_on_air_warnings,
        "original_muxdelay": original_muxdelay,
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
        # "Too high" means over the SAFETY_MARGIN ceiling or no clean relay
        # muxdelay - not necessarily over the capacity itself, so show the
        # real share of capacity rather than calling it an overshoot.
        print("  {} profile(s) are configured too high on real motion content "
              "(over the {:.0f}% safety limit, or relay not clean):".format(
                  len(over_capacity), SAFETY_MARGIN * 100))
        for r in over_capacity:
            print("    - {}: real output {:.1f}% of capacity ({} of {} bit/s)".format(
                r["profile_name"], r["original_real_bps"] / r["capacity_bps"] * 100,
                r["original_real_bps"], int(r["capacity_bps"])))
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
    if not results:
        print("  None - no profile was measured successfully.")
    elif not changed:
        print("  None - every tested profile's configured bitrate is already safe.")
    else:
        print("  Edit PROFILES in dvbs2_profiles.py:")
        for r in changed:
            print("    \"{}\": video_bitrate_kbps {} -> {:.0f}".format(
                r["profile_name"], r["original_kbps"], r["best_kbps"]))
    if results:
        # The relay has a single muxdelay for every profile, so it has to
        # cover the most demanding one.
        needed = max(r["best_muxdelay"] for r in results)
        # Only ever suggest raising it: a clip is at best an estimate of
        # real content - 2026-09-23 a recorded on-air room scene still
        # needed 1.0s where the benchmark clip was clean at 0.7s.
        if needed > tx.CBR_MUXDELAY_SECONDS:
            print("  Set CBR_MUXDELAY_SECONDS in datv_tx_plus.py: {} -> {} "
                  "(largest minimum among tested profiles).".format(
                      tx.CBR_MUXDELAY_SECONDS, needed))
        else:
            print("  CBR_MUXDELAY_SECONDS ({}s) already covers every tested profile "
                  "(clip needs >= {}s; keep the margin for real content).".format(
                      tx.CBR_MUXDELAY_SECONDS, needed))
    if errors:
        print("  Investigate/re-run failed profile(s): {}".format(
            ", ".join(name for name, _ in errors)))


def describe_overlays():
    if not TRIAL_OVERLAYS:
        return "off"
    overlay_settings.init(PROJECT_DIR)
    settings = overlay_settings.load()
    return "as on air - top banner {}, bottom banner {}, marquee {}".format(
        *("on" if settings[key] else "off"
          for key in ("top_banner", "bottom_banner", "marquee")))


def _trial_worker_main(argv):
    """Entry point when this script is re-invoked as a --_trial subprocess
    (see run_encode_trial_subprocess()) - never called directly by a user.
    """
    profile_name, video_bitrate_kbps, clip_path, out_ts_path = (
        argv[0], int(argv[1]), argv[2], argv[3])
    profile = tx.PROFILES[profile_name]
    run_encode_trial(profile, video_bitrate_kbps, clip_path, out_ts_path)


def main():
    global RUN_DIR
    run_stamp = time.strftime("%Y-%m-%d_%H%M")
    RUN_DIR = os.path.join(WORK_DIR, run_stamp)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    # Everything printed also goes to the run's own log file.
    sys.stdout = _Tee(sys.stdout, open(os.path.join(RUN_DIR, "tune.log"), "w"))
    log("Run folder: {}".format(RUN_DIR))
    log("Clips: {}".format(", ".join(os.path.basename(p) for p in TEST_CLIPS.values())))
    log("Overlays (banners/marquee): {}".format(describe_overlays()))

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

    # The summary is also saved on its own in results/ (versioned in git),
    # so the history of what was measured survives runs/ being cleaned out.
    summary = io.StringIO()
    with contextlib.redirect_stdout(summary):
        print_final_report(results, errors, run_start)
    print(summary.getvalue(), end="")
    results_path = os.path.join(RESULTS_DIR, "{}_tune_profiles_{}.txt".format(
        run_stamp, "+".join(r["profile_name"] for r in results) or "none"))
    with open(results_path, "w") as f:
        f.write("Clips: {}\n".format(", ".join(os.path.basename(p) for p in TEST_CLIPS.values())))
        f.write("Overlays (banners/marquee): {}\n".format(describe_overlays()))
        f.write(summary.getvalue())
    log("Summary saved to {}".format(results_path))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_trial":
        _trial_worker_main(sys.argv[2:])
    else:
        main()
