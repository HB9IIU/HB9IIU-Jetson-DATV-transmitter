# tuning/

Offline measurement tools for picking each DVB-S2 profile's settings from real
hardware measurements, not guesses. Nothing here transmits: everything runs
on the Jetson with file output, and no Pluto or RF is involved.

## Layout

| Path | What | In git? |
|---|---|---|
| `record_benchmark_clip.py` | Records the 90 s camera + mic benchmark clip | yes |
| `tune_profiles.py` | Tunes camera/video profiles (`video_bitrate_kbps`) against a benchmark clip | yes |
| `tune_profiles_for_testcard.py` | Same, for the testcard profiles | yes |
| `clips/` | Benchmark clips (large) | no |
| `runs/` | Trial output: encoded `.ts` files, PSNR references | no |
| `results/` | Short text summary per tuning run | yes |

## 1. Record the benchmark clip

Point the camera at what you actually transmit (normally the shack, with you
in it), then perform for 90 s: talk, move, lean in and out, hold something
up to the camera. Keep some fine detail in frame (bookshelf, front panels).
Don't do anything more extreme than you would on air.

Stop any transmission or camera preview in the web UI first (the camera
must be free), then run from a Windows terminal:

```
ssh -t daniel@jetson-nano.local "cd jetson-stream-panel && .venv/bin/python3 tuning/record_benchmark_clip.py"
```

Pick the C920 when asked. A 10 s countdown lets the camera settle, then
recording runs for 90 s (the C920's light is on). Afterwards it derives
`clips/camera_clip_{640x360,960x540,1280x720}_90s.mkv`. Earlier clips are
moved to `clips/old_<timestamp>/`, never overwritten.

## 2. Tune

`.venv/bin/python3 tuning/tune_profiles.py` (the app's venv - plain `python3`
lacks paho-mqtt, which `datv_tx_plus` imports): see its docstring. Overnight job.

## Results log

Add one line per tuning run here (date, what changed, link to `results/`).
