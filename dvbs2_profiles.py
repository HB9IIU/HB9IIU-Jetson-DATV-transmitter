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
# Resolution/bitrate choice per symbol rate/FEC also comes from real testing
# (not guesses): 960x540 was found to look noticeably better than 1280x720
# at the same bitrate (fewer compression mosaics on motion), and lower
# symbol rates get a lower resolution to match their smaller bitrate
# budget.
PROFILES = {
    "sr250_fec23": {"symbol_rate": 250000, "fec": "2/3", "resolution": (640, 360),
                    "video_bitrate_kbps": 191, "audio_bitrate_kbps": 32},
    "sr250_fec34": {"symbol_rate": 250000, "fec": "3/4", "resolution": (640, 360),
                    "video_bitrate_kbps": 233, "audio_bitrate_kbps": 32},
    "sr333_fec23": {"symbol_rate": 333000, "fec": "2/3", "resolution": (960, 540),
                    "video_bitrate_kbps": 292, "audio_bitrate_kbps": 32},
    "sr333_fec34": {"symbol_rate": 333000, "fec": "3/4", "resolution": (960, 540),
                    "video_bitrate_kbps": 342, "audio_bitrate_kbps": 32},
    "sr500_fec23": {"symbol_rate": 500000, "fec": "2/3", "resolution": (960, 540),
                    "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
    "sr500_fec34": {"symbol_rate": 500000, "fec": "3/4", "resolution": (960, 540),
                    "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
    "sr500_fec34_720p": {"symbol_rate": 500000, "fec": "3/4", "resolution": (1280, 720),
                          "video_bitrate_kbps": 575, "audio_bitrate_kbps": 32},
    "sr500_fec23_720p": {"symbol_rate": 500000, "fec": "2/3", "resolution": (1280, 720),
                          "video_bitrate_kbps": 498, "audio_bitrate_kbps": 32},
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
