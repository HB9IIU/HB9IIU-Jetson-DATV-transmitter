"""Process controller for the web-managed DATV engine.

The GStreamer worker runs in a separate process. Milestone 2 remains strictly
local-file-only: this module and its worker contain no Pluto or RF output path.
"""

import datetime
import os
import signal
import subprocess
import sys
import threading

from dvbs2_profiles import PROFILES


class DatvEngine(object):
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self._lock = threading.Lock()
        self._process = None
        self._state = "stopped"
        self._last_error = None
        self._output_file = None
        self._log_handle = None

    def _close_log(self):
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def _status_unlocked(self):
        return {
            "state": self._state,
            "last_error": self._last_error,
            "output_file": self._output_file,
            "rf_possible": False,
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
                        self._last_error = "datv_tx_plus adapter exited with code {}. See web_stream.log.".format(return_code)
            return self._status_unlocked()

    def start_testcard(self, testcard_name, symbol_rate, fec):
        profile_key = "sr{}_fec{}".format(symbol_rate, fec.replace("/", ""))
        if symbol_rate == 500:
            profile_key += "_720p"
        if profile_key not in PROFILES:
            raise ValueError("Unsupported SR/FEC combination")

        testcard_dir = os.path.abspath(os.path.join(self.project_dir, "testcards"))
        source_path = os.path.abspath(os.path.join(testcard_dir, testcard_name))
        if os.path.dirname(source_path) != testcard_dir:
            raise ValueError("Invalid testcard path")
        if not os.path.isfile(source_path):
            raise ValueError("Testcard not found")

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A stream is already running")

            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            output_name = "web_test_output_{}.ts".format(timestamp)
            output_path = os.path.join(self.project_dir, output_name)
            command = [
                sys.executable,
                os.path.join(self.project_dir, "datv_web_worker.py"),
                "--testcard", source_path,
                "--output", output_path,
                "--profile", profile_key,
            ]

            self._state = "starting"
            self._last_error = None
            self._output_file = output_name
            try:
                self._log_handle = open(os.path.join(self.project_dir, "web_stream.log"), "ab")
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
