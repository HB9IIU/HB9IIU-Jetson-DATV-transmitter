# Jetson Nano Project

Rebuilding a DATV (amateur TV over satellite) streaming setup, one small
working step at a time. `old_project_TO_DELETE/` holds the previous,
more complex attempt — kept around for reference until it's no longer needed.

## Stage 0 — `stage0.py`

**What it proves:** the Jetson can encode video with its hardware encoder
and the result can actually reach another computer over the network.
Nothing more than that yet — no camera, no web UI, no satellite/RF.

**How it works:**
1. Runs *on the Jetson Nano* (`192.168.0.178`), via SSH/PyCharm remote interpreter.
2. Generates a test pattern (`videotestsrc` — a built-in GStreamer color bars
   pattern, not a real camera).
3. Encodes it with the Jetson's hardware H.265 encoder (`nvv4l2h265enc`) —
   chosen over H.264 because DATV over the QO-100 transponder is bandwidth
   constrained, and H.265 needs noticeably less bitrate for the same quality.
4. Packages it as MPEG-TS and sends it over the network as UDP to your PC.

```
[test pattern] -> [Jetson hardware encoder] -> [network: UDP] -> [your PC / VLC]
```

**Running it** (on the Jetson):
```
python3 stage0.py --host 192.168.0.5 --port 5000
```
- `--host` — the IP of the machine that will watch the stream (your PC).
- `--port` — which UDP port to send it on (default `5000`).
- `--bitrate-kbps` — video bitrate (default `3000`).

**Watching it** (on your PC, in VLC):
`Media > Open Network Stream >` `udp://@:5000`

**Note:** if nothing shows up, check that your PC's firewall allows
incoming UDP on that port — Windows blocks unsolicited inbound traffic
by default.

## Stage 1 — `stage1.py`

Same pipeline as Stage 0, but the source is a real camera instead of a
generated test pattern. On startup it detects connected cameras under
`/dev/video*` and asks in the terminal which one to stream:

- **CSI camera** (Sony IMX219 sensor) — via `nvarguscamerasrc`. Includes
  `--flip-method` (default `2`, 180°) since the camera is physically
  mounted upside-down, and `--wbmode`/`--saturation` for white balance tuning.
- **USB webcam** (e.g. Logitech C920) — via `v4l2src`, MJPEG capture.

```
[camera] -> [Jetson hardware encoder] -> [network: UDP] -> [your PC / VLC]
```

**Known limitation — CSI camera pink/magenta color tint:** the CSI camera
module appears to be missing an IR-cut filter (a "NoIR" variant), letting
extra infrared light through and skewing colors pink, especially indoors.
White balance and saturation tuning can't fully correct this — it's a
hardware limitation, not a pipeline bug. Parked for now since it doesn't
block progress on the actual DATV/RF pipeline; revisit later by checking
for a missing physical IR filter or swapping the camera module.
**The USB webcam (C920) has normal colors and is confirmed working** —
prefer it over the CSI camera until the tint issue is resolved.

## Roadmap

Planned next steps, in order:
1. ~~Stage 0 — hardware encode + network delivery~~ ✅
2. ~~Swap the test pattern for a real camera~~ ✅ (see color tint limitation above)
3. Add PlutoSDR / RF output for actual QO-100 transmission (video only).
   Plan: send MPEG-TS over UDP to the Pluto's existing "PlutoDVB" companion
   firmware, which does the DVB-S2 modulation on-board (control via MQTT/HTTP)
   — same approach the old project used. A LimeSDR was considered as an
   alternative, but would need a from-scratch software DVB-S2 modulator
   (e.g. GNU Radio) feeding raw I/Q over SoapySDR, so we're sticking with Pluto.
4. Add audio (kept separate from step 3 so RF issues and audio issues
   don't get debugged at the same time)
5. Wrap it in a minimal web control endpoint
6. Build a UI on top
