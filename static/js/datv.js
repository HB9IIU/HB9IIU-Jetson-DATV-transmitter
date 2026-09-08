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
