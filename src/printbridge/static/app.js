"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const STATE_CLASS = {
  queued: "busy", slicing: "busy", sending: "busy", printing: "busy",
  ready: "ok", sent: "ok", printed: "ok", failed: "bad", stopped: "warn",
};
const HAS_GCODE = ["ready", "sent", "printing", "printed", "stopped"];
const IDLE = ["ready", "failed", "sent", "printed", "stopped"];

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts = { ...opts, body: JSON.stringify(opts.json), headers: { "Content-Type": "application/json" } };
    delete opts.json;
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.replace("/login");
    throw new Error("Signed out");
  }
  if (!res.ok) {
    let detail = res.statusText;
    let code;
    try {
      const body = await res.json();
      if (body.detail && typeof body.detail === "object" && !Array.isArray(body.detail)) {
        code = body.detail.code;
        body.detail = body.detail.message;
      }
      detail = typeof body.detail === "string" ? body.detail
        : Array.isArray(body.detail) ? body.detail.map((d) => `${d.loc?.slice(-1)[0] ?? ""}: ${d.msg}`).join("; ")
        : JSON.stringify(body.detail);
    } catch { /* non-JSON error */ }
    throw Object.assign(new Error(detail), { code });
  }
  return res.status === 204 ? null : res.json();
}

function fmtDuration(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${sec % 60}s`;
}

function fmtTemp(t) {
  if (t.actual == null) return "—";
  const target = t.target ? ` / ${Math.round(t.target)}` : "";
  return `${t.actual.toFixed(0)}${target} °C`;
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1e3) return `${bytes} B`;
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(0)} KB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

/* ---------- printer ---------- */

let printer = { online: false, state: "", supports_light: false };
let lightOn = false;
let profilesInfo = null;
const isPrinting = () => printer.online && /print|paus/i.test(printer.state);

async function refreshPrinter() {
  let s;
  try { s = await api("/api/printer"); } catch (e) { s = { online: false, state: "Error", message: e.message, nozzle: {}, bed: {} }; }
  printer = s;
  const pill = $("#printer-state");
  pill.textContent = s.state;
  const st = s.state.toLowerCase();
  pill.className = "pill " + (!s.online ? "bad" : st.includes("print") ? "busy" : st.includes("paus") ? "warn" : st.includes("error") ? "bad" : "ok");
  $("#t-nozzle").textContent = fmtTemp(s.nozzle || {});
  $("#t-bed").textContent = fmtTemp(s.bed || {});
  $("#printer-msg").textContent = s.message || "";

  const active = isPrinting() && s.file;
  $("#print-progress").hidden = !active;
  if (active) {
    const pct = s.progress ?? 0;
    $("#p-file").textContent = s.file.split("/").pop();
    $("#p-pct").textContent = `${pct.toFixed(1)}%`;
    $("#p-bar").style.width = `${pct}%`;
    const left = s.remaining ? ` · ${fmtDuration(s.remaining)} left` : "";
    $("#p-time").textContent = `${fmtDuration(s.elapsed)} elapsed${left}`;
    const paused = /paus/i.test(s.state);
    $('[data-ctl="pause"]').disabled = paused;
    $('[data-ctl="resume"]').disabled = !paused;
  }

  renderSpoolLine(s.spool);
  $("#babystep").hidden = !isPrinting();
  const bt = s.babystep_total || 0;
  $("#babystep-total").textContent = `${bt > 0 ? "+" : bt < 0 ? "−" : "±"}${Math.abs(bt).toFixed(2)} mm`;
  syncWebcam(s.webcam);

  const blocked = !s.online || isPrinting();
  $$("[data-cmd]").forEach((b) => { b.disabled = blocked; });
  $("#gcode-form button").disabled = blocked;
  $("#light-toggle").hidden = !s.supports_light;
  if (s.light != null) setLight(s.light);
  $("#light-toggle").disabled = !s.online;
  updatePrintButtons();
}

document.querySelectorAll("[data-ctl]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.ctl;
    if (action === "cancel" && !confirm("Cancel the current print?")) return;
    btn.disabled = true;
    try {
      await api("/api/printer/control", { method: "POST", json: { action } });
    } catch (e) { alert(`Could not ${action}: ${e.message}`); }
    refreshPrinter();
  });
});

async function sendCommand(body, btn) {
  if (btn) btn.disabled = true;
  try {
    await api("/api/printer/command", { method: "POST", json: body });
    return true;
  } catch (e) {
    alert(e.message);
    return false;
  } finally {
    if (btn) btn.disabled = false;
    setTimeout(refreshPrinter, 800);
  }
}

$$("[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const command = btn.dataset.cmd;
    if (command === "preheat") {
      const t = profilesInfo?.preheat[$("#preheat-material").value] || { nozzle: 200, bed: 60 };
      sendCommand({ command, nozzle: t.nozzle, bed: t.bed }, btn);
    } else {
      sendCommand({ command }, btn);
    }
  });
});

$$("#babystep [data-z]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/printer/command", { method: "POST", json: { command: "babystep", z: Number(btn.dataset.z) } });
      const t = r.babystep_total;
      $("#babystep-total").textContent = `${t > 0 ? "+" : t < 0 ? "−" : "±"}${Math.abs(t).toFixed(2)} mm`;
    } catch (e) { alert(e.message); }
    btn.disabled = false;
  });
});

/* spools */
function renderSpoolLine(spool) {
  const bar = $("#spool-bar");
  if (!spool) {
    $("#spool-text").textContent = "Not tracked";
    bar.hidden = true;
    return;
  }
  const pct = Math.max(0, Math.min(100, (spool.remaining_g / spool.total_g) * 100));
  $("#spool-text").textContent = `${spool.name} · ${spool.material.toUpperCase()} · ${Math.round(spool.remaining_g)} g left`;
  bar.hidden = false;
  bar.firstElementChild.style.width = `${pct}%`;
  bar.classList.toggle("low", spool.remaining_g < 50);
}

const spdlg = $("#spools");

async function loadSpools() {
  const list = await api("/api/spools");
  const ul = $("#spool-list");
  ul.replaceChildren();
  if (!list.length) ul.append(Object.assign(document.createElement("li"), { className: "muted small", textContent: "No spools yet." }));
  for (const sp of list) {
    const li = document.createElement("li");
    li.className = "spool-item" + (sp.loaded ? " loaded" : "");
    const main = document.createElement("div");
    main.className = "spool-main";
    const title = Object.assign(document.createElement("strong"), { textContent: sp.name });
    if (sp.loaded) title.append(Object.assign(document.createElement("span"), { className: "pill ok", textContent: "loaded" }));
    const bar = Object.assign(document.createElement("div"), { className: "bar spool-bar" + (sp.remaining_g < 50 ? " low" : "") });
    bar.append(Object.assign(document.createElement("div"), { style: `width:${Math.min(100, (sp.remaining_g / sp.total_g) * 100)}%` }));
    main.append(title, Object.assign(document.createElement("span"), {
      className: "muted small", textContent: `${sp.material.toUpperCase()} · ${Math.round(sp.remaining_g)} of ${Math.round(sp.total_g)} g left`,
    }), bar);
    const acts = document.createElement("div");
    acts.className = "row gap";
    const btn = (label, fn, cls = "ghost small-btn") => {
      const b = Object.assign(document.createElement("button"), { type: "button", className: cls, textContent: label });
      b.addEventListener("click", async () => { try { await fn(); } catch (e) { alert(e.message); } await loadSpools(); refreshPrinter(); });
      return b;
    };
    const body = (extra) => ({ name: sp.name, material: sp.material, total_g: sp.total_g, remaining_g: sp.remaining_g, loaded: sp.loaded, ...extra });
    if (!sp.loaded) acts.append(btn("Load", () => api(`/api/spools/${sp.id}`, { method: "PUT", json: body({ loaded: true }) })));
    acts.append(btn("Set amount…", async () => {
      const v = prompt(`Grams of filament left on ${sp.name}:`, Math.round(sp.remaining_g));
      if (v === null || v.trim() === "") return;
      await api(`/api/spools/${sp.id}`, { method: "PUT", json: body({ remaining_g: Number(v) }) });
    }));
    acts.append(btn("Delete", async () => {
      if (confirm(`Delete spool ${sp.name}?`)) await api(`/api/spools/${sp.id}`, { method: "DELETE" });
    }, "ghost small-btn danger"));
    li.append(main, acts);
    ul.append(li);
  }
}

$("#open-spools").addEventListener("click", () => {
  const sel = $("#spool-material");
  sel.replaceChildren(...Object.entries(profilesInfo?.materials ?? { pla: "PLA" }).map(([v, l]) => new Option(l.split(" —")[0], v)));
  sel.value = $("#material").value || "pla";
  $("#spool-err").textContent = "";
  spdlg.showModal();
  loadSpools();
});
$("#spools-close").addEventListener("click", () => spdlg.close());
$("#spool-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/spools", { method: "POST", json: {
      name: f.name.value.trim(), material: f.material.value, total_g: Number(f.total_g.value),
      remaining_g: f.remaining_g.value === "" ? null : Number(f.remaining_g.value), loaded: f.loaded.checked,
    } });
    f.name.value = "";
    f.remaining_g.value = "";
    await loadSpools();
    refreshPrinter();
  } catch (err) { $("#spool-err").textContent = err.message; }
});

/* starting a print, with the spool check */
async function startPrint(job) {
  if (!confirm(`Send ${job.name} to the printer and start printing now?\nMake sure the bed is clear.`)) return false;
  try {
    await api(`/api/jobs/${job.id}/send`, { method: "POST", json: { start: true } });
  } catch (e) {
    if (e.code !== "spool") throw e;
    if (!confirm(`${e.message}\n\nPrint anyway?`)) return false;
    await api(`/api/jobs/${job.id}/send`, { method: "POST", json: { start: true, ignore_spool: true } });
  }
  return true;
}

/* webcam */
let webcamPaused = false;
let webcamOn = false;
function syncWebcam(enabled) {
  const fig = $("#webcam");
  fig.hidden = !enabled;
  const img = $("#webcam-img");
  const want = enabled && !webcamPaused && !document.hidden;
  if (want && !webcamOn) {
    img.src = `/api/webcam?t=${Date.now()}`;
    webcamOn = true;
  } else if (!want && webcamOn) {
    img.removeAttribute("src");
    webcamOn = false;
  }
}
$("#webcam-img").addEventListener("error", () => {
  $("#webcam-status").textContent = "Webcam unavailable";
  webcamOn = false;
});
$("#webcam-img").addEventListener("load", () => { $("#webcam-status").textContent = "Webcam"; });
$("#webcam-toggle").addEventListener("click", (e) => {
  webcamPaused = !webcamPaused;
  e.currentTarget.textContent = webcamPaused ? "Resume" : "Pause";
  syncWebcam(printer.webcam);
});
document.addEventListener("visibilitychange", () => syncWebcam(printer.webcam));

function setLight(on) {
  lightOn = on;
  const btn = $("#light-toggle");
  btn.setAttribute("aria-pressed", String(on));
  btn.textContent = on ? "Light on" : "Light off";
}

$("#light-toggle").addEventListener("click", async (e) => {
  const next = !lightOn;
  if (await sendCommand({ command: next ? "light_on" : "light_off" }, e.currentTarget)) setLight(next);
});

$("#gcode-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#gcode-input");
  if (!input.value.trim()) return;
  if (await sendCommand({ command: "gcode", gcode: input.value }, $("#gcode-form button"))) input.value = "";
});

/* ---------- printer storage ---------- */

const filesDlg = $("#files");
let remoteFiles = [];

async function loadFiles() {
  $("#files-summary").textContent = "Loading…";
  $("#files-err").textContent = "";
  $("#files-body").replaceChildren();
  try {
    remoteFiles = await api("/api/printer/files");
  } catch (e) {
    $("#files-summary").textContent = "";
    $("#files-err").textContent = e.message;
    return;
  }
  const total = remoteFiles.reduce((n, f) => n + (f.size || 0), 0);
  const mine = remoteFiles.filter((f) => f.job_id).length;
  $("#files-summary").textContent = `${remoteFiles.length} files · ${fmtSize(total)} · ${mine} uploaded by PrintBridge. `
    + "Dates come from the print host's clock, which on the Wi-Fi Box is often wrong.";
  for (const f of remoteFiles) {
    const tr = document.createElement("tr");
    const cb = Object.assign(document.createElement("input"), { type: "checkbox", value: f.name, disabled: f.printing });
    cb.setAttribute("aria-label", `Select ${f.name}`);
    const name = document.createElement("td");
    name.append(f.name);
    if (f.job_id) name.append(Object.assign(document.createElement("span"), { className: "pill ok", textContent: "PrintBridge" }));
    if (f.printing) name.append(Object.assign(document.createElement("span"), { className: "pill busy", textContent: "printing" }));
    const td = (text, cls) => Object.assign(document.createElement("td"), { textContent: text, className: cls || "" });
    const cbCell = document.createElement("td");
    cbCell.append(cb);
    tr.append(cbCell, name, td(fmtSize(f.size), "num"), td(f.modified ? new Date(f.modified * 1000).toLocaleDateString() : ""));
    $("#files-body").append(tr);
  }
  syncFileSelection();
}

function syncFileSelection() {
  const boxes = $$("#files-body input[type=checkbox]:not(:disabled)");
  const checked = boxes.filter((b) => b.checked);
  $("#files-delete").disabled = checked.length === 0;
  $("#files-delete").textContent = checked.length ? `Delete ${checked.length} selected` : "Delete selected";
  $("#files-all").checked = boxes.length > 0 && checked.length === boxes.length;
}

$("#open-files").addEventListener("click", () => { filesDlg.showModal(); loadFiles(); });
$("#files-close").addEventListener("click", () => filesDlg.close());
$("#files-body").addEventListener("change", syncFileSelection);
$("#files-all").addEventListener("change", (e) => {
  $$("#files-body input[type=checkbox]:not(:disabled)").forEach((b) => { b.checked = e.target.checked; });
  syncFileSelection();
});
$("#files-select-mine").addEventListener("click", () => {
  const mine = new Set(remoteFiles.filter((f) => f.job_id).map((f) => f.name));
  $$("#files-body input[type=checkbox]:not(:disabled)").forEach((b) => { b.checked = mine.has(b.value); });
  syncFileSelection();
});
$("#files-delete").addEventListener("click", async () => {
  const names = $$("#files-body input[type=checkbox]:checked").map((b) => b.value);
  if (!names.length || !confirm(`Permanently delete ${names.length} file(s) from the printer's storage?`)) return;
  try {
    const r = await api("/api/printer/files/delete", { method: "POST", json: { names } });
    await loadFiles();
    if (r.errors.length) $("#files-err").textContent = r.errors.join("\n");
  } catch (e) { $("#files-err").textContent = e.message; }
});

/* ---------- jobs ---------- */

let jobsCache = [];
let jobsSnapshot = "";

function jobMeta(job) {
  const parts = [new Date(job.created * 1000).toLocaleString()];
  if (job.size) parts.push(`📐 ${job.size.map((v) => v.toFixed(1)).join(" × ")} mm`);
  const o = job.options;
  if (o) {
    let s = `${profilesInfo?.qualities[o.quality]?.split(" —")[0] ?? o.quality} · ${profilesInfo?.materials[o.material]?.split(" —")[0] ?? o.material} · ${o.infill}% infill`;
    if (o.scale !== 100) s += ` · ${o.scale}%`;
    if (o.rotate) s += ` · ${o.rotate}°`;
    if (o.copies > 1) s += ` · ×${o.copies}`;
    parts.push(s);
  }
  const st = job.stats;
  if (st) {
    if (st.print_time) parts.push(`⏱ ${st.print_time}`);
    if (st.filament_g) parts.push(`${st.filament_g.toFixed(1)} g`);
    else if (st.filament_mm) parts.push(`${(st.filament_mm / 1000).toFixed(2)} m`);
    if (st.layers) parts.push(`${st.layers} layers`);
  }
  if (job.state === "printed" && job.finished_at) parts.push(`printed ${new Date(job.finished_at * 1000).toLocaleString()}`);
  else if (job.state === "sent") parts.push(job.started_print ? "print started" : "uploaded to printer");
  return parts.join(" · ");
}

function printTitle() {
  if (!printer.online) return "Printer is offline";
  if (isPrinting()) return "Printer is busy — wait for the current print to finish";
  return "Send to the printer and start printing";
}

function updatePrintButtons() {
  const blocked = !printer.online || isPrinting();
  $$('#jobs [data-act="print"]').forEach((b) => {
    const job = jobsCache.find((j) => j.id === b.closest(".job").dataset.id);
    b.disabled = blocked || !job || !HAS_GCODE.includes(job.state) || job.state === "printing";
    b.title = printTitle();
  });
}

const selected = new Set();

function renderJobs(jobs) {
  const snap = JSON.stringify(jobs);
  // Don't rebuild the list while someone is using a select inside it.
  const focus = document.activeElement?.classList;
  if (snap === jobsSnapshot || focus?.contains("job-plate") || focus?.contains("cal-pick")) return;
  jobsSnapshot = snap;
  jobsCache = jobs;
  const list = $("#jobs");
  list.replaceChildren();
  $("#jobs-empty").hidden = jobs.length > 0;
  const tpl = $("#job-tpl");
  for (const job of jobs) {
    const li = tpl.content.firstElementChild.cloneNode(true);
    li.dataset.id = job.id;
    $(".job-name", li).textContent = job.name;
    const pill = $(".job-state", li);
    pill.textContent = job.state;
    pill.classList.add(STATE_CLASS[job.state] || "x");
    $(".job-meta", li).textContent = jobMeta(job);
    $(".job-error", li).textContent = job.error || "";

    const info = job.source_info;
    if (info?.warnings?.length && job.options) {
      const warn = $(".job-warn", li);
      warn.hidden = false;
      const usingOriginal = job.options.original_size && info.scale;
      $(".warn-text", warn).textContent = usingOriginal
        ? `${info.warnings.filter((w) => !w.startsWith("This file scales")).join(" ")} Printing at the model's original size (the file's ${Math.round(info.scale * 100)}% scale is ignored).`.trim()
        : `⚠ ${info.warnings.join(" ")}`;
      const btn = $('[data-act="orig-size"]', warn);
      if (info.scale && IDLE.includes(job.state)) {
        btn.hidden = false;
        btn.textContent = usingOriginal ? `Use the file's ${Math.round(info.scale * 100)}%` : "Use original size";
      }
    }

    const sel = $(".job-select", li);
    const combinable = !!job.options && IDLE.includes(job.state) && !job.calibration;
    sel.disabled = !combinable;
    sel.checked = combinable && selected.has(job.id);
    if (!combinable) selected.delete(job.id);

    const img = $(".thumb img", li);
    if (job.thumbnail) {
      img.src = `/api/jobs/${job.id}/thumbnail.png?v=${job.stats?.print_seconds ?? ""}${job.options?.scale ?? ""}`;
      img.hidden = false;
    }

    const cal = job.calibration;
    if (cal && cal.bands.length && HAS_GCODE.includes(job.state)) {
      const box = $(".cal-result", li);
      box.hidden = false;
      const unit = cal.kind === "temperature" ? " °C" : " mm";
      $(".cal-bands", li).textContent = `Bands from the bottom, ${cal.bands[1].z_from - cal.bands[0].z_from} mm each: `
        + cal.bands.map((b) => `${b.value}${unit}`).join(" → ");
      const pick = $(".cal-pick", li);
      cal.bands.forEach((b, i) => pick.append(new Option(`${i + 1}: ${b.value}${unit} (${b.z_from}–${b.z_to} mm)`, b.value)));
    } else if (cal && cal.kind === "dimension" && HAS_GCODE.includes(job.state)) {
      const box = $(".cal-result", li);
      box.hidden = false;
      box.replaceChildren(Object.assign(document.createElement("span"), {
        className: "muted",
        textContent: "Should measure 20.0 mm on every side (the bump is extra, on the X side). Within ±0.2 mm is normal for this printer.",
      }));
    } else if (cal && cal.kind === "first_layer" && HAS_GCODE.includes(job.state)) {
      const box = $(".cal-result", li);
      box.hidden = false;
      box.replaceChildren(Object.assign(document.createElement("span"), {
        className: "muted",
        textContent: "While it prints, use Live Z on the Printer card until the squares are smooth with no gaps between lines, then enter that offset on the printer's screen to keep it.",
      }));
    }

    if (job.plates?.length) {
      const pick = $(".plate-pick", li);
      pick.hidden = false;
      const sel = $(".job-plate", li);
      for (const p of job.plates) {
        const objs = p.objects.length ? ` (${p.objects.slice(0, 3).join(", ")}${p.objects.length > 3 ? "…" : ""})` : "";
        sel.append(new Option(`${p.id}${p.name ? ` · ${p.name}` : ""}${objs}`, p.id));
      }
      sel.value = job.options?.plate ?? job.plates[0].id;
      sel.disabled = !IDLE.includes(job.state);
    }

    const hasGcode = HAS_GCODE.includes(job.state);
    const dl = $('[data-act="download"]', li);
    if (hasGcode) dl.href = `/api/jobs/${job.id}/gcode`; else dl.remove();
    $('[data-act="upload"]', li).disabled = !hasGcode || job.state === "printing";
    $('[data-act="delete"]', li).disabled = !IDLE.includes(job.state);
    if (!job.options || !IDLE.includes(job.state)) $('[data-act="reslice"]', li).remove();
    if (!job.options && !hasGcode) $('[data-act="view"]', li).remove();
    list.append(li);
  }
  updatePrintButtons();
  syncCombine();
  queueThumbnails(jobs);
}

function syncCombine() {
  const n = selected.size;
  const btn = $("#combine-btn");
  btn.disabled = n < 2;
  btn.textContent = n >= 2 ? `Combine ${n} selected` : "Combine selected";
}

/* thumbnails are rendered in the browser from each job's 3D preview */
const thumbQueue = new Set();
let thumbBusy = false;
function queueThumbnails(jobs) {
  for (const j of jobs) {
    if (j.options && !j.thumbnail && HAS_GCODE.includes(j.state)) thumbQueue.add(j.id);
  }
  drainThumbnails();
}
async function drainThumbnails() {
  if (thumbBusy || !window.PrintBridgeViewer?.thumbnail) return;
  const id = thumbQueue.values().next().value;
  if (!id) return;
  thumbQueue.delete(id);
  thumbBusy = true;
  try {
    const job = jobsCache.find((j) => j.id === id);
    if (job && !job.thumbnail) {
      const blob = await window.PrintBridgeViewer.thumbnail(job);
      if (blob) {
        await fetch(`/api/jobs/${id}/thumbnail`, { method: "PUT", body: blob, headers: { "Content-Type": "image/png" } });
        jobsSnapshot = "";
      }
    }
  } catch { /* a missing thumbnail is not worth bothering anyone about */ }
  thumbBusy = false;
  if (thumbQueue.size) setTimeout(drainThumbnails, 200);
  else refreshJobs();
}
window.addEventListener("printbridge-viewer-ready", () => queueThumbnails(jobsCache));
window.addEventListener("printbridge-jobs-changed", () => { jobsSnapshot = ""; refreshJobs(); });

$("#combine-btn").addEventListener("click", async () => {
  const ids = [...selected];
  const name = prompt(`Name for the combined plate of ${ids.length} models:`, "combined");
  if (!name?.trim()) return;
  try {
    await api("/api/jobs/combine", { method: "POST", json: { job_ids: ids, name: name.trim(), options: readOptions() } });
    selected.clear();
  } catch (e) { alert(e.message); }
  jobsSnapshot = "";
  refreshJobs();
});

async function refreshJobs() {
  try { renderJobs(await api("/api/jobs")); } catch { /* keep the last list on transient errors */ }
}

$("#jobs").addEventListener("change", async (ev) => {
  if (ev.target.classList.contains("job-select")) {
    const id = ev.target.closest(".job").dataset.id;
    if (ev.target.checked) selected.add(id); else selected.delete(id);
    syncCombine();
    return;
  }
  if (!ev.target.classList.contains("job-plate")) return;
  const job = jobsCache.find((j) => j.id === ev.target.closest(".job").dataset.id);
  ev.target.blur();
  try {
    await api(`/api/jobs/${job.id}/reslice`, { method: "POST", json: { ...job.options, plate: Number(ev.target.value) } });
  } catch (e) { alert(e.message); }
  jobsSnapshot = "";
  refreshJobs();
});

$("#jobs").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".job").dataset.id;
  const job = jobsCache.find((j) => j.id === id);
  const act = btn.dataset.act;
  if (act === "view") {
    window.PrintBridgeViewer?.open(job, job.options ? "model" : "toolpaths");
    return;
  }
  if (act === "orig-size") {
    try {
      await api(`/api/jobs/${id}/reslice`, { method: "POST", json: { ...job.options, original_size: !job.options.original_size } });
    } catch (e) { alert(e.message); }
    jobsSnapshot = "";
    refreshJobs();
    return;
  }
  if (act === "cal-save") {
    const value = Number($(".cal-pick", btn.closest(".job")).value);
    try {
      const r = await api(`/api/jobs/${id}/calibration/apply`, { method: "POST", json: { value } });
      alert(`Saved to the ${r.material.toUpperCase()} profile: ${Object.entries(r.saved).map(([k, v]) => `${k} = ${v}`).join(", ")}`);
      loadProfiles();
    } catch (e) { alert(e.message); }
    return;
  }
  try {
    if (act === "reslice") {
      await api(`/api/jobs/${id}/reslice`, { method: "POST", json: readOptions() });
    } else if (act === "delete") {
      if (!confirm(`Delete ${job.name}?`)) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
    } else if (act === "print") {
      await startPrint(job);
    } else {
      await api(`/api/jobs/${id}/send`, { method: "POST", json: { start: false } });
    }
  } catch (e) { alert(e.message); }
  jobsSnapshot = "";
  refreshJobs();
});

/* ---------- upload ---------- */

const form = $("#upload-form");
const fileInput = $("#file");
const drop = $("#drop");

function showFile() {
  const f = fileInput.files[0];
  $("#drop-title").textContent = f ? f.name : "Drop a 3MF, STL or OBJ here";
}
fileInput.addEventListener("change", showFile);
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; showFile(); }
});

$("#import-btn").addEventListener("click", async () => {
  const input = $("#import-url");
  const url = input.value.trim();
  if (!url) return;
  const btn = $("#import-btn");
  btn.disabled = true;
  btn.textContent = "Downloading…";
  $("#upload-err").textContent = "";
  try {
    await api("/api/jobs/import", { method: "POST", json: { url, options: readOptions() } });
    input.value = "";
    jobsSnapshot = "";
    refreshJobs();
  } catch (e) {
    $("#upload-err").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Import";
  }
});
$("#import-url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#import-btn").click(); } });

$("#infill").addEventListener("input", (e) => { $("#infill-out").textContent = `${e.target.value}%`; });

function readOptions() {
  const fd = new FormData(form);
  const num = (k, d) => (fd.get(k) === "" || fd.get(k) == null ? d : Number(fd.get(k)));
  const opt = {
    quality: fd.get("quality"),
    material: fd.get("material"),
    infill: Number(fd.get("infill")),
    supports: fd.get("supports"),
    brim: num("brim", 0),
    arrange: fd.has("arrange"),
    probe_bed: fd.has("probe_bed"),
    scale: num("scale", 100),
    rotate: num("rotate", 0),
    copies: num("copies", 1),
  };
  for (const k of ["nozzle_temp", "bed_temp"]) {
    if (fd.get(k) !== "" && fd.get(k) != null) opt[k] = Number(fd.get(k));
  }
  return opt;
}

function applyOptions(o) {
  const set = (name, v) => { if (form.elements[name]) form.elements[name].value = v ?? ""; };
  for (const k of ["quality", "material", "infill", "supports", "brim", "scale", "rotate", "copies", "nozzle_temp", "bed_temp"]) set(k, o[k]);
  form.elements.arrange.checked = !!o.arrange;
  form.elements.probe_bed.checked = !!o.probe_bed;
  $("#infill-out").textContent = `${o.infill}%`;
}

function uploadWithProgress(body) {
  // fetch() cannot report upload progress, so use XHR here.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const bar = $("#upload-bar");
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.hidden = false;
      bar.firstElementChild.style.width = `${pct}%`;
      $("#upload-btn").textContent = pct < 100 ? `Uploading ${pct}%` : "Processing…";
    };
    xhr.onload = () => {
      bar.hidden = true;
      if (xhr.status === 401) { location.replace("/login"); return reject(new Error("Signed out")); }
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
      const d = data?.detail;
      reject(new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => { bar.hidden = true; reject(new Error("Upload failed: network error")); };
    xhr.send(body);
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;
  const btn = $("#upload-btn");
  btn.disabled = true;
  btn.textContent = "Uploading…";
  $("#upload-err").textContent = "";
  const body = new FormData();
  body.append("file", file);
  body.append("options", JSON.stringify(readOptions()));
  try {
    await uploadWithProgress(body);
    fileInput.value = "";
    showFile();
    jobsSnapshot = "";
    refreshJobs();
  } catch (err) {
    $("#upload-err").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload & slice";
  }
});

/* ---------- presets ---------- */

let presetsCache = {};

async function loadPresets(select) {
  presetsCache = await api("/api/presets");
  const sel = $("#preset");
  sel.replaceChildren(new Option("Preset…", ""), ...Object.keys(presetsCache).map((n) => new Option(n, n)));
  sel.value = select && presetsCache[select] ? select : "";
  $("#preset-delete").disabled = !sel.value;
}

$("#preset").addEventListener("change", (e) => {
  const p = presetsCache[e.target.value];
  if (p) applyOptions(p);
  $("#preset-delete").disabled = !e.target.value;
});
$("#preset-save").addEventListener("click", async () => {
  const name = prompt("Save the current settings as a preset named:", $("#preset").value || "");
  if (!name?.trim()) return;
  try {
    await api(`/api/presets/${encodeURIComponent(name.trim())}`, { method: "PUT", json: readOptions() });
    loadPresets(name.trim());
  } catch (e) { alert(e.message); }
});
$("#preset-delete").addEventListener("click", async () => {
  const name = $("#preset").value;
  if (!name || !confirm(`Delete preset "${name}"?`)) return;
  await api(`/api/presets/${encodeURIComponent(name)}`, { method: "DELETE" });
  loadPresets();
});

/* ---------- settings ---------- */

const dlg = $("#settings");
const sform = $("#settings-form");
const NOTIFY_FLAGS = ["on_start", "on_finish", "on_stop", "on_error"];
const EMAIL_FIELDS = ["smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_password", "email_from", "email_to"];
const MQTT_FIELDS = ["host", "port", "username", "password", "topic_prefix", "discovery_prefix"];

async function loadPrices() {
  const c = await api("/api/costs");
  const box = $("#price-fields");
  box.replaceChildren(...Object.entries(c.prices).map(([m, price]) => {
    const label = document.createElement("label");
    label.append(profilesInfo?.materials[m]?.split(" —")[0] ?? m);
    label.append(Object.assign(document.createElement("input"), { type: "number", min: 0, step: "any", value: price || "", placeholder: "0", name: `price_${m}` }));
    label.lastChild.dataset.material = m;
    return label;
  }));
}

async function loadMqttStatus() {
  const el = $("#mqtt-status");
  try {
    const m = await api("/api/mqtt/status");
    el.className = "small " + (m.connected ? "ok-text" : m.error ? "error" : "muted");
    el.textContent = !m.enabled ? "" : m.connected ? "Connected to the broker ✓" : m.error ? `Not connected: ${m.error}` : "Connecting…";
  } catch { el.textContent = ""; }
}

$("#open-settings").addEventListener("click", async () => {
  const s = await api("/api/settings");
  sform.kind.value = s.printer.kind;
  sform.url.value = s.printer.url;
  sform.api_key.value = s.printer.api_key;
  sform.verify_tls.checked = s.printer.verify_tls;
  sform.slicer_path.value = s.slicer_path;
  sform.slice_timeout.value = s.slice_timeout;
  sform.keep_days.value = s.keep_days || "";
  sform.ntfy_url.value = s.notify.ntfy_url;
  sform.ntfy_token.value = s.notify.ntfy_token;
  sform.webhook_url.value = s.notify.webhook_url;
  NOTIFY_FLAGS.forEach((k) => { sform[k].checked = s.notify[k]; });
  for (const k of EMAIL_FIELDS) sform[k].value = s.notify[k] ?? "";
  sform.mqtt_enabled.checked = s.mqtt.enabled;
  sform.mqtt_tls.checked = s.mqtt.tls;
  for (const k of MQTT_FIELDS) sform[`mqtt_${k}`].value = s.mqtt[k] ?? "";
  sform.webcam_url.value = s.webcam_url;
  sform.public_url.value = s.public_url || location.origin;
  sform.currency.value = s.currency;
  $("#restore-result").textContent = "";
  loadPrices();
  loadMqttStatus();
  $("#settings-err").textContent = "";
  $("#notify-result").textContent = "";
  syncHostFields();
  dlg.showModal();
});
$("#settings-cancel").addEventListener("click", () => dlg.close());

function syncHostFields() {
  const box = sform.kind.value === "creality_box";
  $("#box-hint").hidden = !box;
  sform.querySelectorAll(".http-only").forEach((el) => { el.hidden = box; });
  $("#host-url").placeholder = { creality_box: "192.168.1.50", octoprint: "http://octopi.local", moonraker: "http://mainsail.local" }[sform.kind.value];
}
$("#host-kind").addEventListener("change", syncHostFields);

async function saveSettings() {
  const body = {
    printer: {
      kind: sform.kind.value,
      url: sform.url.value.trim(),
      api_key: sform.api_key.value.trim(),
      verify_tls: sform.verify_tls.checked,
    },
    slicer_path: sform.slicer_path.value.trim(),
    slice_timeout: Number(sform.slice_timeout.value) || 900,
    keep_days: Number(sform.keep_days.value) || 0,
    notify: {
      ntfy_url: sform.ntfy_url.value.trim(),
      ntfy_token: sform.ntfy_token.value.trim(),
      webhook_url: sform.webhook_url.value.trim(),
      ...Object.fromEntries(NOTIFY_FLAGS.map((k) => [k, sform[k].checked])),
      ...Object.fromEntries(EMAIL_FIELDS.map((k) => [k, k === "smtp_port" ? Number(sform[k].value) || 587 : sform[k].value.trim()])),
    },
    mqtt: {
      enabled: sform.mqtt_enabled.checked,
      tls: sform.mqtt_tls.checked,
      ...Object.fromEntries(MQTT_FIELDS.map((k) => [k, k === "port" ? Number(sform[`mqtt_${k}`].value) || 1883 : sform[`mqtt_${k}`].value.trim()])),
    },
    webcam_url: sform.webcam_url.value.trim(),
    public_url: sform.public_url.value.trim(),
    currency: sform.currency.value.trim() || "$",
  };
  await api("/api/settings", { method: "PUT", json: body });
  const prices = Object.fromEntries($$("#price-fields input").map((i) => [i.dataset.material, Number(i.value) || 0]));
  await api("/api/costs", { method: "PUT", json: { currency: body.currency, prices } });
}

sform.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await saveSettings();
    dlg.close();
    loadProfiles();
    refreshPrinter();
  } catch (err) {
    $("#settings-err").textContent = err.message;
  }
});

$("#notify-test").addEventListener("click", async () => {
  const out = $("#notify-result");
  out.className = "small muted";
  out.textContent = "Sending…";
  try {
    await saveSettings();
    await api("/api/notify/test", { method: "POST" });
    out.className = "small ok-text";
    out.textContent = "Sent ✓";
  } catch (e) {
    out.className = "small error";
    out.textContent = e.message;
  }
});

$("#restore-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file || !confirm("Restore this backup? It replaces your settings, presets and profile edits.")) return;
  const out = $("#restore-result");
  const body = new FormData();
  body.append("file", file);
  try {
    const r = await api("/api/restore", { method: "POST", body });
    out.className = "small ok-text";
    out.textContent = `Restored ${r.restored.length} files ✓`;
    setTimeout(() => location.reload(), 1200);
  } catch (err) {
    out.className = "small error";
    out.textContent = err.message;
  }
});

/* ---------- history ---------- */

const hdlg = $("#history");

async function loadHistory() {
  const [h, c] = await Promise.all([api("/api/history"), api("/api/costs")]);
  const cur = c.currency;
  const stat = (label, value) => {
    const d = document.createElement("div");
    d.append(Object.assign(document.createElement("span"), { className: "label", textContent: label }),
             Object.assign(document.createElement("span"), { className: "value", textContent: value }));
    return d;
  };
  const t = (x) => [
    stat("Prints", `${x.prints}${x.success_rate != null ? ` · ${x.success_rate}% ok` : ""}`),
    stat("Print time", `${x.hours} h`),
    stat("Filament", x.filament_g >= 1000 ? `${(x.filament_g / 1000).toFixed(2)} kg` : `${x.filament_g} g`),
    stat("Cost", `${cur}${x.cost.toFixed(2)}`),
  ];
  const stats = $("#history-stats");
  stats.replaceChildren(
    Object.assign(document.createElement("h3"), { textContent: "This month" }), ...t(h.month),
    Object.assign(document.createElement("h3"), { textContent: "All time" }), ...t(h.all),
  );
  const body = $("#history-body");
  body.replaceChildren();
  $("#history-empty").hidden = h.records.length > 0;
  for (const r of h.records) {
    const tr = document.createElement("tr");
    const td = (text, cls = "") => Object.assign(document.createElement("td"), { textContent: text, className: cls });
    const pill = Object.assign(document.createElement("span"), {
      className: `pill ${r.result === "printed" ? "ok" : "warn"}`,
      textContent: r.result === "printed" ? "printed" : `stopped ${Math.round(r.progress ?? 0)}%`,
    });
    const res = document.createElement("td");
    res.append(pill);
    const act = document.createElement("td");
    const job = r.job_id && jobsCache.find((j) => j.id === r.job_id);
    if (job && HAS_GCODE.includes(job.state)) {
      const b = Object.assign(document.createElement("button"), { type: "button", className: "ghost small-btn", textContent: "Reprint" });
      b.addEventListener("click", async () => {
        try {
          if (await startPrint(job)) hdlg.close();
        } catch (e) { alert(e.message); }
      });
      act.append(b);
    }
    tr.append(td(r.name), res, td(fmtDuration(r.duration), "num"),
      td(r.filament_g ? `${r.filament_g} g` : "", "num"), td(r.cost ? `${cur}${r.cost.toFixed(2)}` : "", "num"),
      td(new Date(r.ended_at * 1000).toLocaleString()), act);
    body.append(tr);
  }
}

$("#open-history").addEventListener("click", () => { hdlg.showModal(); loadHistory(); });
$("#history-close").addEventListener("click", () => hdlg.close());

/* ---------- calibration ---------- */

const cdlg = $("#calibrate");
const cform = $("#calibrate-form");
const CAL_HINTS = {
  temperature: "°C. The bottom band prints at “From”. Each band is 8 mm tall; up to 12 bands.",
  retraction: "mm of retraction. The bottom band uses “From”. Each band is 5 mm tall; up to 12 bands.",
  first_layer: "Prints one layer only, at fixed positions: corners and centre of the bed.",
  dimension: "About 15 minutes and 4 g of filament. Measure it with calipers (or a ruler) once it has cooled.",
};

async function syncCalibration() {
  const kind = cform.kind.value;
  const ranged = kind === "temperature" || kind === "retraction";
  $$(".cal-range", cform).forEach((el) => { el.hidden = !ranged; });
  $("#cal-hint").textContent = CAL_HINTS[kind];
  $("#cal-err").textContent = "";
  if (ranged) {
    const d = await api(`/api/calibration/defaults?kind=${kind}&material=${cform.material.value}`);
    cform.start.value = d.start;
    cform.end.value = d.end;
    cform.step.value = d.step;
  }
}

$("#open-calibrate").addEventListener("click", async () => {
  const sel = $("#cal-material");
  sel.replaceChildren(...Object.entries(profilesInfo?.materials ?? { pla: "PLA" }).map(([v, l]) => new Option(l.split(" —")[0], v)));
  sel.value = $("#material").value || "pla";
  cdlg.showModal();
  syncCalibration();
});
$("#calibrate-close").addEventListener("click", () => cdlg.close());
cform.addEventListener("change", (e) => { if (e.target.name === "kind" || e.target.name === "material") syncCalibration(); });
cform.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    kind: cform.kind.value,
    material: cform.material.value,
    start: Number(cform.start.value),
    end: Number(cform.end.value),
    step: Number(cform.step.value),
  };
  try {
    await api("/api/calibration", { method: "POST", json: body });
    cdlg.close();
    jobsSnapshot = "";
    refreshJobs();
  } catch (err) { $("#cal-err").textContent = err.message; }
});

/* ---------- profile editor ---------- */

const pdlg = $("#profiles");
let currentProfile = null;
let profileDirty = false;

async function loadProfileList(select) {
  const files = await api("/api/profile-files");
  const nav = $("#profile-list");
  nav.replaceChildren();
  const groups = { printer: "Printer", quality: "Quality", material: "Material" };
  for (const [kind, title] of Object.entries(groups)) {
    nav.append(Object.assign(document.createElement("h3"), { textContent: title }));
    for (const f of files.filter((x) => x.kind === kind)) {
      const b = Object.assign(document.createElement("button"), { type: "button", className: "profile-item" });
      b.dataset.kind = f.kind;
      b.dataset.name = f.name;
      b.append(f.kind === "printer" ? "Ender-3 V3 SE" : f.label);
      if (f.overridden || !f.builtin) {
        b.append(Object.assign(document.createElement("span"), { className: "pill warn", textContent: f.builtin ? "edited" : "custom" }));
      }
      if (select && select.kind === f.kind && select.name === f.name) b.classList.add("active");
      nav.append(b);
    }
  }
}

async function openProfile(kind, name) {
  if (profileDirty && !confirm("Discard unsaved changes?")) return;
  const p = await api(`/api/profile-files/${kind}/${name}`);
  currentProfile = p;
  profileDirty = false;
  $$(".profile-item").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind && b.dataset.name === name));
  $("#profile-name").textContent = kind === "printer" ? "Ender-3 V3 SE (machine)" : `${p.label} · ${kind}/${name}.ini`;
  const badge = $("#profile-badge");
  badge.hidden = !(p.overridden || !p.builtin);
  badge.textContent = p.builtin ? "edited" : "custom";
  badge.className = "pill warn";
  const ta = $("#profile-text");
  ta.value = p.text;
  ta.disabled = false;
  $("#profile-save").disabled = true;
  $("#profile-err").textContent = "";
  const reset = $("#profile-reset");
  reset.hidden = !(p.overridden || !p.builtin);
  reset.textContent = p.builtin ? "Reset to default" : "Delete profile";
}

$("#open-profiles").addEventListener("click", async () => {
  await loadProfileList();
  pdlg.showModal();
});
$("#profiles-close").addEventListener("click", () => {
  if (profileDirty && !confirm("Discard unsaved changes?")) return;
  profileDirty = false;
  pdlg.close();
});
$("#profile-list").addEventListener("click", (e) => {
  const b = e.target.closest(".profile-item");
  if (b) openProfile(b.dataset.kind, b.dataset.name);
});
$("#profile-text").addEventListener("input", () => {
  profileDirty = true;
  $("#profile-save").disabled = false;
});
$("#profile-save").addEventListener("click", async () => {
  if (!currentProfile) return;
  const { kind, name } = currentProfile;
  try {
    await api(`/api/profile-files/${kind}/${name}`, { method: "PUT", json: { text: $("#profile-text").value } });
    profileDirty = false;
    await loadProfileList({ kind, name });
    await openProfile(kind, name);
    loadProfiles();
  } catch (e) { $("#profile-err").textContent = e.message; }
});
$("#profile-reset").addEventListener("click", async () => {
  if (!currentProfile) return;
  const { kind, name, builtin } = currentProfile;
  if (!confirm(builtin ? "Discard your changes and restore the built-in profile?" : `Delete the ${name} profile?`)) return;
  await api(`/api/profile-files/${kind}/${name}`, { method: "DELETE" });
  profileDirty = false;
  await loadProfileList(builtin ? { kind, name } : null);
  if (builtin) await openProfile(kind, name);
  else {
    currentProfile = null;
    $("#profile-text").value = "";
    $("#profile-text").disabled = true;
    $("#profile-name").textContent = "Select a profile";
    $("#profile-reset").hidden = $("#profile-badge").hidden = true;
  }
  loadProfiles();
});

async function newProfile(kind) {
  const name = prompt(`Name for the new ${kind} profile (lowercase letters, digits, - and _):`, "");
  if (!name) return;
  const base = currentProfile?.kind === kind ? currentProfile.text
    : (await api(`/api/profile-files/${kind}/${kind === "quality" ? "standard" : "pla"}`)).text;
  const text = base.replace(/^#.*$/m, `# ${name}`);
  try {
    await api(`/api/profile-files/${kind}/${name}`, { method: "PUT", json: { text } });
    await loadProfileList({ kind, name });
    await openProfile(kind, name);
    loadProfiles();
  } catch (e) { alert(e.message); }
}
$("#profile-new-quality").addEventListener("click", () => newProfile("quality"));
$("#profile-new-material").addEventListener("click", () => newProfile("material"));

/* ---------- boot ---------- */

async function loadProfiles() {
  const p = await api("/api/profiles");
  profilesInfo = p;
  const fill = (sel, entries, def) => {
    const el = $(sel);
    const current = el.value || def;
    el.replaceChildren(...Object.entries(entries).map(([v, label]) => new Option(label, v)));
    el.value = entries[current] ? current : def;
  };
  fill("#quality", p.qualities, p.defaults.quality);
  fill("#material", p.materials, p.defaults.material);
  const pre = $("#preheat-material");
  const cur = pre.value || "pla";
  pre.replaceChildren(...Object.entries(p.materials).map(([v, label]) => {
    const t = p.preheat[v];
    return new Option(`${label.split(" —")[0]} ${t.nozzle}/${t.bed} °C`, v);
  }));
  pre.value = p.materials[cur] ? cur : Object.keys(p.materials)[0];
  $("#slicer-warning").textContent = p.slicer_found ? "" : "⚠ PrusaSlicer not found — set its path in Settings";
  jobsSnapshot = "";
}

api("/api/auth/status").then((a) => {
  if (!a.enabled) return;
  const btn = $("#logout");
  btn.hidden = false;
  btn.title = `Signed in as ${a.username}`;
  btn.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    location.replace("/login");
  });
}).catch(() => {});

fetch("/healthz").then((r) => r.json()).then((h) => {
  $$("[data-version]").forEach((el) => { el.textContent = ` · v${h.version}`; });
}).catch(() => {});

loadProfiles().then(refreshJobs);
loadPresets();
refreshPrinter();
setInterval(refreshPrinter, 5000);
setInterval(() => {
  if (!document.hidden) refreshJobs();
}, 2000);
