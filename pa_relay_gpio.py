"""Thin wrapper around Jetson.GPIO for the PA-relay safety interlock.

BOARD pin 18 - same physical pin as ptt_gpio.py's standalone blink/smoke-
test script (left untouched; this module supersedes it for the real app,
but the standalone script stays useful for a quick hardware sanity check
independent of the whole Flask app).

Import-guarded exactly like app.py already guards `paho.mqtt.client` (see
app.py's own `try: import paho.mqtt.client as mqtt except ImportError:
mqtt = None`) - Jetson.GPIO only exists on the Jetson itself, and app.py
must still import cleanly on a plain dev machine.

Every public function here catches GPIO exceptions and returns False
rather than raising - a GPIO permission error (e.g. the deploy user isn't
in the `gpio` group / no udev rule - this repo has no systemd unit, app.py
is launched as a normal SSH user via PyCharm, not root) must show up to the
caller as "couldn't do it", never as an unhandled 500 that takes the whole
Flask process down mid-transmission.

HIGH = relay energized = CN0417 pre-amp powered (matches ptt_gpio.py's own
blink test, which drives the pin HIGH for "on"). Flip RELAY_PIN's polarity
here (swap GPIO.HIGH/GPIO.LOW in engage()/disengage()) if the real relay
board turns out to be active-low - confirm on real hardware before trusting
this at power.
"""

import atexit
import logging
import threading

try:
    import Jetson.GPIO as GPIO
except ImportError:
    GPIO = None

RELAY_PIN = 18  # BOARD numbering - matches ptt_gpio.py's PIN

log = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False
_engaged = False
_fault_reason = None


def _mark_fault(reason):
    global _fault_reason
    _fault_reason = reason
    log.warning("pa_relay_gpio: %s", reason)


def _ensure_setup_locked():
    """Caller must hold _lock. Idempotent - safe to call before every
    engage()/disengage(), same idea as pluto_fft_bridge.py's own _started
    flag."""
    global _initialized
    if _initialized:
        return True
    if GPIO is None:
        _mark_fault("Jetson.GPIO is not installed")
        return False
    try:
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)
    except Exception as exc:
        _mark_fault("GPIO setup failed: {}".format(exc))
        return False
    _initialized = True
    return True


def engage():
    """Energizes the relay (GPIO HIGH) - powers the CN0417 pre-amp. Returns
    True only if the GPIO call actually succeeded; the caller
    (pa_relay.RelayController) must not assume success otherwise."""
    global _engaged
    with _lock:
        if not _ensure_setup_locked():
            return False
        try:
            GPIO.output(RELAY_PIN, GPIO.HIGH)
        except Exception as exc:
            _mark_fault("GPIO engage failed: {}".format(exc))
            return False
        _engaged = True
        return True


def disengage():
    """De-energizes the relay (GPIO LOW). Only clears _engaged on confirmed
    success (or when there was never anything to turn off) - if the GPIO
    call itself fails, _engaged is deliberately left alone rather than
    optimistically claiming "off", since the caller treats a False return
    as a fault, not as "already safe"."""
    global _engaged
    with _lock:
        if GPIO is None or not _initialized:
            # Nothing was ever set up, so nothing could be energized -
            # trying setup() here just to immediately drive LOW isn't worth
            # risking a setup error masking "this was never engaged".
            _engaged = False
            return True
        try:
            GPIO.output(RELAY_PIN, GPIO.LOW)
        except Exception as exc:
            _mark_fault("GPIO disengage failed: {}".format(exc))
            return False
        _engaged = False
        return True


def is_engaged():
    """Last-known *commanded* state, not a GPIO.input() readback - an
    output pin's input() isn't a trustworthy source of truth for whether
    the physical relay/wiring actually did what was commanded. A mismatch
    there is what fault_reason() is for, not this getter."""
    return _engaged


def fault_reason():
    return _fault_reason


def clear_fault():
    """Called by pa_relay.RelayController when a fresh stream starts - a
    stale fault from a previous stream shouldn't block a new attempt from a
    clean slate."""
    global _fault_reason
    _fault_reason = None


def _cleanup_on_exit():
    disengage()
    if GPIO is not None and _initialized:
        try:
            GPIO.cleanup()
        except Exception:
            pass  # best-effort on process exit


atexit.register(_cleanup_on_exit)
