# ---------------------------------------------------------------------
# facemap_3dmm ONNX inference script
# ---------------------------------------------------------------------
import os
import math
import argparse
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np
import cv2
from PIL import Image
from skimage import io

try:
    import onnxruntime as ort
except ImportError as e:
    raise RuntimeError("Please install onnxruntime: pip install onnxruntime") from e


FACE_IMG_FBOX_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/face_img_fbox.txt"
MEANFACE_PATH_URL      = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/meanFace.npy"
SHAPEBASIS_PATH_URL    = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/shapeBasis.npy"
BLENDSHAPE_PATH_URL    = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/blendShape.npy"


def download_if_missing(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    print(f"[Download] {url} -> {dst}")
    urlretrieve(url, str(dst))


def ensure_assets(work_dir: Path) -> dict:
    """Download required assets if not present, return their paths."""
    fbox_path  = work_dir / "face_img_fbox.txt"
    mean_path  = work_dir / "meanFace.npy"
    sb_path    = work_dir / "shapeBasis.npy"
    bs_path    = work_dir / "blendShape.npy"

    download_if_missing(FACE_IMG_FBOX_PATH_URL, fbox_path)
    download_if_missing(MEANFACE_PATH_URL, mean_path)
    download_if_missing(SHAPEBASIS_PATH_URL, sb_path)
    download_if_missing(BLENDSHAPE_PATH_URL, bs_path)

    return {
        "fbox": fbox_path,
        "mean": mean_path,
        "shapeBasis": sb_path,
        "blendShape": bs_path,
    }


def save_image(image: Image.Image, out_dir: Path, filename: str, desc: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    image.save(path)
    print(f"Saving {desc} to {path}")


def _rotation_matrices(pitch: float, yaw: float, roll: float) -> np.ndarray:
    """
    rotation logic:
      p_matrix = rotation around X by -pi
      r_matrix = yaw_matrix @ (pitch_matrix @ (p_matrix @ roll_matrix))
    """
    # p_matrix: rotate around X by -pi
    cp = math.cos(-math.pi)
    sp = math.sin(-math.pi)
    p_matrix = np.array([[1, 0, 0],
                         [0, cp, -sp],
                         [0, sp,  cp]], dtype=np.float32)

    cr = math.cos(-roll);  sr = math.sin(-roll)
    roll_matrix = np.array([[cr, -sr, 0],
                            [sr,  cr, 0],
                            [0,    0, 1]], dtype=np.float32)

    cy = math.cos(-yaw);   sy = math.sin(-yaw)
    yaw_matrix = np.array([[cy, 0, sy],
                           [0,  1, 0 ],
                           [-sy,0, cy]], dtype=np.float32)

    cpi = math.cos(-pitch); spi = math.sin(-pitch)
    pitch_matrix = np.array([[1,  0,   0 ],
                             [0, cpi, -spi],
                             [0, spi,  cpi]], dtype=np.float32)

    r_matrix = yaw_matrix @ (pitch_matrix @ (p_matrix @ roll_matrix))
    return r_matrix


def preprocess(image_path: str, fbox_path: Path):
    """
    
    - read RGB with skimage.io.imread
    - load fbox [x0, x1, y0, y1]
    - crop and resize to 128x128
    - float32, NCHW, shape (1,3,128,128)
    """
    img = io.imread(image_path)  # RGB
    fbox = np.loadtxt(fbox_path).astype(np.int32)
    x0, x1, y0, y1 = int(fbox[0]), int(fbox[1]), int(fbox[2]), int(fbox[3])

    h = y1 - y0 + 1
    w = x1 - x0 + 1

    crop = img[y0:y1+1, x0:x1+1]
    crop_128 = cv2.resize(crop, (128, 128), interpolation=cv2.INTER_LINEAR).astype(np.float32)

    # HWC -> CHW, add batch
    inp = np.transpose(crop_128, (2, 0, 1))[None, ...].astype(np.float32)
    meta = {"x0": x0, "y0": y0, "w": w, "h": h}
    return img, inp, meta


def postprocess(output_265: np.ndarray, assets: dict, meta: dict):
    """
    
    Returns landmark (68,2) in original image coordinates.
    """
    # Load assets
    face = np.load(assets["mean"]).reshape(3 * 68, 1).astype(np.float32)
    basis_id = np.load(assets["shapeBasis"]).reshape(3 * 68, 219).astype(np.float32)
    basis_exp = np.load(assets["blendShape"]).reshape(3 * 68, 39).astype(np.float32)

    out = output_265.reshape(-1).astype(np.float32)  # (265,)

    alpha_id = out[0:219] * 3.0
    alpha_exp = out[219:258] * 0.5 + 0.5
    pitch = out[258] * (math.pi / 2.0)
    yaw   = out[259] * (math.pi / 2.0)
    roll  = out[260] * (math.pi / 2.0)
    tX    = out[261] * 60.0
    tY    = out[262] * 60.0
    f     = out[263] * 150.0 + 450.0
    tZ    = 500.0

    # Reconstruct 3D vertices (68,3)
    v = (face
         + basis_id @ alpha_id.reshape(219, 1)
         + basis_exp @ alpha_exp.reshape(39, 1)).reshape(68, 3)

    # Rotate
    r_matrix = _rotation_matrices(pitch, yaw, roll)  # (3,3)
    v = v @ r_matrix.T

    # Translate
    v[:, 0] += tX
    v[:, 1] += tY
    v[:, 2] += tZ

    # Project to 2D in 128x128 crop space, then map back to original image coords
    landmark = v[:, 0:2] * (np.array([f, f], dtype=np.float32) / tZ) + (128.0 / 2.0)

    # Map from 128x128 to original crop size, then add top-left offset
    landmark[:, 0] = landmark[:, 0] * (meta["w"] / 128.0) + meta["x0"]
    landmark[:, 1] = landmark[:, 1] * (meta["h"] / 128.0) + meta["y0"]

    return landmark


def draw_landmarks(rgb_img: np.ndarray, landmark: np.ndarray):
    
    bgr = cv2.cvtColor(rgb_img, cv2.COLOR_RGB2BGR)
    for i in range(landmark.shape[0]):
        x, y = int(landmark[i, 0]), int(landmark[i, 1])
        bgr = cv2.circle(bgr, (x, y), 2, (0, 0, 255), -1)
    return bgr


def run_onnx(model_path: str, image_path: str, work_dir: Path, out_dir: Path, providers=None):
    assets = ensure_assets(work_dir)

    rgb_img, inp, meta = preprocess(image_path, assets["fbox"])

    # Create session
    sess_opt = ort.SessionOptions()
    sess = ort.InferenceSession(model_path, sess_options=sess_opt,
                               providers=providers or ["CPUExecutionProvider"])

    # Resolve input name dynamically (avoid guessing)
    input_name = sess.get_inputs()[0].name
    outputs = sess.run(None, {input_name: inp})

    # Expect first output as (1,265) like original script
    out0 = outputs[0]
    out0 = np.asarray(out0)
    if out0.ndim == 2 and out0.shape[0] == 1:
        output_265 = out0[0]
    else:
        # best-effort: flatten then take first 265 values
        output_265 = out0.reshape(-1)[:265]

    landmark = postprocess(output_265, assets, meta)

    # Save landmarks txt
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savetxt(out_dir / "demo_output_lmk.txt", landmark.astype(np.float32))
    print(f"Saving landmarks to {out_dir / 'demo_output_lmk.txt'}")

    # Draw and save image
    bgr = draw_landmarks(rgb_img, landmark)
    out_img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    save_image(out_img, out_dir, "output.jpg", "image")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Path to facemap_3dmm ONNX model (e.g., facemap_3dmm.onnx)")
    parser.add_argument("--image", required=True, help="Path to input image")
    parser.add_argument("--work_dir", default=".", help="Dir to cache assets (fbox / meanFace / shapeBasis / blendShape)")
    parser.add_argument("--out_dir", default="./out", help="Output directory")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).resolve()
    out_dir = Path(args.out_dir).resolve()

    run_onnx(args.model, args.image, work_dir, out_dir)


if __name__ == "__main__":
    main()