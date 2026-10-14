"use strict";
// New print: upload, import from link, slicing options and presets.
// Classic scripts (not modules): they share top-level names and load in order.

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

/* find models: opens the site's own search in a new tab (no site allows apps to download directly) */
const FIND_SITES = {
  printables: "https://www.printables.com/search/models?q=",
  makerworld: "https://makerworld.com/en/search/models?keyword=",
  thingiverse: "https://www.thingiverse.com/search?type=things&q=",
  myminifactory: "https://www.myminifactory.com/search/?query=",
  cults: "https://cults3d.com/en/search?q=",
};
$("#find-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#find-q").value.trim();
  const site = e.submitter?.dataset.site || "printables";
  if (!q) { $("#find-q").focus(); return; }
  window.open(FIND_SITES[site] + encodeURIComponent(q), "_blank", "noopener");
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
