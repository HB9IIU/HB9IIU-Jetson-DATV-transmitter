"""On this dev machine (no Jetson.GPIO installed), pa_relay_gpio must still
import cleanly, and every public call must fail safe (return False / log a
fault, never raise) - the same expectation app.py already relies on for its
own `try: import paho.mqtt.client as mqtt except ImportError: mqtt = None`
guard.
"""

import pytest

import pa_relay_gpio


@pytest.fixture(autouse=True)
def _reset_module_state():
    # Module-level state persists across tests in the same process -
    # reset it so tests don't depend on run order.
    pa_relay_gpio._initialized = False
    pa_relay_gpio._engaged = False
    pa_relay_gpio._fault_reason = None
    yield


def test_imports_without_jetson_gpio():
    assert pa_relay_gpio.GPIO is None


def test_engage_fails_safe_without_hardware():
    assert pa_relay_gpio.engage() is False
    assert pa_relay_gpio.is_engaged() is False
    assert pa_relay_gpio.fault_reason() is not None


def test_disengage_is_a_safe_noop_without_hardware():
    # Nothing was ever set up, so there's nothing to turn off - this must
    # report success (True), not a fault, since "never engaged" already
    # means "safe".
    assert pa_relay_gpio.disengage() is True
    assert pa_relay_gpio.is_engaged() is False
    assert pa_relay_gpio.fault_reason() is None


def test_clear_fault_resets_the_reason():
    pa_relay_gpio.engage()
    assert pa_relay_gpio.fault_reason() is not None
    pa_relay_gpio.clear_fault()
    assert pa_relay_gpio.fault_reason() is None
