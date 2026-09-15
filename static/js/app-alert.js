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
