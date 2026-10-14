"use strict";
// History, calibration and the profile editor dialogs.
// Classic scripts (not modules): they share top-level names and load in order.

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
