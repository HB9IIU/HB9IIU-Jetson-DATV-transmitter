/**
 * RX page "Tuner 1" panel - polls /api/rx/info, the latest status the
 * modified OpenTuner pushes once per second (its Properties panel values).
 * Values are null when unknown (e.g. no lock) and shown as "--".
 */
(function () {
  'use strict';

  const POLL_MS = 1000;
  // OpenTuner sends every second - older than this means it stopped.
  const STALE_S = 5;

  const lockBadge = document.querySelector('#rx-info-lock');
  const marginValue = document.querySelector('#rx-info-margin');
  const merValue = document.querySelector('#rx-info-mer');
  const rows = document.querySelector('#rx-info-rows');
  const ageLabel = document.querySelector('#rx-info-age');
  const qualityBar = document.querySelector('#rx-info-quality');
  if (!lockBadge || !rows) return;

  // D margin = dB above the point where decoding fails - the usual DATV
  // quality figure. Bar is full at QUALITY_FULL_DB.
  const QUALITY_FULL_DB = 10;

  function qualityColour(margin) {
    if (margin < 0) return '#dc3545';   // no decode
    if (margin < 1) return '#fd7e14';   // breaking up
    if (margin < 3) return '#ffc107';   // OK but marginal
    return '#25c26e';                   // solid
  }

  function setQuality(margin) {
    const hasMargin = margin !== null && margin !== undefined;
    const percent = hasMargin ? Math.max(0, Math.min(100, (margin / QUALITY_FULL_DB) * 100)) : 0;
    qualityBar.style.width = percent + '%';
    qualityBar.style.backgroundColor = hasMargin ? qualityColour(margin) : 'transparent';
    marginValue.style.color = hasMargin ? qualityColour(margin) : '';
  }

  const known = (value) => value !== null && value !== undefined && value !== '';
  const show = (value, suffix = '') => (known(value) ? value + suffix : '--');
  const mhz = (khz) => (known(khz) ? (khz / 1000).toFixed(3) + ' MHz' : '--');
  const joined = (...parts) => parts.filter(known).join(' · ') || '--';

  function setBadge(label, style) {
    lockBadge.textContent = label;
    lockBadge.className = `badge rounded-pill ms-auto text-bg-${style}`;
  }

  function render(info) {
    const fresh = info.age_s !== null && info.age_s !== undefined && info.age_s < STALE_S;
    if (!fresh) {
      setBadge('NO DATA', 'secondary');
      marginValue.textContent = '--';
      merValue.textContent = '--';
      setQuality(null);
      rows.innerHTML = '';
      ageLabel.textContent = info.age_s === null || info.age_s === undefined
        ? 'Nothing received from OpenTuner yet.'
        : `No update from OpenTuner for ${Math.round(info.age_s)} s.`;
      return;
    }
    setBadge(info.locked ? (info.demod_state || 'LOCKED') : (info.demod_state || 'SEARCHING'),
      info.locked ? 'success' : 'warning');
    marginValue.textContent = known(info.db_margin) ? 'D' + info.db_margin.toFixed(1) : '--';
    setQuality(info.locked ? info.db_margin : null);
    merValue.textContent = show(info.mer_db, ' dB');

    const table = [
      ['Service', joined(info.service_name, info.service_provider)],
      ['Frequency', mhz(info.requested_freq_khz)],
      ['Symbol rate', show(info.symbol_rate_ksps, ' kS/s')],
      ['Modcod', show(info.modcod)],
      // OpenTuner reports "0x0" when it doesn't know (its own panel too).
      ['Video', joined(info.video_codec, info.video_resolution === '0x0' ? null : info.video_resolution)],
      // Same for audio: "0 Hz, 0 channels" means unknown.
      ['Audio', joined(info.audio_codec, /^0 Hz/.test(info.audio_rate || '') ? null : info.audio_rate)],
      ['Null packets', show(info.null_packets_percent, ' %')],
      ['RF level', show(info.rf_level_db, ' dB')],
      ['Carrier offset', known(info.carrier_offset_hz) ? (info.carrier_offset_hz / 1000).toFixed(1) + ' kHz' : '--'],
      ['BER', show(info.ber)],
      ['LPDC errors', show(info.lpdc_errors)],
    ];
    rows.innerHTML = '';
    table.forEach(([label, value]) => {
      const row = document.createElement('tr');
      const labelCell = document.createElement('td');
      labelCell.className = 'text-muted-custom text-nowrap ps-0';
      labelCell.textContent = label;
      const valueCell = document.createElement('td');
      valueCell.className = 'fw-semibold text-end pe-0';
      valueCell.textContent = value;
      row.append(labelCell, valueCell);
      rows.appendChild(row);
    });
    ageLabel.textContent = '';
  }

  async function poll() {
    try {
      const response = await fetch('/api/rx/info', { cache: 'no-store' });
      if (response.ok) render(await response.json());
    } catch (_error) {
      // Keep the last values; the next poll will tell.
    }
  }

  poll();
  window.setInterval(poll, POLL_MS);
}());
