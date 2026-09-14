"""Process controller for the web-managed DATV engine.

The GStreamer worker runs in a separate process, driving datv_tx_plus.py's
real Pluto TX path (TX_OUTPUT="pluto") - starting a stream here keys up
Pluto and transmits.
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time

from dvbs2_profiles import CAMERA_VIDEO_PROFILE_NAMES, TESTCARD_PROFILE_NAMES
import usb_video_key

LOG_TAIL_CHARS = 4000
ORPHAN_STOP_TIMEOUT_SECONDS = 8
# AD9361 register supports -89 to 0 dB, but real spectrum-analyzer
# measurement (2026-09-10) showed no further measurable RF output change
# below -60 dB on this specific Pluto/antenna setup - see the comment on
# GAIN_DB in datv_tx_plus.py for the full story.
GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 0.0
# Matches the selectable uplink range in static/js/batc-spectrum.js
# (START_MHZ/END_MHZ minus TRANSPONDER_OFFSET_MHZ: 10490.5-8090 to
# 10499.5-8090) - the actual QO-100 wideband transponder uplink span.
FREQUENCY_MIN_HZ = 2400500000
FREQUENCY_MAX_HZ = 2409500000


def _validate_gain(gain_db):
    if not GAIN_MIN_DB <= gain_db <= GAIN_MAX_DB:
        raise ValueError("TX gain must be between {:.0f} and {:.0f} dB".format(
            GAIN_MIN_DB, GAIN_MAX_DB))


def _validate_frequency(frequency_hz):
    if not FREQUENCY_MIN_HZ <= frequency_hz <= FREQUENCY_MAX_HZ:
        raise ValueError("Frequency must be between {:.1f} and {:.1f} MHz".format(
            FREQUENCY_MIN_HZ / 1e6, FREQUENCY_MAX_HZ / 1e6))


class DatvEngine(object):
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.log_path = os.path.join(project_dir, "web_stream.log")
        self._lock = threading.Lock()
        self._process = None
        self._state = "stopped"
        self._last_error = None
        self._log_handle = None
        self._log_start_offset = 0
        self._kill_orphaned_workers()

    def _find_worker_pids(self):
        try:
            result = subprocess.run(
                ["pgrep", "-f", "datv_web_worker.py"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(pid) for pid in result.stdout.split()]

    def _kill_orphaned_workers(self):
        """Kill any datv_web_worker.py left running from a previous app.py
        instance.

        Restarting app.py resets this engine's own _process tracking to
        None, so without this check a leftover process from before the
        restart keeps silently transmitting/holding the Pluto MQTT
        connection, and the next stream start piles a SECOND process on
        top instead of detecting the conflict - both processes then fight
        over the same MQTT topics, which is what actually caused two
        separate "stream won't acknowledge/won't respond to the gain
        slider" mysteries in one session (2026-09-10), only found by hand
        over SSH both times. Runs once here, at __init__ time - any match
        found this early is guaranteed to predate this engine instance,
        since it hasn't spawned anything of its own yet.
        """
        pids = self._find_worker_pids()
        if not pids:
            return

        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGINT)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + ORPHAN_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self._find_worker_pids():
                return
            time.sleep(0.5)

        # Still alive after a graceful SIGINT and an 8s wait - escalate.
        for pid in self._find_worker_pids():
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _close_log(self):
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def _log_tail(self):
        # web_stream.log is opened in append mode across runs, so a plain
        # tail of the whole file could surface a stale error from a
        # previous attempt - only read what this run itself wrote.
        try:
            with open(self.log_path, "rb") as log_file:
                log_file.seek(self._log_start_offset)
                tail = log_file.read()
        except OSError:
            return ""
        return tail.decode("utf-8", "replace").strip()[-LOG_TAIL_CHARS:]

    def _status_unlocked(self):
        return {
            "state": self._state,
            "last_error": self._last_error,
            "rf_possible": True,
        }

    def status(self):
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                return_code = self._process.returncode
                self._process = None
                self._close_log()
                if self._state != "stopping":
                    self._state = "stopped" if return_code == 0 else "error"
                    if return_code != 0:
                        tail = self._log_tail()
                        self._last_error = "datv_tx_plus adapter exited with code {}.{}".format(
                            return_code, "\n" + tail if tail else " See web_stream.log.")
            return self._status_unlocked()

    def _launch(self, extra_args):
        """Shared subprocess-launch logic behind start_testcard()/
        start_camera()/start_video() - only the datv_web_worker.py argv
        differs between them, everything about actually spawning it,
        locking against a concurrent start, and tracking state is
        identical.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A stream is already running")

            command = [
                sys.executable,
                os.path.join(self.project_dir, "datv_web_worker.py"),
            ] + extra_args

            self._state = "starting"
            self._last_error = None
            try:
                self._log_start_offset = (
                    os.path.getsize(self.log_path) if os.path.isfile(self.log_path) else 0)
                self._log_handle = open(self.log_path, "ab")
                self._process = subprocess.Popen(
                    command,
                    cwd=self.project_dir,
                    stdout=self._log_handle,
                    stderr=self._log_handle,
                    start_new_session=True,
                )
            except OSError as exc:
                self._state = "error"
                self._last_error = str(exc)
                self._process = None
                self._close_log()
                raise RuntimeError(str(exc))
            self._state = "streaming"
            return self._status_unlocked()

    def start_testcard(self, testcard_name, symbol_rate, fec, gain_db, frequency_hz):
        try:
            profile_key = TESTCARD_PROFILE_NAMES[(symbol_rate, fec)]
        except KeyError:
            raise ValueError("Unsupported SR/FEC combination")
        _validate_gain(gain_db)
        _validate_frequency(frequency_hz)

        testcard_dir = os.path.abspath(os.path.join(self.project_dir, "testcards"))
        source_path = os.path.abspath(os.path.join(testcard_dir, testcard_name))
        if os.path.dirname(source_path) != testcard_dir:
            raise ValueError("Invalid testcard path")
        if not os.path.isfile(source_path):
            raise ValueError("Testcard not found")

        return self._launch([
            "--source", "testcard",
            "--testcard", source_path,
            "--profile", profile_key,
            "--gain", str(gain_db),
            "--frequency", str(frequency_hz),
        ])

    def start_camera(self, camera_device, camera_is_csi, audio_device, symbol_rate, fec,
                      gain_db, frequency_hz, top_banner, bottom_banner, marquee, marquee_text):
        try:
            profile_key = CAMERA_VIDEO_PROFILE_NAMES[(symbol_rate, fec)]
        except KeyError:
            raise ValueError("Unsupported SR/FEC combination")
        _validate_gain(gain_db)
        _validate_frequency(frequency_hz)
        if not camera_device or not audio_device:
            raise ValueError("Camera and audio device are required")

        return self._launch([
            "--source", "camera",
            "--profile", profile_key,
            "--gain", str(gain_db),
            "--frequency", str(frequency_hz),
            "--camera-device", camera_device,
            "--camera-is-csi", "1" if camera_is_csi else "0",
            "--audio-device", audio_device,
            "--top-banner", "1" if top_banner else "0",
            "--bottom-banner", "1" if bottom_banner else "0",
            "--marquee", "1" if marquee else "0",
            "--marquee-text", marquee_text,
        ])

    def start_video(self, video_path, symbol_rate, fec, gain_db, frequency_hz,
                     top_banner, bottom_banner, marquee, marquee_text):
        try:
            profile_key = CAMERA_VIDEO_PROFILE_NAMES[(symbol_rate, fec)]
        except KeyError:
            raise ValueError("Unsupported SR/FEC combination")
        _validate_gain(gain_db)
        _validate_frequency(frequency_hz)

        # video_path is an absolute path app.py already resolved and
        # whitelisted against its own all_preprocessed_videos() (SD card,
        # plus the USB video key when mounted - see usb_video_key.py).
        # Re-validated here too, since this is what actually reaches the
        # GStreamer subprocess - same defense-in-depth as
        # start_testcard()'s testcard_dir check above.
        source_path = os.path.abspath(str(video_path))
        folder_path = os.path.dirname(source_path)
        folder_name = os.path.basename(folder_path)
        allowed_roots = {os.path.abspath(self.project_dir)}
        usb_root = usb_video_key.mounted_root()
        if usb_root:
            allowed_roots.add(usb_root)
        if (not re.fullmatch(r"preprocessed_\d+x\d+", folder_name)
                or os.path.dirname(folder_path) not in allowed_roots):
            raise ValueError("Invalid video path")
        if not os.path.isfile(source_path):
            raise ValueError("Video not found")

        return self._launch([
            "--source", "video",
            "--profile", profile_key,
            "--gain", str(gain_db),
            "--frequency", str(frequency_hz),
            "--video", source_path,
            "--top-banner", "1" if top_banner else "0",
            "--bottom-banner", "1" if bottom_banner else "0",
            "--marquee", "1" if marquee else "0",
            "--marquee-text", marquee_text,
        ])

    def stop(self):
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._process = None
                self._close_log()
                self._state = "stopped"
                return self._status_unlocked()
            self._state = "stopping"

        os.killpg(os.getpgid(process.pid), signal.SIGINT)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait()

        with self._lock:
            self._process = None
            self._close_log()
            self._state = "stopped"
            return self._status_unlocked()
