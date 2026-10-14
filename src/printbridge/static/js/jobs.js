"use strict";
// Job list: rendering, actions, plates, calibration results, thumbnails and combining.
// Classic scripts (not modules): they share top-level names and load in order.

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
