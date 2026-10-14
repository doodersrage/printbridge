// 3D preview on the Ender-3 V3 SE bed: the model as it will be placed, and the
// sliced toolpaths layer by layer (three.js, vendored).
import * as THREE from "./vendor/three.module.min.js";
import { STLLoader } from "./vendor/STLLoader.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const BED = 220;
const MAX_Z = 250;
const HAS_GCODE = ["ready", "sent", "printing", "printed", "stopped"];

// Feature colours, close to PrusaSlicer's preview. Cura-style names map onto the same colours.
const FEATURE_COLORS = {
  "Perimeter": "#ffd84d",
  "External perimeter": "#ff8a3d",
  "Overhang perimeter": "#3a5bff",
  "Internal infill": "#c0392b",
  "Solid infill": "#a259d9",
  "Top solid infill": "#ff4d6d",
  "Ironing": "#ff9e80",
  "Bridge infill": "#4d8fd1",
  "Gap fill": "#e8e8e8",
  "Skirt/Brim": "#14a085",
  "Skirt": "#14a085",
  "Brim": "#14a085",
  "Support material": "#2ecc40",
  "Support material interface": "#1b8a2a",
  "Wipe tower": "#b3e3ab",
  "Custom": "#7f8c8d",
  "Extrusion": "#ff8a3d",
  "WALL-OUTER": "#ff8a3d",
  "WALL-INNER": "#ffd84d",
  "FILL": "#c0392b",
  "SKIN": "#a259d9",
  "SUPPORT": "#2ecc40",
  "SUPPORT-INTERFACE": "#1b8a2a",
  "SKIRT": "#14a085",
  "PRIME-TOWER": "#b3e3ab",
};
const FALLBACK = ["#e67e22", "#16a085", "#8e44ad", "#2980b9", "#d35400", "#27ae60"];

const $ = (id) => document.getElementById(id);
const dlg = $("viewer");
const canvas = $("viewer-canvas");
const statusEl = $("viewer-status");
const infoEl = $("viewer-info");
const legendEl = $("viewer-legend");
const layerBar = $("layer-bar");
const layerRange = $("layer-range");
const layerOut = $("layer-out");
const layerOnly = $("layer-only");
const resizeBar = $("resize-bar");

let renderer, scene, camera, controls, bedGroup, frameId;
let job = null;
let mode = "model";
let token = 0;
const views = { model: null, toolpaths: null }; // {object, info, bounds, ...}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function setup() {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(40, 1, 0.5, 5000);
  camera.up.set(0, 0, 1); // slicer coordinates are Z-up
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.screenSpacePanning = true;

  scene.add(new THREE.HemisphereLight(0xffffff, 0x555555, 1.8));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(260, -220, 400);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.5);
  fill.position.set(-200, 300, 150);
  scene.add(fill);

  new ResizeObserver(resize).observe(canvas.parentElement);
}

function buildBed() {
  if (bedGroup) {
    scene.remove(bedGroup);
    bedGroup.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); });
  }
  bedGroup = new THREE.Group();
  const plate = new THREE.Mesh(
    new THREE.PlaneGeometry(BED, BED),
    new THREE.MeshStandardMaterial({ color: cssVar("--bed") || "#2b2b2b", roughness: 0.9 }),
  );
  plate.position.set(BED / 2, BED / 2, -0.05);
  bedGroup.add(plate);

  const grid = new THREE.GridHelper(BED, 22, cssVar("--grid-major"), cssVar("--grid"));
  grid.rotation.x = Math.PI / 2;
  grid.position.set(BED / 2, BED / 2, 0);
  bedGroup.add(grid);

  const volume = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(BED, BED, MAX_Z)),
    new THREE.LineBasicMaterial({ color: cssVar("--grid"), transparent: true, opacity: 0.6 }),
  );
  volume.position.set(BED / 2, BED / 2, MAX_Z / 2);
  bedGroup.add(volume);

  const origin = new THREE.Mesh(new THREE.CircleGeometry(3, 24), new THREE.MeshBasicMaterial({ color: cssVar("--accent") }));
  origin.position.set(0, 0, 0.05);
  bedGroup.add(origin);

  scene.add(bedGroup);
  scene.background = new THREE.Color(cssVar("--viewer-bg") || cssVar("--bg"));
}

function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  if (!w || !h || !renderer) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

function frame(box) {
  const size = box.getSize(new THREE.Vector3());
  const target = box.getCenter(new THREE.Vector3());
  const dist = Math.max(size.x, size.y, size.z, 60) * 2.1;
  camera.position.set(target.x + dist * 0.55, target.y - dist * 1.05, target.z + dist * 0.75);
  controls.target.copy(target);
  controls.update();
}

const bedBox = () => new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(BED, BED, 40));

function fit() {
  const v = views[mode];
  frame(v?.bounds ? v.bounds.clone() : bedBox());
}

function loop() {
  frameId = requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, camera);
}

function disposeViews() {
  for (const key of Object.keys(views)) {
    const v = views[key];
    if (v?.object) {
      scene.remove(v.object);
      v.object.geometry.dispose();
      v.object.material.dispose();
    }
    views[key] = null;
  }
}

function setStatus(text) {
  statusEl.textContent = text || "";
  statusEl.hidden = !text;
}

function setInfo(text, isError = false) {
  infoEl.textContent = text || "";
  infoEl.classList.toggle("error", isError);
}

async function fetchOk(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    location.replace("/login");
    throw new Error("Signed out");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.arrayBuffer();
}

/* ---- model ---- */

async function loadModel(t) {
  const geometry = new STLLoader().parse(await fetchOk(`/api/jobs/${job.id}/preview.stl`));
  if (t !== token) return geometry.dispose();
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const size = box.getSize(new THREE.Vector3());
  const offBed = box.min.x < -0.5 || box.min.y < -0.5 || box.max.x > BED + 0.5 || box.max.y > BED + 0.5 || box.max.z > MAX_Z;
  const mesh = new THREE.Mesh(
    geometry,
    new THREE.MeshStandardMaterial({ color: offBed ? cssVar("--bad") : cssVar("--accent"), roughness: 0.55, metalness: 0.05 }),
  );
  const bounds = box.clone();
  // A model outside the build volume is only meaningful next to the bed.
  if (offBed) bounds.union(new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(BED, BED, 0)));
  const tris = geometry.attributes.position.count / 3;
  let info = `${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} mm · ${tris.toLocaleString()} triangles`;
  if (offBed) info += " · extends outside the 220 × 220 × 250 mm build volume";
  return { object: mesh, bounds, info, error: offBed };
}

/* ---- toolpaths ---- */

function parseToolpaths(buf) {
  const view = new DataView(buf);
  if (new TextDecoder().decode(new Uint8Array(buf, 0, 4)) !== "PBTP") throw new Error("Unrecognised preview data");
  const hlen = view.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, hlen)));
  const n = header.segments;
  const coords = new Float32Array(buf, 8 + hlen, n * 4);
  const types = new Uint8Array(buf, 8 + hlen + n * 16, n);
  return { header, coords, types };
}

function colorFor(name, i) {
  return new THREE.Color(FEATURE_COLORS[name] || FALLBACK[i % FALLBACK.length]);
}

async function loadToolpaths(t) {
  const { header, coords, types } = parseToolpaths(await fetchOk(`/api/jobs/${job.id}/toolpaths`));
  if (t !== token) return null;
  const n = header.segments;
  if (!n) throw new Error("No extrusion moves found in this G-code");

  const positions = new Float32Array(n * 6);
  const colors = new Uint8Array(n * 6);
  const palette = header.types.map((name, i) => colorFor(name, i));
  const rgb = palette.map((c) => [Math.round(c.r * 255), Math.round(c.g * 255), Math.round(c.b * 255)]);
  const layerStart = [];
  let seg = 0;
  for (const layer of header.layers) {
    layerStart.push(seg);
    for (; seg < layer.end; seg++) {
      const o = seg * 6, c = seg * 4;
      positions[o] = coords[c];
      positions[o + 1] = coords[c + 1];
      positions[o + 2] = layer.z;
      positions[o + 3] = coords[c + 2];
      positions[o + 4] = coords[c + 3];
      positions[o + 5] = layer.z;
      const [r, g, b] = rgb[types[seg]];
      colors[o] = colors[o + 3] = r;
      colors[o + 1] = colors[o + 4] = g;
      colors[o + 2] = colors[o + 5] = b;
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3, true));
  const lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true }));

  const used = [...new Set(types)].sort((a, b) => a - b);
  // Frame the part itself, not the purge line the start G-code draws at the bed's edge.
  const custom = header.types.indexOf("Custom");
  const bounds = new THREE.Box3();
  const p = new THREE.Vector3();
  for (let i = 0; i < n; i++) {
    if (types[i] === custom) continue;
    bounds.expandByPoint(p.set(positions[i * 6], positions[i * 6 + 1], positions[i * 6 + 2]));
    bounds.expandByPoint(p.set(positions[i * 6 + 3], positions[i * 6 + 4], positions[i * 6 + 5]));
  }
  if (bounds.isEmpty()) {
    const [x0, y0, x1, y1] = header.bounds;
    bounds.set(new THREE.Vector3(x0, y0, 0), new THREE.Vector3(x1, y1, header.layers.at(-1)?.z ?? 0));
  }
  bounds.min.z = 0;
  let info = `${header.layers.length.toLocaleString()} layers · ${n.toLocaleString()} moves`;
  if (header.truncated) info += " · preview truncated (very large file)";
  return { object: lines, bounds, info, layers: header.layers, layerStart, legend: used.map((i) => [header.types[i], palette[i]]) };
}

function applyLayer() {
  const v = views.toolpaths;
  if (!v) return;
  const idx = Number(layerRange.value) - 1;
  const end = v.layers[idx].end;
  const start = layerOnly.checked ? v.layerStart[idx] : 0;
  v.object.geometry.setDrawRange(start * 2, (end - start) * 2);
  layerOut.textContent = `${idx + 1} / ${v.layers.length} · Z ${v.layers[idx].z.toFixed(2)} mm`;
}
layerRange.addEventListener("input", applyLayer);
layerOnly.addEventListener("change", applyLayer);

function renderLegend(entries) {
  legendEl.replaceChildren(...entries.map(([name, color]) => {
    const s = document.createElement("span");
    const sw = document.createElement("i");
    sw.style.background = `#${color.getHexString()}`;
    s.append(sw, name);
    return s;
  }));
  legendEl.hidden = !entries.length;
}

/* ---- mode switching ---- */

async function show(next) {
  mode = next;
  const t = token;
  for (const [key, v] of Object.entries(views)) if (v?.object) v.object.visible = key === mode;
  document.querySelectorAll("#viewer .tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  layerBar.hidden = mode !== "toolpaths";
  resizeBar.hidden = true;
  legendEl.hidden = true;

  let v = views[mode];
  if (!v) {
    setInfo("");
    setStatus(mode === "model" ? "Loading model…" : "Reading G-code…");
    try {
      v = await (mode === "model" ? loadModel(t) : loadToolpaths(t));
    } catch (e) {
      if (t === token && mode === next) setStatus(e.message);
      return;
    }
    if (!v || t !== token) return;
    views[next] = v;
    scene.add(v.object);
    v.object.visible = mode === next;
    if (mode !== next) return;
  }
  setStatus("");
  setInfo(v.info, v.error);
  if (mode === "model") setupResize(v);
  if (mode === "toolpaths") {
    layerRange.max = String(v.layers.length);
    if (!v.touched) {
      layerRange.value = String(v.layers.length);
      layerOnly.checked = false;
      v.touched = true;
    }
    applyLayer();
    renderLegend(v.legend);
  }
  fit();
}

async function open(j, startMode = "model") {
  if (!renderer) setup();
  token++;
  disposeViews();
  buildBed();
  job = j;
  $("viewer-title").textContent = j.name;
  const canModel = !!j.options;
  const canPaths = HAS_GCODE.includes(j.state);
  $("tab-model").disabled = !canModel;
  $("tab-toolpaths").disabled = !canPaths;
  $("tab-toolpaths").title = canPaths ? "" : "Available once the job has been sliced";
  dlg.showModal();
  resize();
  frame(bedBox());
  cancelAnimationFrame(frameId);
  loop();
  show(startMode === "toolpaths" && canPaths ? "toolpaths" : canModel ? "model" : "toolpaths");
}

dlg.addEventListener("close", () => {
  token++;
  cancelAnimationFrame(frameId);
  disposeViews();
});
document.querySelectorAll("#viewer .tabs button").forEach((b) => b.addEventListener("click", () => {
  if (!b.disabled && b.dataset.mode !== mode) show(b.dataset.mode);
}));
$("viewer-close").addEventListener("click", () => dlg.close());
$("viewer-fit").addEventListener("click", fit);
$("viewer-bed").addEventListener("click", () => frame(bedBox()));

/* ---- resize by dimensions ---- */

let resizeBase = null; // current printed size, and the job's current scale %

function setupResize(v) {
  const IDLE = ["ready", "failed", "sent", "printed", "stopped"];
  if (!job?.options || !IDLE.includes(job.state) || job.calibration) return;
  const size = v.bounds && !v.error ? v.bounds.getSize(new THREE.Vector3()) : null;
  if (!size) return;
  resizeBase = { w: size.x, d: size.y, h: size.z, scale: job.options.scale };
  for (const k of ["w", "d", "h"]) resizeBar.elements[k].value = resizeBase[k].toFixed(1);
  resizeBar.querySelector("button").disabled = true;
  $("resize-note").textContent = job.options.copies > 1 ? "(size of all copies together)" : "";
  resizeBar.hidden = false;
}

resizeBar.addEventListener("input", (e) => {
  if (!resizeBase || !["w", "d", "h"].includes(e.target.name)) return;
  const factor = Number(e.target.value) / resizeBase[e.target.name];
  if (!(factor > 0)) return;
  for (const k of ["w", "d", "h"]) if (k !== e.target.name) resizeBar.elements[k].value = (resizeBase[k] * factor).toFixed(1);
  const pct = resizeBase.scale * factor;
  const btn = resizeBar.querySelector("button");
  btn.disabled = Math.abs(factor - 1) < 0.001 || pct < 10 || pct > 1000;
  $("resize-note").textContent = pct < 10 || pct > 1000 ? "Out of range (10–1000%)" : `Scale ${pct.toFixed(1)}%`;
});

resizeBar.addEventListener("submit", async (e) => {
  e.preventDefault();
  const factor = Number(resizeBar.elements.w.value) / resizeBase.w;
  const scale = Math.round(resizeBase.scale * factor * 100) / 100;
  const res = await fetch(`/api/jobs/${job.id}/reslice`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...job.options, scale }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    $("resize-note").textContent = body.detail || "Could not re-slice";
    return;
  }
  dlg.close();
  window.dispatchEvent(new Event("printbridge-jobs-changed"));
});

/* ---- thumbnails ---- */

let thumbRenderer = null;
const THUMB = 256;

async function thumbnail(j) {
  if (!thumbRenderer) {
    const c = document.createElement("canvas");
    c.width = c.height = THUMB;
    thumbRenderer = new THREE.WebGLRenderer({ canvas: c, antialias: true, alpha: true, preserveDrawingBuffer: true });
    thumbRenderer.setSize(THUMB, THUMB, false);
  }
  const res = await fetch(`/api/jobs/${j.id}/preview.stl`);
  if (!res.ok) return null;
  const geometry = new STLLoader().parse(await res.arrayBuffer());
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  const s = new THREE.Scene();
  s.add(new THREE.HemisphereLight(0xffffff, 0x666666, 1.9));
  const key = new THREE.DirectionalLight(0xffffff, 1.7);
  key.position.set(1, -1.3, 2);
  s.add(key);
  const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: "#f97316", roughness: 0.5, metalness: 0.05 }));
  s.add(mesh);

  const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 5000);
  cam.up.set(0, 0, 1);
  const radius = Math.max(size.length() / 2, 1);
  const dist = radius / Math.sin(THREE.MathUtils.degToRad(15)) * 1.05;
  const dir = new THREE.Vector3(0.55, -1.0, 0.75).normalize();
  cam.position.copy(center).addScaledVector(dir, dist);
  cam.lookAt(center);
  thumbRenderer.setClearColor(0x000000, 0);
  thumbRenderer.render(s, cam);
  const blob = await new Promise((r) => thumbRenderer.domElement.toBlob(r, "image/png"));
  geometry.dispose();
  mesh.material.dispose();
  return blob;
}

window.PrintBridgeViewer = { open, thumbnail };
window.dispatchEvent(new Event("printbridge-viewer-ready"));
