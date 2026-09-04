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
- **USB webcam** (e.g. Logitech C920) — via `v4l2src`, MJPEG capture, plus
  **audio** from its built-in microphone (AAC via `voaacenc`, a software
  encoder — the Jetson has no hardware audio encoder, only hardware video).
  The CSI camera has no microphone, so that path stays video-only.

```
[camera (+ mic for USB)] -> [Jetson hardware encoder] -> [network: UDP] -> [your PC / VLC]
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

## Stage 2 — `datv_tx.py` — real QO-100 DATV transmission over the Pluto

**This is the big milestone: a single script that configures the Pluto,
streams live camera + audio to it, and produces a real, receiver-confirmed
DVB-S2 lock on QO-100 frequencies.** No arguments needed — just run it,
Ctrl+C to stop (which cleanly keys PTT off first).

```
[C920 camera + mic] -> [Jetson hardware H.265 encoder] -> [UDP :8282] -> [Pluto: DVB-S2 modulator] -> [RF]
```

**The Pluto's firmware:** stock/factory Pluto firmware can't do DATV at
all — it's just a raw radio. This project uses
[F5OEO's plutosdr-fw ("PlutoDVB2")](https://github.com/F5OEO/plutosdr-fw/releases),
which turns the Pluto into a standalone DVB-S2 transmitter with its own
onboard MQTT broker (port `1883`, credentials `root`/`analog`) for control.

**How we actually learned the protocol:** not by guessing — by reading
[DATV-Red](https://github.com/Psynosaur/DATV-Red)'s real Node-RED source
(`.node-red/flows.json`), which is the actual PC-side control app this
firmware is designed to work with. That's where the real MQTT topic
scheme, payload formats, and a genuine working preset (`profiles/p1.json`)
came from — not the abandoned old project.

**Confirmed protocol details:**
- Video delivery: MPEG-TS over **UDP to `<pluto_ip>:8282`**.
- Control: MQTT on the Pluto itself, topics `cmd/pluto/<callsign>/...`
  (commands) and `dt/pluto/<callsign>/...` (telemetry it publishes back).
- DVB-S2 settings used (matching DATV-Red's real preset): symbol rate
  `333000`, `FEC 4/5`, `frame long`, no pilots, QPSK.

**A real firmware bug we found and fixed:** publishing `tx/mute` over
MQTT does **not** reliably power up the Pluto's TX local oscillator on
this firmware build — the modulator can look fully configured and
"unmuted" while no RF is ever actually emitted. Confirmed by directly
reading/writing the real hardware attribute
`/sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown`
(`0` = on, `1` = off) over SSH, and observing an actual DVB-S2 lock
appear/disappear on a receiver as it's toggled. `datv_tx.py` sets this
directly as the real PTT mechanism, not just the MQTT topic.

**Also found: a picture-freezing bug from a missing encoder setting.**
The Jetson's hardware encoder needs an explicit `iframeinterval` (keyframe
interval) — without it, video can look frozen (same class of bug fixed
earlier in Stage 1's camera pipeline, but not carried over when this
script was first written). Fixed by setting `iframeinterval={FPS}` plus
`queue` elements around the encoder.

**Video bitrate must fit the DVB-S2 channel capacity**, or the modulator
silently refuses to key up: at `SR=333000`/`FEC=4/5` the channel carries
about 528 kbps total. Currently split as ~380 kbps video + 48 kbps audio,
matching DATV-Red's own real preset proportions.

**The Pluto's IP is found automatically** on startup (mDNS `_iio._tcp`,
falling back to its USB default `192.168.2.1`) — no IP to hardcode or
type in.

**Known cosmetic gap:** the receiver shows "Program: Station1" /
"Provider: ?" instead of a real name — GStreamer's `mpegtsmux` has no
property for DVB SDT service name/provider (confirmed via
`gst-inspect-1.0`), unlike ffmpeg's muxer which DATV-Red uses for this.
Fixing it would mean switching muxers or post-processing with TSDuck;
left alone since it's purely a display label with no functional effect.

## Roadmap

1. ~~Stage 0 — hardware encode + network delivery~~ ✅
2. ~~Swap the test pattern for a real camera~~ ✅ (see color tint limitation above)
3. ~~PlutoSDR / RF output for real QO-100 transmission~~ ✅ (`datv_tx.py`,
   see Stage 2 above — confirmed with a real receiver lock, picture, and audio)
4. Wrap it in a minimal web control endpoint
5. Build a UI on top
