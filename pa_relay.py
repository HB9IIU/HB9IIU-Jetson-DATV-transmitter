"""Operator-controlled PTT relay plus independent spectrum observation.

The PTT relay can be switched on whenever a stream is running. Spectrum
analysis is advisory display data only: it never enables, disables, blocks,
or switches the relay. Stopping or crashing the stream still switches the
relay off.

Lives in app.py's own long-running process (constructed once at Flask
startup - see app.py), NOT in the per-stream datv_web_worker.py/
datv_tx_plus.py subprocess: that subprocess is spawned fresh for every
stream and has no access to pluto_fft_bridge's live frames or to a
browser's confirm click - only app.py has all three things this state
machine needs (live FFT, live stream state, the Flask request carrying
operator confirmation) at once.

Constructed with getter callables rather than importing app.py directly, so
the dependency stays one-directional (this module never imports app) - the
same shape as datv_web_worker.py patching values into datv_tx_plus rather
than the reverse.

States: idle -> waiting -> ready -> engaged, plus a sticky fault state.
  idle:    no stream running.
  waiting: streaming, spectrum not yet a stable plateau.
  ready:   spectrum has held a stable plateau for several consecutive checks.
  engaged: operator confirmed; the relay is energized.
  fault:   a GPIO call failed. Sticky - stays until a fresh stream start
           gives it a clean slate (see tick()), never auto-clears while
           idle, so a real hardware problem doesn't just quietly vanish
           from the UI on its own.
Spectrum changes never alter an engaged relay. Stream stop/crash does.
"""

import logging
import threading
import time

import pa_relay_gpio
from pluto_signal_stability import evaluate_frame

log = logging.getLogger(__name__)

MONITOR_TICK_SECONDS = 0.25
# Debounce lives here, not in pluto_signal_stability.evaluate_frame() - that
# function is a stateless per-frame judgement; a single good-looking frame
# could just be noise that happened to line up. Debouncing keeps the
# advisory status label from changing too easily.
STABLE_CONSECUTIVE_FRAMES = 10   # ~2.5s at the tick rate above
UNSTABLE_CONSECUTIVE_FRAMES = 3  # ~0.75s


class RelayController(object):
    STATES = ("idle", "waiting", "ready", "engaged", "fault")

    def __init__(self, get_stream_state, get_latest_fft, get_span_hz, get_zoom_span_hz):
        self._get_stream_state = get_stream_state
        self._get_latest_fft = get_latest_fft
        self._get_span_hz = get_span_hz
        self._get_zoom_span_hz = get_zoom_span_hz

        self._lock = threading.Lock()
        self._state = "idle"
        self._good_streak = 0
        self._bad_streak = 0
        self._last_result = None  # most recent StabilityResult, for status()
        # Tracks whether the *previous* tick saw "streaming", not just the
        # current one - see tick()'s own comment on why this matters: a
        # bare "is stream_state == 'streaming' right now" check can't tell
        # "this is the same ongoing stream that already faulted" apart from
        # "a brand new stream just started", and only the latter should
        # ever clear a fault.
        self._was_streaming = False

    def status(self):
        with self._lock:
            result = self._last_result
            return {
                "state": self._state,
                "streaming": self._get_stream_state() == "streaming",
                "engaged": pa_relay_gpio.is_engaged(),
                "fault_reason": pa_relay_gpio.fault_reason() if self._state == "fault" else None,
                "detector": {
                    "is_plateau": result.is_plateau,
                    "margin_ratio": result.margin_ratio,
                    "coverage_ratio": result.coverage_ratio,
                    "reason": result.reason,
                } if result is not None else None,
            }

    def request_engage(self):
        """Operator-initiated while streaming; spectrum state is irrelevant. Returns
        (ok, state) so the Flask route can pick the right HTTP status."""
        with self._lock:
            if self._get_stream_state() != "streaming":
                return False, self._state
            if pa_relay_gpio.engage():
                self._state = "engaged"
                return True, self._state
            self._state = "fault"
            return False, self._state

    def request_disengage(self):
        """Operator-initiated manual e-stop - always allowed, from any
        state, no confirmation required (removing power is never gated).
        Targets "waiting" rather than deciding idle-vs-waiting itself -
        tick() re-derives the right state (idle if the stream has in fact
        already ended) within one tick regardless, so there's only one
        place that logic lives."""
        with self._lock:
            self._disengage_locked(next_state="waiting")
            return self._state

    def force_disengage_and_idle(self):
        """Called synchronously by app.py's stream_stop() - belt-and-
        suspenders alongside tick()'s own backstop (see tick() below),
        firing within the same request that stops the stream rather than
        waiting for the next monitor tick."""
        with self._lock:
            self._disengage_locked(next_state="idle")

    def _disengage_locked(self, next_state):
        if pa_relay_gpio.disengage():
            self._state = next_state
            self._good_streak = 0
            self._bad_streak = 0
        else:
            self._state = "fault"

    def tick(self):
        """Called by the background monitor thread every
        MONITOR_TICK_SECONDS. Self-healing by design - reacts to the
        stream engine's *actual* current state on every tick rather than
        relying solely on being told about start/stop, so a subprocess
        that dies without going through stream_stop() (detected via
        DatvEngine.status()'s own poll) still gets caught here.
        """
        with self._lock:
            stream_state = self._get_stream_state()
            is_streaming = stream_state == "streaming"

            if not is_streaming:
                if self._state in ("engaged", "ready", "waiting"):
                    self._disengage_locked(next_state="idle")
                    self._last_result = None
                # A "fault" state is left exactly as-is here on purpose -
                # it stays visible to the operator even after the stream
                # stops, until they deliberately start a new one (below),
                # rather than silently clearing in the background.
                self._was_streaming = False
                return

            if not self._was_streaming:
                # Genuine fresh start - the *previous* tick saw the stream
                # not running, this one does. Always begins from a clean
                # slate, including clearing any previous fault. Checking
                # self._was_streaming (not just "is self._state idle/fault
                # right now") is what stops an ongoing, still-faulted
                # stream from having its fault silently wiped on every
                # subsequent tick just because stream_state still reads
                # "streaming".
                pa_relay_gpio.clear_fault()
                self._state = "waiting"
                self._good_streak = 0
                self._bad_streak = 0
                self._last_result = None
            self._was_streaming = True

            bins = self._get_latest_fft()
            if bins is None:
                self._on_bad_frame()
                return

            result = evaluate_frame(bins, self._get_span_hz(), self._get_zoom_span_hz())
            self._last_result = result
            if result.is_plateau:
                self._on_good_frame()
            else:
                self._on_bad_frame()

    def _on_good_frame(self):
        self._good_streak += 1
        self._bad_streak = 0
        if self._state == "waiting" and self._good_streak >= STABLE_CONSECUTIVE_FRAMES:
            self._state = "ready"

    def _on_bad_frame(self):
        self._bad_streak += 1
        self._good_streak = 0
        if self._bad_streak < UNSTABLE_CONSECUTIVE_FRAMES:
            return
        if self._state == "ready":
            self._state = "waiting"
        # An engaged relay remains engaged: spectrum analysis is advisory
        # only. Stream stop/crash and the operator's OFF action still
        # disengage it.


_monitor_started = False


def start_background_monitor(controller):
    """Idempotent - same pattern as pluto_fft_bridge.start_background_reader()
    (safe against Flask's debug-mode reloader re-importing this module)."""
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True

    def _run_loop():
        while True:
            try:
                controller.tick()
            except Exception:
                log.exception("pa_relay monitor tick failed")
            time.sleep(MONITOR_TICK_SECONDS)

    threading.Thread(target=_run_loop, daemon=True).start()
