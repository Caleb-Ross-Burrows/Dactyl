"use strict";

// ---------------------------------------------------------------- constants

const PLOTLY_SOURCES = [
  "/static/vendor/plotly.min.js",                      // local copy, works offline
  "https://cdn.plot.ly/plotly-2.35.2.min.js",          // fallback
];
const THEME = { paper: "#161b22", plot: "#0d1117", text: "#c9d1d9", grid: "#30363d" };
const POLL_MS = 1500;
const PREFETCH_AHEAD = 8;
const MAX_CACHED_FRAMES = 300;
const DEFAULT_PARAMS = ["force", "v", "z"];

// column -> [label, unit]
const LABELS = {
  t: ["Time", "s"],
  step: ["Step", ""],
  z: ["Payload position (rear face)", "m"],
  v: ["Velocity", "m/s"],
  a: ["Acceleration", "m/s²"],
  force: ["Axial force", "N"],
  kinetic_energy: ["Kinetic energy", "J"],
  work_done: ["Work done by force", "J"],
  B_max: ["Peak |B| in view", "T"],
  B_p995: ["|B| 99.5th percentile", "T"],
  B_payload_mean: ["Mean |B| in payload", "T"],
  B_payload_max: ["Peak |B| in payload", "T"],
};

function describe(col) {
  if (LABELS[col]) return LABELS[col];
  const m = col.match(/^coil_(\d+)_(flux_linkage|voltage)$/);
  if (m) return m[2] === "voltage" ? [`Coil ${m[1]} voltage`, "V"] : [`Coil ${m[1]} flux linkage`, "Wb"];
  return [col, ""];
}
const axisTitle = (col) => { const [l, u] = describe(col); return u ? `${l} (${u})` : l; };

// -------------------------------------------------------------------- state

const state = {
  runs: [],
  runId: null,
  meta: null,
  cols: {},
  n: 0,                 // number of recorded steps
  step: 0,
  selected: new Set(DEFAULT_PARAMS),
  xcol: "t",
  playId: 0,            // bumped to cancel a running playback loop
  playing: false,
  showToken: 0,
  frames: new Map(),    // "run:step" -> Float32Array
  inflight: new Map(),
  pollTimer: null,
  axes: null,           // density plot axes for the current run
  densityReady: false,
};

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmt = (v) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Math.abs(v) >= 1e4 || (v !== 0 && Math.abs(v) < 1e-3) ? v.toExponential(3) : Number(v.toPrecision(5)).toString();

async function getJSON(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${url}: ${resp.status}`);
  return resp.json();
}

// ------------------------------------------------------------------ plotly

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error(src));
    document.head.appendChild(s);
  });
}

async function ensurePlotly() {
  for (const src of PLOTLY_SOURCES) {
    if (window.Plotly) return;
    try { await loadScript(src); } catch (_) { /* try the next source */ }
  }
  if (!window.Plotly) {
    throw new Error("Could not load Plotly. Connect to the internet once, or save plotly.min.js to viewer/static/vendor/.");
  }
}

function baseLayout(extra = {}) {
  return Object.assign({
    paper_bgcolor: THEME.paper, plot_bgcolor: THEME.plot,
    font: { color: THEME.text, size: 12 },
    margin: { l: 70, r: 20, t: 10, b: 42 },
  }, extra);
}
const axisStyle = (extra = {}) => Object.assign({ gridcolor: THEME.grid, zerolinecolor: THEME.grid, linecolor: THEME.grid }, extra);
const PLOT_CONFIG = { responsive: true, displaylogo: false };

// ------------------------------------------------------------ density plot

function gridAxes(grid) {
  const dz = (grid.z_max - grid.z_min) / (grid.n_z - 1);
  const dr = grid.r_max / (grid.n_r - 1);
  const x = Array.from({ length: grid.n_z }, (_, i) => grid.z_min + i * dz);
  const rPos = Array.from({ length: grid.n_r }, (_, i) => i * dr);
  const y = rPos.slice(1).reverse().map((v) => -v).concat(rPos);     // mirrored about the axis
  return { x, y };
}

// Frame is (n_r, n_z) row-major; build the mirrored (2*n_r - 1, n_z) matrix Plotly wants
function frameToMatrix(data, grid) {
  const { n_r: nR, n_z: nZ } = grid;
  const rows = [];
  for (let k = 0; k < 2 * nR - 1; k++) {
    const ri = Math.abs(k - (nR - 1));
    rows.push(Array.from(data.subarray(ri * nZ, (ri + 1) * nZ)));
  }
  return rows;
}

function rect(x0, x1, y0, y1, line, fill) {
  return { type: "rect", xref: "x", yref: "y", x0, x1, y0, y1, line: { color: line, width: 1.5 }, fillcolor: fill, layer: "above" };
}

function coilShapes() {
  const shapes = [];
  for (const c of state.meta.coils) {
    for (const sign of [1, -1]) {
      const [a, b] = [sign * c.inner_radius, sign * c.outer_radius];
      shapes.push(rect(c.z0, c.z0 + c.length, Math.min(a, b), Math.max(a, b), "#f59e0b", "rgba(245,158,11,0.25)"));
    }
  }
  return shapes;
}

function payloadShapes(z) {
  const p = state.meta.payload;
  const line = "#3fb950", fill = "rgba(63,185,80,0.15)";
  if (p.type === "sphere") {
    return [{ type: "circle", xref: "x", yref: "y", x0: z, x1: z + 2 * p.radius, y0: -p.radius, y1: p.radius,
              line: { color: line, width: 2 }, fillcolor: fill, layer: "above" }];
  }
  const [r0, r1] = p.type === "tube" ? [p.inner_radius, p.outer_radius] : [0, p.radius];
  if (r0 === 0) return [rect(z, z + p.length, -r1, r1, line, fill)];
  return [rect(z, z + p.length, r0, r1, line, fill), rect(z, z + p.length, -r1, -r0, line, fill)];
}

const shapesAt = (z) => coilShapes().concat(payloadShapes(z));

function currentZ() {
  const z = state.cols.z && state.cols.z[state.step];
  return z === undefined || z === null ? 0 : z;
}

function autoColourMax() {
  const vals = (state.cols.B_p995 || []).filter(Number.isFinite);
  return vals.length ? Math.max(...vals) : 1;
}
function colourMax() {
  return $("#cmax-auto").checked ? autoColourMax() : (parseFloat($("#cmax").value) || autoColourMax());
}
function syncColourInput() {
  if ($("#cmax-auto").checked) $("#cmax").value = Number(autoColourMax().toPrecision(3));
}

async function initDensity() {
  const grid = state.meta.grid;
  state.axes = gridAxes(grid);
  const empty = state.axes.y.map(() => state.axes.x.map(() => null));
  $("#density").innerHTML = "";
  await Plotly.newPlot("density", [{
    type: "heatmap", x: state.axes.x, y: state.axes.y, z: empty,
    zmin: 0, zmax: colourMax(), colorscale: "Inferno", zsmooth: "fast",
    colorbar: { title: { text: "|B| (T)" }, thickness: 14 },
    hovertemplate: "z = %{x:.3f} m<br>r = %{y:.3f} m<br>|B| = %{z:.4f} T<extra></extra>",
  }], baseLayout({
    xaxis: axisStyle({ title: "z (m)", range: [grid.z_min, grid.z_max], constrain: "domain" }),
    yaxis: axisStyle({ title: "r (m)", scaleanchor: "x", scaleratio: 1, range: [-grid.r_max, grid.r_max], constrain: "domain" }),
    shapes: shapesAt(currentZ()),
    margin: { l: 70, r: 20, t: 10, b: 45 },
  }), PLOT_CONFIG);
  state.densityReady = true;
}

// ------------------------------------------------------------------ frames

async function fetchFrame(runId, step) {
  const key = `${runId}:${step}`;
  if (state.frames.has(key)) return state.frames.get(key);
  if (state.inflight.has(key)) return state.inflight.get(key);

  const promise = fetch(`/api/runs/${encodeURIComponent(runId)}/frame/${step}`)
    .then((resp) => { if (!resp.ok) throw new Error(`frame ${step}: ${resp.status}`); return resp.arrayBuffer(); })
    .then((buf) => {
      const data = new Float32Array(buf);
      state.frames.set(key, data);
      if (state.frames.size > MAX_CACHED_FRAMES) state.frames.delete(state.frames.keys().next().value);
      return data;
    })
    .finally(() => state.inflight.delete(key));
  state.inflight.set(key, promise);
  return promise;
}

function prefetch(from) {
  for (let i = from; i < Math.min(state.n, from + PREFETCH_AHEAD); i++) {
    fetchFrame(state.runId, i).catch(() => {});
  }
}

async function showFrame(step) {
  if (state.n === 0) return;
  step = Math.max(0, Math.min(state.n - 1, step));
  state.step = step;
  const token = ++state.showToken;
  updateTransport();
  updateReadout();
  updateMarkers();

  let data;
  try { data = await fetchFrame(state.runId, step); }
  catch (error) { console.warn(error); return; }
  if (token !== state.showToken || !state.densityReady) return;    // a newer request superseded this one

  await Plotly.update("density",
    { z: [frameToMatrix(data, state.meta.grid)], zmax: [colourMax()] },
    { shapes: shapesAt(currentZ()) });
  prefetch(step + 1);
}

// --------------------------------------------------------------- transport

function updateTransport() {
  const slider = $("#slider");
  slider.max = Math.max(0, state.n - 1);
  slider.value = state.step;
  $("#step-label").textContent = state.n ? `step ${state.step} / ${state.n - 1}   t = ${fmt((state.cols.t || [])[state.step])} s` : "no steps yet";
  $("#play").textContent = state.playing ? "Pause" : "Play";
}

function updateReadout() {
  const box = $("#readout");
  box.innerHTML = "";
  if (!state.n) return;
  for (const col of Object.keys(state.cols)) {
    if (col === "step") continue;
    const [label, unit] = describe(col);
    const row = document.createElement("div");
    row.innerHTML = `<span class="k"></span><span class="mono"></span>`;
    row.firstChild.textContent = label;
    row.lastChild.textContent = `${fmt(state.cols[col][state.step])} ${unit}`;
    box.appendChild(row);
  }
}

function stopPlayback() {
  state.playId++;
  state.playing = false;
  updateTransport();
}

async function startPlayback() {
  if (!state.n) return;
  const id = ++state.playId;
  state.playing = true;
  if (state.step >= state.n - 1 && !isRunning()) state.step = -1;      // replay from the start
  updateTransport();

  while (state.playing && id === state.playId) {
    const started = performance.now();
    if (state.step >= state.n - 1) {
      if (isRunning()) { await sleep(300); continue; }                  // wait for the next step to be recorded
      if ($("#loop").checked) { await showFrame(0); }
      else { break; }
    } else {
      await showFrame(state.step + 1);
    }
    const interval = 1000 / parseFloat($("#fps").value);
    await sleep(Math.max(0, interval - (performance.now() - started)));
  }
  if (id === state.playId) { state.playing = false; updateTransport(); }
}

const isRunning = () => state.meta && state.meta.status === "running";

// ------------------------------------------------------------- time series

let syncingRange = false;

function plotDivs() { return [...document.querySelectorAll("#plots .plot")]; }

function seriesFor(col) {
  const x = state.cols[state.xcol] || [];
  const y = state.cols[col] || [];
  return { x, y };
}

function markerFor(col) {
  const { x, y } = seriesFor(col);
  const i = Math.min(state.step, x.length - 1);
  return i >= 0 ? { x: [x[i]], y: [y[i]] } : { x: [], y: [] };
}

function buildPlots() {
  const holder = $("#plots");
  Plotly.purge && plotDivs().forEach((d) => Plotly.purge(d));
  holder.innerHTML = "";
  const columns = Object.keys(state.cols).filter((c) => state.selected.has(c));

  for (const col of columns) {
    const div = document.createElement("div");
    div.className = "plot";
    div.dataset.col = col;
    holder.appendChild(div);
    const { x, y } = seriesFor(col);
    const m = markerFor(col);
    Plotly.newPlot(div, [
      { x, y, mode: "lines+markers", marker: { size: 3 }, line: { width: 1.6, color: "#58a6ff" }, hovertemplate: "%{x:.4g}, %{y:.5g}<extra></extra>" },
      { x: m.x, y: m.y, mode: "markers", marker: { size: 10, color: "#f0f6fc", line: { color: "#f85149", width: 2 } }, hoverinfo: "skip" },
    ], baseLayout({
      showlegend: false,
      xaxis: axisStyle({ title: axisTitle(state.xcol) }),
      yaxis: axisStyle({ title: { text: axisTitle(col), font: { size: 11 } } }),
      margin: { l: 80, r: 20, t: 8, b: 40 },
    }), PLOT_CONFIG).then(() => {
      div.on("plotly_click", (ev) => {
        const point = ev.points.find((p) => p.curveNumber === 0);
        if (point) { stopPlayback(); showFrame(point.pointIndex); }
      });
      div.on("plotly_relayout", (ev) => syncRange(div, ev));
    });
  }
}

// Keep the x zoom of all parameter plots together
function syncRange(source, ev) {
  if (syncingRange) return;
  const hasRange = ev["xaxis.range[0]"] !== undefined;
  if (!hasRange && !ev["xaxis.autorange"]) return;
  const update = hasRange ? { "xaxis.range": [ev["xaxis.range[0]"], ev["xaxis.range[1]"]] } : { "xaxis.autorange": true };
  syncingRange = true;
  Promise.all(plotDivs().filter((d) => d !== source).map((d) => Plotly.relayout(d, update)))
    .finally(() => { syncingRange = false; });
}

function refreshPlotData() {
  for (const div of plotDivs()) {
    const { x, y } = seriesFor(div.dataset.col);
    const m = markerFor(div.dataset.col);
    Plotly.restyle(div, { x: [x, m.x], y: [y, m.y] }, [0, 1]);
  }
}

function updateMarkers() {
  for (const div of plotDivs()) {
    const m = markerFor(div.dataset.col);
    Plotly.restyle(div, { x: [m.x], y: [m.y] }, [1]);
  }
}

// ---------------------------------------------------------------- controls

function buildControls() {
  const cols = Object.keys(state.cols);
  if (!cols.includes(state.xcol)) state.xcol = cols.includes("t") ? "t" : cols[0];

  const xsel = $("#xcol");
  xsel.innerHTML = "";
  for (const c of ["t", "z", "step"].filter((c) => cols.includes(c))) {
    xsel.add(new Option(axisTitle(c), c, false, c === state.xcol));
  }

  const chips = $("#params");
  chips.innerHTML = "";
  for (const col of cols.filter((c) => c !== "step")) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.selected.has(col);
    box.addEventListener("change", () => {
      box.checked ? state.selected.add(col) : state.selected.delete(col);
      buildPlots();
    });
    label.append(box, document.createTextNode(describe(col)[0]));
    chips.appendChild(label);
  }
}

function setStatus() {
  const m = state.meta;
  const badge = $("#status");
  badge.textContent = m.status;
  badge.className = `badge ${m.status}`;
  badge.title = m.message || "";
  const p = m.payload;
  $("#summary").textContent =
    `${m.coils.length} coils · ${p.type} (${p.material}, ${p.mass} kg) · dt = ${m.dt} s · ${m.steps} steps` +
    (m.message ? ` · ${m.message}` : "");
}

// ------------------------------------------------------------------ loading

async function loadRunList(keepSelection = true) {
  state.runs = await getJSON("/api/runs");
  const sel = $("#run");
  sel.innerHTML = "";
  for (const r of state.runs) {
    sel.add(new Option(`${r.id}  [${r.status}, ${r.steps} steps]`, r.id, false, keepSelection && r.id === state.runId));
  }
}

async function loadRun(runId) {
  stopPlayback();
  clearInterval(state.pollTimer);
  const data = await getJSON(`/api/runs/${encodeURIComponent(runId)}`);

  state.runId = runId;
  state.meta = data.meta;
  state.cols = data.columns;
  state.n = (state.cols.t || []).length;
  state.step = 0;
  state.frames.clear();
  state.densityReady = false;
  location.hash = `run=${encodeURIComponent(runId)}`;

  setStatus();
  buildControls();
  buildPlots();
  syncColourInput();
  await initDensity();
  $("#follow").checked = isRunning();
  updateTransport();
  updateReadout();

  // A finished run starts at the beginning; a live run starts at its latest step
  if (state.n) await showFrame(isRunning() ? state.n - 1 : 0);
  if (isRunning()) state.pollTimer = setInterval(pollRun, POLL_MS);
}

async function pollRun() {
  try {
    const data = await getJSON(`/api/runs/${encodeURIComponent(state.runId)}`);
    const grew = (data.columns.t || []).length !== state.n;
    state.meta = data.meta;
    state.cols = data.columns;
    state.n = (state.cols.t || []).length;
    setStatus();
    syncColourInput();
    if (grew) {
      // New columns can appear part way through a run (e.g. the first step lacks some)
      if (Object.keys(state.cols).length !== $("#params").children.length + 1) { buildControls(); buildPlots(); }
      refreshPlotData();
      if ($("#follow").checked && !state.playing) await showFrame(state.n - 1);
      else { updateTransport(); }
    }
    if (!isRunning()) {
      clearInterval(state.pollTimer);
      $("#follow").checked = false;
      await loadRunList();
    }
  } catch (error) {
    console.warn("poll failed", error);
  }
}

// ---------------------------------------------------------------------- init

function wireEvents() {
  $("#run").addEventListener("change", (e) => loadRun(e.target.value));
  $("#refresh").addEventListener("click", async () => { await loadRunList(); if (state.runId) await loadRun(state.runId); });
  $("#play").addEventListener("click", () => (state.playing ? stopPlayback() : startPlayback()));
  $("#prev").addEventListener("click", () => { stopPlayback(); showFrame(state.step - 1); });
  $("#next").addEventListener("click", () => { stopPlayback(); showFrame(state.step + 1); });
  $("#slider").addEventListener("input", (e) => {
    const target = parseInt(e.target.value, 10);      // read first: stopPlayback() re-syncs the slider to the old step
    stopPlayback();
    showFrame(target);
  });
  $("#xcol").addEventListener("change", (e) => { state.xcol = e.target.value; buildPlots(); });
  $("#cmax").addEventListener("input", () => { $("#cmax-auto").checked = false; if (state.densityReady) Plotly.restyle("density", { zmax: [colourMax()] }); });
  $("#cmax-auto").addEventListener("change", () => { syncColourInput(); if (state.densityReady) Plotly.restyle("density", { zmax: [colourMax()] }); });
  $("#follow").addEventListener("change", (e) => { if (e.target.checked && state.n) showFrame(state.n - 1); });
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement.tagName) && document.activeElement.type !== "range") return;
    if (e.key === " ") { e.preventDefault(); state.playing ? stopPlayback() : startPlayback(); }
    else if (e.key === "ArrowRight") { stopPlayback(); showFrame(state.step + 1); }
    else if (e.key === "ArrowLeft") { stopPlayback(); showFrame(state.step - 1); }
  });
}

async function main() {
  const placeholder = $("#density");
  try {
    await ensurePlotly();
    wireEvents();

    const wanted = new URLSearchParams(location.hash.slice(1)).get("run");
    await loadRunList(false);
    if (!state.runs.length) {
      placeholder.innerHTML = `<p class="placeholder">No runs found in the results folder.<br>Run a simulation (it records automatically), then press Refresh.</p>`;
      return;
    }
    const initial = state.runs.some((r) => r.id === wanted) ? wanted : state.runs[0].id;
    $("#run").value = initial;
    await loadRun(initial);
  } catch (error) {
    console.error(error);
    placeholder.innerHTML = `<p class="placeholder"></p>`;
    placeholder.firstChild.textContent = error.message;
  }
}

main();
