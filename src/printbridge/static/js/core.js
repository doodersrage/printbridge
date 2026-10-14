"use strict";
// Shared helpers: DOM shortcuts, the API client and formatting.
// Classic scripts (not modules): they share top-level names and load in order.

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
