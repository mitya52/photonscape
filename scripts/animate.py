#!/usr/bin/env python3

import argparse
import json
import os
import tempfile
from pathlib import Path

import cv2
import numpy as np

from photonscape import (
    ViewerSession,
    RENDER_W,
    RENDER_H,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Render multiple images with identical scene parameters and rotating camera into a single MP4",
    )
    p.add_argument("scene", help="Scene JSON exported from PhotonScape UI")
    p.add_argument("images", nargs="+", help="Image files (FITS, TIFF, PNG, JPEG)")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--frames-per-image", type=int, default=15)
    p.add_argument("--angle-per-image", type=float, default=15.0,
                    help="Camera azimuth rotation per image in degrees")
    p.add_argument("--output", default="animation.mp4")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.scene) as f:
        scene = json.load(f)

    stretch_name = scene["stretch"]
    z_scale_name = scene["z_scale"]
    downsample = scene["downsample"]
    crop = scene.get("crop")
    z_crop = tuple(scene["z_crop"])
    start_azimuth = scene["azimuth"]
    elevation = scene["elevation"]
    distance_factor = scene["distance_factor"]
    show_border = scene.get("border", False)

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp.name, fourcc, args.fps, (RENDER_W, RENDER_H))

    total_images = len(args.images)
    current_azimuth = start_azimuth

    for img_idx, image_path in enumerate(args.images):
        print(f"[{img_idx + 1}/{total_images}] {image_path}")

        raw_bytes = Path(image_path).read_bytes()

        sess = ViewerSession()
        sess.stretch_name = stretch_name
        sess.z_scale_name = z_scale_name
        sess.show_border = show_border
        sess.elevation = elevation
        sess.distance_factor = distance_factor

        sess.load_from_bytes(raw_bytes, Path(image_path).stem, downsample)

        sess.z_crop = z_crop

        if crop is not None:
            r0, r1, c0, c1 = crop
            h, w = sess.data.shape[:2]
            r0 = max(0, min(h, r0))
            r1 = max(0, min(h, r1))
            c0 = max(0, min(w, c0))
            c1 = max(0, min(w, c1))
            if r1 - r0 >= 4 and c1 - c0 >= 4:
                sess.crop = (r0, r1, c0, c1)
                sess._build_crop_surf()
                sess._show_surface(sess._crop_surf)
            else:
                sess._apply_z_crop()
                sess._apply_border()
        else:
            sess._apply_z_crop()
            sess._apply_border()

        for frame_idx in range(args.frames_per_image):
            if args.frames_per_image > 1:
                t = frame_idx / (args.frames_per_image - 1)
            else:
                t = 0.0
            sess.azimuth = current_azimuth + t * args.angle_per_image

            frame_bytes = sess.render_3d()
            img_array = cv2.imdecode(
                np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR,
            )
            label = Path(image_path).name
            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = 0.6
            thickness = 1
            (tw, th), baseline = cv2.getTextSize(label, font, scale, thickness)
            tx = (RENDER_W - tw) // 2
            ty = RENDER_H - 12
            cv2.putText(img_array, label, (tx + 1, ty + 1), font, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
            cv2.putText(img_array, label, (tx, ty), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
            writer.write(img_array)

        current_azimuth += args.angle_per_image

        if sess._plotter is not None:
            sess._plotter.close()

        print(f"  {args.frames_per_image} frames, az {current_azimuth - args.angle_per_image:.1f} -> {current_azimuth:.1f}")

    writer.release()

    output_path = Path(args.output)
    os.replace(tmp.name, str(output_path))

    total_frames = total_images * args.frames_per_image
    duration = total_frames / args.fps
    print(f"Wrote {output_path} ({total_frames} frames, {duration:.1f}s at {args.fps} FPS)")


if __name__ == "__main__":
    main()
