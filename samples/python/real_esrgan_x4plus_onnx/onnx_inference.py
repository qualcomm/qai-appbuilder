
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)

import os
import numpy as np
import cv2
import onnxruntime as ort

# Real-ESRGAN x4plus ONNX (fixed input 128x128) reference runner for ONNX Runtime.
# This variant matches models that expect NCHW float input (tensor(float)) and fixed H/W=128.
# Many Real-ESRGAN QNN/ORT examples normalize input with /255 to float32 and use fixed-size patches. 


def load_image_rgb(path: str) -> np.ndarray:
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def save_image_rgb(path: str, rgb: np.ndarray) -> None:
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok = cv2.imwrite(path, bgr)
    if not ok:
        raise RuntimeError(f"cv2.imwrite failed: {path}")


def to_nchw_float01(rgb: np.ndarray) -> np.ndarray:
    """RGB HWC uint8 -> NCHW float32 in [0,1]."""
    x = rgb.astype(np.float32) / 255.0
    x = np.transpose(x, (2, 0, 1))  # HWC -> CHW
    x = np.expand_dims(x, 0)        # -> NCHW
    return x


def _reflect_pad_rgb(rgb: np.ndarray, pad_bottom: int, pad_right: int) -> np.ndarray:
    if pad_bottom == 0 and pad_right == 0:
        return rgb
    return cv2.copyMakeBorder(
        rgb, 0, pad_bottom, 0, pad_right, borderType=cv2.BORDER_REFLECT_101
    )


def _from_output_to_rgb(y) -> np.ndarray:
    """Convert ORT output to RGB uint8.

    Supports:
      - NCHW float32/float16
      - NHWC float32/float16
      - NCHW/NHWC uint8
    """
    y = np.asarray(y)

    # Remove batch dim if present
    if y.ndim == 4:
        y0 = y[0]
        # NCHW
        if y0.shape[0] == 3:
            y0 = np.transpose(y0, (1, 2, 0))
        # else assume HWC already (NHWC)
    elif y.ndim == 3:
        y0 = y
        if y0.shape[0] == 3:
            y0 = np.transpose(y0, (1, 2, 0))
    else:
        raise RuntimeError(f"Unexpected output shape: {y.shape}")

    if y0.dtype == np.uint8:
        return y0

    # float output: assume either [0,1] or [0,255]
    y0 = y0.astype(np.float32)
    m = float(np.nanmax(y0)) if y0.size else 0.0
    if m <= 1.5:
        y0 = np.clip(y0, 0.0, 1.0) * 255.0
    else:
        y0 = np.clip(y0, 0.0, 255.0)
    return (y0 + 0.5).astype(np.uint8)


def run_realesrgan_onnx(
    onnx_path: str,
    in_img_path: str,
    out_img_path: str,
    tile: int = 128,
    scale: int = 4,
    providers=None,
):
    if providers is None:
        providers = ["CPUExecutionProvider"]

    so = ort.SessionOptions()
    sess = ort.InferenceSession(onnx_path, sess_options=so, providers=providers)

    inp = sess.get_inputs()[0]
    out = sess.get_outputs()[0]

    print("[ORT] input :", inp.name, inp.shape, inp.type)
    print("[ORT] output:", out.name, out.shape, out.type)

    rgb = load_image_rgb(in_img_path)
    h, w = rgb.shape[:2]

    # Pad to tile multiples
    pad_h = (tile - (h % tile)) % tile
    pad_w = (tile - (w % tile)) % tile
    rgb_pad = _reflect_pad_rgb(rgb, pad_h, pad_w)
    H, W = rgb_pad.shape[:2]

    out_pad = np.zeros((H * scale, W * scale, 3), dtype=np.uint8)

    first = True
    for y in range(0, H, tile):
        for x in range(0, W, tile):
            patch = rgb_pad[y:y+tile, x:x+tile, :]

            # Model expects float input
            x_in = to_nchw_float01(patch)

            y_out = sess.run([out.name], {inp.name: x_in})[0]

            if first:
                print("[ORT] first tile in shape:", x_in.shape, x_in.dtype)
                print("[ORT] first tile out shape:", np.asarray(y_out).shape, np.asarray(y_out).dtype)
                first = False

            patch_out = _from_output_to_rgb(y_out)

            oy, ox = y * scale, x * scale
            out_pad[oy:oy+patch_out.shape[0], ox:ox+patch_out.shape[1], :] = patch_out

    out_rgb = out_pad[:h*scale, :w*scale, :]
    save_image_rgb(out_img_path, out_rgb)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real-ESRGAN runner (ORT/QNN wrapper compatible)")
    parser.add_argument("--model", default="Real-ESRGAN-x4plus.onnx", help="Path to model (.onnx or .bin)")
    parser.add_argument("--in_img", default="input.jpg", help="Input image path")
    parser.add_argument("--out_img", default="output_x4.png", help="Output image path")
    parser.add_argument("--tile", type=int, default=128, help="Tile size")
    parser.add_argument("--scale", type=int, default=4, help="Upscale factor")
    args = parser.parse_args()

    run_realesrgan_onnx(
        onnx_path=args.model,
        in_img_path=args.in_img,
        out_img_path=args.out_img,
        tile=args.tile,
        scale=args.scale,
        providers=["CPUExecutionProvider"],
    )
    print("Saved:", args.out_img)