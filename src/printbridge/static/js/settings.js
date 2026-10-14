"use strict";
// Settings dialog: print host, notifications, MQTT, webcam, prices and backup.
// Classic scripts (not modules): they share top-level names and load in order.

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
