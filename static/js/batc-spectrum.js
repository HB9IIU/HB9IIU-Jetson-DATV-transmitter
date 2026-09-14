/**
 * Read-only BATC QO-100 wideband FFT display.
 * Data format follows BATC's official web client: binary Uint16 FFT frames.
 */
(function () {
  'use strict';

  const FFT_URL = 'wss://eshail.batc.org.uk/wb/fft';
  const FFT_PROTOCOL = 'fft';
  const START_MHZ = 10490.5;
  const END_MHZ = 10499.5;
  const DISPLAY_FLOOR = 0.12;
  const DISPLAY_CEILING = 0.58;
  const CHANNEL_CENTERS = Array.from({ length: 14 }, (_, index) => 10492.75 + index * 0.5);
  const TRANSPONDER_OFFSET_MHZ = 8090;
  const canvas = document.querySelector('#batc-spectrum-canvas');
  const status = document.querySelector('#batc-status');
  const frequencyInput = document.querySelector('#frequency');

  if (!canvas || !status) return;

  const context = canvas.getContext('2d');
  let socket = null;
  let reconnectTimer = null;
  let latestFrame = null;
  let renderPending = false;
  let intentionallyClosed = false;
  let slotStates = new Map();
  let selectedCenter = null;

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
    const bottom = height - 44;
    const plotHeight = bottom - top;

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
      const x = index * width / (labels.length - 1);
      context.beginPath();
      context.moveTo(x, top);
      context.lineTo(x, bottom);
      context.stroke();
      context.textAlign = index === 0 ? 'left' : (index === labels.length - 1 ? 'right' : 'center');
      context.fillText(`${frequency.toFixed(1)}${index === labels.length - 1 ? ' MHz' : ''}`, x, height - 7);
    });

    function frequencyX(frequency) {
      return (frequency - START_MHZ) * width / (END_MHZ - START_MHZ);
    }

    const channelSpacing = frequencyX(CHANNEL_CENTERS[1]) - frequencyX(CHANNEL_CENTERS[0]);
    const slotWidth = Math.max(6, channelSpacing * 0.62);
    let noiseFloor = null;
    let activityThreshold = null;
    if (frame && frame.length >= 2) {
      const sortedFrame = Array.from(frame).sort((a, b) => a - b);
      noiseFloor = sortedFrame[Math.floor(sortedFrame.length / 2)];

      const beaconStart = Math.floor((10490.8 - START_MHZ) * frame.length / (END_MHZ - START_MHZ));
      const beaconEnd = Math.ceil((10492.2 - START_MHZ) * frame.length / (END_MHZ - START_MHZ));
      const beaconSamples = Array.from(frame.slice(beaconStart, beaconEnd)).sort((a, b) => a - b);
      const beaconLevel = beaconSamples[Math.floor(beaconSamples.length / 2)];
      activityThreshold = noiseFloor + (beaconLevel - noiseFloor) * 0.5;
    }

    function slotHasTransmission(center) {
      if (activityThreshold === null) return null;
      const startIndex = Math.max(0, Math.floor((center - 0.2 - START_MHZ) * frame.length / (END_MHZ - START_MHZ)));
      const endIndex = Math.min(frame.length, Math.ceil((center + 0.2 - START_MHZ) * frame.length / (END_MHZ - START_MHZ)));
      const samples = Array.from(frame.slice(startIndex, endIndex)).sort((a, b) => a - b);
      const upperQuartile = samples[Math.floor(samples.length * 0.75)];
      return upperQuartile > activityThreshold;
    }

    slotStates = new Map();
    CHANNEL_CENTERS.forEach((center) => {
      const active = slotHasTransmission(center);
      slotStates.set(center, active);
      context.fillStyle = active === null ? '#647483' : (active ? '#ff4d5e' : '#25c26e');
      context.fillRect(frequencyX(center) - slotWidth / 2, bottom + 10, slotWidth, 6);
      if (center === selectedCenter) {
        context.strokeStyle = '#ffffff';
        context.lineWidth = 2;
        context.strokeRect(frequencyX(center) - slotWidth / 2 - 2, bottom + 8, slotWidth + 4, 10);
      }
    });
    context.fillStyle = '#8fa6b8';
    context.textAlign = 'left';
    context.font = '9px system-ui, sans-serif';
    context.fillText('500 kHz slots', 4, bottom + 15);

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
    if (!frequencyInput || frequencyInput.disabled) return;
    const rectangle = canvas.getBoundingClientRect();
    const x = event.clientX - rectangle.left;
    const y = event.clientY - rectangle.top;
    const slotY = canvas.clientHeight - 34;
    if (Math.abs(y - slotY) > 10) return;

    const clickedFrequency = START_MHZ + x * (END_MHZ - START_MHZ) / canvas.clientWidth;
    const center = CHANNEL_CENTERS.reduce((closest, candidate) => (
      Math.abs(candidate - clickedFrequency) < Math.abs(closest - clickedFrequency) ? candidate : closest
    ));
    const spacingPixels = 0.5 * canvas.clientWidth / (END_MHZ - START_MHZ);
    const centerPixels = (center - START_MHZ) * canvas.clientWidth / (END_MHZ - START_MHZ);
    if (Math.abs(x - centerPixels) > spacingPixels * 0.31 || slotStates.get(center) !== false) return;

    selectedCenter = center;
    frequencyInput.value = (center - TRANSPONDER_OFFSET_MHZ).toFixed(3);
    frequencyInput.dispatchEvent(new Event('change', { bubbles: true }));
    draw(latestFrame);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) disconnect();
    else connect();
  });
  window.addEventListener('resize', () => draw(latestFrame));
  window.addEventListener('pagehide', disconnect);

  draw(null);
  connect();
}());








