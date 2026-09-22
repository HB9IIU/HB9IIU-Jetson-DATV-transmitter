"""RelayController's state-machine transitions, fully isolated from real
hardware and real FFT data: pa_relay_gpio.engage()/disengage() and
pluto_signal_stability.evaluate_frame() (imported into pa_relay's own
namespace) are monkeypatched, and the stream-state/FFT getters are plain
closures over a small mutable harness so each test can just flip
.stream_state / .bins and call .tick().
"""

import pa_relay
import pa_relay_gpio
from pluto_signal_stability import StabilityResult

GOOD_RESULT = StabilityResult(True, 0.8, 0.3, "stable plateau")
BAD_RESULT = StabilityResult(False, 0.05, 0.0, "no clear plateau above noise floor")


class Harness:
    """Bundles a RelayController with mutable fake getters. self.bins is a
    placeholder only when evaluate_frame is monkeypatched (which every test
    below does) - its actual content never matters, only whether it's None
    (simulating "no FFT frame available")."""

    def __init__(self):
        self.stream_state = "streaming"
        self.bins = [1] * 32
        self.relay = pa_relay.RelayController(
            get_stream_state=lambda: self.stream_state,
            get_latest_fft=lambda: self.bins,
            get_span_hz=lambda: None,
            get_zoom_span_hz=lambda: None,
        )

    def tick(self, times=1):
        for _ in range(times):
            self.relay.tick()

    def state(self):
        return self.relay.status()["state"]


def _stub_evaluate(monkeypatch, result):
    monkeypatch.setattr(pa_relay, "evaluate_frame",
                         lambda bins, span_hz, zoom_span_hz: result)


def _stub_gpio(monkeypatch, engage_ok=True, disengage_ok=True):
    calls = {"engage": 0, "disengage": 0}

    def fake_engage():
        calls["engage"] += 1
        return engage_ok

    def fake_disengage():
        calls["disengage"] += 1
        return disengage_ok

    monkeypatch.setattr(pa_relay_gpio, "engage", fake_engage)
    monkeypatch.setattr(pa_relay_gpio, "disengage", fake_disengage)
    monkeypatch.setattr(pa_relay_gpio, "fault_reason", lambda: "simulated GPIO failure")
    monkeypatch.setattr(pa_relay_gpio, "clear_fault", lambda: None)
    return calls


def _reach_ready(harness, monkeypatch):
    _stub_evaluate(monkeypatch, GOOD_RESULT)
    harness.tick(times=pa_relay.STABLE_CONSECUTIVE_FRAMES)
    assert harness.state() == "ready"


def test_idle_while_no_stream_is_running(monkeypatch):
    _stub_gpio(monkeypatch)
    _stub_evaluate(monkeypatch, GOOD_RESULT)
    harness = Harness()
    harness.stream_state = "stopped"
    harness.tick()
    assert harness.state() == "idle"


def test_waiting_then_ready_after_enough_good_frames(monkeypatch):
    _stub_gpio(monkeypatch)
    _stub_evaluate(monkeypatch, GOOD_RESULT)
    harness = Harness()

    harness.tick()  # 1st tick: idle -> waiting, and counts as 1 good frame
    assert harness.state() == "waiting"

    harness.tick(times=pa_relay.STABLE_CONSECUTIVE_FRAMES - 2)  # streak -> 9
    assert harness.state() == "waiting"

    harness.tick()  # streak -> 10
    assert harness.state() == "ready"


def test_ready_never_auto_engages(monkeypatch):
    _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    # tick() alone never moves past "ready" - only an explicit
    # request_engage() call does (see pa_relay.py's own docstring: this is
    # a deliberate human-in-the-loop gate).
    harness.tick(times=5)
    assert harness.state() == "ready"


def test_operator_confirm_engages_the_relay(monkeypatch):
    calls = _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)

    ok, state = harness.relay.request_engage()
    assert ok is True
    assert state == "engaged"
    assert calls["engage"] == 1


def test_ready_drops_back_to_waiting_on_bad_frames(monkeypatch):
    _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)

    _stub_evaluate(monkeypatch, BAD_RESULT)
    harness.tick(times=pa_relay.UNSTABLE_CONSECUTIVE_FRAMES)
    assert harness.state() == "waiting"


def test_engaged_auto_disengages_on_signal_loss(monkeypatch):
    calls = _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()
    assert harness.state() == "engaged"

    _stub_evaluate(monkeypatch, BAD_RESULT)
    harness.tick(times=pa_relay.UNSTABLE_CONSECUTIVE_FRAMES)
    # This is the actual point of the whole feature: a degraded signal
    # after arming must cut power automatically, not wait for a human.
    assert harness.state() == "waiting"
    assert calls["disengage"] == 1


def test_engaged_disengages_when_stream_stops(monkeypatch):
    calls = _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()

    harness.stream_state = "stopped"
    harness.tick()
    assert harness.state() == "idle"
    assert calls["disengage"] == 1


def test_missing_fft_frame_counts_as_a_bad_frame(monkeypatch):
    _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)

    harness.bins = None
    harness.tick(times=pa_relay.UNSTABLE_CONSECUTIVE_FRAMES)
    assert harness.state() == "waiting"


def test_engage_failure_goes_to_fault_and_reports_reason(monkeypatch):
    _stub_gpio(monkeypatch, engage_ok=False)
    harness = Harness()
    _reach_ready(harness, monkeypatch)

    ok, state = harness.relay.request_engage()
    assert ok is False
    assert state == "fault"
    assert harness.relay.status()["fault_reason"] == "simulated GPIO failure"


def test_fault_is_sticky_while_stream_keeps_running(monkeypatch):
    _stub_gpio(monkeypatch, engage_ok=False)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()
    assert harness.state() == "fault"

    # Still streaming - a fault must not silently self-clear in the
    # background; it should only reset on a fresh stream start.
    harness.tick(times=5)
    assert harness.state() == "fault"


def test_fault_clears_on_a_fresh_stream_start(monkeypatch):
    _stub_gpio(monkeypatch, engage_ok=False)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()
    assert harness.state() == "fault"

    # Stopping the stream doesn't silently clear the fault - it should stay
    # visible to the operator until they deliberately start again.
    harness.stream_state = "stopped"
    harness.tick()
    assert harness.state() == "fault"

    harness.stream_state = "streaming"
    _stub_evaluate(monkeypatch, GOOD_RESULT)
    harness.tick()
    assert harness.state() == "waiting"


def test_force_disengage_and_idle(monkeypatch):
    calls = _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()

    harness.relay.force_disengage_and_idle()
    assert harness.state() == "idle"
    assert calls["disengage"] == 1


def test_manual_disengage_targets_waiting_and_self_heals_to_idle(monkeypatch):
    calls = _stub_gpio(monkeypatch)
    harness = Harness()
    _reach_ready(harness, monkeypatch)
    harness.relay.request_engage()

    state = harness.relay.request_disengage()
    assert state == "waiting"
    assert calls["disengage"] == 1

    # Stream has actually already stopped underneath - the very next tick
    # corrects "waiting" to "idle" on its own (only one place decides
    # idle-vs-waiting: tick() itself).
    harness.stream_state = "stopped"
    harness.tick()
    assert harness.state() == "idle"
