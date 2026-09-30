/**
 * Centered, dark-themed replacement for window.alert() - the native browser
 * alert() renders as a plain OS dialog anchored to the top of the window,
 * which clashes with this app's dark Bootstrap theme. Built lazily on first
 * call and reused after that. Shared by every page (loaded before
 * datv.js/setup.js) so there's one look for every "something went wrong"
 * message across the app.
 */
let appAlertOverlay = null;

function showAppAlert(message) {
  if (!appAlertOverlay) {
    appAlertOverlay = document.createElement('div');
    appAlertOverlay.className =
      'position-fixed top-0 start-0 w-100 h-100 d-none align-items-center justify-content-center p-3';
    appAlertOverlay.style.background = 'rgba(0,0,0,0.75)';
    appAlertOverlay.style.zIndex = '1080';
    appAlertOverlay.innerHTML = `
      <div class="surface p-4 text-center" style="width: 100%; max-width: 380px;">
        <p class="mb-4" id="app-alert-message"></p>
        <button type="button" class="btn btn-primary px-4" id="app-alert-ok">OK</button>
      </div>`;
    document.body.appendChild(appAlertOverlay);
    const hide = () => {
      appAlertOverlay.classList.add('d-none');
      appAlertOverlay.classList.remove('d-flex');
    };
    appAlertOverlay.querySelector('#app-alert-ok').addEventListener('click', hide);
    // Clicking the backdrop itself (not the card) also dismisses it.
    appAlertOverlay.addEventListener('click', (event) => {
      if (event.target === appAlertOverlay) hide();
    });
  }
  appAlertOverlay.querySelector('#app-alert-message').textContent = message;
  appAlertOverlay.classList.remove('d-none');
  appAlertOverlay.classList.add('d-flex');
}

/**
 * Same look as showAppAlert(), but with Cancel / confirm buttons instead of
 * window.confirm(). Resolves true only when the confirm button is clicked;
 * Cancel, Escape or a backdrop click resolve false.
 */
let appConfirmOverlay = null;

function showAppConfirm(title, message, confirmLabel = 'OK') {
  if (!appConfirmOverlay) {
    appConfirmOverlay = document.createElement('div');
    appConfirmOverlay.className =
      'position-fixed top-0 start-0 w-100 h-100 d-none align-items-center justify-content-center p-3';
    appConfirmOverlay.style.background = 'rgba(0,0,0,0.75)';
    appConfirmOverlay.style.zIndex = '1080';
    appConfirmOverlay.innerHTML = `
      <div class="surface p-4 text-center" style="width: 100%; max-width: 420px;">
        <div class="fs-1 text-warning mb-2" aria-hidden="true">⚠</div>
        <h2 class="h5 fw-bold mb-2" id="app-confirm-title"></h2>
        <p class="text-muted-custom mb-4" id="app-confirm-message"></p>
        <div class="d-flex gap-2 justify-content-center">
          <button type="button" class="btn btn-outline-secondary px-4" id="app-confirm-cancel">Cancel</button>
          <button type="button" class="btn btn-warning fw-bold px-4" id="app-confirm-ok"></button>
        </div>
      </div>`;
    document.body.appendChild(appConfirmOverlay);
  }
  appConfirmOverlay.querySelector('#app-confirm-title').textContent = title;
  appConfirmOverlay.querySelector('#app-confirm-message').textContent = message;
  const okButton = appConfirmOverlay.querySelector('#app-confirm-ok');
  const cancelButton = appConfirmOverlay.querySelector('#app-confirm-cancel');
  okButton.textContent = confirmLabel;
  appConfirmOverlay.classList.remove('d-none');
  appConfirmOverlay.classList.add('d-flex');
  // Focus Cancel so a stray Enter doesn't confirm.
  cancelButton.focus();

  return new Promise((resolve) => {
    const finish = (confirmed) => {
      appConfirmOverlay.classList.add('d-none');
      appConfirmOverlay.classList.remove('d-flex');
      okButton.removeEventListener('click', onOk);
      cancelButton.removeEventListener('click', onCancel);
      appConfirmOverlay.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve(confirmed);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdrop = (event) => { if (event.target === appConfirmOverlay) finish(false); };
    const onKey = (event) => { if (event.key === 'Escape') finish(false); };
    okButton.addEventListener('click', onOk);
    cancelButton.addEventListener('click', onCancel);
    appConfirmOverlay.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
  });
}
