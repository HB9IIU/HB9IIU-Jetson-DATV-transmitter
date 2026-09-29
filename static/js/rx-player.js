/**
 * Player for OpenTuner's "Jetson stream" - reads /rx/stream.mp4 (relayed
 * as-is by rx_relay.py) with fetch() and feeds it to a <video> through Media
 * Source Extensions. Used by the RX page (always on) and by the Home page's
 * on-air monitor (only while transmitting) - see createRxPlayer() below.
 *
 * One HTTP response = one station: it starts with the init segment and
 * ends when OpenTuner loses lock or retunes, and we then reconnect with a
 * fresh MediaSource (a new station's init must never go into the old
 * SourceBuffer). 503 = nothing being received yet - keep polling. The codec
 * string is read from the init segment, since audio is optional.
 *
 * Retunes are hidden: when a station ends, its last frame is copied onto a
 * canvas over the video with a "Retuning…" overlay ("No signal" after 15 s),
 * and both stay up until the next station's first frame is actually
 * playing - so neither the gap nor the new MediaSource's black start shows.
 */
(function () {
  'use strict';

  const NO_SIGNAL_RETRY_MS = 1000;
  const RECONNECT_MS = 300;
  const RETUNE_TO_NO_SIGNAL_MS = 15000;
  // Lag = seconds between playback and the newest buffered video. OpenTuner
  // sends one fragment per second, so the lag saw-tooths by 1 s; keeping
  // only 0.3 s in reserve (the first version) ran dry whenever a fragment
  // was a little late - short freezes. Now: aim for ~1.5 s and steer with a
  // slightly faster/slower playback rate instead of jumping.
  const TARGET_LAG_S = 1.5;
  const SPEED_UP_ABOVE_S = 3.0;
  const SLOW_DOWN_BELOW_S = 0.6;
  const JUMP_ABOVE_S = 6.0;
  const FAST_RATE = 1.08;
  const SLOW_RATE = 0.95;
  const KEEP_BEHIND_S = 30;
  const UNMUTED_KEY = 'rxPlayerUnmuted';

  class NoSignal extends Error {}

  function boxes(bytes, start, end) {
    const found = [];
    let offset = start;
    while (offset + 8 <= end) {
      const size = new DataView(bytes.buffer, bytes.byteOffset + offset, 4).getUint32(0);
      const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
      if (size < 8 || offset + size > end) break;
      found.push({ type, start: offset, end: offset + size });
      offset += size;
    }
    return found;
  }

  function indexOfType(bytes, type) {
    const codes = Array.from(type, (char) => char.charCodeAt(0));
    for (let i = 0; i + 4 <= bytes.length; i += 1) {
      if (bytes[i] === codes[0] && bytes[i + 1] === codes[1] && bytes[i + 2] === codes[2] && bytes[i + 3] === codes[3]) return i;
    }
    return -1;
  }

  const hex2 = (value) => value.toString(16).padStart(2, '0');

  // MSE codec string from the moov box, e.g. 'avc1.640028,mp4a.40.2'.
  function codecString(moov) {
    const codecs = [];
    const avcC = indexOfType(moov, 'avcC');
    if (avcC >= 0) {
      const config = avcC + 4;
      codecs.push('avc1.' + hex2(moov[config + 1]) + hex2(moov[config + 2]) + hex2(moov[config + 3]));
    }
    if (indexOfType(moov, 'mp4a') >= 0) codecs.push('mp4a.40.2');
    return codecs.join(',');
  }

  function concat(a, b) {
    const joined = new Uint8Array(a.length + b.length);
    joined.set(a, 0);
    joined.set(b, a.length);
    return joined;
  }

  /**
   * elements: { video, freezeCanvas, overlay, statusLabel (optional) }.
   * Returns { start(), stop() } - nothing is fetched until start().
   */
  function createRxPlayer({ video, freezeCanvas, overlay, statusLabel }) {
    let wanted = false;
    let session = 0;
    let abortController = null;
    let retryTimer = null;
    let liveEdgeTimer = null;
    let noSignalTimer = null;
    let covered = false;
    let autoMuting = false;

    function tryPlay() {
      video.play().catch((error) => {
        if (error.name !== 'NotAllowedError' || video.muted) return;
        // Sound not allowed yet on this page load - play muted instead.
        // volumechange fires later (async) - its handler clears the flag.
        autoMuting = true;
        video.muted = true;
        video.play().catch(() => {});
      });
    }

    // Only problems are shown (e.g. a stream this browser can't play);
    // retune/no-signal states are on the video overlay instead.
    function setStatus(text, isError) {
      if (!statusLabel) return;
      statusLabel.textContent = isError ? text : '';
      statusLabel.classList.toggle('d-none', !isError);
    }

    function showOverlay(text) {
      overlay.textContent = text;
      overlay.classList.remove('d-none');
    }

    // Station gone: freeze the last frame under "Retuning…". Call before
    // teardown(), which clears the video.
    function coverWithLastFrame() {
      if (covered) return;
      covered = true;
      if (video.videoWidth && video.readyState >= 2) {
        freezeCanvas.width = video.videoWidth;
        freezeCanvas.height = video.videoHeight;
        freezeCanvas.getContext('2d').drawImage(video, 0, 0);
        freezeCanvas.classList.remove('d-none');
      }
      showOverlay('Retuning…');
      noSignalTimer = window.setTimeout(() => showOverlay('No signal'), RETUNE_TO_NO_SIGNAL_MS);
    }

    function uncover() {
      covered = false;
      if (noSignalTimer) window.clearTimeout(noSignalTimer);
      noSignalTimer = null;
      freezeCanvas.classList.add('d-none');
      overlay.classList.add('d-none');
    }

    // Keeps playback ~TARGET_LAG_S behind the newest video. Jumps only when
    // playback sits before the buffered range (a new station's timeline
    // doesn't start at 0) or is far behind (network hiccup).
    function chaseLiveEdge(sourceBuffer) {
      if (!sourceBuffer.buffered.length || sourceBuffer.updating) return;
      const last = sourceBuffer.buffered.length - 1;
      const start = sourceBuffer.buffered.start(last);
      const end = sourceBuffer.buffered.end(last);
      const lag = end - video.currentTime;
      if (video.currentTime < start || lag > JUMP_ABOVE_S) {
        // New station: wait for the full cushion (2 fragments) before
        // starting, or the first seconds stutter. The frozen frame covers it.
        if (end - start < TARGET_LAG_S) return;
        video.currentTime = Math.max(start, end - TARGET_LAG_S);
        video.playbackRate = 1;
      } else if (lag > SPEED_UP_ABOVE_S) {
        video.playbackRate = FAST_RATE;
      } else if (lag < SLOW_DOWN_BELOW_S) {
        video.playbackRate = SLOW_RATE;
      } else if ((video.playbackRate > 1 && lag < TARGET_LAG_S + 0.5) ||
                 (video.playbackRate < 1 && lag > TARGET_LAG_S)) {
        video.playbackRate = 1;
      }
      if (video.paused) tryPlay();
    }

    async function play(mySession) {
      abortController = new AbortController();
      const response = await fetch('/rx/stream.mp4', { cache: 'no-store', signal: abortController.signal });
      if (response.status === 503) throw new NoSignal();
      if (!response.ok || !response.body) throw new Error('Stream request failed (HTTP ' + response.status + ')');
      const reader = response.body.getReader();

      // Collect bytes until the init segment (ftyp + moov) is complete.
      let pending = new Uint8Array(0);
      let moovBox = null;
      while (!moovBox) {
        const { value, done } = await reader.read();
        if (done) return;  // station gone before it started - reconnect
        pending = concat(pending, value);
        moovBox = boxes(pending, 0, pending.length).find((box) => box.type === 'moov');
      }
      const codecs = codecString(pending.subarray(moovBox.start, moovBox.end));
      const mime = `video/mp4; codecs="${codecs}"`;
      if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) {
        throw new Error(`This browser can't play this stream (${codecs}).`);
      }

      const mediaSource = new MediaSource();
      video.src = URL.createObjectURL(mediaSource);
      await new Promise((resolve) => mediaSource.addEventListener('sourceopen', resolve, { once: true }));
      const sourceBuffer = mediaSource.addSourceBuffer(mime);
      const queue = [pending];
      let appending = false;

      function pump() {
        if (appending || !queue.length || mySession !== session) return;
        const chunk = queue.shift();
        appending = true;
        try {
          sourceBuffer.appendBuffer(chunk);
        } catch (error) {
          appending = false;
          if (error.name !== 'QuotaExceededError' || video.currentTime <= 5) throw error;
          // Free old video, then retry this chunk on the next updateend.
          queue.unshift(chunk);
          appending = true;
          sourceBuffer.remove(0, video.currentTime - 5);
        }
      }
      let startedPlaying = false;
      sourceBuffer.addEventListener('updateend', () => {
        appending = false;
        // First fragment in: go live straight away rather than on the timer.
        if (!startedPlaying) chaseLiveEdge(sourceBuffer);
        pump();
      });
      video.addEventListener('playing', () => {
        if (mySession !== session) return;
        startedPlaying = true;
        uncover();
        setStatus('Playing · ' + codecs);
      }, { once: true });

      liveEdgeTimer = window.setInterval(() => {
        if (sourceBuffer.updating) return;
        chaseLiveEdge(sourceBuffer);
        if (video.currentTime > KEEP_BEHIND_S + 5) sourceBuffer.remove(0, video.currentTime - KEEP_BEHIND_S);
      }, 500);

      pump();
      tryPlay();
      setStatus('Starting · ' + codecs);
      while (mySession === session) {
        const { value, done } = await reader.read();
        if (done) return;  // lost lock / retune - reconnect for the next station
        queue.push(value);
        pump();
      }
    }

    function teardown() {
      if (abortController) abortController.abort();
      abortController = null;
      if (liveEdgeTimer) window.clearInterval(liveEdgeTimer);
      liveEdgeTimer = null;
      if (retryTimer) window.clearTimeout(retryTimer);
      retryTimer = null;
      video.pause();
      video.removeAttribute('src');
      video.load();
    }

    function scheduleConnect(mySession, delayMs) {
      retryTimer = window.setTimeout(() => { if (wanted && mySession === session) connect(); }, delayMs);
    }

    function connect() {
      teardown();
      session += 1;
      const mySession = session;
      play(mySession).then(() => {
        if (mySession !== session || !wanted) return;
        coverWithLastFrame();
        teardown();
        setStatus('Station changed or signal lost - waiting for OpenTuner…');
        scheduleConnect(mySession, RECONNECT_MS);
      }).catch((error) => {
        if (mySession !== session || !wanted) return;
        if (error instanceof NoSignal) {
          // Still retuning (overlay already up), or nothing received yet.
          if (!covered) {
            teardown();
            showOverlay('No signal');
          }
          setStatus('No stream from OpenTuner - waiting (tuner 1 must be locked)…');
        } else {
          coverWithLastFrame();
          teardown();
          setStatus(error.message + ' Retrying…', true);
        }
        scheduleConnect(mySession, NO_SIGNAL_RETRY_MS);
      });
    }

    function start() {
      if (wanted) return;
      wanted = true;
      connect();
    }

    function stop() {
      if (!wanted) return;
      wanted = false;
      session += 1;
      teardown();
      uncover();
      setStatus('');
    }

    window.addEventListener('pagehide', stop);

    // Remember the user's mute choice. Browsers may refuse to start with
    // sound on a fresh page load (tryPlay() then falls back to muted without
    // overwriting the saved choice); Chrome often allows it on sites used a lot.
    video.addEventListener('volumechange', () => {
      if (autoMuting) {
        autoMuting = false;
        return;
      }
      window.localStorage.setItem(UNMUTED_KEY, video.muted ? '0' : '1');
    });
    video.muted = window.localStorage.getItem(UNMUTED_KEY) !== '1';

    return { start, stop };
  }

  window.createRxPlayer = createRxPlayer;

  // RX page: always on while the page is open.
  const rxVideo = document.querySelector('#rx-video');
  if (rxVideo) {
    createRxPlayer({
      video: rxVideo,
      freezeCanvas: document.querySelector('#rx-freeze'),
      overlay: document.querySelector('#rx-overlay'),
      statusLabel: document.querySelector('#rx-player-status'),
    }).start();
  }
}());
