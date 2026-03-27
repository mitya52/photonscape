# PhotonScape

3D surface viewer for astronomical images (FITS, TIFF, PNG, JPEG).

## Install

### Local

```
pip install -r requirements.txt
```

Requires Python 3.10+.

### Docker

```bash
docker build -t photonscape .
```

## Usage

```
python photonscape.py
```

Open `http://localhost:8182` in browser and upload an image.

With Docker:

```bash
docker run --rm -p 8182:8182 photonscape
```

### UI Controls

- **Upload**: click "Open file" or drag-and-drop. Select max image size before uploading for slower machines.
- **3D view**: drag to rotate, scroll to zoom. Radio buttons for stretch mode and Z-scale.
- **2D preview**: drag to crop, double-click to reset.

## License

MIT
