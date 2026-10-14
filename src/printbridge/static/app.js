"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const STATE_CLASS = {
  queued: "busy", slicing: "busy", sending: "busy",
  ready: "ok", sent: "ok", failed: "bad",
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.replace("/login");
    throw new Error("Signed out");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error */ }
    throw new Error(detail);
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

/* ---------- printer ---------- */

async function refreshPrinter() {
  let s;
  try { s = await api("/api/printer"); } catch (e) { s = { online: false, state: "Error", message: e.message, nozzle: {}, bed: {} }; }
  const pill = $("#printer-state");
  pill.textContent = s.state;
  const st = s.state.toLowerCase();
  pill.className = "pill " + (!s.online ? "bad" : st.includes("print") ? "busy" : st.includes("paus") ? "warn" : st.includes("error") ? "bad" : "ok");
  $("#t-nozzle").textContent = fmtTemp(s.nozzle || {});
  $("#t-bed").textContent = fmtTemp(s.bed || {});
  $("#printer-msg").textContent = s.message || "";

  const active = s.online && /print|paus/i.test(s.state) && s.file;
  $("#print-progress").hidden = !active;
  if (active) {
    const pct = s.progress ?? 0;
    $("#p-file").textContent = s.file;
    $("#p-pct").textContent = `${pct.toFixed(1)}%`;
    $("#p-bar").style.width = `${pct}%`;
    const left = s.remaining != null ? ` · ${fmtDuration(s.remaining)} left` : "";
    $("#p-time").textContent = `${fmtDuration(s.elapsed)} elapsed${left}`;
    const paused = /paus/i.test(s.state);
    $('[data-ctl="pause"]').disabled = paused;
    $('[data-ctl="resume"]').disabled = !paused;
  }
}

document.querySelectorAll("[data-ctl]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.ctl;
    if (action === "cancel" && !confirm("Cancel the current print?")) return;
    btn.disabled = true;
    try {
      await api("/api/printer/control", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
    } catch (e) { alert(`Could not ${action}: ${e.message}`); }
    refreshPrinter();
  });
});

/* ---------- jobs ---------- */

let jobsCache = [];

function jobMeta(job) {
  const parts = [new Date(job.created * 1000).toLocaleString()];
  const o = job.options;
  if (o) parts.push(`${o.quality} · ${o.material.toUpperCase()} · ${o.infill}% infill`);
  const st = job.stats;
  if (st) {
    if (st.print_time) parts.push(`⏱ ${st.print_time}`);
    if (st.filament_g) parts.push(`${st.filament_g.toFixed(1)} g`);
    else if (st.filament_mm) parts.push(`${(st.filament_mm / 1000).toFixed(2)} m`);
    if (st.layers) parts.push(`${st.layers} layers`);
  }
  if (job.sent_at) parts.push(job.started_print ? "printing started" : "uploaded to host");
  return parts.join(" · ");
}

function renderJobs(jobs) {
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
    pill.classList.add(STATE_CLASS[job.state] || "");
    $(".job-meta", li).textContent = jobMeta(job);
    $(".job-error", li).textContent = job.error || "";

    const hasGcode = job.state === "ready" || job.state === "sent";
    const dl = $('[data-act="download"]', li);
    if (hasGcode) dl.href = `/api/jobs/${job.id}/gcode`; else dl.remove();
    $('[data-act="upload"]', li).disabled = !hasGcode;
    $('[data-act="print"]', li).disabled = !hasGcode;
    const busy = ["queued", "slicing", "sending"].includes(job.state);
    $('[data-act="delete"]', li).disabled = busy;
    if (!job.options || busy) $('[data-act="reslice"]', li).remove();
    if (!job.options) $('[data-act="view"]', li).remove();
    list.append(li);
  }
}

async function refreshJobs() {
  try { renderJobs(await api("/api/jobs")); } catch { /* keep the last list on transient errors */ }
}

$("#jobs").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".job").dataset.id;
  const job = jobsCache.find((j) => j.id === id);
  const act = btn.dataset.act;
  if (act === "view") {
    window.PrintBridgeViewer?.open(job);
    return;
  }
  try {
    if (act === "reslice") {
      await api(`/api/jobs/${id}/reslice`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(readOptions()),
      });
    } else if (act === "delete") {
      if (!confirm(`Delete ${job.name}?`)) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
    } else {
      const start = act === "print";
      if (start && !confirm(`Send ${job.name} to the printer and start printing now?\nMake sure the bed is clear.`)) return;
      await api(`/api/jobs/${id}/send`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start }),
      });
    }
  } catch (e) { alert(e.message); }
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

$("#infill").addEventListener("input", (e) => { $("#infill-out").textContent = `${e.target.value}%`; });

function readOptions() {
  const fd = new FormData(form);
  const opt = {
    quality: fd.get("quality"),
    material: fd.get("material"),
    infill: Number(fd.get("infill")),
    supports: fd.get("supports"),
    brim: Number(fd.get("brim") || 0),
    arrange: fd.has("arrange"),
    probe_bed: fd.has("probe_bed"),
  };
  for (const k of ["nozzle_temp", "bed_temp"]) {
    const v = fd.get(k);
    if (v !== "" && v != null) opt[k] = Number(v);
  }
  return opt;
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
    await api("/api/jobs", { method: "POST", body });
    fileInput.value = "";
    showFile();
    refreshJobs();
  } catch (err) {
    $("#upload-err").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload & slice";
  }
});

/* ---------- settings ---------- */

const dlg = $("#settings");
const sform = $("#settings-form");

$("#open-settings").addEventListener("click", async () => {
  const s = await api("/api/settings");
  sform.kind.value = s.printer.kind;
  sform.url.value = s.printer.url;
  sform.api_key.value = s.printer.api_key;
  sform.verify_tls.checked = s.printer.verify_tls;
  sform.slicer_path.value = s.slicer_path;
  sform.slice_timeout.value = s.slice_timeout;
  $("#settings-err").textContent = "";
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

sform.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    printer: {
      kind: sform.kind.value,
      url: sform.url.value.trim(),
      api_key: sform.api_key.value.trim(),
      verify_tls: sform.verify_tls.checked,
    },
    slicer_path: sform.slicer_path.value.trim(),
    slice_timeout: Number(sform.slice_timeout.value) || 900,
  };
  try {
    await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    dlg.close();
    loadProfiles();
    refreshPrinter();
  } catch (err) {
    $("#settings-err").textContent = err.message;
  }
});

/* ---------- boot ---------- */

async function loadProfiles() {
  const p = await api("/api/profiles");
  const fill = (sel, entries, def) => {
    const el = $(sel);
    const current = el.value || def;
    el.replaceChildren(...Object.entries(entries).map(([v, label]) => new Option(label, v)));
    el.value = current;
  };
  fill("#quality", p.qualities, p.defaults.quality);
  fill("#material", p.materials, p.defaults.material);
  $("#slicer-warning").textContent = p.slicer_found ? "" : "⚠ PrusaSlicer not found — set its path in Settings";
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

loadProfiles();
refreshPrinter();
refreshJobs();
setInterval(refreshPrinter, 5000);
setInterval(() => {
  if (!document.hidden) refreshJobs();
}, 2000);
