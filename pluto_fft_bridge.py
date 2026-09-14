"""Background bridge from PlutoDVB2's local RX WebFFT service to app.py's
own Flask API - no separate process/port, unlike fft_relay.py (which this
reuses the connect/retry idiom from). A single daemon thread keeps an
asyncio loop alive, connects to the Pluto's internal FFT websocket (only
reachable on the private USB link - see fft_relay.py's own docstring for
why a bridge is needed at all), and keeps the latest frame in memory for
Flask request threads to read.

Confirmed on real hardware: TX-to-RX leakage inside the Pluto is visible on
this FFT even with no antenna connected, which is the whole point - lets
the web UI show "yes, RF is actually going out" independent of what
datv_tx_plus.py itself reports.
"""

import asyncio
import struct
import threading
import time

import websockets

PLUTO_FFT_WS_URL = "ws://192.168.2.1:7681/websocket"
RECONNECT_DELAY_SECONDS = 2
# If the last frame is older than this, treat it as "no data" rather than
# showing a frozen, increasingly misleading picture (e.g. after RX WebFFT
# is disabled again on stream stop, or the Pluto link drops).
FRAME_STALE_SECONDS = 5.0

_lock = threading.Lock()
_latest_bins = None
_latest_timestamp = 0.0
_started = False


def _store_frame(raw_bytes):
    global _latest_bins, _latest_timestamp
    if len(raw_bytes) < 2:
        return
    # JS's `new Uint16Array(arrayBuffer)` (fft_viewer.html) reinterprets raw
    # bytes using the platform's native endianness, which is little-endian
    # on every real browser/device - '<H' matches that, not a guess.
    count = len(raw_bytes) // 2
    bins = struct.unpack("<{}H".format(count), raw_bytes[:count * 2])
    with _lock:
        _latest_bins = bins
        _latest_timestamp = time.monotonic()


async def _pluto_reader():
    while True:
        try:
            async with websockets.connect(PLUTO_FFT_WS_URL) as pluto_ws:
                async for message in pluto_ws:
                    if isinstance(message, (bytes, bytearray)):
                        _store_frame(message)
        except Exception:
            # Expected whenever the Pluto's RX WebFFT service isn't
            # currently enabled (e.g. no stream running) - just keep
            # retrying quietly rather than logging noise for a normal state.
            pass
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)


def _run_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_pluto_reader())


def start_background_reader():
    """Idempotent - safe to call more than once (e.g. Flask's debug-mode
    reloader re-importing this module), only ever starts one thread."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_run_event_loop, daemon=True).start()


def get_latest_frame():
    """Returns a plain list of ints for the current frame, or None if
    nothing recent enough has arrived (Pluto's WebFFT not enabled, or the
    link is down)."""
    with _lock:
        bins, timestamp = _latest_bins, _latest_timestamp
    if bins is None or time.monotonic() - timestamp > FRAME_STALE_SECONDS:
        return None
    return list(bins)
