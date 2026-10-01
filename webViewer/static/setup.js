"use strict";

// "New simulation" panel. Builds on the helpers in app.js (state, $, getJSON, loadRun, ...).
// The form is the source of truth: every change is sent to /api/validate, which reports field errors,
// warnings and the resulting layout (drawn as a preview). Nothing is checked here that the server
// does not also check, so the browser and the simulation can never disagree about what is valid.

(() => {
  const STORAGE_KEY = "dactyl.setup";
  const VALIDATE_DELAY_MS = 250;
  const JOB_POLL_MS = 1000;

  const setupState = {
    defaults: null,
    library: null,        // {materials: [...], awg: [...]} or null if FEMM's library wasn't found
    valid: false,
    seq: 0,               // discards validation replies that arrive out of order
    timer: null,
    job: null,
    pollTimer: null,
    openedRun: null,      // run id of the job we already switched the viewer to
    previewReady: false,
  };

  const FIELD_NAMES = {
    name: "Name", dt: "Time step", max_steps: "Max steps", max_time: "Max time", start_z: "Payload start z",
    min_z: "Furthest back z", coil_spacing: "Gap between coils", coils: "Coils", payload: "Payload",
    "payload.type": "Payload shape", "payload.material": "Payload material", "payload.mass": "Payload mass",
    "payload.radius": "Payload radius", "payload.inner_radius": "Payload inner radius",
    "payload.outer_radius": "Payload outer radius", "payload.length": "Payload length",
  };
  const COIL_FIELD_NAMES = { inner_radius: "inner radius", length: "length", turns: "turns", awg: "wire", current: "current" };
  const COIL_FIELDS = ["inner_radius", "length", "turns", "awg", "current"];
  const NEW_COIL = { inner_radius: 0.1, length: 0.2, turns: 500, awg: 10, current: 10, schedule: null };
  const clone = (x) => (x ? JSON.parse(JSON.stringify(x)) : null);

  // Current schedules, one entry per coil row: null (constant current) or {interp, points: [{t, current}]}.
  // `stash` keeps a schedule that was switched off, so ticking the box again brings it back.
  let schedules = [];
  let stash = [];
  let editing = -1;          // coil whose schedule is open in the editor, or -1

  const root = () => document.querySelector("#setup");
  const fieldElements = () => [...root().querySelectorAll("[data-path]")];

  function humanize(path) {
    const sp = path.match(/^coils\[(\d+)\]\.schedule(?:\.points\[(\d+)\]\.(\w+)|\.(\w+))?$/);
    if (sp) {
      const base = `Coil ${Number(sp[1]) + 1} schedule`;
      if (sp[2] !== undefined) return `${base}, point ${Number(sp[2]) + 1} ${sp[3] === "t" ? "time" : "current"}`;
      return sp[4] ? `${base} ${sp[4]}` : base;
    }
    const m = path.match(/^coils\[(\d+)\]\.(\w+)$/);
    if (m) return `Coil ${Number(m[1]) + 1} ${COIL_FIELD_NAMES[m[2]] || m[2]}`;
    const c = path.match(/^coils\[(\d+)\]$/);
    if (c) return `Coil ${Number(c[1]) + 1}`;
    return FIELD_NAMES[path] || path;
  }

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body === undefined ? {} : body),
    });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* no body */ }
    return { status: resp.status, data };
  }

  // ------------------------------------------------------------ form <-> config

  function readValue(el) {
    if (el.tagName === "SELECT" && !el.dataset.num) return el.value;
    if (el.type === "number" || el.dataset.num) {
      const text = el.value.trim();
      return text === "" ? null : Number(text);
    }
    return el.value;
  }

  function writeValue(el, value) {
    el.value = value === null || value === undefined ? "" : String(value);
  }

  function readForm() {
    const cfg = { payload: {}, coils: [] };
    for (const el of fieldElements()) {
      const path = el.dataset.path;
      if (path.startsWith("coils[")) continue;
      const wrapper = el.closest("[data-for]");
      if (wrapper && wrapper.hidden) continue;            // a dimension this shape doesn't use
      if (path.startsWith("payload.")) cfg.payload[path.slice(8)] = readValue(el);
      else cfg[path] = readValue(el);
    }
    root().querySelectorAll("#coil-table tbody tr").forEach((row, i) => {
      const coil = {};
      for (const el of row.querySelectorAll("[data-field]")) coil[el.dataset.field] = readValue(el);
      coil.schedule = clone(schedules[i]);
      cfg.coils.push(coil);
    });
    return cfg;
  }

  function applyPayloadType() {
    const type = root().querySelector('[data-path="payload.type"]').value;
    for (const wrapper of root().querySelectorAll("[data-for]")) {
      wrapper.hidden = !wrapper.dataset.for.split(" ").includes(type);
    }
  }

  function awgControl(value) {
    const lib = setupState.library;
    if (!lib || !lib.awg.length) {
      const input = document.createElement("input");
      input.type = "number"; input.step = "1"; input.value = value ?? "";
      return input;
    }
    const select = document.createElement("select");
    select.dataset.num = "1";
    const sizes = lib.awg.slice();
    if (value !== null && value !== undefined && !sizes.includes(value)) sizes.push(value);   // keep, the validator will flag it
    for (const size of sizes) select.add(new Option(`${size} AWG`, size, false, size === value));
    return select;
  }

  function addCoilRow(coil, index) {
    const tbody = root().querySelector("#coil-table tbody");
    const row = tbody.insertRow();
    row.insertCell().textContent = index + 1;

    for (const field of COIL_FIELDS) {
      const cell = row.insertCell();
      let input;
      if (field === "awg") input = awgControl(coil.awg);
      else { input = document.createElement("input"); input.type = "number"; input.step = field === "turns" ? "1" : "any"; writeValue(input, coil[field]); }
      input.dataset.field = field;
      input.dataset.path = `coils[${index}].${field}`;
      cell.appendChild(input);
    }

    const schedCell = row.insertCell();
    const schedBtn = document.createElement("button");
    schedBtn.type = "button"; schedBtn.className = "sched-btn"; schedBtn.dataset.action = "schedule";
    schedBtn.dataset.schedButton = index;
    schedBtn.title = "Edit this coil's current over time";
    schedCell.appendChild(schedBtn);

    const computed = row.insertCell();
    computed.className = "computed";
    computed.textContent = "–";

    const actions = row.insertCell();
    const copy = document.createElement("button");
    copy.type = "button"; copy.textContent = "Copy"; copy.title = "Add another coil with the same settings";
    copy.dataset.action = "copy";
    const remove = document.createElement("button");
    remove.type = "button"; remove.textContent = "✕"; remove.title = "Remove this coil";
    remove.dataset.action = "remove";
    actions.append(copy, " ", remove);
  }

  function rebuildCoils(coils, keepStash = false) {
    schedules = coils.map((c) => clone(c.schedule));
    stash = coils.map((_, i) => (keepStash && stash[i]) || null);
    if (editing >= coils.length) editing = -1;

    root().querySelector("#coil-table tbody").innerHTML = "";
    coils.forEach((coil, i) => addCoilRow(coil, i));
    for (const button of root().querySelectorAll('button[data-action="remove"]')) {
      button.disabled = coils.length <= 1;
    }
    renderEditor();
  }

  // ----------------------------------------------------------- schedule editor

  function scheduleSummary(s) {
    if (!s) return "Constant";
    const n = s.points.length;
    return `${n} point${n === 1 ? "" : "s"}, ${s.interp === "linear" ? "ramp" : "hold"}`;
  }

  function refreshScheduleButtons() {
    root().querySelectorAll("#coil-table tbody tr").forEach((row, i) => {
      const button = row.querySelector(".sched-btn");
      button.textContent = scheduleSummary(schedules[i]);
      button.classList.toggle("active", i === editing);
      const current = row.querySelector('[data-field="current"]');
      current.disabled = !!schedules[i];
      current.title = schedules[i] ? "Not used: this coil follows its schedule" : "";
    });
  }

  function renderEditor() {
    const box = root().querySelector("#schedule-editor");
    refreshScheduleButtons();
    if (editing < 0 || editing >= schedules.length) { box.hidden = true; return; }
    box.hidden = false;

    const s = schedules[editing];
    root().querySelector("#sched-title").textContent = `Coil ${editing + 1} current`;
    root().querySelector("#sched-enabled").checked = !!s;
    const interp = root().querySelector("#sched-interp");
    interp.value = s ? s.interp : "step";
    interp.disabled = !s;
    interp.dataset.path = `coils[${editing}].schedule.interp`;
    root().querySelector("#sched-add").disabled = !s;
    root().querySelector("#sched-help").hidden = !s;

    const table = root().querySelector("#sched-table");
    table.classList.toggle("off", !s);
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = "";
    if (!s) return;
    s.points.forEach((point, j) => {
      const row = tbody.insertRow();
      row.insertCell().textContent = j + 1;
      for (const field of ["t", "current"]) {
        const input = document.createElement("input");
        input.type = "number"; input.step = "any";
        writeValue(input, point[field]);
        input.dataset.sfield = field;
        input.dataset.spoint = j;
        input.dataset.path = `coils[${editing}].schedule.points[${j}].${field}`;
        row.insertCell().appendChild(input);
      }
      const remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "✕"; remove.title = "Remove this point";
      remove.dataset.sact = "remove"; remove.dataset.spoint = j;
      remove.disabled = s.points.length <= 1;
      row.insertCell().appendChild(remove);
    });
  }

  function writeForm(cfg) {
    for (const el of fieldElements()) {
      const path = el.dataset.path;
      if (path.startsWith("coils[")) continue;
      const value = path.startsWith("payload.") ? (cfg.payload || {})[path.slice(8)] : cfg[path];
      writeValue(el, value);
    }
    applyPayloadType();
    editing = -1;
    rebuildCoils((cfg.coils && cfg.coils.length ? cfg.coils : [NEW_COIL]).map((c) => ({ ...c })));
  }

  function configFromRun(meta) {
    if (meta.config) return meta.config;
    // Older runs didn't store their settings: rebuild them from what was recorded
    const coils = meta.coils.map((c) => ({ inner_radius: c.inner_radius, length: c.length, turns: c.turns, awg: c.awg, current: c.current }));
    const first = meta.coils[0];
    const spacing = meta.coils.length > 1 ? meta.coils[1].z0 - (first.z0 + first.length) : setupState.defaults.coil_spacing;
    const { type, material, mass, radius, length, inner_radius, outer_radius } = meta.payload;
    const payload = { type, material, mass };
    for (const [k, v] of Object.entries({ radius, length, inner_radius, outer_radius })) if (v !== undefined) payload[k] = v;
    return {
      name: "", dt: meta.dt, max_steps: setupState.defaults.max_steps, max_time: null,
      min_z: meta.min_z ?? setupState.defaults.min_z, start_z: (state.cols.z || [0])[0] ?? 0,
      coil_spacing: spacing, coils, payload,
    };
  }

  // -------------------------------------------------------------- validation

  function scheduleValidate() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(readForm()));
    clearTimeout(setupState.timer);
    setupState.timer = setTimeout(validate, VALIDATE_DELAY_MS);
  }

  async function validate() {
    const seq = ++setupState.seq;
    let reply;
    try { reply = await postJSON("/api/validate", readForm()); }
    catch (error) { showMessages([{ level: "error", text: `Could not reach the server: ${error.message}` }]); setupState.valid = false; updateButtons(); return; }
    if (seq !== setupState.seq) return;                  // a newer edit superseded this check

    for (const el of fieldElements()) { el.classList.remove("invalid"); el.removeAttribute("title"); }
    for (const b of root().querySelectorAll(".sched-btn")) b.classList.remove("invalid");
    const messages = [];
    const result = reply.data;

    if (!result || reply.status !== 200) {
      messages.push({ level: "error", text: (result && result.error) || `Server returned ${reply.status}` });
      setupState.valid = false;
    } else if (!result.ok) {
      setupState.valid = false;
      for (const e of result.errors) {
        const el = root().querySelector(`[data-path="${CSS.escape(e.field)}"]`);
        if (el) { el.classList.add("invalid"); el.title = e.message; }
        const sched = e.field.match(/^coils\[(\d+)\]\.schedule/);
        if (sched) root().querySelector(`[data-sched-button="${sched[1]}"]`)?.classList.add("invalid");
        messages.push({ level: "error", text: `${humanize(e.field)}: ${e.message}` });
      }
    } else {
      setupState.valid = true;
      result.geometry.coils.forEach((c, i) => {
        const cell = root().querySelectorAll("#coil-table tbody tr")[i]?.querySelector(".computed");
        if (cell) cell.textContent = Number(c.outer_radius.toPrecision(4));
      });
      for (const w of result.warnings) messages.push({ level: "warning", text: `${humanize(w.field)}: ${w.message}` });
      drawPreview(result.geometry);
      drawSchedulePlot(result.config, result.geometry);
    }
    showMessages(messages);
    updateButtons();
  }

  function showMessages(messages) {
    const box = root().querySelector("#setup-messages");
    box.innerHTML = "";
    if (!messages.length) return;
    const list = document.createElement("ul");
    for (const m of messages) {
      const item = document.createElement("li");
      item.className = m.level;
      item.textContent = m.text;
      list.appendChild(item);
    }
    box.appendChild(list);
  }

  // ----------------------------------------------------------------- preview

  async function drawPreview(g) {
    try { await ensurePlotly(); } catch (_) { return; }
    const meta = { coils: g.coils, payload: g.payload };
    const maxR = Math.max(...g.coils.map((c) => c.outer_radius));
    const yr = 1.5 * maxR;
    const x0 = Math.min(g.min_z, g.start_z) - 0.05;
    const x1 = Math.max(g.max_z, g.start_z + g.payload_extent) + 0.05;
    const marker = (x, color, text) => ({
      line: { type: "line", xref: "x", yref: "y", x0: x, x1: x, y0: -yr, y1: yr, line: { color, width: 1, dash: "dash" } },
      note: { x, y: yr, xref: "x", yref: "y", text, showarrow: false, yanchor: "bottom", xanchor: "left", font: { size: 10, color } },
    });
    const back = marker(g.min_z, "#f85149", "back limit");
    const finish = marker(g.max_z, "#3fb950", "end of last coil");

    await Plotly.react("preview", [{ x: [x0, x1], y: [0, 0], mode: "lines", line: { color: THEME.grid, width: 1 }, hoverinfo: "skip" }], baseLayout({
      showlegend: false,
      xaxis: axisStyle({ title: "z (m)", range: [x0, x1], constrain: "domain" }),
      yaxis: axisStyle({ title: "r (m)", range: [-yr, yr], scaleanchor: "x", scaleratio: 1, constrain: "domain" }),
      shapes: shapesAt(g.start_z, meta).concat([back.line, finish.line]),
      annotations: [back.note, finish.note],
      margin: { l: 60, r: 20, t: 18, b: 40 },
    }), PLOT_CONFIG);
  }

  async function drawSchedulePlot(config, g) {
    const holder = root().querySelector("#schedule-plot");
    const any = config.coils.some((c) => c.schedule);
    holder.hidden = !any;
    if (!any) return;
    try { await ensurePlotly(); } catch (_) { return; }

    // Show the interesting part: a little past the last schedule point, never beyond the end of the run
    const lastPoint = Math.max(...config.coils.filter((c) => c.schedule).map((c) => c.schedule.points[c.schedule.points.length - 1].t));
    const end = lastPoint > 0 ? Math.min(g.run_time, lastPoint * 1.3) : Math.min(g.run_time, 1);
    const traces = config.coils.map((coil, i) => {
      let x, y, shape = "linear", dash = "dot";
      if (coil.schedule) {
        const pts = coil.schedule.points.map((p) => [p.t, p.current]);
        if (pts[0][0] > 0) pts.unshift([0, pts[0][1]]);                       // first value is held before the first point
        if (pts[pts.length - 1][0] < end) pts.push([end, pts[pts.length - 1][1]]);   // last value is held afterwards
        x = pts.map((p) => p[0]); y = pts.map((p) => p[1]);
        shape = coil.schedule.interp === "step" ? "hv" : "linear";
        dash = "solid";
      } else { x = [0, end]; y = [coil.current, coil.current]; }
      return { x, y, mode: "lines", name: `Coil ${i + 1}`, line: { shape, dash, width: 2 },
               hovertemplate: `Coil ${i + 1}: %{y:.4g} A at %{x:.4g} s<extra></extra>` };
    });
    await Plotly.react("schedule-plot", traces, baseLayout({
      showlegend: true, legend: { orientation: "h", y: 1.18 },
      xaxis: axisStyle({ title: "time (s)", range: [0, end] }),
      yaxis: axisStyle({ title: "current (A)", exponentformat: "none" }),
      margin: { l: 60, r: 20, t: 24, b: 40 },
    }), PLOT_CONFIG);
  }

  // -------------------------------------------------------------- run control

  function jobRunning() { return setupState.job && setupState.job.state === "running"; }

  function updateButtons() {
    root().querySelector("#run-sim").disabled = !setupState.valid || jobRunning();
    root().querySelector("#stop-sim").disabled = !jobRunning() || setupState.job.stopping;
  }

  function renderJob(job) {
    setupState.job = job;
    const status = root().querySelector("#job-status");
    const log = root().querySelector("#job-log");
    if (job.state === "idle") { status.textContent = ""; log.hidden = true; updateButtons(); return; }

    const seconds = Math.round(job.elapsed);
    if (job.state === "running") {
      status.textContent = job.stopping ? `Stopping… (${seconds} s)` : `Running… ${seconds} s${job.run_id ? "" : " (starting FEMM)"}`;
    } else {
      const outcome = (job.log.slice().reverse().find((l) => l.startsWith("finished: ")) || "").slice(10);
      status.textContent =
        job.returncode === 2 ? "Not started: invalid settings" :
        outcome && outcome !== "complete" ? `Ended (${outcome}) after ${seconds} s` :
        job.returncode === 0 ? `Finished in ${seconds} s` :
        `Failed (exit code ${job.returncode}), see the log`;
    }
    log.hidden = false;
    log.textContent = job.log.join("\n");
    log.scrollTop = log.scrollHeight;
    updateButtons();
  }

  async function openRun(runId) {
    await loadRunList(false);
    if (!state.runs.some((r) => r.id === runId)) return false;       // the run folder appears after the first step
    $("#run").value = runId;
    await loadRun(runId);
    setupState.openedRun = runId;
    updateButtons();
    return true;
  }

  async function pollJob() {
    let job;
    try { job = await getJSON("/api/simulations/current"); } catch (_) { return; }
    renderJob(job);
    if (job.run_id && setupState.openedRun !== job.run_id) await openRun(job.run_id);
    if (job.state !== "running") {
      clearInterval(setupState.pollTimer);
      setupState.pollTimer = null;
      await loadRunList();                                           // refresh the status/step counts in the list
    }
  }

  function startPolling() {
    if (setupState.pollTimer) return;
    setupState.pollTimer = setInterval(pollJob, JOB_POLL_MS);
    pollJob();
  }

  async function runSimulation() {
    const button = root().querySelector("#run-sim");
    button.disabled = true;
    const { status, data } = await postJSON("/api/simulations", readForm());
    if (status === 202) {
      setupState.openedRun = null;
      renderJob(data.job);
      startPolling();
      return;
    }
    const messages = status === 422 && data.errors
      ? data.errors.map((e) => ({ level: "error", text: `${humanize(e.field)}: ${e.message}` }))
      : [{ level: "error", text: (data && data.error) || `Could not start (HTTP ${status})` }];
    showMessages(messages);
    if (status === 409) startPolling();                               // someone else's run is active: show it
    updateButtons();
  }

  // -------------------------------------------------------------------- init

  function wire() {
    const panel = root();
    panel.addEventListener("input", (e) => { if (e.target.matches("[data-path]")) scheduleValidate(); });
    panel.addEventListener("change", (e) => {
      if (e.target.matches('[data-path="payload.type"]')) applyPayloadType();
      if (e.target.matches("[data-path]")) scheduleValidate();
    });

    panel.querySelector("#add-coil").addEventListener("click", () => {
      const cfg = readForm();
      cfg.coils.push({ ...(cfg.coils[cfg.coils.length - 1] || NEW_COIL) });
      stash.push(null);
      rebuildCoils(cfg.coils, true);
      scheduleValidate();
    });
    panel.querySelector("#coil-table").addEventListener("click", (e) => {
      const action = e.target.dataset && e.target.dataset.action;
      if (!action) return;
      const index = e.target.closest("tr").sectionRowIndex;
      if (action === "schedule") {
        editing = editing === index ? -1 : index;
        renderEditor();
        return;
      }
      const cfg = readForm();
      if (action === "copy") { cfg.coils.splice(index + 1, 0, { ...cfg.coils[index] }); stash.splice(index + 1, 0, null); }
      else if (cfg.coils.length > 1) { cfg.coils.splice(index, 1); stash.splice(index, 1); }
      editing = -1;
      rebuildCoils(cfg.coils, true);
      scheduleValidate();
    });

    // Schedule editor. These handlers update the model first; the panel-level handler then re-validates.
    const editor = panel.querySelector("#schedule-editor");
    editor.addEventListener("input", (e) => {
      const el = e.target;
      if (el.dataset.sfield && schedules[editing]) schedules[editing].points[Number(el.dataset.spoint)][el.dataset.sfield] = readValue(el);
    });
    editor.addEventListener("change", (e) => {
      if (e.target.id === "sched-interp" && schedules[editing]) {
        schedules[editing].interp = e.target.value;
        refreshScheduleButtons();
        scheduleValidate();
      }
    });
    editor.querySelector("#sched-enabled").addEventListener("change", (e) => {
      if (editing < 0) return;
      if (e.target.checked) {
        const current = readForm().coils[editing].current;
        schedules[editing] = stash[editing] || { interp: "step", points: [{ t: 0, current: Number.isFinite(current) ? current : 0 }] };
        stash[editing] = null;
      } else {
        stash[editing] = schedules[editing];
        schedules[editing] = null;
      }
      renderEditor();
      scheduleValidate();
    });
    editor.querySelector("#sched-add").addEventListener("click", () => {
      const s = schedules[editing];
      if (!s) return;
      const last = s.points[s.points.length - 1];
      const t = last && Number.isFinite(last.t) ? Math.round((last.t + 0.05) * 1e6) / 1e6 : 0;
      s.points.push({ t, current: last && Number.isFinite(last.current) ? last.current : 0 });
      renderEditor();
      scheduleValidate();
    });
    editor.querySelector("#sched-table").addEventListener("click", (e) => {
      if (e.target.dataset.sact !== "remove" || !schedules[editing]) return;
      schedules[editing].points.splice(Number(e.target.dataset.spoint), 1);
      renderEditor();
      scheduleValidate();
    });
    editor.querySelector("#sched-close").addEventListener("click", () => { editing = -1; renderEditor(); });

    panel.querySelector("#run-sim").addEventListener("click", runSimulation);
    panel.querySelector("#stop-sim").addEventListener("click", async () => {
      panel.querySelector("#stop-sim").disabled = true;
      await postJSON("/api/simulations/stop");
      pollJob();
    });
    panel.querySelector("#load-run").addEventListener("click", () => {
      if (!state.meta) { showMessages([{ level: "warning", text: "Select a run in the header first." }]); return; }
      writeForm(configFromRun(state.meta));
      scheduleValidate();
    });
    panel.querySelector("#reset-setup").addEventListener("click", () => {
      writeForm(setupState.defaults);
      scheduleValidate();
    });
  }

  async function init() {
    let info;
    try { info = await getJSON("/api/setup"); }
    catch (error) { root().querySelector("#setup-hint").textContent = `unavailable: ${error.message}`; return; }
    setupState.defaults = info.defaults;
    setupState.library = info.library;

    const datalist = root().querySelector("#materials");
    for (const name of (info.library ? info.library.materials : [])) datalist.appendChild(new Option(name));

    let cfg = info.defaults;
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && Array.isArray(saved.coils) && saved.payload) cfg = saved;
    } catch (_) { /* ignore a corrupt saved value */ }
    writeForm(cfg);
    wire();
    validate();

    // A simulation may already be running (page reload, or started in another tab)
    try {
      const job = await getJSON("/api/simulations/current");
      renderJob(job);
      if (job.state === "running") { root().open = true; startPolling(); }
    } catch (_) { /* viewer still works without it */ }

    // Nothing recorded yet: start with the form open
    try { if (!(await getJSON("/api/runs")).length) root().open = true; } catch (_) { /* ignore */ }
    updateButtons();
  }

  init();
})();
