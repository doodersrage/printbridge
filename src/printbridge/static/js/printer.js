"use strict";
// Printer card: status, controls, spools, webcam and printer storage.
// Classic scripts (not modules): they share top-level names and load in order.

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
