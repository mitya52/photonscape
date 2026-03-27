const view3d = document.getElementById("view3d");
const view2d = document.getElementById("view2d");
const timing = document.getElementById("timing");
const loadingOverlay = document.getElementById("loadingOverlay");
const emptyState = document.getElementById("emptyState");
const fileInput = document.getElementById("fileInput");
const fileInfo = document.getElementById("fileInfo");
const downsampleSelect = document.getElementById("downsampleSelect");

function showTiming(ms) {
  timing.textContent = ms.toFixed(0) + "ms";
  timing.classList.add("visible");
}

let loadingTimer = null;

function showLoading(on) {
  if (on) {
    if (!loadingTimer) {
      loadingTimer = setTimeout(() => {
        loadingOverlay.classList.add("visible");
      }, 100);
    }
  } else {
    clearTimeout(loadingTimer);
    loadingTimer = null;
    loadingOverlay.classList.remove("visible");
  }
}

function hideEmptyState() {
  emptyState.classList.add("hidden");
  document.body.classList.remove("no-image");
}

function showEmptyState() {
  emptyState.classList.remove("hidden");
  document.body.classList.add("no-image");
}

// ── Build controls from server config ──

const sg = document.getElementById("stretchGroup");
STRETCH_NAMES.forEach((name, i) => {
  const label = document.createElement("label");
  label.innerHTML = `<input type="radio" name="stretch" value="${name}" ${i === 0 ? "checked" : ""}><span>${name}</span>`;
  sg.appendChild(label);
});

const zg = document.getElementById("zscaleGroup");
Z_SCALES.forEach(name => {
  const label = document.createElement("label");
  label.innerHTML = `<input type="radio" name="zscale" value="${name}" ${name === DEFAULT_Z ? "checked" : ""}><span>${name}</span>`;
  zg.appendChild(label);
});

DOWNSAMPLE_OPTIONS.forEach(val => {
  const opt = document.createElement("option");
  opt.value = val;
  opt.textContent = val === 0 ? "Original" : val + "px";
  downsampleSelect.appendChild(opt);
});

const gpuSection = document.getElementById("gpuSection");
const gpuToggle = document.getElementById("gpuToggle");
if (!HAS_GPU) {
  gpuSection.style.display = "none";
} else {
  gpuToggle.checked = true;
}

// ── Z Crop slider ──

const zCropLo = document.getElementById("zCropLo");
const zCropHi = document.getElementById("zCropHi");
const zCropFill = document.getElementById("zCropFill");
const zCropLoLabel = document.getElementById("zCropLoLabel");
const zCropHiLabel = document.getElementById("zCropHiLabel");
let zCropTimer = null;

function updateZCropFill() {
  const lo = parseInt(zCropLo.value, 10);
  const hi = parseInt(zCropHi.value, 10);
  const loP = lo / 10;
  const hiP = hi / 10;
  zCropFill.style.left = loP + "%";
  zCropFill.style.width = (hiP - loP) + "%";
  zCropLoLabel.textContent = loP.toFixed(0) + "%";
  zCropHiLabel.textContent = hiP.toFixed(0) + "%";
}

function onZCropChange() {
  let lo = parseInt(zCropLo.value, 10);
  let hi = parseInt(zCropHi.value, 10);
  if (lo > hi) {
    if (this === zCropLo) zCropLo.value = hi;
    else zCropHi.value = lo;
  }
  updateZCropFill();
  clearTimeout(zCropTimer);
  zCropTimer = setTimeout(() => {
    const loVal = parseInt(zCropLo.value, 10) / 1000;
    const hiVal = parseInt(zCropHi.value, 10) / 1000;
    sendCmd({ z_crop: [loVal, hiVal] });
  }, 150);
}

zCropLo.addEventListener("input", onZCropChange);
zCropHi.addEventListener("input", onZCropChange);
updateZCropFill();

// ── Border toggle ──

const borderToggle = document.getElementById("borderToggle");
borderToggle.addEventListener("change", () => {
  sendCmd({ border: borderToggle.checked });
});

// ── WebSocket ──

const ws = new WebSocket("ws://" + location.host + "/ws");
let pendingCmd = null;
let waitingFrame = false;
let cmdTimestamp = 0;

ws.binaryType = "blob";

ws.onmessage = e => {
  if (e.data instanceof Blob) {
    const url = URL.createObjectURL(e.data);
    const old = view3d.src;
    view3d.src = url;
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    showTiming(performance.now() - cmdTimestamp);
    showLoading(false);
    waitingFrame = false;
    if (animating) {
      turntableStep();
    } else if (pendingCmd) {
      const cmd = pendingCmd;
      pendingCmd = null;
      sendCmd(cmd);
    }
  } else {
    const msg = JSON.parse(e.data);
    if (msg.preview2d) {
      view2d.src = "/render2d?t=" + Date.now();
    }
    if (msg.info) {
      fileInfo.textContent = msg.info;
    }
  }
};

ws.onopen = () => {
  fetch("/state").then(r => r.json()).then(s => {
    if (HAS_GPU) gpuToggle.checked = s.gpu;
    downsampleSelect.value = s.downsample;
    if (s.border !== undefined) borderToggle.checked = s.border;
    if (s.z_crop) {
      zCropLo.value = Math.round(s.z_crop[0] * 1000);
      zCropHi.value = Math.round(s.z_crop[1] * 1000);
      updateZCropFill();
    }
    if (s.loaded) {
      hideEmptyState();
      fileInfo.textContent = s.info || "";
      const sr = sg.querySelector(`input[value="${s.stretch}"]`);
      if (sr) sr.checked = true;
      const zr = zg.querySelector(`input[value="${s.z_scale}"]`);
      if (zr) zr.checked = true;
      sendCmd({ refresh: true });
      view2d.src = "/render2d?t=" + Date.now();
    }
  });
};

function sendCmd(cmd) {
  if (waitingFrame) {
    pendingCmd = cmd;
    return;
  }
  waitingFrame = true;
  cmdTimestamp = performance.now();
  if (!animating) showLoading(true);
  ws.send(JSON.stringify(cmd));
}

// ── Controls ──

sg.addEventListener("change", e => {
  if (e.target.name === "stretch") sendCmd({ stretch: e.target.value });
});

zg.addEventListener("change", e => {
  if (e.target.name === "zscale") sendCmd({ z_scale: e.target.value });
});

downsampleSelect.addEventListener("change", () => {
  sendCmd({ downsample: parseInt(downsampleSelect.value, 10) });
});

gpuToggle.addEventListener("change", () => {
  sendCmd({ gpu: gpuToggle.checked });
});

// ── 3D drag rotation ──

let dragging = false, lastX = 0, lastY = 0, accumDX = 0, accumDY = 0;
let dragTimer = null;
const DRAG_INTERVAL = 50;

view3d.ondragstart = () => false;

function flushRotation() {
  if (accumDX === 0 && accumDY === 0) return;
  const dx = accumDX, dy = accumDY;
  accumDX = 0;
  accumDY = 0;
  sendCmd({ rotate: { dx, dy } });
}

view3d.addEventListener("mousedown", e => {
  if (e.button !== 0) return;
  if (animating) stopTurntable();
  dragging = true;
  lastX = e.clientX;
  lastY = e.clientY;
  accumDX = 0;
  accumDY = 0;
  dragTimer = setInterval(flushRotation, DRAG_INTERVAL);
  e.preventDefault();
  e.stopPropagation();
});

window.addEventListener("mousemove", e => {
  if (!dragging) return;
  e.preventDefault();
  accumDX += e.clientX - lastX;
  accumDY += e.clientY - lastY;
  lastX = e.clientX;
  lastY = e.clientY;
});

window.addEventListener("mouseup", () => {
  if (!dragging) return;
  dragging = false;
  clearInterval(dragTimer);
  flushRotation();
});

// ── Zoom ──

let wheelAccum = 0, wheelTimer = null;

view3d.addEventListener("wheel", e => {
  e.preventDefault();
  wheelAccum += e.deltaY > 0 ? -0.1 : 0.1;
  if (!wheelTimer) {
    wheelTimer = setTimeout(() => {
      const z = wheelAccum;
      wheelAccum = 0;
      wheelTimer = null;
      sendCmd({ zoom: z });
    }, 50);
  }
}, { passive: false });

// ── 2D crop ──

const cropCanvas = document.getElementById("cropCanvas");
const cropCtx = cropCanvas.getContext("2d");
let cropStart = null;
let activeCrop = null;

function resizeCropCanvas() {
  const rect = view2d.getBoundingClientRect();
  cropCanvas.width = rect.width * devicePixelRatio;
  cropCanvas.height = rect.height * devicePixelRatio;
  cropCtx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}

function drawCropOverlay(dragRect) {
  const rect = view2d.getBoundingClientRect();
  const cw = rect.width, ch = rect.height;
  cropCtx.clearRect(0, 0, cw, ch);

  if (activeCrop && !dragRect) {
    const nw = view2d.naturalWidth, nh = view2d.naturalHeight;
    if (nw > 0 && nh > 0) {
      const sx = cw / nw, sy = ch / nh;
      const x = activeCrop.c0 * sx, y = activeCrop.r0 * sy;
      const w = (activeCrop.c1 - activeCrop.c0) * sx;
      const h = (activeCrop.r1 - activeCrop.r0) * sy;
      cropCtx.strokeStyle = "#ff6b6b";
      cropCtx.lineWidth = 1.5;
      cropCtx.setLineDash([4, 3]);
      cropCtx.strokeRect(x, y, w, h);
      cropCtx.setLineDash([]);
    }
  }

  if (dragRect) {
    cropCtx.strokeStyle = "#5eb8f7";
    cropCtx.lineWidth = 1;
    cropCtx.strokeRect(dragRect.x, dragRect.y, dragRect.w, dragRect.h);
  }
}

view2d.addEventListener("load", () => { resizeCropCanvas(); drawCropOverlay(); });
new ResizeObserver(() => { resizeCropCanvas(); drawCropOverlay(); }).observe(view2d);

cropCanvas.addEventListener("mousedown", e => {
  const rect = cropCanvas.getBoundingClientRect();
  cropStart = { cx: e.clientX - rect.left, cy: e.clientY - rect.top };
  e.preventDefault();
});

cropCanvas.addEventListener("mousemove", e => {
  if (!cropStart) return;
  const rect = cropCanvas.getBoundingClientRect();
  const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
  const x = Math.min(cropStart.cx, cx), y = Math.min(cropStart.cy, cy);
  const w = Math.abs(cx - cropStart.cx), h = Math.abs(cy - cropStart.cy);
  drawCropOverlay({ x, y, w, h });
});

cropCanvas.addEventListener("mouseup", e => {
  if (!cropStart) return;
  const rect = cropCanvas.getBoundingClientRect();
  const nw = view2d.naturalWidth, nh = view2d.naturalHeight;
  if (nw <= 0 || nh <= 0) { cropStart = null; return; }
  const sx = nw / rect.width, sy = nh / rect.height;
  const endX = (e.clientX - rect.left) * sx;
  const endY = (e.clientY - rect.top) * sy;
  const startX = cropStart.cx * sx;
  const startY = cropStart.cy * sy;
  cropStart = null;
  const c0 = Math.round(Math.min(startX, endX));
  const c1 = Math.round(Math.max(startX, endX));
  const r0 = Math.round(Math.min(startY, endY));
  const r1 = Math.round(Math.max(startY, endY));
  if (c1 - c0 > 4 && r1 - r0 > 4) {
    activeCrop = { r0, r1, c0, c1 };
    drawCropOverlay();
    sendCmd({ crop: { r0, r1, c0, c1 } });
  } else {
    drawCropOverlay();
  }
});

cropCanvas.addEventListener("dblclick", e => {
  e.preventDefault();
  activeCrop = null;
  drawCropOverlay();
  sendCmd({ clear_crop: true });
});

// ── Export Dialog ──

const exportOverlay = document.getElementById("exportOverlay");
const exportDialog = document.getElementById("exportDialog");
const exportTitle = document.getElementById("exportTitle");
const exportBody = document.getElementById("exportBody");
const exportConfirm = document.getElementById("exportConfirm");
const exportCancel = document.getElementById("exportCancel");
const exportClose = document.getElementById("exportClose");
let exportAction = null;

function openExportDialog(title, bodyHTML, onConfirm) {
  exportTitle.textContent = title;
  exportBody.innerHTML = bodyHTML;
  exportAction = onConfirm;
  exportOverlay.classList.add("visible");
}

function closeExportDialog() {
  exportOverlay.classList.remove("visible");
  exportAction = null;
}

exportCancel.addEventListener("click", closeExportDialog);
exportClose.addEventListener("click", closeExportDialog);
exportOverlay.addEventListener("click", e => {
  if (e.target === exportOverlay) closeExportDialog();
});

window.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (exportOverlay.classList.contains("visible")) closeExportDialog();
    else if (animating) stopTurntable();
  }
});

exportConfirm.addEventListener("click", () => {
  if (exportAction) exportAction();
});

// ── Screenshot ──

document.getElementById("screenshotBtn").addEventListener("click", () => {
  if (!view3d.src || !view3d.naturalWidth) return;

  openExportDialog("Export Screenshot", `
    <div class="export-field">
      <div class="export-field-label">Format</div>
      <div class="radio-group">
        <label><input type="radio" name="ss-format" value="png" checked><span>PNG</span></label>
        <label><input type="radio" name="ss-format" value="jpg"><span>JPG</span></label>
      </div>
    </div>
    <div class="export-field" id="ssQualityField" style="display:none">
      <div class="export-field-label">Quality</div>
      <div class="export-slider-row">
        <input type="range" id="ssQuality" min="1" max="100" value="92">
        <span class="export-slider-value" id="ssQualityVal">92</span>
      </div>
    </div>
  `, () => {
    const fmt = exportBody.querySelector('input[name="ss-format"]:checked').value;
    const quality = parseInt(exportBody.querySelector("#ssQuality").value, 10);
    closeExportDialog();
    const params = new URLSearchParams({ format: fmt, quality });
    const a = document.createElement("a");
    a.href = "/screenshot?" + params;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  const formatRadios = exportBody.querySelectorAll('input[name="ss-format"]');
  const qualityField = exportBody.querySelector("#ssQualityField");
  const qualitySlider = exportBody.querySelector("#ssQuality");
  const qualityVal = exportBody.querySelector("#ssQualityVal");

  qualitySlider.addEventListener("input", () => {
    qualityVal.textContent = qualitySlider.value;
  });

  formatRadios.forEach(r => r.addEventListener("change", () => {
    const sel = exportBody.querySelector('input[name="ss-format"]:checked').value;
    qualityField.style.display = sel === "jpg" ? "" : "none";
  }));
});

// ── Turntable Animation ──

const animateBtn = document.getElementById("animateBtn");
const animIndicator = document.getElementById("animIndicator");
const animText = animIndicator.querySelector("span:last-child");
const recordBtn = document.getElementById("recordBtn");
let animating = false;
let lastAnimFrame = 0;
const TURNTABLE_STEP = -1.5;
const ANIM_MIN_INTERVAL = 33;

function turntableStep() {
  if (!animating) return;
  const now = performance.now();
  const wait = Math.max(0, ANIM_MIN_INTERVAL - (now - lastAnimFrame));
  setTimeout(() => {
    if (!animating) return;
    lastAnimFrame = performance.now();
    sendCmd({ rotate: { dx: TURNTABLE_STEP / 0.5, dy: 0 } });
  }, wait);
}

function stopTurntable() {
  animating = false;
  animateBtn.classList.remove("active");
  animateBtn.querySelector("svg").innerHTML =
    '<polygon points="5 3 19 12 5 21 5 3"/>';
  animateBtn.dataset.tooltip = "Rotate";
  animIndicator.classList.remove("visible");
}

animateBtn.addEventListener("click", () => {
  if (!view3d.src || !view3d.naturalWidth) return;

  animating = !animating;

  if (animating) {
    animateBtn.classList.add("active");
    animateBtn.querySelector("svg").innerHTML =
      '<rect x="6" y="6" width="12" height="12" rx="1"/>';
    animateBtn.dataset.tooltip = "Stop";
    animText.textContent = "Turntable";
    animIndicator.classList.add("visible");
    lastAnimFrame = performance.now();
    turntableStep();
  } else {
    stopTurntable();
  }
});

// ── Record turntable video ──

recordBtn.addEventListener("click", () => {
  if (!view3d.src || !view3d.naturalWidth) return;
  if (animating) stopTurntable();

  openExportDialog("Export Video", `
    <div class="export-field">
      <div class="export-field-label">Duration</div>
      <div class="export-slider-row">
        <input type="range" id="vidDuration" min="1" max="30" value="5" step="1">
        <span class="export-slider-value" id="vidDurationVal">5s</span>
      </div>
    </div>
    <div class="export-field">
      <div class="export-field-label">FPS</div>
      <div class="radio-group">
        <label><input type="radio" name="vid-fps" value="15"><span>15</span></label>
        <label><input type="radio" name="vid-fps" value="24"><span>24</span></label>
        <label><input type="radio" name="vid-fps" value="30" checked><span>30</span></label>
        <label><input type="radio" name="vid-fps" value="60"><span>60</span></label>
      </div>
    </div>
    <div class="export-field">
      <div class="export-field-label">Rotation Speed</div>
      <div class="export-slider-row">
        <input type="range" id="vidSpeed" min="25" max="200" value="50" step="25">
        <span class="export-slider-value" id="vidSpeedVal">0.5x</span>
      </div>
    </div>
    <div class="export-field">
      <div class="export-field-label">Quality</div>
      <div class="export-slider-row">
        <input type="range" id="vidQuality" min="1" max="100" value="90">
        <span class="export-slider-value" id="vidQualityVal">90</span>
      </div>
    </div>
  `, async () => {
    const duration = parseInt(exportBody.querySelector("#vidDuration").value, 10);
    const fps = parseInt(exportBody.querySelector('input[name="vid-fps"]:checked').value, 10);
    const speed = parseInt(exportBody.querySelector("#vidSpeed").value, 10) / 100;
    const quality = parseInt(exportBody.querySelector("#vidQuality").value, 10);
    closeExportDialog();

    recordBtn.classList.add("active");
    recordBtn.disabled = true;
    animText.textContent = "Rendering video...";
    animIndicator.classList.add("visible");

    try {
      const params = new URLSearchParams({ fps, speed, quality, duration });
      const resp = await fetch("/record?" + params);
      if (!resp.ok) throw new Error("Record failed");
      const blob = await resp.blob();
      const disposition = resp.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="(.+)"/);
      const filename = match ? match[1] : "photonscape-turntable.mp4";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (_) {}

    recordBtn.classList.remove("active");
    recordBtn.disabled = false;
    animIndicator.classList.remove("visible");
  });

  const durationSlider = exportBody.querySelector("#vidDuration");
  const durationVal = exportBody.querySelector("#vidDurationVal");
  const speedSlider = exportBody.querySelector("#vidSpeed");
  const speedVal = exportBody.querySelector("#vidSpeedVal");
  const qualitySlider = exportBody.querySelector("#vidQuality");
  const qualityVal = exportBody.querySelector("#vidQualityVal");

  durationSlider.addEventListener("input", () => {
    durationVal.textContent = durationSlider.value + "s";
  });
  speedSlider.addEventListener("input", () => {
    speedVal.textContent = parseFloat((parseInt(speedSlider.value, 10) / 100).toFixed(2)) + "x";
  });
  qualitySlider.addEventListener("input", () => {
    qualityVal.textContent = qualitySlider.value;
  });
});

// ── Export Scene ──

document.getElementById("exportSceneBtn").addEventListener("click", () => {
  if (!view3d.src || !view3d.naturalWidth) return;
  const a = document.createElement("a");
  a.href = "/export-scene";
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
});

// ── File Upload ──

async function uploadFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("downsample", downsampleSelect.value);
  showLoading(true);
  hideEmptyState();
  const resp = await fetch("/upload", { method: "POST", body: fd });
  const data = await resp.json();
  fileInfo.textContent = data.info || "";
  activeCrop = null;
  zCropLo.value = 0;
  zCropHi.value = 1000;
  updateZCropFill();
  sendCmd({ refresh: true });
  view2d.src = "/render2d?t=" + Date.now();
}

document.getElementById("uploadBtn").addEventListener("click", () => {
  fileInput.click();
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) uploadFile(file);
});

// ── Drag and Drop (full page) ──

const dragOverlay = document.getElementById("dragOverlay");
let dragCounter = 0;

window.addEventListener("dragenter", e => {
  e.preventDefault();
  dragCounter++;
  if (dragCounter === 1) {
    dragOverlay.classList.add("visible");
  }
});

window.addEventListener("dragleave", e => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter === 0) {
    dragOverlay.classList.remove("visible");
  }
});

window.addEventListener("dragover", e => {
  e.preventDefault();
});

window.addEventListener("drop", e => {
  e.preventDefault();
  dragCounter = 0;
  dragOverlay.classList.remove("visible");
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});

// ── Tooltips ──

const tooltip = document.getElementById("tooltip");
let tooltipTimer = null;
let tooltipTarget = null;

document.addEventListener("mouseover", e => {
  const el = e.target.closest("[data-tooltip]");
  if (el === tooltipTarget) return;
  clearTimeout(tooltipTimer);
  tooltip.classList.remove("visible");
  tooltipTarget = el;
  if (!el) return;
  tooltipTimer = setTimeout(() => {
    const rect = el.getBoundingClientRect();
    tooltip.textContent = el.dataset.tooltip;
    tooltip.style.left = rect.left + rect.width / 2 + "px";
    tooltip.style.top = rect.bottom + 8 + "px";
    tooltip.style.transform = "translateX(-50%)";
    tooltip.classList.add("visible");
  }, 400);
});

document.addEventListener("mouseout", e => {
  const el = e.target.closest("[data-tooltip]");
  if (!el) return;
  const related = e.relatedTarget;
  if (related && el.contains(related)) return;
  clearTimeout(tooltipTimer);
  tooltip.classList.remove("visible");
  tooltipTarget = null;
});
