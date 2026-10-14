"use strict";
// Start-up: load data and begin polling. Loaded last.
// Classic scripts (not modules): they share top-level names and load in order.

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
