(function () {
  'use strict';
  const connection = document.querySelector('#pluto-connection');
  if (!connection) return;

  async function poll() {
    try {
      const response = await fetch('/api/telemetry', { cache: 'no-store' });
      if (!response.ok) throw new Error('Telemetry request failed');
      const data = await response.json();
      connection.innerHTML = `<span class="nav-device-dot" aria-hidden="true"></span>Pluto ${data.pluto_connected ? 'connected' : 'disconnected'}`;
      connection.className = `nav-device-status ${data.pluto_connected ? 'nav-device-online' : 'nav-device-offline'}`;
      connection.title = data.pluto_connected ? 'Fresh Pluto telemetry received' : 'No recent Pluto telemetry';
    } catch (_error) {
      connection.innerHTML = '<span class="nav-device-dot" aria-hidden="true"></span>Pluto unavailable';
      connection.className = 'nav-device-status nav-device-offline';
      connection.title = 'Jetson could not read Pluto telemetry';
    }
  }
  poll();
  window.setInterval(poll, 2000);
}());
