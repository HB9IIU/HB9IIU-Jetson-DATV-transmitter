"""DVB-S2 profile table shared by datv_tx_plus.py and datv_tx_plus_fft.py.

Extracted 2026-09-07: both scripts kept their own separate copy of PROFILES
(plus FRAME/PILOTS and the capacity formula that depends on them), which
meant every bitrate correction from tune_profiles.py had to be hand-mirrored
into both files - exactly the kind of duplication that causes one file to
silently drift out of date. One shared module, imported by both, removes
that risk: edit here once, both scripts see it.

Add more profiles (e.g. "sr333_fec45") to PROFILES below the same way - no
other code needs to change.
"""

import math

# video_bitrate_kbps values were re-measured by tune_profiles.py
# (2026-09-07) against real hardware output, not just calculated from the
# DVB-S2 formula: the nvv4l2h265enc encoder's real muxed bitrate doesn't
# match its bitrate= target exactly (varies by profile, from ~0% to ~35%
# overshoot on real motion content), so most of these needed lowering to
# stay safely under the channel's real TS capacity, and one (sr333_fec34)
# actually had spare real capacity and was raised instead (sr500_fec34_720p
# needed a second run after its first attempt hit a one-off trial stall -
# no repro, likely a transient resource hiccup rather than anything
# specific to this profile, since the very next profile at the same
# resolution ran cleanly).
#
# Re-checked 2026-09-23 for the camera/video profiles against a harder
# room-scene benchmark clip, with on-air overlays and the exact CBR relay
# (tuning/results/2026-09-23_1718_*.txt).
#
# SR250 was removed from every mode (2026-09-24) to keep things simple -
# only SR333 and SR500 remain.
#
# Resolution/bitrate choice per symbol rate/FEC also comes from real testing
# (not guesses): 960x540 was found to look noticeably better than 1280x720
# at the same bitrate (fewer compression mosaics on motion), and lower
# symbol rates get a lower resolution to match their smaller bitrate
# budget. (That 960x540 finding was with the old encoder settings -
# UltraFast preset, 1 s GOP, 1 reference frame. With the 2026-09-24
# settings 1280x720 looks good, and every profile now uses it.)
PROFILES = {
    # Video-mode profiles: 1280x720 since 2026-09-24 (every mode is now
    # 720p - video files are only converted at that size). Bitrates carried
    # over from 960x540 and confirmed unchanged at 720p with the new encoder
    # settings by tune_profiles.py on the movie clip (90 s of demo.mkv from
    # its busiest stretch, 109 s; tuning/results/2026-09-25_0844_*.txt): all
    # four already at their safe optimum, relay-clean from muxdelay 0.7s.
    "sr333_fec23": {"symbol_rate": 333000, "fec": "2/3", "resolution": (1280, 720),
                    "video_bitrate_kbps": 292, "audio_bitrate_kbps": 32},
    "sr333_fec34": {"symbol_rate": 333000, "fec": "3/4", "resolution": (1280, 720),
                    "video_bitrate_kbps": 342, "audio_bitrate_kbps": 32},
    "sr500_fec23": {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                    "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
    "sr500_fec34": {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                    "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
    "sr500_fec34_720p": {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                          "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
    "sr500_fec23_720p": {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                          "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
    # Confirmed via tune_profiles_for_testcard.py (2026-09-10): the sr333
    # pair is safe at 1280x720 with the exact same video_bitrate_kbps as
    # their 960x540 counterparts above - the testcard-at-720p hypothesis
    # holds for sr333 too, not just sr500.
    "sr333_fec34_720p": {"symbol_rate": 333000, "fec": "3/4", "resolution": (1280, 720),
                          "video_bitrate_kbps": 342, "audio_bitrate_kbps": 32},
    "sr333_fec23_720p": {"symbol_rate": 333000, "fec": "2/3", "resolution": (1280, 720),
                          "video_bitrate_kbps": 292, "audio_bitrate_kbps": 32},
    # Camera-only profiles (2026-09-24), inspired by OBS + Easy DATV (which ran 1600x900 on SR500 3/4)
    # once the encoder got a 4 s GOP / 4 refs / slow preset - see ENCODER_*
    # in datv_tx_plus.py. 1280x720 is the ceiling: 1600x900 needs a 1080p
    # camera capture, which the Nano's CPU couldn't decode/scale in real time
    # (on-air test: frozen picture, ever-growing alsasrc drops). Bitrates are
    # tuned by tune_profiles.py with the new encoder settings on the
    # room-scene camera clip (tuning/results/2026-09-24_1602_*.txt): all
    # relay-clean at muxdelay 1.0s; sr500 stayed at its carried-over values.
    "sr333_fec23_camera": {"symbol_rate": 333000, "fec": "2/3", "resolution": (1280, 720),
                           "video_bitrate_kbps": 298, "audio_bitrate_kbps": 32},
    "sr333_fec34_camera": {"symbol_rate": 333000, "fec": "3/4", "resolution": (1280, 720),
                           "video_bitrate_kbps": 350, "audio_bitrate_kbps": 32},
    "sr500_fec23_camera": {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                           "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
    "sr500_fec34_camera": {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                           "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
}

# (symbol_rate_ksps, fec) -> which PROFILES entry to use, one table per
# SOURCE family - single source of truth for both datv_tx_plus.py (which
# lets the user set SR/FEC/SOURCE directly) and datv_engine.py (which
# receives symbol_rate/fec from the web UI). Keeping this here instead of
# duplicated in both avoids exactly the kind of silent drift that made
# datv_engine.py briefly pick the wrong (non-recommended) resolution for
# sr333 in the web UI (2026-09-10) - its old ad-hoc "_720p only if
# symbol_rate == 500" logic predated the sr333 720p additions.
#
# Testcard mode always prefers the highest resolution confirmed safe on
# real testcard content by tune_profiles_for_testcard.py (see
# testcard_summary.txt). Since 2026-09-24 every mode runs at 1280x720; the
# three tables stay separate because each is tuned on its own kind of
# content (still testcard, room-scene camera, movie clips), which the
# encoder's real bitrate overshoot depends on.
TESTCARD_PROFILE_NAMES = {
    (333, "2/3"): "sr333_fec23_720p",   # 1280x720
    (333, "3/4"): "sr333_fec34_720p",   # 1280x720
    (500, "2/3"): "sr500_fec23_720p",   # 1280x720
    (500, "3/4"): "sr500_fec34_720p",   # 1280x720
}
CAMERA_PROFILE_NAMES = {
    (333, "2/3"): "sr333_fec23_camera",  # 1280x720
    (333, "3/4"): "sr333_fec34_camera",  # 1280x720
    (500, "2/3"): "sr500_fec23_camera",  # 1280x720
    (500, "3/4"): "sr500_fec34_camera",  # 1280x720
}
VIDEO_PROFILE_NAMES = {
    (333, "2/3"): "sr333_fec23",
    (333, "3/4"): "sr333_fec34",
    (500, "2/3"): "sr500_fec23",
    (500, "3/4"): "sr500_fec34",
}

# Applied uniformly to every profile above via configure_pluto() - not part
# of the per-profile dict since neither script ever varies these per
# profile, only symbol rate/FEC/resolution/bitrate change between profiles.
FRAME = "long"
PILOTS = True


def calculate_dvbs2_ts_bitrate(profile):
    """Return exact DVB-S2 QPSK normal-frame TS capacity in bit/s.

    This is the ETSI frame calculation used by the dvbs2rate utility. It
    deliberately replaces the nonexistent PlutoDVB2 MQTT
    ``tx/dvbs2/ts/bitrate`` telemetry topic. For example, 500 kS/s, 3/4,
    long frame and pilots on gives the previously verified 726038 bit/s.
    """
    fec_parameters = {
        "2/3": (2, 3, 10),
        "3/4": (3, 4, 12),
    }
    try:
        fec_num, fec_den, bch = fec_parameters[profile["fec"]]
    except KeyError:
        raise ValueError("Unsupported DVB-S2 FEC for TS calculation: {}".format(
            profile["fec"]))

    fec_frame_bits = 64800.0
    modulation_bits = 2.0  # QPSK
    data_symbols = fec_frame_bits / modulation_bits
    pilot_symbols = 36.0 if PILOTS else 0.0
    pilot_blocks = math.ceil(data_symbols / 90.0 / 16.0 - 1.0)
    frame_symbols = data_symbols + 90.0 + pilot_blocks * pilot_symbols
    useful_bits = fec_frame_bits * fec_num / fec_den - 16.0 * bch - 80.0
    return int(profile["symbol_rate"] / frame_symbols * useful_bits)
