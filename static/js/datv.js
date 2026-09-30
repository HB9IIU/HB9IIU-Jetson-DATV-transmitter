/**
 * Presentation-only source selection. No hardware or Flask API calls are made.
 */
const sourceOptions = document.querySelectorAll('input[name="source-type"]');
const cameraPanel = document.querySelector('#camera-source-panel');
const filePanel = document.querySelector('#file-source-panel');
const cameraSelect = document.querySelector('#video-source');
const audioSelect = document.querySelector('#audio-source');
const preview = document.querySelector('#camera-preview');
const previewImage = document.querySelector('#camera-preview-image');
const previewMessage = document.querySelector('#preview-message');
const testcardOptions = document.querySelector('#testcard-options');
const videoOptions = document.querySelector('#video-options');
const testcardSelect = document.querySelector('#testcard-source');
const testcardPrevButton = document.querySelector('#testcard-prev');
const testcardNextButton = document.querySelector('#testcard-next');
const testcardCarouselLabel = document.querySelector('#testcard-carousel-label');
const videoSelect = document.querySelector('#prepared-video');
const filePreview = document.querySelector('#file-preview');
const filePreviewImage = document.querySelector('#file-preview-image');
const filePreviewMessage = document.querySelector('#file-preview-message');
let filePreviewRequest = 0;

function stopPreview() {
  previewImage.removeAttribute('src');
  preview.classList.remove('is-live');
}

function startPreview() {
  const selected = cameraSelect.options[cameraSelect.selectedIndex];
  const previewUrl = selected ? selected.dataset.previewUrl : '';
  stopPreview();
  if (!previewUrl) {
    previewMessage.textContent = 'NO CAMERA DETECTED';
    return;
  }
  previewMessage.textContent = 'STARTING PREVIEW…';
  previewImage.src = `${previewUrl}&_=${Date.now()}`;
  // An MJPEG response is an open-ended multipart stream, so some browsers
  // never dispatch a conventional completed `load` event. Display the image
  // immediately; the error handler below restores the message if it fails.
  preview.classList.add('is-live');
}

function showFilePreview(select, unavailableText) {
  const requestId = ++filePreviewRequest;
  const selected = select.options[select.selectedIndex];
  const previewUrl = selected ? selected.dataset.previewUrl : '';
  filePreview.classList.remove('is-live');
  filePreviewImage.removeAttribute('src');
  if (!previewUrl) {
    filePreviewMessage.textContent = unavailableText;
    return;
  }
  filePreviewMessage.textContent = 'LOADING PREVIEW…';
  // Install handlers before assigning src. The request ID prevents an older
  // response from changing the state after another item has been selected.
  filePreviewImage.onload = () => {
    if (requestId !== filePreviewRequest) return;
    filePreview.classList.add('is-live');
  };
  filePreviewImage.onerror = () => {
    if (requestId !== filePreviewRequest) return;
    filePreview.classList.remove('is-live');
    filePreviewMessage.textContent = 'PREVIEW UNAVAILABLE';
  };
  filePreviewImage.src = previewUrl;
}

function updateFileType() {
  const testcardSelected = document.querySelector('#source-testcard').checked;
  testcardOptions.classList.toggle('d-none', !testcardSelected);
  videoOptions.classList.toggle('d-none', testcardSelected);
  testcardPrevButton?.classList.toggle('d-none', !testcardSelected);
  testcardNextButton?.classList.toggle('d-none', !testcardSelected);
  showFilePreview(
    testcardSelected ? testcardSelect : videoSelect,
    testcardSelected ? 'NO TESTCARDS FOUND' : 'NO PREPARED VIDEOS FOUND'
  );
  if (testcardSelected) updateTestcardCarouselLabel();
}

function updateTestcardCarouselLabel() {
  if (!testcardCarouselLabel) return;
  const selected = testcardSelect.options[testcardSelect.selectedIndex];
  testcardCarouselLabel.textContent = selected && selected.value ? selected.textContent : 'NO TESTCARDS FOUND';
}

function stepTestcard(direction) {
  const optionCount = testcardSelect.options.length;
  if (optionCount === 0) return;
  testcardSelect.selectedIndex = (testcardSelect.selectedIndex + direction + optionCount) % optionCount;
  testcardSelect.dispatchEvent(new Event('change'));
}

testcardPrevButton?.addEventListener('click', () => stepTestcard(-1));
testcardNextButton?.addEventListener('click', () => stepTestcard(1));

// The browser previews are small, dedicated JPEGs. Warm them into the
// browser cache shortly after page load so the carousel arrows respond
// immediately, even when the Jetson is busy starting or stopping a stream.
function preloadTestcardPreviews() {
  testcardSelect?.querySelectorAll('option[data-preview-url]').forEach((option) => {
    const previewUrl = option.dataset.previewUrl;
    if (previewUrl) {
      const image = new Image();
      image.src = previewUrl;
    }
  });
}

window.setTimeout(preloadTestcardPreviews, 250);

previewImage.addEventListener('load', () => preview.classList.add('is-live'));
previewImage.addEventListener('error', () => {
  preview.classList.remove('is-live');
  previewMessage.textContent = 'PREVIEW UNAVAILABLE';
});
cameraSelect.addEventListener('change', startPreview);
testcardSelect.addEventListener('change', () => {
  showFilePreview(testcardSelect, 'NO TESTCARDS FOUND');
  updateTestcardCarouselLabel();
});
videoSelect.addEventListener('change', () => showFilePreview(videoSelect, 'NO PREPARED VIDEOS FOUND'));

sourceOptions.forEach((option) => {
  option.addEventListener('change', () => {
    const cameraSelected = option.value === 'camera';
    cameraPanel.classList.toggle('d-none', !cameraSelected);
    filePanel.classList.toggle('d-none', cameraSelected);
    if (cameraSelected) {
      startPreview();
    } else {
      stopPreview();
      updateFileType();
    }
  });
});

if (document.querySelector('#source-camera').checked) {
  startPreview();
} else {
  updateFileType();
}

const frequencyInput = document.querySelector('#frequency');
const symbolRateSelect = document.querySelector('#symbol-rate');
const fecSelect = document.querySelector('#fec');
const signalSummary = document.querySelector('#signal-summary');

function updateSignalSummary() {
  if (!frequencyInput || !symbolRateSelect || !fecSelect || !signalSummary) return;
  const frequency = frequencyInput.value ? `${frequencyInput.value} MHz` : 'Select a frequency';
  signalSummary.textContent = `DVB-S2 · QPSK · ${frequency} · ${symbolRateSelect.value} kS/s · FEC ${fecSelect.value}`;
}

frequencyInput?.addEventListener('change', updateSignalSummary);
symbolRateSelect?.addEventListener('change', updateSignalSummary);
fecSelect?.addEventListener('change', updateSignalSummary);
updateSignalSummary();

// Prepared videos live in preprocessed_<W>x<H>/ folders, one per
// resolution - which folder is right depends on the selected SR/FEC (see
// video_folder_for_sr_fec() in app.py), so the list has to refresh
// whenever either selector changes, not just once at page load.
async function refreshPreparedVideos() {
  if (!symbolRateSelect || !fecSelect || !videoSelect) return;
  const previouslySelected = videoSelect.value;
  try {
    const response = await fetch(
      `/api/videos?symbol_rate=${encodeURIComponent(symbolRateSelect.value)}&fec=${encodeURIComponent(fecSelect.value)}`
    );
    if (!response.ok) throw new Error('Video list request failed');
    const data = await response.json();
    videoSelect.innerHTML = '';
    if (data.videos.length === 0) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No prepared videos found';
      videoSelect.appendChild(option);
    } else {
      data.videos.forEach((video) => {
        const option = document.createElement('option');
        option.value = video.value;
        option.textContent = video.label;
        option.dataset.previewUrl = video.preview_url;
        videoSelect.appendChild(option);
      });
      if (data.videos.some((video) => video.value === previouslySelected)) {
        videoSelect.value = previouslySelected;
      }
    }
  } catch (_error) {
    videoSelect.innerHTML = '<option value="">Unable to load videos</option>';
  }
  if (document.querySelector('#source-video')?.checked) {
    showFilePreview(videoSelect, 'NO PREPARED VIDEOS FOUND');
  }
}

symbolRateSelect?.addEventListener('change', refreshPreparedVideos);
fecSelect?.addEventListener('change', refreshPreparedVideos);

// USB video keys are managed on their own page now (see setup.html /
// static/js/setup.js) - the prepared-videos list here is already always
// fresh (both the server-rendered initial list and refreshPreparedVideos()
// re-scan SD card + USB key on every call, see app.py's
// _preprocessed_roots()), so there's nothing to poll or react to here.

const txGain = document.querySelector('#tx-gain');
const txGainValue = document.querySelector('#tx-gain-value');
function updateTxGain() {
  if (!txGain || !txGainValue) return;
  const dbValue = parseFloat(txGain.value);
  const min = parseFloat(txGain.min);
  const max = parseFloat(txGain.max);
  const percent = Math.round(((dbValue - min) / (max - min)) * 100);
  txGainValue.textContent = `${percent}%`;
}
txGain?.addEventListener('input', updateTxGain);
updateTxGain();

// Live TX gain: publishes over MQTT immediately, independent of whether a
// stream is currently running, so dragging the slider changes real RF
// power mid-transmission - not just a value applied once at stream start.
// Debounced (not sent on every pixel of drag) so a fast drag doesn't flood
// MQTT with dozens of commands per second.
let gainSendTimer = null;
function sendLiveGain() {
  if (!txGain) return;
  clearTimeout(gainSendTimer);
  gainSendTimer = setTimeout(() => {
    fetch('/api/gain', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gain_db: txGain.value }),
    }).catch(() => {});
  }, 120);
}
txGain?.addEventListener('input', sendLiveGain);

function resetTxGain() {
  if (!txGain) return;
  txGain.value = txGain.min;
  updateTxGain();
  sendLiveGain();
}

function formatTelemetryNumber(value, decimals, suffix) {
  return typeof value === 'number' ? `${value.toFixed(decimals)}${suffix}` : `--${suffix}`;
}

async function updateTelemetry() {
  const connection = document.querySelector('#pluto-connection');
  try {
    const response = await fetch('/api/telemetry', { cache: 'no-store' });
    if (!response.ok) throw new Error('Telemetry request failed');
    const data = await response.json();
    document.querySelector('#jetson-cpu-temp').textContent = formatTelemetryNumber(data.jetson_cpu_temp_c, 1, ' °C');
    document.querySelector('#jetson-cpu-load').textContent = formatTelemetryNumber(data.jetson_cpu_load_percent, 0, '%');
    document.querySelector('#fan-state').textContent = data.fan?.state || '--';
    document.querySelector('#fan-pwm').textContent = typeof data.fan?.pwm === 'number' ? `PWM ${data.fan.pwm}` : 'PWM --';
    document.querySelector('#pluto-temp').textContent = formatTelemetryNumber(data.pluto_temp_c, 1, ' °C');
    connection.innerHTML = `<span class="nav-device-dot" aria-hidden="true"></span>Pluto ${data.pluto_connected ? 'connected' : 'disconnected'}`;
    connection.className = `nav-device-status ${data.pluto_connected ? 'nav-device-online' : 'nav-device-offline'}`;
  } catch (_error) {
    connection.innerHTML = '<span class="nav-device-dot" aria-hidden="true"></span>Pluto unavailable';
    connection.className = 'nav-device-status nav-device-offline';
  }
}

updateTelemetry();
window.setInterval(updateTelemetry, 2000);

// Operator-controlled PTT relay (see pa_relay.py). The local Pluto RX
// spectrum supplies advisory status; only the operator controls the relay.
// This state is human-timescale (state changes are debounced server-side
// over several seconds of frames), so it's polled at 1s like
// #stream-status-message - not at the FFT canvas's 150ms redraw rate.
const spectrumAdvisoryStatus = document.querySelector('#spectrum-advisory-status');
const paRelayEngageButton = document.querySelector('#pa-relay-engage-button');
const paRelayDisengageButton = document.querySelector('#pa-relay-disengage-button');
let pttRelayEngaged = false;
// Drawn green in the local RX spectrum while the advisory says "seems OK".
let spectrumLooksOk = false;

// Spectrum-only advisory shown next to the signal summary. It has no role
// in PTT relay availability or switching.
function renderSpectrumAdvisory(data) {
  spectrumLooksOk = Boolean(data.streaming && data.detector?.is_plateau);
  let label = 'Spectrum: waiting for stream';
  let title = 'Start the stream to check the local Pluto RX spectrum';
  let statusClass = 'text-secondary';
  if (data.streaming) {
    if (!data.detector) {
      label = 'Checking spectrum…';
      title = 'Waiting for spectrum data';
      statusClass = 'text-amber';
    } else if (data.detector.is_plateau) {
      label = 'Spectrum seems OK';
      title = 'The local RX spectrum looks like a stable DVB-S2 signal';
      statusClass = 'text-success';
    } else {
      label = 'Check spectrum';
      title = data.detector.reason || 'The spectrum does not look stable';
      statusClass = 'text-danger';
    }
  }
  if (spectrumAdvisoryStatus) {
    spectrumAdvisoryStatus.innerHTML = `<span class="telemetry-dot me-2"></span>${label}`;
    spectrumAdvisoryStatus.title = title;
    spectrumAdvisoryStatus.className = `small fw-semibold text-nowrap ms-auto ${statusClass}`;
  }
}

function renderPttButtons() {
  const streamIsRunning = streamState === 'streaming';
  paRelayEngageButton?.classList.toggle('d-none', !streamIsRunning || pttRelayEngaged);
  paRelayDisengageButton?.classList.toggle('d-none', !pttRelayEngaged);
}

async function fetchPttRelayStatus() {
  try {
    const response = await fetch('/api/relay/status', { cache: 'no-store' });
    if (!response.ok) throw new Error('Relay status request failed');
    const data = await response.json();
    pttRelayEngaged = Boolean(data.engaged);
    renderPttButtons();
  } catch (_error) {
    pttRelayEngaged = false;
    renderPttButtons();
  }
}

async function fetchSpectrumStatus() {
  try {
    const response = await fetch('/api/spectrum/status', { cache: 'no-store' });
    if (!response.ok) throw new Error('Spectrum status request failed');
    renderSpectrumAdvisory(await response.json());
  } catch (_error) {
    spectrumLooksOk = false;
    if (spectrumAdvisoryStatus) {
      spectrumAdvisoryStatus.innerHTML = '<span class="telemetry-dot me-2"></span>Spectrum status unavailable';
      spectrumAdvisoryStatus.title = 'The spectrum status could not be loaded';
      spectrumAdvisoryStatus.className = 'small fw-semibold text-nowrap ms-auto text-secondary';
    }
  }
}

paRelayEngageButton?.addEventListener('click', async () => {
  if (!spectrumLooksOk && !await showAppConfirm(
    'Spectrum does not look OK',
    'Are you sure you want to switch the PTT relay on?',
    'Switch PTT relay ON')) {
    return;
  }
  paRelayEngageButton.disabled = true;
  try {
    const response = await fetch('/api/relay/engage', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not switch the PTT relay on');
  } catch (error) {
    showAppAlert(error.message);
  } finally {
    paRelayEngageButton.disabled = false;
    await fetchPttRelayStatus();
  }
});

// No confirmation on the way down - removing power from the pre-amp is
// never gated, unlike engaging it.
paRelayDisengageButton?.addEventListener('click', async () => {
  paRelayDisengageButton.disabled = true;
  try {
    await fetch('/api/relay/disengage', { method: 'POST' });
  } catch (_error) {
    // best-effort - the poll below reflects whatever the server reports
    // either way, and disengage is always safe to retry.
  } finally {
    paRelayDisengageButton.disabled = false;
    await fetchPttRelayStatus();
  }
});

fetchPttRelayStatus();
fetchSpectrumStatus();
window.setInterval(fetchPttRelayStatus, 1000);
window.setInterval(fetchSpectrumStatus, 1000);

const localFftCanvas = document.querySelector('#local-fft-canvas');
const localFftStatus = document.querySelector('#local-fft-status');
const localFftContext = localFftCanvas?.getContext('2d');
const localFftFreqLeft = document.querySelector('#local-fft-freq-left');
const localFftFreqCenter = document.querySelector('#local-fft-freq-center');
const localFftFreqRight = document.querySelector('#local-fft-freq-right');

function formatMHz(hz) {
  return `${(hz / 1e6).toFixed(3)} MHz`;
}

// Our own signal always sits exactly in the middle of the capture by
// construction (app.py always tunes the Pluto's RX to our own TX
// frequency), so a centre crop reliably isolates it. zoomSpanHz is
// app.py's exact calculation (margin + occupied-bandwidth + margin,
// margin == occupied bandwidth) from the live symbol rate - not a guessed
// fraction, so the margins on each side of the plateau end up the same
// width as the plateau itself.
function cropToCenter(bins, spanHz, zoomSpanHz) {
  const hzPerBin = spanHz / (bins.length - 1);
  const cropCount = Math.max(8, Math.min(bins.length, Math.round(zoomSpanHz / hzPerBin)));
  const start = Math.floor((bins.length - cropCount) / 2);
  return bins.slice(start, start + cropCount);
}

function updateLocalFftAxis(data) {
  if (!localFftFreqLeft || !localFftFreqCenter || !localFftFreqRight) return;
  if (data.center_hz && data.zoom_span_hz) {
    localFftFreqLeft.textContent = formatMHz(data.center_hz - data.zoom_span_hz / 2);
    localFftFreqCenter.textContent = formatMHz(data.center_hz);
    localFftFreqRight.textContent = formatMHz(data.center_hz + data.zoom_span_hz / 2);
  } else {
    localFftFreqLeft.textContent = '--';
    localFftFreqCenter.textContent = '--';
    localFftFreqRight.textContent = '--';
  }
}

function drawLocalFft(bins, spanHz, zoomSpanHz) {
  if (!localFftContext || !localFftCanvas) return;
  // Match the canvas's internal pixel buffer to its displayed CSS size -
  // without this it renders at a fixed default resolution (300x150) and
  // looks blurry/stretched inside the actual panel size.
  const displayWidth = localFftCanvas.clientWidth;
  const displayHeight = localFftCanvas.clientHeight;
  if (localFftCanvas.width !== displayWidth) localFftCanvas.width = displayWidth;
  if (localFftCanvas.height !== displayHeight) localFftCanvas.height = displayHeight;
  const width = localFftCanvas.width;
  const height = localFftCanvas.height;
  localFftContext.clearRect(0, 0, width, height);
  if (!bins || bins.length === 0) return;

  const plotted = (spanHz && zoomSpanHz) ? cropToCenter(bins, spanHz, zoomSpanHz) : bins;

  // Auto-scale to the plotted range's own min/max, same reasoning as
  // fft_viewer.html: real RX noise-floor values are a small fraction of
  // full 16-bit scale, so a fixed axis would flatten everything to an
  // invisible line near zero even with a real signal present.
  let frameMin = plotted[0];
  let frameMax = plotted[0];
  for (let i = 1; i < plotted.length; i++) {
    if (plotted[i] < frameMin) frameMin = plotted[i];
    if (plotted[i] > frameMax) frameMax = plotted[i];
  }
  const range = Math.max(1, frameMax - frameMin);
  // Leave headroom above the plateau instead of letting it touch the top
  // edge - the trace only ever uses the bottom part of the canvas height.
  const topMarginFraction = 0.2;
  const plotHeight = height * (1 - topMarginFraction);
  const points = plotted.map((value, i) => ({
    x: (i / (plotted.length - 1)) * width,
    y: height - ((value - frameMin) / range) * plotHeight,
  }));

  // Filled area under the trace, fading out towards the bottom - same
  // idea as the BATC spectrum panel's look.
  const rgb = spectrumLooksOk ? '51, 209, 122' : '255, 51, 51';
  const gradient = localFftContext.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, `rgba(${rgb}, .35)`);
  gradient.addColorStop(1, `rgba(${rgb}, 0)`);
  localFftContext.beginPath();
  localFftContext.moveTo(points[0].x, height);
  for (const point of points) localFftContext.lineTo(point.x, point.y);
  localFftContext.lineTo(points[points.length - 1].x, height);
  localFftContext.closePath();
  localFftContext.fillStyle = gradient;
  localFftContext.fill();

  localFftContext.strokeStyle = `rgb(${rgb})`;
  localFftContext.lineWidth = 1.5;
  localFftContext.beginPath();
  points.forEach((point, i) => {
    if (i === 0) localFftContext.moveTo(point.x, point.y);
    else localFftContext.lineTo(point.x, point.y);
  });
  localFftContext.stroke();
}

async function fetchLocalFft() {
  if (!localFftCanvas) return;
  try {
    const response = await fetch('/api/fft', { cache: 'no-store' });
    if (!response.ok) throw new Error('FFT request failed');
    const data = await response.json();
    const hasSignal = Array.isArray(data.bins) && data.bins.length > 0;
    localFftStatus.textContent = hasSignal ? 'SIGNAL' : 'NO SIGNAL';
    localFftStatus.classList.toggle('text-bg-success', hasSignal);
    localFftStatus.classList.toggle('text-bg-secondary', !hasSignal);
    updateLocalFftAxis(data);
    drawLocalFft(data.bins, data.span_hz, data.zoom_span_hz);
  } catch (_error) {
    localFftStatus.textContent = 'UNAVAILABLE';
    localFftStatus.classList.remove('text-bg-success');
    localFftStatus.classList.add('text-bg-secondary');
    drawLocalFft(null);
  }
}

if (localFftCanvas) {
  fetchLocalFft();
  window.setInterval(fetchLocalFft, 150);
}

const streamToggle = document.querySelector('#stream-toggle');
const copyLogButton = document.querySelector('#copy-log-button');
let streamState = 'stopped';
// The engine keeps its last error until the next start, so only show it if
// this page itself saw the start - not again on every refresh.
let streamStartSeen = false;

copyLogButton?.addEventListener('click', async () => {
  const text = document.querySelector('#stream-status-message')?.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    const originalLabel = copyLogButton.textContent;
    copyLogButton.textContent = 'Copied!';
    window.setTimeout(() => { copyLogButton.textContent = originalLabel; }, 1500);
  } catch (_error) {
    showAppAlert('Could not copy to clipboard - your browser may be blocking clipboard access on this connection (clipboard access usually requires HTTPS or localhost).');
  }
});

function setSourceControlsDisabled(disabled) {
  sourceOptions.forEach((input) => { input.disabled = disabled; });
  // While on air the source can't be changed anyway - collapse card 1 to
  // just its three source buttons (the active one stays highlighted) to
  // save vertical space; everything comes back on Stop.
  document.querySelector('#source-card')?.classList.toggle('source-collapsed', disabled);
  [cameraSelect, audioSelect, testcardSelect, videoSelect,
    testcardPrevButton, testcardNextButton,
    frequencyInput, symbolRateSelect, fecSelect].forEach((element) => {
    if (element) element.disabled = disabled;
  });
}

/**
 * On-air monitor: while transmitting, card 1 shows our own signal as
 * received back from the satellite - OpenTuner (auto-tuned to our downlink
 * on stream start, see the Setup page) pushes it to the Jetson, same player
 * and status feed as the RX page (rx-player.js / rx-info.js).
 * Test without going on air: open the Home page with ?monitor=test - the
 * monitor shows while stopped and OpenTuner is tuned to the beacon.
 */
const MONITOR_TEST = new URLSearchParams(window.location.search).get('monitor') === 'test';
const sourceCard = document.querySelector('#source-card');
const sourceCardTitle = document.querySelector('#source-card-title');
const txMonitor = document.querySelector('#tx-monitor');
const txMonitorPlayer = txMonitor && window.createRxPlayer ? window.createRxPlayer({
  video: document.querySelector('#tx-monitor-video'),
  freezeCanvas: document.querySelector('#tx-monitor-freeze'),
  overlay: document.querySelector('#tx-monitor-overlay'),
}) : null;
const txMonitorInfo = txMonitor && window.createRxInfo ? window.createRxInfo({
  lockBadge: document.querySelector('#tx-monitor-lock'),
  marginValue: document.querySelector('#tx-monitor-margin'),
  merValue: document.querySelector('#tx-monitor-mer'),
  qualityBar: document.querySelector('#tx-monitor-quality'),
  summary: document.querySelector('#tx-monitor-summary'),
}) : null;
let txMonitorShown = false;
const txCalLabel = document.querySelector('#tx-monitor-txcal');
const txCalApply = document.querySelector('#tx-monitor-txcal-apply');
let txCalTimer = null;

// "TX correction" line: the Pluto's TX error measured on our own signal
// (app.py's _measure_tx()), applied from the next stream start on.
async function refreshTxCalibration() {
  try {
    const response = await fetch('/api/tx/calibration', { cache: 'no-store' });
    if (!response.ok) return;
    const cal = await response.json();
    const fmt = (khz) => (khz > 0 ? '+' : '') + khz + ' kHz';
    let text = cal.status ? 'TX error: ' + cal.status.toLowerCase() : 'TX error: waiting for data…';
    let canApply = false;
    if (cal.suggested_khz !== null) {
      if (cal.suggested_khz === cal.tx_correction_khz) {
        text = 'TX correction ' + fmt(cal.tx_correction_khz) + ' ✓ confirmed';
      } else {
        text = 'TX error measured: correction ' + fmt(cal.suggested_khz)
          + ' (now ' + fmt(cal.tx_correction_khz) + ')';
        canApply = !cal.auto_apply;
      }
    }
    txCalLabel.textContent = text;
    txCalLabel.title = text;
    txCalApply.classList.toggle('d-none', !canApply);
  } catch (_error) {
    // Next poll will tell.
  }
}

txCalApply?.addEventListener('click', async () => {
  txCalApply.disabled = true;
  try {
    const response = await fetch('/api/tx/calibration/apply', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    showAppAlert('TX correction set to ' + result.tx_correction_khz + ' kHz - used from the next stream start.');
    await refreshTxCalibration();
  } catch (error) {
    showAppAlert(error.message);
  } finally {
    txCalApply.disabled = false;
  }
});

function setTxMonitor(show) {
  if (!txMonitorPlayer || show === txMonitorShown) return;
  txMonitorShown = show;
  sourceCard.classList.toggle('monitor-active', show);
  txMonitor.classList.toggle('d-none', !show);
  sourceCardTitle.textContent = show ? 'On-air monitor' : 'Select source';
  // "1" = step 1 (choose a source) - meaningless for the monitor.
  document.querySelector('#source-card-step')?.classList.toggle('d-none', show);
  if (show) {
    txMonitorPlayer.start();
    txMonitorInfo.start();
    refreshTxCalibration();
    txCalTimer = window.setInterval(refreshTxCalibration, 2000);
  } else {
    txMonitorPlayer.stop();
    txMonitorInfo.stop();
    if (txCalTimer) window.clearInterval(txCalTimer);
    txCalTimer = null;
  }
}

if (MONITOR_TEST) {
  fetch('/api/opentuner/tune', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ downlink_khz: 10491500, symbol_rate: 1500 }),
  }).catch(() => {});
  setTxMonitor(true);
}

function renderStreamButton(status) {
  if (!streamToggle) return;
  const previousStreamState = streamState;
  streamState = status.state;
  renderPttButtons();
  setTxMonitor(status.state === 'streaming' || MONITOR_TEST);
  // Source can only be changed while fully stopped - switching source mid-
  // stream would need a pipeline restart (a brief RF dropout while the
  // receiver re-acquires lock), so we require an explicit Stop first
  // instead of restarting automatically underneath the user.
  setSourceControlsDisabled(status.state !== 'stopped' && status.state !== 'error');
  // Reset to 0% (near-off) whenever a stream actually ends - a safety
  // default so the next start never silently reuses whatever power level
  // was left over from before, on a page nobody refreshed.
  if (status.state === 'stopped' && previousStreamState !== 'stopped') {
    resetTxGain();
  }
  if (status.state === 'starting' || status.state === 'streaming') streamStartSeen = true;
  if (status.state === 'stopped') streamStartSeen = false;
  const hasErrorText = status.state === 'error' && !!status.last_error && streamStartSeen;
  const busy = status.state === 'starting' || status.state === 'stopping';
  streamToggle.disabled = busy;
  streamToggle.classList.toggle('btn-success', status.state !== 'streaming' && !hasErrorText);
  streamToggle.classList.toggle('btn-danger', status.state === 'streaming' || hasErrorText);
  if (status.state === 'streaming') streamToggle.textContent = '■ Stop stream';
  else if (status.state === 'starting') streamToggle.textContent = 'Starting…';
  else if (status.state === 'stopping') streamToggle.textContent = 'Stopping…';
  else streamToggle.textContent = '▶ Start stream';
  streamToggle.title = status.state === 'streaming' ? 'Transmitting via Pluto' : '';

  const statusMessage = document.querySelector('#stream-status-message');
  if (statusMessage) {
    statusMessage.classList.toggle('text-danger', hasErrorText);
    statusMessage.textContent = hasErrorText ? status.last_error : '';
  }
  copyLogButton?.classList.toggle('d-none', !hasErrorText);
}

async function fetchStreamStatus() {
  if (!streamToggle) return;
  try {
    const response = await fetch('/api/stream/status', { cache: 'no-store' });
    if (response.ok) renderStreamButton(await response.json());
  } catch (_error) {
    streamToggle.disabled = true;
  }
}

streamToggle?.addEventListener('click', async () => {
  streamToggle.disabled = true;
  try {
    let response;
    if (streamState === 'streaming') {
      response = await fetch('/api/stream/stop', { method: 'POST' });
    } else {
      if (!frequencyInput.value) {
        showAppAlert('Select a frequency first (click a green slot on the BATC spectrum).');
        streamToggle.disabled = false;
        return;
      }
      // Fresh check rather than the last telemetry poll, so a Pluto that
      // just came back (or just dropped out) is seen straight away.
      const telemetry = await fetch('/api/telemetry', { cache: 'no-store' }).then((r) => r.json()).catch(() => null);
      if (!telemetry?.pluto_connected) {
        showAppAlert('The Pluto is not online. Check that it is powered on and connected, then wait for "Pluto connected" at the top of the page.');
        streamToggle.disabled = false;
        return;
      }
      const source = document.querySelector('input[name="source-type"]:checked')?.value;
      const body = {
        source,
        symbol_rate: symbolRateSelect.value,
        fec: fecSelect.value,
        gain_db: txGain.value,
        frequency: frequencyInput.value,
      };
      if (source === 'testcard') {
        body.testcard = testcardSelect.value;
      } else if (source === 'camera') {
        body.camera_device = cameraSelect.value;
        body.audio_device = document.querySelector('#audio-source')?.value || '';
      } else if (source === 'video') {
        body.video = videoSelect.value;
      } else {
        showAppAlert('Select a source first.');
        streamToggle.disabled = false;
        return;
      }
      streamStartSeen = true;
      response = await fetch('/api/stream/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Stream action failed');
    renderStreamButton(result);
    if (result.state === 'streaming') {
      renderPttButtons();
      renderSpectrumAdvisory({ streaming: true, detector: null });
    } else {
      await fetchPttRelayStatus();
      await fetchSpectrumStatus();
    }
  } catch (error) {
    showAppAlert(error.message);
    await fetchStreamStatus();
  }
});

fetchStreamStatus();
window.setInterval(fetchStreamStatus, 1000);
