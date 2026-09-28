# tuning/

Offline measurement tools for picking each DVB-S2 profile's settings from real
hardware measurements, not guesses. Nothing here transmits: everything runs
on the Jetson with file output, and no Pluto or RF is involved.

All commands below are typed **in a console on the Jetson** (SSH session or
local terminal). Start every session with:

```
cd ~/jetson-stream-panel
```

Always use `.venv/bin/python3` (the app's venv) - plain `python3` lacks
paho-mqtt, which `datv_tx_plus` imports.

## Layout

| Path | What | In git? |
|---|---|---|
| `tune_all.py` | Runs all three tunings below in a row (camera, video, testcard) | yes |
| `record_benchmark_clip.py` | Records the 90 s camera + mic benchmark clip | yes |
| `tune_profiles.py` | Tunes camera/video profiles (`video_bitrate_kbps`) against a benchmark clip | yes |
| `tune_profiles_for_testcard.py` | Same, for the testcard profiles | yes |
| `clips/` | Benchmark clips (large) | no |
| `runs/` | Trial output: encoded `.ts` files, PSNR references, logs | no |
| `results/` | Short text summary per tuning run | yes |

## The benchmark clips

- `clips/camera_clip_1280x720_90s.mkv` - for the camera profiles. Recorded
  with `record_benchmark_clip.py` (see 1.).
- `clips/test_clip_1280x720_90s.mkv` - for the video profiles: the busiest
  90 s of `original videos/demo.m4v` (from 1:49, picked by frame-difference
  motion), cut by hand on 2026-09-25 and converted to H.264 on 2026-09-27.
  No script makes it.
- Testcard profiles need no clip.

Both clips are near-lossless H.264 (CRF 10), so the Jetson decodes them in
hardware like the on-air video files. Until 2026-09-26 they were lossless
FFV1, which the CPU could only just decode in real time - trials dropped
frames at random and the camera results were noisy. The FFV1 originals are
kept in `clips/old_ffv1/`. Keep the clips: a re-run on the same clips is
directly comparable.

## 1. Record a new camera clip (only if your camera scene changed)

Point the camera at what you actually transmit (normally the shack, with you
in it), then perform for 90 s: talk, move, lean in and out, hold something
up to the camera. Keep some fine detail in frame (bookshelf, front panels).
Don't do anything more extreme than you would on air.

Stop any transmission or camera preview in the web UI first (the camera
must be free), then:

```
.venv/bin/python3 tuning/record_benchmark_clip.py
```

Pick the C920 when asked. A 10 s countdown lets the camera settle, then
recording runs for 90 s (the C920's light is on). Earlier clips are moved to
`clips/old_<timestamp>/`, never overwritten.

## 2. Tune everything (recommended)

Stop any transmission in the web UI first. Takes about 4 hours (~25 min per
camera/video profile, ~12 min per testcard profile). Start it so it keeps
running after you close the console:

```
mkdir -p tuning/runs && nohup .venv/bin/python3 -u tuning/tune_all.py > tuning/runs/tune_all.log 2>&1 &
```

Watch it (Ctrl+C only stops watching, not the tuning):

```
tail -f tuning/runs/tune_all.log
```

Stop it early:

```
pkill -f tuning/tune_
```

Done when the log ends with **ALL DONE**. The summaries are in `results/`
(one file per group) - hand them to Claude to update `dvbs2_profiles.py`.

## 3. Tune one group only (optional)

- Camera profiles: `.venv/bin/python3 tuning/tune_profiles.py` (as set in
  `PROFILES_TO_TUNE` / `TEST_CLIPS` at the top of the script).
- Testcard profiles: `.venv/bin/python3 tuning/tune_profiles_for_testcard.py`
  (~1 hour; first finds the hardest test card image, then tunes on it).
- Video profiles: easiest via `tune_all.py`, or ask Claude for the one-liner.

These run in the foreground - the console must stay open.

## Results log

Add one line per tuning run here (date, what changed, link to `results/`).
