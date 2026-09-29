/**
 * RX page: BATC QO-100 wideband spectrum (same feed and look as the Home
 * page's batc-spectrum.js) where clicking a signal tunes OpenTuner to it via
 * /api/opentuner/tune (Quick Tune UDP - see opentuner_quicktune.py).
 *
 * A clicked signal's centre and symbol rate are measured off the spectrum
 * itself: a DVB-S2 signal's half-power width is roughly its symbol rate, so
 * the measured width is snapped to the nearest standard QO-100 symbol rate.
 */
(function () {
  'use strict';

  const FFT_URL = 'wss://eshail.batc.org.uk/wb/fft';
  const FFT_PROTOCOL = 'fft';
  const START_MHZ = 10490.5;
  const END_MHZ = 10499.5;
  const SPAN_MHZ = END_MHZ - START_MHZ;
  const DISPLAY_FLOOR = 0.12;
  const DISPLAY_CEILING = 0.58;
  const BEACON_MHZ = 10491.5;
  const BEACON_SR = 1500;
  const STANDARD_SRS = [25, 33, 66, 125, 250, 333, 500, 1000, 1500, 2000];
  // How far from the click to look for the signal's peak.
  const SEARCH_MHZ = 0.1;
  // Peak must stand this far above the noise floor (raw Uint16 units).
  const MIN_SIGNAL = 2000;
  const SMOOTH_RADIUS = 3;

  const canvas = document.querySelector('#rx-spectrum-canvas');
  const status = document.querySelector('#batc-status');
  const tunedLabel = document.querySelector('#rx-tuned');
  const message = document.querySelector('#rx-message');

  if (!canvas || !status) return;

  const context = canvas.getContext('2d');
  let socket = null;
  let reconnectTimer = null;
  let latestFrame = null;
  let renderPending = false;
  let intentionallyClosed = false;
  let tunedSignal = null;

  function setStatus(label, style) {
    status.textContent = label;
    status.className = `badge rounded-pill text-bg-${style}`;
  }

  function canvasSize() {
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.floor(canvas.clientWidth));
    const height = Math.max(1, Math.floor(canvas.clientHeight));
    const pixelWidth = Math.floor(width * ratio);
    const pixelHeight = Math.floor(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width, height };
  }

  function draw(frame) {
    const { width, height } = canvasSize();
    const top = 10;
    const bottom = height - 24;
    const plotHeight = bottom - top;
    const frequencyX = (frequency) => (frequency - START_MHZ) * width / SPAN_MHZ;

    context.clearRect(0, 0, width, height);
    context.strokeStyle = 'rgba(127, 151, 169, .20)';
    context.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = top + (plotHeight * i / 4);
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }

    const labels = [10490.5, 10492.5, 10494.5, 10496.5, 10498.5, 10499.5];
    context.font = '10px system-ui, sans-serif';
    context.fillStyle = '#8193a1';
    labels.forEach((frequency, index) => {
      const x = frequencyX(frequency);
      context.beginPath();
      context.moveTo(x, top);
      context.lineTo(x, bottom);
      context.stroke();
      context.textAlign = index === 0 ? 'left' : (index === labels.length - 1 ? 'right' : 'center');
      context.fillText(`${frequency.toFixed(1)}${index === labels.length - 1 ? ' MHz' : ''}`, x, height - 7);
    });

    if (tunedSignal) {
      const halfWidthMhz = tunedSignal.sr * 1.35 / 2000;
      const left = frequencyX(tunedSignal.centerMhz - halfWidthMhz);
      const right = frequencyX(tunedSignal.centerMhz + halfWidthMhz);
      context.fillStyle = 'rgba(37, 194, 110, .18)';
      context.fillRect(left, top, right - left, plotHeight);
      context.strokeStyle = '#25c26e';
      context.lineWidth = 1.5;
      context.strokeRect(left, top, right - left, plotHeight);
    }

    if (!frame || frame.length < 2) {
      context.fillStyle = '#91a4b5';
      context.textAlign = 'center';
      context.fillText('Waiting for BATC spectrum data…', width / 2, height / 2);
      return;
    }

    const points = [];
    for (let x = 0; x < width; x += 1) {
      const sourceIndex = x * (frame.length - 1) / Math.max(1, width - 1);
      const left = Math.floor(sourceIndex);
      const fraction = sourceIndex - left;
      const sample = frame[left] + fraction * (frame[Math.min(left + 1, frame.length - 1)] - frame[left]);
      const normalized = Math.max(0, Math.min(
        1,
        (sample / 65535 - DISPLAY_FLOOR) / (DISPLAY_CEILING - DISPLAY_FLOOR)
      ));
      points.push([x, bottom - normalized * plotHeight]);
    }

    const gradient = context.createLinearGradient(0, top, 0, bottom);
    gradient.addColorStop(0, 'rgba(255, 212, 59, .62)');
    gradient.addColorStop(1, 'rgba(255, 212, 59, .03)');
    context.beginPath();
    context.moveTo(points[0][0], bottom);
    points.forEach((point) => context.lineTo(point[0], point[1]));
    context.lineTo(points[points.length - 1][0], bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();

    context.beginPath();
    points.forEach((point, index) => {
      if (index === 0) context.moveTo(point[0], point[1]);
      else context.lineTo(point[0], point[1]);
    });
    context.strokeStyle = '#ffd43b';
    context.lineWidth = 1.5;
    context.stroke();
  }

  function scheduleDraw(frame) {
    latestFrame = frame;
    if (renderPending) return;
    renderPending = true;
    window.requestAnimationFrame(() => {
      renderPending = false;
      draw(latestFrame);
    });
  }

  function smooth(frame) {
    const result = new Float64Array(frame.length);
    for (let i = 0; i < frame.length; i += 1) {
      let sum = 0;
      let count = 0;
      for (let j = Math.max(0, i - SMOOTH_RADIUS); j <= Math.min(frame.length - 1, i + SMOOTH_RADIUS); j += 1) {
        sum += frame[j];
        count += 1;
      }
      result[i] = sum / count;
    }
    return result;
  }

  function nearestStandardSr(measuredKsps) {
    return STANDARD_SRS.reduce((best, candidate) => (
      Math.abs(Math.log(candidate / measuredKsps)) < Math.abs(Math.log(best / measuredKsps)) ? candidate : best
    ));
  }

  // Returns { centerMhz, sr } for the signal nearest clickedMhz, or null.
  function findSignal(frame, clickedMhz) {
    const bins = frame.length;
    const mhzPerBin = SPAN_MHZ / (bins - 1);
    const binFrequency = (index) => START_MHZ + index * mhzPerBin;
    const smoothed = smooth(frame);
    const noise = Array.from(smoothed).sort((a, b) => a - b)[Math.floor(bins / 2)];

    const clickedBin = Math.round((clickedMhz - START_MHZ) / mhzPerBin);
    const searchBins = Math.round(SEARCH_MHZ / mhzPerBin);
    let peakBin = -1;
    for (let i = Math.max(0, clickedBin - searchBins); i <= Math.min(bins - 1, clickedBin + searchBins); i += 1) {
      if (peakBin < 0 || smoothed[i] > smoothed[peakBin]) peakBin = i;
    }
    if (peakBin < 0 || smoothed[peakBin] - noise < MIN_SIGNAL) return null;

    const halfLevel = noise + (smoothed[peakBin] - noise) / 2;
    let left = peakBin;
    while (left > 0 && smoothed[left - 1] > halfLevel) left -= 1;
    let right = peakBin;
    while (right < bins - 1 && smoothed[right + 1] > halfLevel) right += 1;

    const centerMhz = (binFrequency(left) + binFrequency(right)) / 2;
    if (Math.abs(centerMhz - BEACON_MHZ) < 0.3) return { centerMhz: BEACON_MHZ, sr: BEACON_SR };
    const widthMhz = (right - left + 1) * mhzPerBin;
    return { centerMhz, sr: nearestStandardSr(widthMhz * 1000) };
  }

  // Line under the spectrum - only for problems; hidden (no gap) otherwise.
  function setMessage(text, isError) {
    message.textContent = text;
    message.classList.toggle('text-danger', Boolean(isError));
    message.classList.toggle('d-none', !text);
  }

  async function tune(signal) {
    const downlinkKhz = Math.round(signal.centerMhz * 1000);
    try {
      const response = await fetch('/api/opentuner/tune', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ downlink_khz: downlinkKhz, symbol_rate: signal.sr }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Request failed');
      tunedSignal = signal;
      tunedLabel.textContent = `Tuned: ${(downlinkKhz / 1000).toFixed(3)} MHz · ${signal.sr} kS/s`;
      setMessage('');
      draw(latestFrame);
    } catch (error) {
      setMessage('Could not tune: ' + error.message, true);
    }
  }

  function connect() {
    if (document.hidden || socket) return;
    intentionallyClosed = false;
    setStatus('CONNECTING', 'secondary');

    try {
      socket = new WebSocket(FFT_URL, FFT_PROTOCOL);
      socket.binaryType = 'arraybuffer';
    } catch (_error) {
      socket = null;
      scheduleReconnect();
      return;
    }

    socket.addEventListener('open', () => setStatus('LIVE', 'success'));
    socket.addEventListener('message', (event) => {
      if (event.data instanceof ArrayBuffer) {
        scheduleDraw(new Uint16Array(event.data));
      }
    });
    socket.addEventListener('close', () => {
      socket = null;
      if (!intentionallyClosed) scheduleReconnect();
    });
    socket.addEventListener('error', () => setStatus('DISCONNECTED', 'danger'));
  }

  function scheduleReconnect() {
    setStatus('DISCONNECTED', 'danger');
    if (reconnectTimer || document.hidden) return;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 2000);
  }

  function disconnect() {
    intentionallyClosed = true;
    if (reconnectTimer) window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
    if (socket) socket.close();
    socket = null;
    setStatus('PAUSED', 'secondary');
  }

  canvas.addEventListener('click', (event) => {
    if (!latestFrame || latestFrame.length < 2) return;
    const rectangle = canvas.getBoundingClientRect();
    const clickedMhz = START_MHZ + (event.clientX - rectangle.left) * SPAN_MHZ / canvas.clientWidth;
    const signal = findSignal(latestFrame, clickedMhz);
    if (!signal) {
      setMessage(`No signal found near ${clickedMhz.toFixed(3)} MHz.`);
      return;
    }
    tune(signal);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) disconnect();
    else connect();
  });
  window.addEventListener('resize', () => draw(latestFrame));
  window.addEventListener('pagehide', disconnect);

  draw(null);
  connect();
  // Start every page load on the beacon - always on air, so OpenTuner locks.
  tune({ centerMhz: BEACON_MHZ, sr: BEACON_SR });
}());
