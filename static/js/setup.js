/**
 * USB video key picker - lists whatever USB drive(s) are currently
 * plugged in (see /api/usb-key/candidates, backed by usb_video_key.py's
 * list_candidates()) and lets the user pick one to use for prepared
 * videos. No formatting, no destructive action - "Use this drive" just
 * adds two folders to whatever's already there.
 */
const usbKeyEmpty = document.querySelector('#usb-key-empty');
const usbKeyList = document.querySelector('#usb-key-list');

function renderCandidate(candidate) {
  const wrapper = document.createElement('div');
  wrapper.className = 'd-flex justify-content-between align-items-center flex-wrap gap-3 p-3 rounded-3';
  wrapper.style.background = '#0a1721';
  wrapper.style.border = '1px solid #243d50';

  const info = document.createElement('div');
  const title = candidate.label || candidate.device;
  const details = [
    candidate.device,
    candidate.fstype ? candidate.fstype.toUpperCase() : 'No filesystem found',
    candidate.size || null,
    candidate.mounted_at || 'Not mounted yet',
  ].filter(Boolean).join(' · ');
  info.innerHTML = `<div class="fw-semibold">${title}</div><div class="small text-muted-custom">${details}</div>`;

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
        window.alert(error.message);
        button.disabled = false;
        button.textContent = originalLabel;
      }
    });
    action.appendChild(button);
  }

  wrapper.appendChild(info);
  wrapper.appendChild(action);
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
 * Overlay settings (top/bottom banner + marquee on/off, marquee text) -
 * saved server-side via /api/overlay-settings so Home page's Start
 * Camera/Start Video pick them up without any control living there (see
 * overlay_settings.py).
 */
const overlayTopBanner = document.querySelector('#overlay-top-banner');
const overlayBottomBanner = document.querySelector('#overlay-bottom-banner');
const overlayMarquee = document.querySelector('#overlay-marquee');
const overlayMarqueeText = document.querySelector('#overlay-marquee-text');
const overlaySaveButton = document.querySelector('#overlay-save-button');
const overlayRestoreButton = document.querySelector('#overlay-restore-button');
const overlaySaveMessage = document.querySelector('#overlay-save-message');

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
    overlayMarqueeText.value = settings.marquee_text || settings.default_marquee_text || '';
  } catch (error) {
    console.error('loadOverlaySettings failed:', error);
    overlaySaveMessage.textContent = 'Could not load overlay settings.';
    overlaySaveMessage.classList.add('text-danger');
  }
}

async function saveOverlaySettings(successMessage) {
  overlaySaveButton.disabled = true;
  overlayRestoreButton.disabled = true;
  overlaySaveMessage.classList.remove('text-danger');
  overlaySaveMessage.textContent = 'Saving…';
  try {
    const response = await fetch('/api/overlay-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        top_banner: overlayTopBanner.checked,
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
    overlayRestoreButton.disabled = false;
  }
}

overlaySaveButton?.addEventListener('click', () => {
  saveOverlaySettings('Saved - takes effect on the next stream start.');
});

// Saves marquee_text as "" - "use the YAML default" (see
// overlay_settings.py / datv_tx_plus.py's MARQUEE_TEXT_OVERRIDE) - then
// reloads so the box shows that default text rather than staying blank.
overlayRestoreButton?.addEventListener('click', async () => {
  overlayMarqueeText.value = '';
  await saveOverlaySettings('Restored the default marquee message.');
  await loadOverlaySettings();
});

loadOverlaySettings();
