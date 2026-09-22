"""pluto_signal_stability.evaluate_frame() is a pure function over plain
lists of ints - no Jetson, no Flask, no MQTT needed to exercise it. These
are the first real fixtures for MIN_MARGIN_RATIO/PLATEAU_COVERAGE_* -
tune those constants against real captured Pluto bin arrays once available
(see the module's own docstring), and add those captures here as
additional cases when that happens.
"""

import pluto_signal_stability as pss


def test_flat_noise_is_not_a_plateau():
    bins = [100 + (i % 5) for i in range(200)]  # 100-104, no real structure
    result = pss.evaluate_frame(bins, span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is False


def test_narrow_spike_is_rejected_as_too_narrow():
    # 92.5% baseline noise (~100-104), 7.5% narrow spike at 1000 - wide
    # enough to clear the peak percentile (so margin_ratio alone wouldn't
    # reject it), but narrow enough that the coverage check must.
    bins = [100 + (i % 5) for i in range(185)] + [1000] * 15
    result = pss.evaluate_frame(bins, span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is False
    assert result.margin_ratio > pss.MIN_MARGIN_RATIO  # confirms *which* check rejected it
    assert result.coverage_ratio < pss.PLATEAU_COVERAGE_MIN


def test_real_looking_flat_top_is_a_plateau():
    # 75% baseline noise (~100-104), 25% elevated flat-top (~495-505) -
    # the shape a locked DVB-S2 signal centred in the zoom window should
    # produce.
    bins = [100 + (i % 5) for i in range(150)] + [495 + (i % 11) for i in range(50)]
    result = pss.evaluate_frame(bins, span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is True
    assert pss.PLATEAU_COVERAGE_MIN <= result.coverage_ratio <= pss.PLATEAU_COVERAGE_MAX


def test_saturated_wide_frame_is_rejected_as_too_wide():
    # 30% baseline (~100), 70% elevated (~500) - real margin, but covering
    # too much of the window to be a genuine narrow occupied-bandwidth
    # plateau; looks more like a saturated/clipped capture.
    bins = [100] * 60 + [500] * 140
    result = pss.evaluate_frame(bins, span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is False
    assert result.coverage_ratio > pss.PLATEAU_COVERAGE_MAX


def test_too_few_bins_is_never_a_plateau():
    result = pss.evaluate_frame([1, 2, 3], span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is False


def test_no_bins_is_never_a_plateau():
    result = pss.evaluate_frame(None, span_hz=None, zoom_span_hz=None)
    assert result.is_plateau is False


def test_crop_to_center_picks_the_middle_slice():
    bins = list(range(20))
    # hz_per_bin = 1000/19 ~= 52.6; zoom_span_hz/hz_per_bin ~= 3.8, but
    # crop_to_center never crops below 8 bins even if the math asks for
    # fewer - a real zoom window is never this tiny relative to the
    # capture, but the floor exists to keep evaluate_frame's percentile
    # math meaningful on a very narrow request.
    cropped = pss.crop_to_center(bins, span_hz=1000, zoom_span_hz=200)
    assert cropped == [6, 7, 8, 9, 10, 11, 12, 13]


def test_crop_to_center_falls_back_to_full_frame_without_span_info():
    bins = list(range(20))
    assert pss.crop_to_center(bins, span_hz=None, zoom_span_hz=None) == bins
