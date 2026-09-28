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
}

function showFilePreview(select, unavailableText) {
  const selected = select.options[select.selectedIndex];
  const previewUrl = selected ? selected.dataset.previewUrl : '';
  filePreview.classList.remove('is-live');
  filePreviewImage.removeAttribute('src');
  if (!previewUrl) {
    filePreviewMessage.textContent = unavailableText;
    return;
  }
  filePreviewMessage.textContent = 'LOADING PREVIEW…';
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

previewImage.addEventListener('load', () => preview.classList.add('is-live'));
previewImage.addEventListener('error', () => {
  preview.classList.remove('is-live');
  previewMessage.textContent = 'PREVIEW UNAVAILABLE';
});
filePreviewImage.addEventListener('load', () => filePreview.classList.add('is-live'));
filePreviewImage.addEventListener('error', () => {
  filePreview.classList.remove('is-live');
  filePreviewMessage.textContent = 'PREVIEW UNAVAILABLE';
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
  const dbText = `${dbValue} dB`.replace('-', '−');
  txGainValue.innerHTML = `${percent}% <small>(${dbText})</small>`;
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
    connection.innerHTML = `<span class="telemetry-dot me-2"></span>Pluto ${data.pluto_connected ? 'connected' : 'disconnected'}`;
    connection.className = `small fw-semibold ${data.pluto_connected ? 'text-success' : 'text-secondary'}`;
  } catch (_error) {
    connection.innerHTML = '<span class="telemetry-dot me-2"></span>Telemetry unavailable';
    connection.className = 'small fw-semibold text-danger';
  }
}

updateTelemetry();
window.setInterval(updateTelemetry, 2000);

// PA relay safety interlock (see pa_relay.py) - the CN0417 pre-amp/200W PA
// only gets powered once the local Pluto RX spectrum has shown a stable
// signal AND the operator explicitly confirms via #pa-relay-engage-button.
// This state is human-timescale (state changes are debounced server-side
// over several seconds of frames), so it's polled at 1s like
// #stream-status-message - not at the FFT canvas's 150ms redraw rate.
const paRelayStatus = document.querySelector('#pa-relay-status');
const paRelayEngageButton = document.querySelector('#pa-relay-engage-button');
const paRelayDisengageButton = document.querySelector('#pa-relay-disengage-button');

// Short labels - they sit next to the signal summary in card 3's header and
// must never wrap onto their own line (that made the card jump in height);
// the full explanation is the tooltip (PA_RELAY_TITLE).
const PA_RELAY_TEXT = {
  idle: 'PA: idle',
  waiting: 'PA: not ready',
  ready: 'PA: ready',
  engaged: 'PA: ENGAGED',
  fault: 'PA: FAULT',
};
const PA_RELAY_TITLE = {
  idle: 'PA relay idle - no transmission',
  waiting: 'PA relay not ready - the local RX spectrum is not stable yet',
  ready: 'PA relay ready - press "Engage PA relay" to power the PA',
  engaged: 'PA relay engaged - PA powered, live',
  fault: 'PA relay fault',
};
const PA_RELAY_CLASS = {
  idle: 'text-secondary',
  waiting: 'text-danger',
  ready: 'text-amber',
  engaged: 'text-success',
  fault: 'text-danger',
};

function renderRelayStatus(data) {
  if (!paRelayStatus) return;
  const state = data.state || 'idle';
  const label = PA_RELAY_TEXT[state] || state;
  paRelayStatus.innerHTML = `<span class="telemetry-dot me-2"></span>${label}`;
  paRelayStatus.title = (state === 'fault' && data.fault_reason)
    ? `${PA_RELAY_TITLE.fault}: ${data.fault_reason}`
    : (PA_RELAY_TITLE[state] || label);
  paRelayStatus.className = `small fw-semibold text-nowrap ms-auto ${PA_RELAY_CLASS[state] || 'text-secondary'}`;
  paRelayEngageButton?.classList.toggle('d-none', state !== 'ready');
  paRelayDisengageButton?.classList.toggle('d-none', state !== 'engaged' && state !== 'fault');
}

async function fetchRelayStatus() {
  if (!paRelayStatus) return;
  try {
    const response = await fetch('/api/relay/status', { cache: 'no-store' });
    if (!response.ok) throw new Error('Relay status request failed');
    renderRelayStatus(await response.json());
  } catch (_error) {
    paRelayStatus.innerHTML = '<span class="telemetry-dot me-2"></span>PA: unavailable';
    paRelayStatus.title = 'PA relay status unavailable';
    paRelayStatus.className = 'small fw-semibold text-nowrap ms-auto text-secondary';
    paRelayEngageButton?.classList.add('d-none');
    paRelayDisengageButton?.classList.add('d-none');
  }
}

paRelayEngageButton?.addEventListener('click', async () => {
  paRelayEngageButton.disabled = true;
  try {
    const response = await fetch('/api/relay/engage', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not engage PA relay');
  } catch (error) {
    showAppAlert(error.message);
  } finally {
    paRelayEngageButton.disabled = false;
    await fetchRelayStatus();
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
    await fetchRelayStatus();
  }
});

fetchRelayStatus();
window.setInterval(fetchRelayStatus, 1000);

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
  const gradient = localFftContext.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, 'rgba(255, 51, 51, .35)');
  gradient.addColorStop(1, 'rgba(255, 51, 51, 0)');
  localFftContext.beginPath();
  localFftContext.moveTo(points[0].x, height);
  for (const point of points) localFftContext.lineTo(point.x, point.y);
  localFftContext.lineTo(points[points.length - 1].x, height);
  localFftContext.closePath();
  localFftContext.fillStyle = gradient;
  localFftContext.fill();

  localFftContext.strokeStyle = '#f33';
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

function renderStreamButton(status) {
  if (!streamToggle) return;
  const previousStreamState = streamState;
  streamState = status.state;
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
  const busy = status.state === 'starting' || status.state === 'stopping';
  streamToggle.disabled = busy;
  streamToggle.classList.toggle('btn-success', status.state !== 'streaming' && status.state !== 'error');
  streamToggle.classList.toggle('btn-danger', status.state === 'streaming' || status.state === 'error');
  if (status.state === 'streaming') streamToggle.textContent = '■ Stop stream';
  else if (status.state === 'starting') streamToggle.textContent = 'Starting…';
  else if (status.state === 'stopping') streamToggle.textContent = 'Stopping…';
  else streamToggle.textContent = '▶ Start stream';
  streamToggle.title = status.state === 'streaming' ? 'Transmitting via Pluto' : '';

  const statusMessage = document.querySelector('#stream-status-message');
  const hasErrorText = status.state === 'error' && !!status.last_error;
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
      response = await fetch('/api/stream/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Stream action failed');
    renderStreamButton(result);
  } catch (error) {
    showAppAlert(error.message);
    await fetchStreamStatus();
  }
});

fetchStreamStatus();
window.setInterval(fetchStreamStatus, 1000);
