#!/usr/bin/env python3

import io
import json
import math
import os
import subprocess
import tempfile
import time
import struct
from pathlib import Path

import cv2
import numpy as np


def _detect_nvidia_gpu():
    try:
        ret = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5)
        return ret.returncode == 0 and b"GPU" in ret.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


HAS_GPU = _detect_nvidia_gpu()

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
if "DISPLAY" not in os.environ and "VTK_DEFAULT_OPENGL_WINDOW" not in os.environ:
    os.environ["VTK_DEFAULT_OPENGL_WINDOW"] = (
        "vtkEGLRenderWindow" if HAS_GPU else "vtkOSOpenGLRenderWindow"
    )

import pyvista as pv
import vtk
import tifffile
from astropy.io import fits as astrofits
from PIL import Image
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn


MAD_NORM = 1.4826
DEFAULT_SHADOWS_CLIPPING = -2.80
DEFAULT_TARGET_BACKGROUND = 0.25


def _detect_format(data):
    header = data[:32]

    if header[:9] == b"SIMPLE  =":
        return "fits"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if header[:2] in (b"II", b"MM"):
        byte_order = "<" if header[:2] == b"II" else ">"
        magic = struct.unpack(byte_order + "H", header[2:4])[0]
        if magic == 42 or magic == 43:
            return "tiff"

    raise ValueError("Unrecognized image format")


BAYER_CV2_CODES = {
    "RGGB": "COLOR_BayerBG2RGB",
    "BGGR": "COLOR_BayerRG2RGB",
    "GRBG": "COLOR_BayerGB2RGB",
    "GBRG": "COLOR_BayerGR2RGB",
}


def _debayer(data_2d, pattern):
    code_name = BAYER_CV2_CODES.get(pattern.upper())
    if code_name is None:
        print(f"Warning: unknown Bayer pattern '{pattern}', skipping debayer")
        return data_2d[:, :, np.newaxis]

    code = getattr(cv2, code_name)
    lo, hi = data_2d.min(), data_2d.max()
    if hi > lo:
        scaled = ((data_2d - lo) / (hi - lo) * 65535).astype(np.uint16)
    else:
        scaled = np.zeros_like(data_2d, dtype=np.uint16)

    rgb_u16 = cv2.cvtColor(scaled, code)
    rgb_f = rgb_u16.astype(np.float32) * ((hi - lo) / 65535.0) + lo
    return rgb_f


def _detect_bayer(header):
    pat = header.get("BAYERPAT")
    if pat:
        return pat.strip().upper()
    ct = header.get("COLORTYP")
    if ct and ct.strip().upper() in BAYER_CV2_CODES:
        return ct.strip().upper()
    return None


def _load_fits(buf):
    with astrofits.open(buf) as hdul:
        hdr = hdul[0].header
        data = hdul[0].data.astype(np.float32)

    meta = {}
    if data.ndim == 2:
        bayer = _detect_bayer(hdr)
        if bayer:
            print(f"Bayer pattern detected: {bayer}")
            data = _debayer(data, bayer)
            meta["bayer"] = bayer
        else:
            data = data[:, :, np.newaxis]
    elif data.ndim == 3:
        data = np.transpose(data, (1, 2, 0))

    return data, meta


def _load_tiff(buf):
    data = tifffile.imread(buf)
    data = np.squeeze(data)

    if data.ndim == 2:
        data = data[:, :, np.newaxis]
    elif data.ndim == 3:
        if data.shape[0] <= 4 and data.shape[1] > 4 and data.shape[2] > 4:
            data = np.transpose(data, (1, 2, 0))

    return data.astype(np.float32)


def _load_pil(buf):
    img = Image.open(buf)
    mode = img.mode

    if mode in ("I", "I;16", "I;16B", "I;16L"):
        data = np.array(img, dtype=np.float32)
        if data.ndim == 2:
            data = data[:, :, np.newaxis]
        return data

    if mode == "F":
        data = np.array(img, dtype=np.float32)
        if data.ndim == 2:
            data = data[:, :, np.newaxis]
        return data

    if mode == "LA":
        img = img.convert("L")
    elif mode == "PA":
        img = img.convert("P").convert("RGB")
    elif mode == "RGBA":
        img = img.convert("RGB")
    elif mode == "P":
        img = img.convert("RGB")
    elif mode == "1":
        img = img.convert("L")
    elif mode == "CMYK":
        img = img.convert("RGB")

    data = np.array(img, dtype=np.float32)
    if data.ndim == 2:
        data = data[:, :, np.newaxis]

    return data


def load_image(raw_bytes):
    fmt = _detect_format(raw_bytes)
    print(f"Detected format: {fmt}")
    buf = io.BytesIO(raw_bytes)

    meta = {"format": fmt}
    if fmt == "fits":
        data, fits_meta = _load_fits(buf)
        meta.update(fits_meta)
    elif fmt == "tiff":
        data = _load_tiff(buf)
    else:
        data = _load_pil(buf)

    return data, meta


def normalize_to_01(data):
    lo, hi = data.min(), data.max()
    if hi > lo:
        return (data - lo) / (hi - lo)
    return np.zeros_like(data)


def mtf(x, m):
    out = np.empty_like(x)
    mask_lo = x <= 0
    mask_hi = x >= 1
    mask_mid = ~mask_lo & ~mask_hi
    out[mask_lo] = 0.0
    out[mask_hi] = 1.0
    xm = x[mask_mid]
    out[mask_mid] = (m - 1.0) * xm / ((2.0 * m - 1.0) * xm - m)
    return out


def mtf_scalar(x, m):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return (m - 1.0) * x / ((2.0 * m - 1.0) * x - m)


def compute_stf_params(channel):
    med = np.median(channel)
    mad = np.median(np.abs(channel - med)) * MAD_NORM

    if med <= 0.5:
        c0 = np.clip(med + DEFAULT_SHADOWS_CLIPPING * mad, 0, 1)
        m = mtf_scalar(med - c0, DEFAULT_TARGET_BACKGROUND)
        return c0, m, 1.0
    else:
        c1 = np.clip(med - DEFAULT_SHADOWS_CLIPPING * mad, 0, 1)
        m = 1.0 - mtf_scalar(c1 - med, DEFAULT_TARGET_BACKGROUND)
        return 0.0, m, c1


def apply_stf_channel(channel, shadows, midtones, highlights):
    if highlights <= shadows:
        return np.zeros_like(channel)
    clipped = np.clip((channel - shadows) / (highlights - shadows), 0, 1)
    return mtf(clipped, midtones)


def stretch_autostretch_linked(data):
    normed = normalize_to_01(data)
    n_channels = normed.shape[2]

    all_shadows, all_midtones, all_highlights = [], [], []
    for c in range(n_channels):
        s, m, h = compute_stf_params(normed[:, :, c])
        all_shadows.append(s)
        all_midtones.append(m)
        all_highlights.append(h)

    shadows = np.mean(all_shadows)
    midtones = np.mean(all_midtones)
    highlights = np.mean(all_highlights)

    out = np.empty_like(normed)
    for c in range(n_channels):
        out[:, :, c] = apply_stf_channel(normed[:, :, c], shadows, midtones, highlights)
    return out


def stretch_autostretch_unlinked(data):
    normed = normalize_to_01(data)
    n_channels = normed.shape[2]

    out = np.empty_like(normed)
    for c in range(n_channels):
        s, m, h = compute_stf_params(normed[:, :, c])
        out[:, :, c] = apply_stf_channel(normed[:, :, c], s, m, h)
    return out


def stretch_linear(data):
    return normalize_to_01(data)


def to_rgb(img):
    c = img.shape[2]
    if c == 1:
        return np.repeat(img, 3, axis=2)
    return img[:, :, :3]


STRETCH_MODES = [
    ("Linear",          stretch_linear),
    ("Auto (unlinked)", stretch_autostretch_unlinked),
    ("Auto (linked)",   stretch_autostretch_linked),
]
STRETCH_NAMES = [name for name, _ in STRETCH_MODES]
STRETCH_FUNCS = {name: fn for name, fn in STRETCH_MODES}

Z_SCALES = [("x1", 1.0), ("x4", 4.0), ("x8", 8.0), ("x16", 16.0)]
Z_SCALE_VALUES = {n: v for n, v in Z_SCALES}
DEFAULT_Z_SCALE = "x4"


def _apply_stretch(data, stretch_fn):
    img = (np.clip(stretch_fn(data), 0, 1) * 255).astype(np.uint8)
    z = img.mean(axis=2).astype(np.float32)
    rgb = to_rgb(img)
    return z, rgb


# ── Web Server ──

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

RENDER_W, RENDER_H = 900, 700


class ViewerSession:

    def __init__(self):
        self.data = None
        self.raw_bytes = None
        self.filename = None
        self.meta = {}
        self.downsample = 0
        self.stretch_name = "Linear"
        self.z_scale_name = DEFAULT_Z_SCALE
        self.use_gpu = HAS_GPU
        self.crop = None
        self.z_crop = (0.0, 1.0)
        self.show_border = True
        self.z_base = None
        self.rgb = None
        self._full_surf = None
        self._crop_surf = None
        self._actor = None
        self._border_actor = None
        self._plotter = None
        self._pl_bounds = None
        self._surf_bounds = None
        self.azimuth = 30.0
        self.elevation = 30.0
        self.distance_factor = 1.0

    def load_from_bytes(self, raw_bytes, filename, downsample):
        self.raw_bytes = raw_bytes
        self.filename = filename
        self.downsample = downsample
        self._apply_load()

    def set_downsample(self, downsample):
        if self.raw_bytes is None or downsample == self.downsample:
            return
        self.downsample = downsample
        self._apply_load()

    def _apply_load(self):
        data, self.meta = load_image(self.raw_bytes)
        h, w = data.shape[:2]
        if self.downsample > 0 and max(h, w) > self.downsample:
            scale = self.downsample / max(h, w)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            data = cv2.resize(data, (new_w, new_h), interpolation=cv2.INTER_AREA)
            if data.ndim == 2:
                data = data[:, :, np.newaxis]
        self.data = data
        self.crop = None
        self.z_crop = (0.0, 1.0)
        self._recompute_stretch()
        self._show_surface(self._full_surf)

    def set_stretch(self, name):
        if name not in STRETCH_FUNCS or name == self.stretch_name:
            return
        self.stretch_name = name
        self._recompute_stretch()
        self._crop_surf = None
        if self.crop is not None:
            self._build_crop_surf()
            self._show_surface(self._crop_surf)
        else:
            self._show_surface(self._full_surf)

    def set_z_scale(self, name):
        if name not in Z_SCALE_VALUES:
            return
        self.z_scale_name = name
        self._apply_z_scale()

    def set_crop(self, r0, r1, c0, c1):
        h, w = self.data.shape[:2]
        # 2D preview is flipud: convert display coords (top=0) to data coords (bottom=0)
        r0, r1 = h - r1, h - r0
        r0, r1 = max(0, r0), min(h, r1)
        c0, c1 = max(0, c0), min(w, c1)
        if r1 - r0 < 4 or c1 - c0 < 4:
            return
        self.crop = (r0, r1, c0, c1)
        self._build_crop_surf()
        self._show_surface(self._crop_surf)

    def clear_crop(self):
        self.crop = None
        self._crop_surf = None
        self._show_surface(self._full_surf)

    def _recompute_stretch(self):
        if self.data is None:
            return
        fn = STRETCH_FUNCS[self.stretch_name]
        self.z_base, self.rgb = _apply_stretch(self.data, fn)
        self._full_surf = self._make_surf(self.z_base, self.rgb)

    def _make_surf(self, z, rgb):
        ch, cw = z.shape[:2]
        grid = pv.ImageData(dimensions=(cw, ch, 1))
        grid.point_data["elevation"] = z.ravel()
        grid.point_data["RGB"] = rgb.reshape(-1, 3)
        return grid.warp_by_scalar("elevation")

    def _build_crop_surf(self):
        r0, r1, c0, c1 = self.crop
        self._crop_surf = self._make_surf(
            self.z_base[r0:r1, c0:c1], self.rgb[r0:r1, c0:c1],
        )

    def set_gpu(self, enabled):
        if enabled == self.use_gpu:
            return
        self.use_gpu = enabled
        self._reset_plotter()

    def _reset_plotter(self):
        if self._plotter is not None:
            self._plotter.close()
        self._plotter = None
        self._actor = None
        self._border_actor = None
        self._pl_bounds = None
        self._ensure_plotter()
        if self.crop is not None and self._crop_surf is not None:
            self._show_surface(self._crop_surf)
        elif self._full_surf is not None:
            self._show_surface(self._full_surf)

    def _ensure_plotter(self):
        if self._plotter is not None:
            return
        if self.use_gpu and HAS_GPU:
            os.environ["__NV_PRIME_RENDER_OFFLOAD"] = "1"
            os.environ["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
        else:
            os.environ.pop("__NV_PRIME_RENDER_OFFLOAD", None)
            os.environ.pop("__GLX_VENDOR_LIBRARY_NAME", None)
        if self.use_gpu and HAS_GPU:
            os.environ["VTK_DEFAULT_OPENGL_WINDOW"] = "vtkEGLRenderWindow"
        else:
            os.environ["VTK_DEFAULT_OPENGL_WINDOW"] = "vtkOSOpenGLRenderWindow"
        pl = pv.Plotter(off_screen=True, window_size=[RENDER_W, RENDER_H])
        pl.set_background("#10101c")
        self._plotter = pl

    def _show_surface(self, surf):
        if surf is None:
            return
        self._ensure_plotter()
        if self._actor is not None:
            self._plotter.remove_actor(self._actor)
        self._remove_border()
        self._surf_bounds = surf.bounds
        self._actor = self._plotter.add_mesh(
            surf, scalars="RGB", rgb=True, show_edges=False, lighting=False,
        )
        scale = Z_SCALE_VALUES[self.z_scale_name]
        self._actor.SetScale(1, 1, scale)
        self._apply_z_crop()
        self._apply_border()
        self._update_bounds()
        self._plotter.render()

    def _remove_border(self):
        if self._border_actor is not None:
            self._plotter.remove_actor(self._border_actor)
            self._border_actor = None

    def _apply_border(self):
        self._remove_border()
        if not self.show_border or self._surf_bounds is None:
            return
        x0, x1, y0, y1, z0, z1 = self._surf_bounds
        scale = Z_SCALE_VALUES[self.z_scale_name]
        z0s, z1s = z0 * scale, z1 * scale
        if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
            return
        z_pad = max((z1s - z0s) * 0.02, 0.5)
        box = pv.Box(bounds=(x0, x1, y0, y1, z0s - z_pad, z1s + z_pad))
        edges = box.extract_all_edges()
        self._border_actor = self._plotter.add_mesh(
            edges, color="#5eb8f7", line_width=1.5, opacity=0.4, lighting=False,
        )

    def _apply_z_crop(self):
        if self._actor is None or self._surf_bounds is None:
            return
        mapper = self._actor.GetMapper()
        mapper.RemoveAllClippingPlanes()
        z_lo, z_hi = self.z_crop
        if z_lo <= 0.0 and z_hi >= 1.0:
            return
        scale = Z_SCALE_VALUES[self.z_scale_name]
        z_min, z_max = self._surf_bounds[4] * scale, self._surf_bounds[5] * scale
        z_range = z_max - z_min if z_max > z_min else 1.0
        if z_lo > 0.0:
            p = vtk.vtkPlane()
            p.SetOrigin(0, 0, z_min + z_lo * z_range)
            p.SetNormal(0, 0, 1)
            mapper.AddClippingPlane(p)
        if z_hi < 1.0:
            p = vtk.vtkPlane()
            p.SetOrigin(0, 0, z_min + z_hi * z_range)
            p.SetNormal(0, 0, -1)
            mapper.AddClippingPlane(p)

    def _apply_z_scale(self):
        if self._actor is None:
            return
        scale = Z_SCALE_VALUES[self.z_scale_name]
        self._actor.SetScale(1, 1, scale)
        self._apply_z_crop()
        self._apply_border()
        self._update_bounds()

    def _update_bounds(self):
        if self._surf_bounds is None:
            return
        x0, x1, y0, y1, z0, z1 = self._surf_bounds
        scale = Z_SCALE_VALUES[self.z_scale_name]
        z0s, z1s = z0 * scale, z1 * scale
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        cz = (z0s + z1s) / 2
        diag = max(x1 - x0, y1 - y0, z1s - z0s, 1.0)
        self._pl_bounds = (cx, cy, cz, diag)

    def apply_command(self, cmd):
        changed = False
        if "stretch" in cmd:
            old = self.stretch_name
            self.set_stretch(cmd["stretch"])
            changed = self.stretch_name != old
        if "z_scale" in cmd:
            old = self.z_scale_name
            self.set_z_scale(cmd["z_scale"])
            changed = changed or self.z_scale_name != old
        if "downsample" in cmd:
            self.set_downsample(cmd["downsample"])
            changed = True
        if "gpu" in cmd:
            self.set_gpu(cmd["gpu"])
            changed = True
        if "rotate" in cmd:
            r = cmd["rotate"]
            self.azimuth += r.get("dx", 0) * 0.5
            self.elevation = max(-89, min(89, self.elevation + r.get("dy", 0) * 0.5))
            changed = True
        if "zoom" in cmd:
            self.distance_factor = max(0.2, min(5.0, self.distance_factor - cmd["zoom"]))
            changed = True
        if "crop" in cmd:
            c = cmd["crop"]
            self.set_crop(c["r0"], c["r1"], c["c0"], c["c1"])
            changed = True
        if "clear_crop" in cmd:
            self.clear_crop()
            changed = True
        if "z_crop" in cmd:
            zc = cmd["z_crop"]
            lo = max(0.0, min(1.0, zc[0]))
            hi = max(0.0, min(1.0, zc[1]))
            if lo > hi:
                lo, hi = hi, lo
            self.z_crop = (lo, hi)
            self._apply_z_crop()
            self._apply_border()
            changed = True
        if "border" in cmd:
            self.show_border = bool(cmd["border"])
            self._apply_border()
            changed = True
        return changed

    def render_3d(self):
        if self.z_base is None:
            return self._blank_jpeg()

        self._ensure_plotter()

        if self._pl_bounds is None:
            return self._blank_jpeg()

        cx, cy, cz, diag = self._pl_bounds
        dist = diag * 1.5 * self.distance_factor
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        x = cx + dist * math.cos(el) * math.sin(az)
        y = cy + dist * math.cos(el) * math.cos(az)
        z = cz + dist * math.sin(el)

        self._plotter.camera.position = (x, y, z)
        self._plotter.camera.focal_point = (cx, cy, cz)
        self._plotter.camera.up = (0, 0, 1)
        self._plotter.render()
        self._plotter.render()

        img = self._plotter.screenshot(transparent_background=False, return_img=True)
        return self._encode_jpeg(img)

    def render_2d(self):
        if self.rgb is None:
            return self._blank_jpeg()
        rgb = np.flipud(self.rgb)
        return self._encode_jpeg(rgb)

    def _encode_png(self, img):
        _, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        return buf.tobytes()

    def _encode_jpeg(self, img, quality=90):
        _, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes()

    def _blank_jpeg(self):
        blank = np.zeros((RENDER_H, RENDER_W, 3), dtype=np.uint8)
        return self._encode_jpeg(blank)


session = ViewerSession()

_index_template = (STATIC_DIR / "index.html").read_text()


def _build_index_html():
    config_js = (
        f"const STRETCH_NAMES = {json.dumps(STRETCH_NAMES)};\n"
        f"const Z_SCALES = {json.dumps([n for n, _ in Z_SCALES])};\n"
        f"const DEFAULT_Z = {json.dumps(DEFAULT_Z_SCALE)};\n"
        f"const DOWNSAMPLE_OPTIONS = {json.dumps(DOWNSAMPLE_OPTIONS)};\n"
        f"const HAS_GPU = {json.dumps(HAS_GPU)};"
    )
    return _index_template.replace("%%CONFIG_SCRIPT%%", config_js)


@app.get("/", response_class=HTMLResponse)
async def index():
    return _build_index_html()


NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}


@app.get("/render3d")
async def render_3d():
    return Response(content=session.render_3d(), media_type="image/jpeg", headers=NO_CACHE_HEADERS)


@app.get("/render2d")
async def render_2d():
    return Response(content=session.render_2d(), media_type="image/jpeg", headers=NO_CACHE_HEADERS)


@app.get("/screenshot")
async def screenshot(format: str = "png", quality: int = 92):
    if session.z_base is None:
        img = np.zeros((RENDER_H, RENDER_W, 3), dtype=np.uint8)
    else:
        session._ensure_plotter()
        session._plotter.render()
        img = session._plotter.screenshot(transparent_background=False, return_img=True)

    quality = max(1, min(100, quality))
    base = session.filename or "photonscape"

    if format == "jpg":
        data = session._encode_jpeg(img, quality=quality)
        media_type = "image/jpeg"
        filename = base + ".jpg"
    else:
        data = session._encode_png(img)
        media_type = "image/png"
        filename = base + ".png"

    return Response(
        content=data,
        media_type=media_type,
        headers={
            **NO_CACHE_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.get("/record")
async def record(fps: int = 30, speed: float = 0.5, quality: int = 90, duration: float = 5.0):
    if session.z_base is None:
        return Response(content=b"", status_code=400)

    fps = max(10, min(60, fps))
    speed = max(0.25, min(2.0, speed))
    quality = max(1, min(100, quality))
    duration = max(1.0, min(60.0, duration))
    total_frames = max(1, int(fps * duration))

    original_azimuth = session.azimuth
    total_degrees = 45.0 * speed * duration
    step = -total_degrees / total_frames

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(tmp.name, fourcc, fps, (RENDER_W, RENDER_H))

        for i in range(total_frames):
            session.azimuth = original_azimuth + i * step
            session._ensure_plotter()

            cx, cy, cz, diag = session._pl_bounds
            dist = diag * 1.5 * session.distance_factor
            az = math.radians(session.azimuth)
            el = math.radians(session.elevation)
            x = cx + dist * math.cos(el) * math.sin(az)
            y = cy + dist * math.cos(el) * math.cos(az)
            z = cz + dist * math.sin(el)

            session._plotter.camera.position = (x, y, z)
            session._plotter.camera.focal_point = (cx, cy, cz)
            session._plotter.camera.up = (0, 0, 1)
            session._plotter.render()

            img = session._plotter.screenshot(transparent_background=False, return_img=True)
            writer.write(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

        writer.release()
        session.azimuth = original_azimuth

        mp4_bytes = Path(tmp.name).read_bytes()
    finally:
        os.unlink(tmp.name)

    filename = (session.filename or "photonscape") + "-turntable.mp4"
    return Response(
        content=mp4_bytes,
        media_type="video/mp4",
        headers={
            **NO_CACHE_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.get("/state")
async def get_state():
    return {
        "loaded": session.data is not None,
        "stretch": session.stretch_name,
        "z_scale": session.z_scale_name,
        "downsample": session.downsample,
        "gpu": session.use_gpu,
        "border": session.show_border,
        "z_crop": list(session.z_crop),
        "crop": list(session.crop) if session.crop else None,
        "azimuth": session.azimuth,
        "elevation": session.elevation,
        "distance_factor": session.distance_factor,
        "info": "",
    }


@app.get("/export-scene")
async def export_scene():
    scene = {
        "stretch": session.stretch_name,
        "z_scale": session.z_scale_name,
        "downsample": session.downsample,
        "crop": list(session.crop) if session.crop else None,
        "z_crop": list(session.z_crop),
        "azimuth": session.azimuth,
        "elevation": session.elevation,
        "distance_factor": session.distance_factor,
        "border": session.show_border,
    }
    filename = (session.filename or "photonscape") + "-scene.json"
    return Response(
        content=json.dumps(scene, indent=2),
        media_type="application/json",
        headers={
            **NO_CACHE_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


DOWNSAMPLE_OPTIONS = [0, 4096, 2048, 1024, 512]


def _build_info():
    if session.data is None:
        return ""
    h, w = session.data.shape[:2]
    ch = session.data.shape[2]
    fmt = session.meta.get("format", "").upper()
    parts = [f"{w}x{h}", f"{ch}ch", fmt]
    bayer = session.meta.get("bayer")
    if bayer:
        parts.append(f"Bayer {bayer}")
    parts.append(session.filename or "")
    return "  ".join(p for p in parts if p)


@app.post("/upload")
async def upload(file: UploadFile = File(...), downsample: int = Form(0)):
    raw_bytes = await file.read()
    session.load_from_bytes(raw_bytes, file.filename, downsample)
    return {"info": _build_info()}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            text = await ws.receive_text()
            cmd = json.loads(text)

            needs_2d = "stretch" in cmd or "refresh" in cmd or "downsample" in cmd
            session.apply_command(cmd)

            t0 = time.monotonic()
            frame = session.render_3d()
            dt = time.monotonic() - t0
            print(f"ws render: {dt*1000:.0f}ms  az={session.azimuth:.1f} el={session.elevation:.1f}")

            await ws.send_bytes(frame)

            resp = {}
            if needs_2d:
                resp["preview2d"] = True
            if "downsample" in cmd and session.data is not None:
                resp["info"] = _build_info()
            if resp:
                await ws.send_text(json.dumps(resp))
    except WebSocketDisconnect:
        pass


PORT = 8182


def main():
    print(f"Starting server on http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
