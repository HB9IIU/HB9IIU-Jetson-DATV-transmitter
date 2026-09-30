/**
 * USB video key picker - lists whatever USB drive(s) are currently
 * plugged in (see /api/usb-key/candidates, backed by usb_video_key.py's
 * list_candidates()) and lets the user pick one to use for prepared
 * videos. No formatting, no destructive action - "Use this drive" just
 * adds two folders to whatever's already there.
 */
const usbKeyEmpty = document.querySelector('#usb-key-empty');
const usbKeyList = document.querySelector('#usb-key-list');

function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return (value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[unitIndex];
}

function renderSpaceGauge(candidate) {
  if (!candidate.total_bytes) return '';
  const usedBytes = candidate.total_bytes - candidate.free_bytes;
  const usedPercent = Math.round((usedBytes / candidate.total_bytes) * 100);
  const barColor = usedPercent >= 95 ? 'bg-danger' : usedPercent >= 80 ? 'bg-warning' : 'bg-success';
  return `
    <div class="mt-2">
      <div class="progress" role="progressbar" aria-label="Free space" aria-valuenow="${usedPercent}"
           aria-valuemin="0" aria-valuemax="100" style="height: 6px; background: #0a1721;">
        <div class="progress-bar ${barColor}" style="width: ${usedPercent}%"></div>
      </div>
      <div class="small text-muted-custom mt-1">${formatBytes(candidate.free_bytes)} free of ${formatBytes(candidate.total_bytes)}</div>
    </div>`;
}

function renderCandidate(candidate) {
  const wrapper = document.createElement('div');
  wrapper.className = 'd-flex flex-column gap-2 p-3 rounded-3';
  wrapper.style.background = '#0a1721';
  wrapper.style.border = '1px solid #243d50';

  const topRow = document.createElement('div');
  topRow.className = 'd-flex justify-content-between align-items-start flex-wrap gap-3';

  const info = document.createElement('div');
  info.className = 'flex-grow-1';
  const title = candidate.label || candidate.device;
  const details = [
    candidate.device,
    candidate.fstype ? candidate.fstype.toUpperCase() : 'No filesystem found',
    candidate.size || null,
    candidate.mounted_at || 'Not mounted yet',
  ].filter(Boolean).join(' · ');
  info.innerHTML = `<div class="fw-semibold">${title}</div><div class="small text-muted-custom">${details}</div>${renderSpaceGauge(candidate)}`;

  const action = document.createElement('div');
  if (candidate.confirmed) {
    action.innerHTML = '<span class="badge text-bg-success">In use for videos</span>';
  } else if (!candidate.mounted_at) {
    action.innerHTML = '<span class="badge text-bg-secondary">Waiting for it to mount…</span>';
  } else {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-success';
    button.textContent = 'Use this drive';
    button.addEventListener('click', async () => {
      const originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = 'Setting up…';
      try {
        const response = await fetch('/api/usb-key/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: candidate.serial }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not set up that drive');
        await refreshCandidates();
      } catch (error) {
        showAppAlert(error.message);
        button.disabled = false;
        button.textContent = originalLabel;
      }
    });
    action.appendChild(button);
  }

  topRow.appendChild(info);
  topRow.appendChild(action);
  wrapper.appendChild(topRow);
  return wrapper;
}

async function refreshCandidates() {
  if (!usbKeyList) return;
  try {
    const response = await fetch('/api/usb-key/candidates', { cache: 'no-store' });
    if (!response.ok) throw new Error('Request failed');
    const data = await response.json();
    usbKeyList.innerHTML = '';
    usbKeyEmpty.classList.toggle('d-none', data.candidates.length > 0);
    data.candidates.forEach((candidate) => usbKeyList.appendChild(renderCandidate(candidate)));
  } catch (_error) {
    usbKeyList.innerHTML = '<div class="text-danger">Could not load USB drive status.</div>';
    usbKeyEmpty.classList.add('d-none');
  }
}

refreshCandidates();
window.setInterval(refreshCandidates, 2000);

/**
 * Overlay settings (top/bottom banner + marquee on/off, top banner/marquee
 * text) - saved server-side via /api/overlay-settings so Home page's Start
 * Camera/Start Video pick them up without any control living there (see
 * overlay_settings.py). Bottom banner has no text field - it's always the
 * live callsign/clock/telemetry overlay.
 */
const overlayTopBanner = document.querySelector('#overlay-top-banner');
const overlayTopBannerText = document.querySelector('#overlay-top-banner-text');
const overlayTopBannerRestoreButton = document.querySelector('#overlay-top-banner-restore-button');
const overlayBottomBanner = document.querySelector('#overlay-bottom-banner');
const overlayMarquee = document.querySelector('#overlay-marquee');
const overlayMarqueeText = document.querySelector('#overlay-marquee-text');
const overlaySaveButton = document.querySelector('#overlay-save-button');
const overlayRestoreButton = document.querySelector('#overlay-restore-button');
const overlaySaveMessage = document.querySelector('#overlay-save-message');
const overlayRestoreButtons = [overlayTopBannerRestoreButton, overlayRestoreButton];

async function loadOverlaySettings() {
  if (!overlaySaveButton) return;
  try {
    const response = await fetch('/api/overlay-settings', { cache: 'no-store' });
    if (!response.ok) throw new Error('Request failed');
    const settings = await response.json();
    overlayTopBanner.checked = settings.top_banner;
    overlayBottomBanner.checked = settings.bottom_banner;
    overlayMarquee.checked = settings.marquee;
    // Show the real default text in place of a blank box when no override
    // is saved, rather than an empty field with a "leave blank" hint.
    overlayTopBannerText.value = settings.top_banner_text || settings.default_top_banner_text || '';
    overlayMarqueeText.value = settings.marquee_text || settings.default_marquee_text || '';
  } catch (error) {
    console.error('loadOverlaySettings failed:', error);
    overlaySaveMessage.textContent = 'Could not load overlay settings.';
    overlaySaveMessage.classList.add('text-danger');
  }
}

async function saveOverlaySettings(successMessage) {
  overlaySaveButton.disabled = true;
  overlayRestoreButtons.forEach((button) => { if (button) button.disabled = true; });
  overlaySaveMessage.classList.remove('text-danger');
  overlaySaveMessage.textContent = 'Saving…';
  try {
    const response = await fetch('/api/overlay-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        top_banner: overlayTopBanner.checked,
        top_banner_text: overlayTopBannerText.value,
        bottom_banner: overlayBottomBanner.checked,
        marquee: overlayMarquee.checked,
        marquee_text: overlayMarqueeText.value,
      }),
    });
    if (!response.ok) throw new Error('Request failed (HTTP ' + response.status + ')');
    overlaySaveMessage.textContent = successMessage;
  } catch (error) {
    console.error('overlay-settings save failed:', error);
    overlaySaveMessage.textContent = 'Could not save overlay settings: ' + error.message;
    overlaySaveMessage.classList.add('text-danger');
  } finally {
    overlaySaveButton.disabled = false;
    overlayRestoreButtons.forEach((button) => { if (button) button.disabled = false; });
  }
}

overlaySaveButton?.addEventListener('click', () => {
  saveOverlaySettings('Saved - takes effect on the next stream start.');
});

// Saves top_banner_text as "" - "use the YAML default" (see
// overlay_settings.py / datv_tx_plus.py's TITLE_TEXT_OVERRIDE) - then
// reloads so the box shows that default text rather than staying blank.
overlayTopBannerRestoreButton?.addEventListener('click', async () => {
  overlayTopBannerText.value = '';
  await saveOverlaySettings('Restored the default title.');
  await loadOverlaySettings();
});

// Same idea, for marquee_text/MARQUEE_TEXT_OVERRIDE.
overlayRestoreButton?.addEventListener('click', async () => {
  overlayMarqueeText.value = '';
  await saveOverlaySettings('Restored the default marquee message.');
  await loadOverlaySettings();
});

loadOverlaySettings();

/**
 * Pluto callsign - used to build every MQTT topic between this app and the
 * Pluto (see app.py's PLUTO_CALLSIGN). Applying a new one pushes it to the
 * Pluto over cmd/pluto/call, which makes the firmware store it and reboot
 * itself (see pluto-ori-ps-main/mqtt_setcall.sh) - the overlay below stays
 * up until /api/telemetry reports the Pluto talking again under the new
 * callsign's topics.
 */
const callsignCurrent = document.querySelector('#callsign-current');
const callsignInput = document.querySelector('#callsign-input');
const callsignApplyButton = document.querySelector('#callsign-apply-button');
const callsignMessage = document.querySelector('#callsign-message');
const callsignRebootOverlay = document.querySelector('#callsign-reboot-overlay');
const callsignRebootSpinner = document.querySelector('#callsign-reboot-spinner');
const callsignRebootStatus = document.querySelector('#callsign-reboot-status');
const callsignRebootDetail = document.querySelector('#callsign-reboot-detail');
const callsignRebootClose = document.querySelector('#callsign-reboot-close');

async function loadCallsign() {
  if (!callsignApplyButton) return;
  try {
    const response = await fetch('/api/pluto/callsign', { cache: 'no-store' });
    if (!response.ok) throw new Error('Request failed');
    const data = await response.json();
    callsignCurrent.textContent = data.callsign;
    callsignInput.value = data.callsign;
  } catch (error) {
    console.error('loadCallsign failed:', error);
    callsignCurrent.textContent = 'unknown';
  }
}

// Polls /api/telemetry (already polled elsewhere in this app) until
// pluto_connected comes back true - the server already zeroes its
// last-message timestamp the moment a new callsign is applied, so an early
// still-true reading here can't be a stale leftover from before the
// reboot (see app.py's pluto_callsign_set()).
async function waitForPlutoBackOnline() {
  const deadlineMs = Date.now() + 120000;
  const slowNoticeAt = Date.now() + 20000;
  while (Date.now() < deadlineMs) {
    try {
      const response = await fetch('/api/telemetry', { cache: 'no-store' });
      if (response.ok) {
        const data = await response.json();
        if (data.pluto_connected) return true;
      }
    } catch (_error) {
      // Keep polling - the Jetson's own network stack can hiccup too.
    }
    if (Date.now() > slowNoticeAt) {
      callsignRebootDetail.textContent = 'Still waiting - this can take a minute or two.';
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  return false;
}

callsignApplyButton?.addEventListener('click', async () => {
  const newCallsign = (callsignInput.value || '').trim().toUpperCase();
  if (!/^[A-Z0-9]{3,10}$/.test(newCallsign)) {
    callsignMessage.textContent = 'Callsign must be 3-10 letters/digits.';
    callsignMessage.classList.add('text-danger');
    return;
  }
  if (!window.confirm(
    'Push "' + newCallsign + '" to the Pluto and reboot it now?\n\n' +
    'Any active transmission will stop, and the Pluto will be offline for a bit.')) {
    return;
  }

  callsignApplyButton.disabled = true;
  callsignMessage.classList.remove('text-danger');
  callsignMessage.textContent = '';
  try {
    const response = await fetch('/api/pluto/callsign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ callsign: newCallsign }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');

    callsignRebootSpinner.classList.remove('d-none');
    callsignRebootStatus.textContent = 'Rebooting Pluto…';
    callsignRebootDetail.textContent = 'Waiting for it to come back online.';
    callsignRebootClose.classList.add('d-none');
    callsignRebootOverlay.classList.remove('d-none');
    callsignRebootOverlay.classList.add('d-flex');

    const backOnline = await waitForPlutoBackOnline();
    callsignRebootSpinner.classList.add('d-none');
    callsignRebootStatus.textContent = backOnline ? 'Pluto is back online.' : 'Still not seeing it - check it manually.';
    callsignRebootDetail.textContent = backOnline
      ? 'Now transmitting under callsign ' + newCallsign + '.'
      : 'Reconnect might just be slow - you can close this and check again later.';
    callsignRebootClose.classList.remove('d-none');

    await loadCallsign();
  } catch (error) {
    callsignMessage.textContent = 'Could not apply callsign: ' + error.message;
    callsignMessage.classList.add('text-danger');
  } finally {
    callsignApplyButton.disabled = false;
  }
});

callsignRebootClose?.addEventListener('click', () => {
  callsignRebootOverlay.classList.add('d-none');
  callsignRebootOverlay.classList.remove('d-flex');
});

loadCallsign();

/**
 * OpenTuner auto-tune - see opentuner_quicktune.py. Blank IP = broadcast to
 * the whole LAN; "Use this PC" fills in the browser's own address as the
 * Jetson sees it (client_ip from /api/opentuner-settings).
 */
const opentunerEnabled = document.querySelector('#opentuner-enabled');
const opentunerIp = document.querySelector('#opentuner-ip');
const opentunerThisPcButton = document.querySelector('#opentuner-this-pc-button');
const opentunerPort = document.querySelector('#opentuner-port');
const opentunerOffset = document.querySelector('#opentuner-offset');
const opentunerRxCorrection = document.querySelector('#opentuner-rx-correction');
const opentunerAutoCalibrate = document.querySelector('#opentuner-auto-calibrate');
const opentunerTxCorrection = document.querySelector('#opentuner-tx-correction');
const opentunerTxAutoApply = document.querySelector('#opentuner-tx-auto-apply');
const opentunerSaveButton = document.querySelector('#opentuner-save-button');
const opentunerTestButton = document.querySelector('#opentuner-test-button');
const opentunerMessage = document.querySelector('#opentuner-message');
let opentunerClientIp = '';

function setOpentunerMessage(text, isError) {
  opentunerMessage.textContent = text;
  opentunerMessage.classList.toggle('text-danger', Boolean(isError));
}

async function loadOpentunerSettings() {
  if (!opentunerSaveButton) return;
  try {
    const response = await fetch('/api/opentuner-settings', { cache: 'no-store' });
    if (!response.ok) throw new Error('Request failed');
    const settings = await response.json();
    opentunerEnabled.checked = settings.enabled;
    opentunerIp.value = settings.target_ip;
    opentunerPort.value = settings.port;
    opentunerOffset.value = settings.lnb_offset_khz;
    opentunerRxCorrection.value = settings.rx_correction_khz;
    opentunerAutoCalibrate.checked = settings.auto_calibrate;
    opentunerTxCorrection.value = settings.tx_correction_khz;
    opentunerTxAutoApply.checked = settings.tx_auto_apply;
    opentunerClientIp = settings.client_ip || '';
  } catch (error) {
    console.error('loadOpentunerSettings failed:', error);
    setOpentunerMessage('Could not load OpenTuner settings.', true);
  }
}

async function saveOpentunerSettings() {
  const response = await fetch('/api/opentuner-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      enabled: opentunerEnabled.checked,
      target_ip: opentunerIp.value.trim(),
      port: opentunerPort.value,
      lnb_offset_khz: opentunerOffset.value,
      rx_correction_khz: opentunerRxCorrection.value || 0,
      auto_calibrate: opentunerAutoCalibrate.checked,
      tx_correction_khz: opentunerTxCorrection.value || 0,
      tx_auto_apply: opentunerTxAutoApply.checked,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
}

opentunerThisPcButton?.addEventListener('click', () => {
  if (opentunerClientIp) opentunerIp.value = opentunerClientIp;
});

opentunerSaveButton?.addEventListener('click', async () => {
  opentunerSaveButton.disabled = true;
  try {
    await saveOpentunerSettings();
    setOpentunerMessage('Saved - used on the next stream start.', false);
  } catch (error) {
    setOpentunerMessage('Could not save: ' + error.message, true);
  } finally {
    opentunerSaveButton.disabled = false;
  }
});

// Saves first so the test uses exactly what's on screen.
opentunerTestButton?.addEventListener('click', async () => {
  opentunerTestButton.disabled = true;
  try {
    await saveOpentunerSettings();
    const response = await fetch('/api/opentuner/test', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    setOpentunerMessage('', false);
  } catch (error) {
    setOpentunerMessage('Test failed: ' + error.message, true);
  } finally {
    opentunerTestButton.disabled = false;
  }
});

loadOpentunerSettings();

/**
 * Restart app - see app.py's app_restart(). The server exits ~1s after
 * answering and systemd starts it again ~3s later, so first wait for it to
 * go away (otherwise the old process could answer the poll), then for it
 * to answer again, then reload the page.
 */
const appRestartButton = document.querySelector('#app-restart-button');
const appRestartMessage = document.querySelector('#app-restart-message');
const appRestartOverlay = document.querySelector('#app-restart-overlay');
const appRestartStatus = document.querySelector('#app-restart-status');
const appRestartDetail = document.querySelector('#app-restart-detail');

async function appIsUp() {
  try {
    const response = await fetch('/api/stream/status', { cache: 'no-store' });
    return response.ok;
  } catch (_error) {
    return false;
  }
}

async function waitForAppRestart() {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const deadlineMs = Date.now() + 60000;
  while (Date.now() < deadlineMs && await appIsUp()) await sleep(500);
  while (Date.now() < deadlineMs) {
    if (await appIsUp()) return true;
    await sleep(1000);
  }
  return false;
}

appRestartButton?.addEventListener('click', async () => {
  if (!window.confirm('Restart the app now?\n\nAny active transmission will stop.')) return;

  appRestartButton.disabled = true;
  appRestartMessage.classList.remove('text-danger');
  appRestartMessage.textContent = '';
  try {
    const response = await fetch('/api/app/restart', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');

    appRestartOverlay.classList.remove('d-none');
    appRestartOverlay.classList.add('d-flex');
    if (await waitForAppRestart()) {
      window.location.reload();
      return;
    }
    appRestartStatus.textContent = 'The app did not come back.';
    appRestartDetail.textContent = 'Check it on the Jetson: journalctl -u datv-app -n 50';
  } catch (error) {
    appRestartMessage.textContent = 'Could not restart: ' + error.message;
    appRestartMessage.classList.add('text-danger');
    appRestartButton.disabled = false;
  }
});
