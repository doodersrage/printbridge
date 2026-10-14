// 3D preview of a job's model on the Ender-3 V3 SE bed (three.js, vendored).
import * as THREE from "./vendor/three.module.min.js";
import { STLLoader } from "./vendor/STLLoader.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const BED = 220;
const MAX_Z = 250;

const dlg = document.getElementById("viewer");
const canvas = document.getElementById("viewer-canvas");
const statusEl = document.getElementById("viewer-status");
const infoEl = document.getElementById("viewer-info");

let renderer, scene, camera, controls, bedGroup, mesh, frameId, resizeObs;
let requestToken = 0;

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function setup() {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
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

  resizeObs = new ResizeObserver(resize);
  resizeObs.observe(canvas.parentElement);
}

function buildBed() {
  if (bedGroup) scene.remove(bedGroup);
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

  // Small marker at the bed origin (front-left) so orientation is obvious.
  const origin = new THREE.Mesh(
    new THREE.CircleGeometry(3, 24),
    new THREE.MeshBasicMaterial({ color: cssVar("--accent") }),
  );
  origin.position.set(0, 0, 0.05);
  bedGroup.add(origin);

  scene.add(bedGroup);
  scene.background = new THREE.Color(cssVar("--viewer-bg") || cssVar("--bg"));
}

function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

function frame(target, size) {
  const dist = Math.max(size, 60) * 2.1;
  camera.position.set(target.x + dist * 0.55, target.y - dist * 1.05, target.z + dist * 0.75);
  controls.target.copy(target);
  controls.update();
}

function fitModel() {
  if (!mesh) return fitBed();
  const box = mesh.geometry.boundingBox.clone();
  // A model outside the build volume is only meaningful next to the bed.
  if (mesh.userData.offBed) box.union(new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(BED, BED, 0)));
  const size = box.getSize(new THREE.Vector3());
  frame(box.getCenter(new THREE.Vector3()), Math.max(size.x, size.y, size.z));
}

function fitBed() {
  frame(new THREE.Vector3(BED / 2, BED / 2, 20), BED * 0.95);
}

function loop() {
  frameId = requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, camera);
}

function clearModel() {
  if (!mesh) return;
  scene.remove(mesh);
  mesh.geometry.dispose();
  mesh.material.dispose();
  mesh = null;
}

function fmt(n) {
  return n.toFixed(1);
}

async function open(job) {
  if (!renderer) setup();
  buildBed();
  clearModel();
  const token = ++requestToken;
  document.getElementById("viewer-title").textContent = job.name;
  infoEl.textContent = "";
  infoEl.classList.remove("error");
  statusEl.textContent = "Loading model…";
  statusEl.hidden = false;
  dlg.showModal();
  resize();
  fitBed();
  cancelAnimationFrame(frameId);
  loop();

  try {
    const res = await fetch(`/api/jobs/${job.id}/preview.stl`);
    if (res.status === 401) return location.replace("/login");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Preview failed (${res.status})`);
    }
    const geometry = new STLLoader().parse(await res.arrayBuffer());
    if (token !== requestToken) return geometry.dispose();
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const size = box.getSize(new THREE.Vector3());
    const offBed = box.min.x < -0.5 || box.min.y < -0.5 || box.max.x > BED + 0.5 || box.max.y > BED + 0.5 || box.max.z > MAX_Z;

    mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: offBed ? cssVar("--bad") : cssVar("--accent"),
        roughness: 0.55,
        metalness: 0.05,
      }),
    );
    mesh.userData.offBed = offBed;
    scene.add(mesh);
    statusEl.hidden = true;

    const tris = geometry.attributes.position.count / 3;
    infoEl.textContent = `${fmt(size.x)} × ${fmt(size.y)} × ${fmt(size.z)} mm · ${tris.toLocaleString()} triangles`;
    if (offBed) {
      infoEl.textContent += " · extends outside the 220 × 220 × 250 mm build volume";
      infoEl.classList.add("error");
    }
    fitModel();
  } catch (e) {
    if (token !== requestToken) return;
    statusEl.textContent = e.message;
  }
}

dlg.addEventListener("close", () => {
  requestToken++;
  cancelAnimationFrame(frameId);
  clearModel();
});
document.getElementById("viewer-close").addEventListener("click", () => dlg.close());
document.getElementById("viewer-fit").addEventListener("click", fitModel);
document.getElementById("viewer-bed").addEventListener("click", fitBed);

window.PrintBridgeViewer = { open };
