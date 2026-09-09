/**
 * Presentation-only source selection. No hardware or Flask API calls are made.
 */
const sourceOptions = document.querySelectorAll('input[name="source-type"]');
const cameraPanel = document.querySelector('#camera-source-panel');
const filePanel = document.querySelector('#file-source-panel');
const cameraSelect = document.querySelector('#video-source');
const preview = document.querySelector('#camera-preview');
const previewImage = document.querySelector('#camera-preview-image');
const previewMessage = document.querySelector('#preview-message');
const fileTypeOptions = document.querySelectorAll('input[name="file-type"]');
const testcardOptions = document.querySelector('#testcard-options');
const videoOptions = document.querySelector('#video-options');
const testcardSelect = document.querySelector('#testcard-source');
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
  const testcardSelected = document.querySelector('#file-testcard').checked;
  testcardOptions.classList.toggle('d-none', !testcardSelected);
  videoOptions.classList.toggle('d-none', testcardSelected);
  showFilePreview(
    testcardSelected ? testcardSelect : videoSelect,
    testcardSelected ? 'NO TESTCARDS FOUND' : 'NO PREPARED VIDEOS FOUND'
  );
}

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
testcardSelect.addEventListener('change', () => showFilePreview(testcardSelect, 'NO TESTCARDS FOUND'));
videoSelect.addEventListener('change', () => showFilePreview(videoSelect, 'NO PREPARED VIDEOS FOUND'));
fileTypeOptions.forEach((option) => option.addEventListener('change', updateFileType));

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

const txGain = document.querySelector('#tx-gain');
const txGainValue = document.querySelector('#tx-gain-value');
function updateTxGain() {
  if (txGain && txGainValue) txGainValue.textContent = `${txGain.value} dB`.replace('-', '−');
}
txGain?.addEventListener('input', updateTxGain);
updateTxGain();

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

const streamToggle = document.querySelector('#stream-toggle');
let streamState = 'stopped';

function renderStreamButton(status) {
  if (!streamToggle) return;
  streamState = status.state;
  const busy = status.state === 'starting' || status.state === 'stopping';
  streamToggle.disabled = busy;
  streamToggle.classList.toggle('btn-success', status.state !== 'streaming');
  streamToggle.classList.toggle('btn-warning', status.state === 'streaming');
  if (status.state === 'streaming') streamToggle.textContent = '■ Stop stream';
  else if (status.state === 'starting') streamToggle.textContent = 'Starting…';
  else if (status.state === 'stopping') streamToggle.textContent = 'Stopping…';
  else streamToggle.textContent = '▶ Start stream';
  streamToggle.title = status.output_file ? `Local file: ${status.output_file}` : '';
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
      const testcardMode = document.querySelector('#source-file')?.checked &&
        document.querySelector('#file-testcard')?.checked;
      if (!testcardMode) {
        window.alert('For this first test, select File and Testcard.');
        streamToggle.disabled = false;
        return;
      }
      response = await fetch('/api/stream/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          testcard: testcardSelect.value,
          symbol_rate: symbolRateSelect.value,
          fec: fecSelect.value,
        }),
      });
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Stream action failed');
    renderStreamButton(result);
  } catch (error) {
    window.alert(error.message);
    await fetchStreamStatus();
  }
});

fetchStreamStatus();
window.setInterval(fetchStreamStatus, 1000);
