# PhotonScape

3D surface viewer for astronomical images (FITS, TIFF, PNG, JPEG).

## Install & Run

```
pip install -e .
photonscape
```

## Architecture

- `photonscape.py` — image loading (in-memory, no disk), stretch algorithms, web server (FastAPI + PyVista offscreen rendering)
- `static/index.html` — web UI markup (template with `%%CONFIG_SCRIPT%%` placeholder)
- `static/style.css` — web UI styles
- `static/app.js` — web UI client logic (WebSocket, controls, drag-and-drop upload)
- Image loading: FITS (astropy), TIFF (tifffile), PNG/JPEG (Pillow), with Bayer debayering (OpenCV)
- Stretch algorithms: linear, auto (linked/unlinked) using midtone transfer function (STF)
- Server has no CLI arguments — all configuration (downsampling, stretch, etc.) is done in the UI

## Code Conventions

- Clean, self-documenting code — no comments unless absolutely necessary for complex algorithms
- Do exactly what user asks — no extra features, no premature optimization
- All Python imports must be at the top of the file — no inline/lazy imports inside functions
