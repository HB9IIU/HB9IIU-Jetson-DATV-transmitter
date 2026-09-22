"""Pure-function detector: does the Pluto's local RX FFT show a clean,
stable DVB-S2 signal (a flat-top occupied-bandwidth plateau), or just noise
or a transient spike?

Used by pa_relay.py to decide when it's safe to arm the PA-relay safety
interlock (see pa_relay.py's own docstring for the full story - this module
only answers "does this one frame look like a locked signal", with no
notion of state/debouncing/time at all. That's deliberate: it's what makes
this trivially unit-testable without any hardware, unlike pa_relay.py's own
RelayController, which needs several consecutive good frames before
trusting it.

FFT bins from pluto_fft_bridge.get_latest_frame() are raw, uncalibrated
uint16 values - there's no absolute dB reference anywhere in this codebase
(see the auto-scaling comment in static/js/datv.js's drawLocalFft()), so
every threshold here is a ratio relative to the frame's own dynamic range,
never an absolute level.
"""

# --- Unvalidated first-pass constants -----------------------------------
# None of these have been checked against a real captured Pluto power-on
# transient or a real locked signal yet (unlike e.g. GAIN_MIN_DB in
# datv_engine.py, which came from an actual spectrum-analyzer measurement
# on real hardware). Tune these once real bin arrays have been captured on
# the Jetson during both a startup transient and a locked stream (dump
# get_latest_frame()'s output to a file during a real stream start), and
# feed those captures into test_pluto_signal_stability.py as fixtures.

# noise_floor/peak use percentiles rather than bare min/max so a single
# hot/dead bin - or a narrow transient spike - doesn't dominate either end.
NOISE_FLOOR_PERCENTILE = 0.10
PEAK_PERCENTILE = 0.95

# peak must clear the noise floor by at least this fraction of peak itself.
# First real-hardware data point (2026-09-22): a visibly clean, locked
# testcard signal at -9.25 dB TX power (a clear flat-top plateau by eye,
# coverage_ratio 0.46 - solidly within the healthy range below) measured
# margin_ratio only ~0.146 via /api/relay/status - well under the original
# 0.35 guess, which is why the state machine sat stuck in "waiting"
# forever despite a genuinely good signal. Lowered with headroom below
# that real reading, not down to it exactly, since a single sample isn't
# enough to know how much margin varies run-to-run - tighten this back up
# once a real "bad" (noise-only / transient) margin_ratio has also been
# captured for comparison, via the same endpoint, e.g. right at stream
# start before it's locked, or at 0% TX power.
MIN_MARGIN_RATIO = 0.10

# A real DVB-S2 flat-top should cover a plausible middle slice of the zoom
# window (deliberately sized to margin + occupied-bandwidth + margin,
# margin == occupied bandwidth - see app.py's FFT_ZOOM_TO_BANDWIDTH_RATIO) -
# too little coverage looks like a narrow spike, too much looks like a
# saturated/clipped frame with no real shape.
PLATEAU_BAND_FRACTION = 0.15
PLATEAU_COVERAGE_MIN = 0.15
PLATEAU_COVERAGE_MAX = 0.60


class StabilityResult(object):
    """Plain data holder - a dataclass would work too, but this file has no
    other dependencies and stays that way on purpose."""

    __slots__ = ("is_plateau", "margin_ratio", "coverage_ratio", "reason")

    def __init__(self, is_plateau, margin_ratio, coverage_ratio, reason):
        self.is_plateau = is_plateau
        self.margin_ratio = margin_ratio
        self.coverage_ratio = coverage_ratio
        self.reason = reason

    def __repr__(self):
        return (
            "StabilityResult(is_plateau={}, margin_ratio={:.3f}, "
            "coverage_ratio={:.3f}, reason={!r})".format(
                self.is_plateau, self.margin_ratio, self.coverage_ratio, self.reason))


def _percentile(sorted_values, fraction):
    """Linear-interpolation percentile over an already-sorted sequence -
    same idea as numpy.percentile's default, reimplemented here rather than
    adding a numpy dependency for one small function."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = fraction * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def crop_to_center(bins, span_hz, zoom_span_hz):
    """Same centre-crop as static/js/datv.js's cropToCenter() - our own
    signal always sits exactly in the middle of the capture by construction
    (app.py always tunes the Pluto's RX to its own live TX frequency), so a
    centre crop reliably isolates it. Deliberately duplicated here (not
    imported - Python vs JS) - same precedent as format_gain_db() being
    duplicated between app.py and datv_tx_plus.py.

    Falls back to the full, uncropped frame if span/zoom aren't known yet
    (e.g. right at stream start before CURRENT_SYMBOL_RATE_KSPS is set) -
    evaluate_frame() below still works on that, just less precisely, and
    is very unlikely to false-positive on an uncropped wide capture.
    """
    if not bins or not span_hz or not zoom_span_hz or len(bins) < 2:
        return list(bins or [])
    hz_per_bin = span_hz / (len(bins) - 1)
    crop_count = max(8, min(len(bins), round(zoom_span_hz / hz_per_bin)))
    start = (len(bins) - crop_count) // 2
    return list(bins[start:start + crop_count])


def evaluate_frame(bins, span_hz, zoom_span_hz):
    """Does this one FFT frame look like a clean, locked DVB-S2 signal?

    Stateless / no debouncing here on purpose - pa_relay.py's
    RelayController is the one that requires several consecutive
    is_plateau=True frames before trusting it.
    """
    plotted = crop_to_center(bins, span_hz, zoom_span_hz)
    if len(plotted) < 8:
        return StabilityResult(False, 0.0, 0.0, "not enough bins in the cropped window")

    ordered = sorted(plotted)
    noise_floor = _percentile(ordered, NOISE_FLOOR_PERCENTILE)
    peak = _percentile(ordered, PEAK_PERCENTILE)
    margin_ratio = (peak - noise_floor) / max(1.0, peak)

    band_floor = peak - (peak - noise_floor) * PLATEAU_BAND_FRACTION
    above_band = sum(1 for value in plotted if value >= band_floor)
    coverage_ratio = above_band / len(plotted)

    if margin_ratio < MIN_MARGIN_RATIO:
        return StabilityResult(
            False, margin_ratio, coverage_ratio, "no clear plateau above noise floor")
    if coverage_ratio < PLATEAU_COVERAGE_MIN:
        return StabilityResult(
            False, margin_ratio, coverage_ratio, "too narrow - looks like a spike, not a flat-top")
    if coverage_ratio > PLATEAU_COVERAGE_MAX:
        return StabilityResult(
            False, margin_ratio, coverage_ratio, "too wide/flat - looks saturated or clipped")
    return StabilityResult(True, margin_ratio, coverage_ratio, "stable plateau")
