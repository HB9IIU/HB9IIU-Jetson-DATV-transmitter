"""Live Pluto + Jetson telemetry on a web page, over the LAN.

Read-only monitor, deliberately kept separate from the transmit path (see
datv_tx_plus.py's module docstring for why that script itself shouldn't grow
a web server): the Pluto publishes its full state tree over MQTT regardless
of which script (if any) is currently transmitting, so this dashboard can be
started/stopped independently at any time without going anywhere near the
GStreamer/PTT-critical code.

It reuses the small MQTT discover/connect/subscribe helpers already proven
in datv_tx_plus.py instead of duplicating them. Importing that module here
is safe - its top-level side effects (forcing this process's TZ to UTC,
loading the GStreamer PyGObject bindings) are harmless for a script that
never builds a pipeline.

Telemetry topics shown here are the real dt/pluto/<CALL>/* status tree
documented in documentation/PlutoDVB2_MQTT_Reference_Manual.pdf section 7 -
whatever the Pluto actually publishes shows up automatically, with no fixed
list to keep in sync. One exception: that manual lists tx/dvbs2/ts/bitrate
as a real telemetry topic, but hardware testing during datv_tx_plus.py
development found the firmware never actually publishes it (see
calculate_dvbs2_ts_bitrate() there) - so it will simply show "--" here,
which is correct, not a dashboard bug.

Run this alongside (or independently of) fft_relay.py; both are read-only
LAN viewers and use different ports.

Run directly with "python telemetry_web.py" - NOT "flask run". The MQTT
connect/subscribe below happens at import time specifically so this also
works under "flask run" (which imports the module for its `app` object but
never calls a main()/entry-point function), but "flask run" still defaults
to binding 127.0.0.1, invisible to the rest of the LAN, and to Flask's
single-threaded dev reloader, which can double-run this module's top-level
code. Plain "python telemetry_web.py" avoids both.
"""

import socket

from flask import Flask, jsonify, render_template_string

import datv_tx_plus as tx

HTTP_PORT = 8001

app = Flask(__name__)
telemetry = {}

print("Looking for the Pluto...")
_pluto_ip = tx.discover_pluto_ip()
# Own client_id, distinct from datv_tx_plus.py's "jetson-datv-tx-plus" - see
# the comment on mqtt_connect() there. Must stay unique from any other
# script connecting to this same broker too (e.g. datv_tx_plus_fft.py).
_mqtt_client = tx.mqtt_connect(_pluto_ip, client_id="jetson-telemetry-web")
tx.subscribe_telemetry(_mqtt_client, tx.CALLSIGN, telemetry)


def get_lan_ip():
    """Best-effort LAN IP for this Jetson - NOT the Pluto's 192.168.2.x USB
    address. Opens a UDP "connection" (no packet actually sent for UDP) to
    pick whichever local interface the OS would route external traffic
    through, then reads that socket's own address back.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _as_float(value, scale=1.0):
    try:
        return float(value) * scale
    except (TypeError, ValueError):
        return None


@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE, callsign=tx.CALLSIGN,
                                   poll_ms=int(tx.TELEMETRY_UPDATE_SECONDS * 1000))


def _expected_ts_bitrate_kbps():
    """The TS bitrate the Pluto *should* be producing right now, computed from
    its own live-reported SR/FEC (not this project's static PROFILES table -
    that would go stale the moment the two disagree, e.g. a different script
    driving TX, or a profile edit made without restarting it). Reuses
    datv_tx_plus.py's own verified ETSI DVB-S2 math; still assumes long
    frame + pilots on, since that function does too (see its docstring) and
    every profile in this project configures the Pluto that way.
    """
    live_sr = telemetry.get("tx/dvbs2/sr")
    live_fec = telemetry.get("tx/dvbs2/fec")
    if not live_sr or not live_fec:
        return None
    try:
        return tx.calculate_dvbs2_ts_bitrate(
            {"symbol_rate": int(live_sr), "fec": live_fec}) / 1000.0
    except (ValueError, KeyError):
        return None


@app.route("/telemetry.json")
def telemetry_json():
    muted = telemetry.get("tx/mute")
    return jsonify({
        "connected": bool(telemetry),
        "jetson": {
            "cpu_percent": tx.read_jetson_cpu_load_percent(),
            "cpu_temp_c": tx.read_jetson_cpu_temp_c(),
        },
        "highlights": {
            "pluto_temp_c": _as_float(telemetry.get("temperature_ad"), scale=0.001),
            "tx_frequency_mhz": _as_float(telemetry.get("tx/frequency"), scale=1e-6),
            "tx_gain_db": _as_float(telemetry.get("tx/gain")),
            "tx_ptt_on": (muted == "0") if muted is not None else None,
            "tx_dvbs2_sr": telemetry.get("tx/dvbs2/sr"),
            "tx_dvbs2_queue": telemetry.get("tx/dvbs2/queue"),
            "tx_dvbs2_ts_bitrate_kbps": _as_float(
                telemetry.get("tx/dvbs2/ts/bitrate"), scale=0.001),
            "tx_dvbs2_ts_bitrate_expected_kbps": _expected_ts_bitrate_kbps(),
        },
        "raw": telemetry,
    })


PAGE_TEMPLATE = """<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ callsign }} - Pluto/Jetson telemetry</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<style>
  /* Validated dark palette (dataviz skill reference instance) reskinning
     Bootstrap's own dark-mode variables, rather than fighting its classes. */
  html[data-bs-theme="dark"] {
    --bs-body-bg: #0d0d0d;          /* page plane */
    --bs-body-color: #ffffff;       /* primary ink */
    --bs-secondary-color: #c3c2b7;  /* secondary ink */
    --bs-tertiary-color: #898781;   /* muted ink (axis/labels) */
    --bs-card-bg: #1a1a19;          /* chart/card surface */
    --bs-border-color: #2c2c2a;     /* hairline */
    --status-good: #0ca30c;
    --status-warning: #fab219;
    --status-critical: #d03b3b;
  }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; padding-bottom: 2rem; }
  .card-label { color: var(--bs-tertiary-color); font-size: 11px; text-transform: uppercase;
                letter-spacing: .04em; display: flex; align-items: center; gap: 6px; }
  .card-value { font-size: 26px; margin-top: 2px; }
  .metric-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .status-good { color: var(--status-good); }
  .status-warning { color: var(--status-warning); }
  .status-critical { color: var(--status-critical); }
  .trend-chart { height: 110px; margin: 4px -8px -4px; }
  #raw td { font-size: 12px; }
  #raw td.key { color: var(--bs-tertiary-color); }
</style>
</head>
<body>
<div class="container-fluid py-4" style="max-width: 1400px;">

  <h1 class="h4 fw-normal mb-4">
    <span class="text-secondary">DATV telemetry</span> - {{ callsign }}
    <span id="status" class="fs-6 status-warning">● connecting...</span>
  </h1>

  <div class="row row-cols-2 row-cols-md-3 row-cols-xl-5 g-3 mb-4">

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label">PTT</div>
        <div class="card-value"><span id="val-ptt">--</span></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label"><span class="metric-dot" style="background:#e66767"></span>Pluto temp</div>
        <div class="card-value"><span id="val-plutoTemp">--</span></div>
        <div id="chart-plutoTemp" class="trend-chart"></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label">TX frequency</div>
        <div class="card-value"><span id="val-freq">--</span></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label">TX gain</div>
        <div class="card-value"><span id="val-gain">--</span></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label">Symbol rate</div>
        <div class="card-value"><span id="val-sr">--</span></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label"><span class="metric-dot" style="background:#3987e5"></span>BBFRAME queue</div>
        <div class="card-value"><span id="val-queue">--</span></div>
        <div id="chart-queue" class="trend-chart"></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label"><span class="metric-dot" style="background:#d95926"></span>TS bitrate</div>
        <div class="card-value"><span id="val-bitrate">--</span></div>
        <div id="bitrate-match" class="small text-secondary">&nbsp;</div>
        <div id="chart-bitrate" class="trend-chart"></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label"><span class="metric-dot" style="background:#199e70"></span>Jetson CPU</div>
        <div class="card-value"><span id="val-cpu">--</span></div>
        <div id="chart-cpu" class="trend-chart"></div>
      </div></div>
    </div>

    <div class="col">
      <div class="card h-100"><div class="card-body">
        <div class="card-label"><span class="metric-dot" style="background:#c98500"></span>Jetson CPU temp</div>
        <div class="card-value"><span id="val-cpuTemp">--</span></div>
        <div id="chart-cpuTemp" class="trend-chart"></div>
      </div></div>
    </div>

  </div>

  <div class="card">
    <div class="card-body">
      <div class="card-label mb-2">Full dt/pluto/{{ callsign }}/# tree</div>
      <div class="table-responsive">
        <table class="table table-sm table-borderless mb-0" id="raw"></table>
      </div>
    </div>
  </div>

</div>

<script src="https://code.highcharts.com/highcharts.js"></script>
<script>
const pollMs = {{ poll_ms }};
const MAX_POINTS = 150;  // ~5 minutes of history at the default 2s poll cadence

const statusEl = document.getElementById("status");
const rawEl = document.getElementById("raw");

Highcharts.setOptions({
  chart: { backgroundColor: "transparent",
           style: { fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif" } },
  title: { text: null },
  credits: { enabled: false },
  legend: { enabled: false },
  xAxis: {
    type: "datetime",
    lineColor: "#383835", tickColor: "#383835",
    gridLineColor: "#2c2c2a",
    labels: { style: { color: "#898781", fontSize: "10px" } }
  },
  yAxis: {
    title: { text: null },
    gridLineColor: "#2c2c2a", gridLineWidth: 1,
    labels: { style: { color: "#898781", fontSize: "10px" } }
  },
  tooltip: {
    backgroundColor: "#1a1a19", borderColor: "#383835", borderRadius: 6,
    style: { color: "#ffffff", fontSize: "12px" }
  },
  plotOptions: {
    area: {
      lineWidth: 2,
      fillOpacity: 0.12,
      marker: { enabled: false, radius: 4, states: { hover: { enabled: true, radiusPlus: 0 } } },
      states: { hover: { lineWidthPlus: 0 } }
    }
  }
});

// Each trend card: where its live value comes from in /telemetry.json, its
// identity color (see the dataviz skill's palette - these four plus the
// isolated Pluto-temp card all pass the validator's adjacent-pair CVD gate
// in this fixed order), and how to format the value.
const TREND_METRICS = [
  { valueId: "val-plutoTemp", chartId: "chart-plutoTemp", color: "#e66767",
    get: d => d.highlights.pluto_temp_c, digits: 1, unit: "\\u00b0C" },
  { valueId: "val-queue", chartId: "chart-queue", color: "#3987e5",
    get: d => numOrNull(d.highlights.tx_dvbs2_queue), digits: 0, unit: "" },
  { valueId: "val-bitrate", chartId: "chart-bitrate", color: "#d95926",
    get: d => d.highlights.tx_dvbs2_ts_bitrate_kbps, digits: 0, unit: " kb/s", isBitrate: true },
  { valueId: "val-cpu", chartId: "chart-cpu", color: "#199e70",
    get: d => d.jetson.cpu_percent, digits: 0, unit: "%" },
  { valueId: "val-cpuTemp", chartId: "chart-cpuTemp", color: "#c98500",
    get: d => d.jetson.cpu_temp_c, digits: 1, unit: "\\u00b0C" },
];

function numOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isNaN(n) ? null : n;
}

function fmt(value, digits, unit) {
  return value === null || value === undefined ? "--" : value.toFixed(digits) + unit;
}

for (const m of TREND_METRICS) {
  m.chart = Highcharts.chart(m.chartId, {
    chart: { type: "area", height: 110, spacing: [2, 2, 2, 2] },
    series: [{ data: [], color: m.color }],
    tooltip: {
      formatter: function () {
        return "<b>" + this.y.toFixed(m.digits) + m.unit + "</b><br>" +
               "<span style=\\"color:#c3c2b7\\">" + Highcharts.dateFormat("%H:%M:%S", this.x) + "</span>";
      }
    }
  });
}

const bitrateMetric = TREND_METRICS.find(m => m.isBitrate);
const bitrateMatchEl = document.getElementById("bitrate-match");
let lastExpectedBitrate = null;

// The Pluto-reported live bitrate should track the value calculated from its
// own live-reported SR/FEC (see _expected_ts_bitrate_kbps() server-side) -
// that calculated number is what "correct" means here, not a guess. Draw it
// as a dashed reference line on the chart and flag any >2% drift, which
// would mean the CBR relay or the modulator config has drifted apart.
function updateBitrateReference(actual, expected) {
  if (expected !== lastExpectedBitrate) {
    bitrateMetric.chart.yAxis[0].removePlotLine("expected-bitrate");
    if (expected !== null) {
      bitrateMetric.chart.yAxis[0].addPlotLine({
        id: "expected-bitrate", value: expected, color: "#898781",
        dashStyle: "Dash", width: 1,
        label: { text: "expected", align: "right", style: { color: "#898781", fontSize: "10px" } }
      });
    }
    lastExpectedBitrate = expected;
  }

  if (actual === null || expected === null) {
    bitrateMatchEl.textContent = expected === null ? "\\u00a0" : "expected " + expected.toFixed(0) + " kb/s";
    bitrateMatchEl.className = "small text-secondary";
    return;
  }
  const deviationPct = Math.abs(actual - expected) / expected * 100;
  if (deviationPct <= 2) {
    bitrateMatchEl.textContent = "\\u2713 matches " + expected.toFixed(0) + " kb/s expected";
    bitrateMatchEl.className = "small status-good";
  } else {
    bitrateMatchEl.textContent = "\\u26a0 expected " + expected.toFixed(0) + " kb/s (" +
                                  deviationPct.toFixed(0) + "% off)";
    bitrateMatchEl.className = "small status-warning";
  }
}

function pushPoint(chart, value) {
  if (value === null || value === undefined) return;
  const shift = chart.series[0].data.length >= MAX_POINTS;
  chart.series[0].addPoint([Date.now(), value], true, shift, false);
}

function setStatus(text, cls) {
  statusEl.textContent = "\\u25cf " + text;
  statusEl.className = "fs-6 " + cls;
}

function renderRawTable(raw) {
  rawEl.textContent = "";
  for (const key of Object.keys(raw).sort()) {
    const row = document.createElement("tr");
    const keyCell = document.createElement("td");
    keyCell.className = "key";
    keyCell.textContent = key;
    const valueCell = document.createElement("td");
    valueCell.textContent = raw[key];
    row.append(keyCell, valueCell);
    rawEl.appendChild(row);
  }
}

async function poll() {
  try {
    const response = await fetch("/telemetry.json");
    const data = await response.json();

    if (data.connected) {
      setStatus("live", "status-good");
    } else {
      setStatus("waiting for Pluto telemetry...", "status-warning");
    }

    const h = data.highlights;
    document.getElementById("val-ptt").textContent =
      h.tx_ptt_on === null ? "--" : (h.tx_ptt_on ? "ON" : "OFF");
    document.getElementById("val-ptt").className =
      "card-value " + (h.tx_ptt_on === null ? "" : (h.tx_ptt_on ? "status-good" : "status-critical"));
    document.getElementById("val-freq").textContent = fmt(h.tx_frequency_mhz, 3, " MHz");
    document.getElementById("val-gain").textContent = fmt(h.tx_gain_db, 0, " dB");
    document.getElementById("val-sr").textContent = fmt(numOrNull(h.tx_dvbs2_sr), 0, " Sym/s");

    for (const m of TREND_METRICS) {
      const value = m.get(data);
      document.getElementById(m.valueId).textContent = fmt(value, m.digits, m.unit);
      pushPoint(m.chart, value);
    }
    updateBitrateReference(h.tx_dvbs2_ts_bitrate_kbps, h.tx_dvbs2_ts_bitrate_expected_kbps);

    renderRawTable(data.raw);
  } catch (exc) {
    setStatus("connection to dashboard backend lost", "status-critical");
  }
}

poll();
setInterval(poll, pollMs);
</script>

</body>
</html>
"""


if __name__ == "__main__":
    print("Telemetry dashboard: http://{}:{}/".format(get_lan_ip(), HTTP_PORT))
    app.run(host="0.0.0.0", port=HTTP_PORT)
