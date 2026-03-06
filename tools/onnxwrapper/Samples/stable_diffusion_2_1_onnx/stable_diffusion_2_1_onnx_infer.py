#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import argparse
from pathlib import Path

import numpy as np
import torch
from PIL import Image

import onnxruntime as ort
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler

# ============================================================
# Utils
# ============================================================
def nhwc_to_nchw(x: np.ndarray) -> np.ndarray:
    return np.transpose(x, (0, 3, 1, 2)).copy()

def nchw_to_nhwc(x: np.ndarray) -> np.ndarray:
    return np.transpose(x, (0, 2, 3, 1)).copy()

def ensure_4d_layout(arr: np.ndarray, inp: ort.NodeArg) -> np.ndarray:
    """
    Best-effort 4D layout match.
    Many SD exports use NCHW for latents (1,4,H,W). Some use NHWC (1,H,W,4).
    If model input shape hints channel position, follow it; otherwise default to NCHW for latents.
    """
    a = np.asarray(arr)
    if a.ndim != 4:
        return a

    shape = getattr(inp, "shape", None)

    def is_ch(v):
        try:
            iv = int(v)
            return iv in (1, 3, 4)
        except Exception:
            return False

    expects_nchw = False
    expects_nhwc = False
    if isinstance(shape, (list, tuple)) and len(shape) == 4:
        # If channel dimension is explicitly hinted
        if is_ch(shape[1]) and not is_ch(shape[3]):
            expects_nchw = True
        elif is_ch(shape[3]) and not is_ch(shape[1]):
            expects_nhwc = True

    # Convert based on expectation
    if expects_nchw:
        if a.shape[1] in (1, 3, 4):  # already NCHW-ish
            return a
        if a.shape[-1] in (1, 3, 4):  # NHWC -> NCHW
            return nhwc_to_nchw(a)
        return a

    if expects_nhwc:
        if a.shape[-1] in (1, 3, 4):  # already NHWC-ish
            return a
        if a.shape[1] in (1, 3, 4):   # NCHW -> NHWC
            return nchw_to_nhwc(a)
        return a

    # Fallback heuristic (SD latents typically have C==4): if last dim==4 assume NHWC and convert to NCHW
    if a.shape[-1] == 4 and a.shape[1] != 4:
        return nhwc_to_nchw(a)

    return a

def find_single_model_file(dir_path: Path, prefer_names=None, allow_exts=(".onnx", ".bin")) -> Path:
    """
    Find a single model file in a directory without hard-coding filename.
    If multiple candidates exist and none matches prefer_names, raise to avoid ambiguity.
    """
    dir_path = Path(dir_path)
    if not dir_path.exists():
        raise FileNotFoundError(f"Model directory not found: {dir_path}")

    exts = tuple((e or "").lower() for e in (allow_exts or ()))
    cands = sorted([p for p in dir_path.iterdir() if p.is_file() and (not exts or p.suffix.lower() in exts)])
    if not cands:
        raise FileNotFoundError(f"No model file (ext in {exts}) found under: {dir_path}")

    if prefer_names:
        pref = [n.lower() for n in prefer_names]
        for p in cands:
            if p.name.lower() in pref:
                return p

    if len(cands) == 1:
        return cands[0]

    for p in cands:
        if p.name.lower() in ("model.onnx", "model.bin", "model"):
            return p

    raise ValueError(f"Ambiguous model files under {dir_path}: {[p.name for p in cands]}")

def make_session(onnx_path: Path, providers):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    # DirectML EP guidance: disable mem pattern + keep sequential to avoid runtime issues. [1](https://github.com/microsoft/onnxruntime/issues/20464)[2](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html)
    try:
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
    except Exception:
        pass

    return ort.InferenceSession(str(onnx_path), sess_options=so, providers=providers)

def get_io_names(sess: ort.InferenceSession):
    in_names = [i.name for i in sess.get_inputs()]
    out_names = [o.name for o in sess.get_outputs()]
    return in_names, out_names

# ============================================================
# Local tokenizer loader (NO HuggingFace network access)
# ============================================================
def load_local_clip_tokenizer(model_root: Path, tokenizer_dir: str | None):
    tok_dir = Path(tokenizer_dir).expanduser().resolve() if tokenizer_dir else (model_root / "tokenizer")
    if not tok_dir.exists():
        raise FileNotFoundError(
            f"[Tokenizer] Directory not found: {tok_dir}\n"
            f"Please create it and put vocab.json + merges.txt inside."
        )
    vocab = tok_dir / "vocab.json"
    merges = tok_dir / "merges.txt"
    if not vocab.exists() or not merges.exists():
        raise FileNotFoundError(
            f"[Tokenizer] Missing files in {tok_dir}\n"
            f"Required: vocab.json and merges.txt"
        )
    print(f"[Tokenizer] Loading local tokenizer from: {tok_dir}")
    return CLIPTokenizer.from_pretrained(str(tok_dir), local_files_only=True)

# ============================================================
# SD2.1 ONNX Pipeline
# ============================================================
class SD21OnnxPipeline:
    def __init__(self, model_root: Path, providers, tokenizer_dir: str | None, vae_scaling_factor: float = 0.18215):
        self.model_root = model_root
        self.vae_scaling_factor = float(vae_scaling_factor)

        # ---------------- ONNX model paths ----------------
        self.text_encoder_path = find_single_model_file(
            model_root / "text_encoder",
            prefer_names=["model.onnx", "text_encoder.onnx", "model.bin", "text_encoder.bin"],
            allow_exts=(".onnx", ".bin"),
        )
        self.unet_path = find_single_model_file(
            model_root / "unet",
            prefer_names=["model.onnx", "unet.onnx", "model.bin", "unet.bin"],
            allow_exts=(".onnx", ".bin"),
        )
        self.vae_decoder_path = find_single_model_file(
            model_root / "vae_decoder",
            prefer_names=["model.onnx", "vae_decoder.onnx", "decoder.onnx", "model.bin", "vae_decoder.bin", "decoder.bin"],
            allow_exts=(".onnx", ".bin"),
        )

        for p in [self.text_encoder_path, self.unet_path, self.vae_decoder_path]:
            if not p.exists():
                raise FileNotFoundError(f"Missing ONNX model: {p}")

        # ---------------- ORT sessions ----------------
        self.sess_text = make_session(self.text_encoder_path, providers)
        self.sess_unet = make_session(self.unet_path, providers)
        self.sess_vae = make_session(self.vae_decoder_path, providers)

        self.text_in, _ = get_io_names(self.sess_text)
        self.unet_in, _ = get_io_names(self.sess_unet)
        self.vae_in, _ = get_io_names(self.sess_vae)

        # ---------------- Tokenizer (LOCAL ONLY) ----------------
        self.tokenizer = load_local_clip_tokenizer(model_root, tokenizer_dir)
        self.max_length = 77

        # ---------------- Scheduler ----------------
        self.scheduler = DPMSolverMultistepScheduler(
            num_train_timesteps=1000,
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
        )

    def _tokenize(self, prompt: str) -> np.ndarray:
        tokens = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.max_length,
            truncation=True,
            return_tensors="np",
        )
        return tokens["input_ids"].astype(np.int32)

    def _text_encode(self, input_ids: np.ndarray) -> np.ndarray:
        feed = {self.text_in[0]: input_ids}
        return self.sess_text.run(None, feed)[0]

    def _unet(self, latents_nhwc: np.ndarray, timestep: np.int64, text_emb: np.ndarray) -> np.ndarray:
        feeds = {}
        for name in self.unet_in:
            if "timestep" in name or name == "t":
                # Match timestep dtype to model expectation (some UNet exports expect float)
                inp = self.sess_unet.get_inputs()[self.unet_in.index(name)]
                ort_type = (inp.type or "").lower()
                if "float16" in ort_type:
                    t_dtype = np.float16
                elif "float" in ort_type:
                    t_dtype = np.float32
                elif "int64" in ort_type:
                    t_dtype = np.int64
                else:
                    t_dtype = np.int32
                feeds[name] = np.array([timestep], dtype=t_dtype)

            elif "encoder" in name or "hidden" in name or "text" in name:
                feeds[name] = text_emb.astype(np.float32)

            else:
                # latents/sample input
                sample = latents_nhwc.astype(np.float32)
                inp = self.sess_unet.get_inputs()[self.unet_in.index(name)]
                sample = ensure_4d_layout(sample, inp)
                feeds[name] = sample

        noise = self.sess_unet.run(None, feeds)[0]

        # Convert output to NHWC if it came out NCHW
        if noise.ndim == 4 and noise.shape[1] == 4:
            noise = nchw_to_nhwc(noise)
        return noise

    def _vae_decode(self, latents_nhwc: np.ndarray) -> np.ndarray:
        lat = latents_nhwc.astype(np.float32)

        # SD2.1 latent scaling: decode uses latents / 0.18215 by default
        # If your VAE graph already bakes scaling in, set --vae_scale 1.0 to disable.
        if self.vae_scaling_factor and self.vae_scaling_factor != 1.0:
            lat = lat / self.vae_scaling_factor

        inp = self.sess_vae.get_inputs()[0]
        lat = ensure_4d_layout(lat, inp)

        img = self.sess_vae.run(None, {self.vae_in[0]: lat})[0]

        if img.ndim == 4:
            img = img[0]
        if img.shape[0] in (3, 4):
            img = np.transpose(img, (1, 2, 0))

        if img.min() < 0:
            img = (img + 1.0) / 2.0
        img = np.clip(img, 0, 1)
        img = (img * 255).round().astype(np.uint8)
        return img

    @torch.no_grad()
    def __call__(self, prompt, negative_prompt, seed, steps, guidance, height, width):
        self.scheduler.set_timesteps(steps)

        cond_ids = self._tokenize(prompt)
        uncond_ids = self._tokenize(negative_prompt)

        cond_emb = self._text_encode(cond_ids)
        uncond_emb = self._text_encode(uncond_ids)

        g = torch.Generator(device="cpu").manual_seed(seed)
        latents = torch.randn((1, 4, height // 8, width // 8), generator=g).numpy()
        # keep internal latents in NHWC for easier handling, but we will convert on model input as needed
        latents = nchw_to_nhwc(latents)

        for i, t in enumerate(self.scheduler.timesteps):
            timestep = np.int64(t.item())
            noise_u = self._unet(latents, timestep, uncond_emb)
            noise_c = self._unet(latents, timestep, cond_emb)
            noise = noise_u + guidance * (noise_c - noise_u)

            latents = nchw_to_nhwc(
                self.scheduler.step(
                    torch.from_numpy(nhwc_to_nchw(noise)),
                    t,
                    torch.from_numpy(nhwc_to_nchw(latents)),
                ).prev_sample.numpy()
            )
            print(f"[{i+1:02d}/{steps}] timestep={int(timestep)}")

        img = self._vae_decode(latents)
        return Image.fromarray(img, "RGB")

# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str, default="spectacular view of northern lights from Alaska")
    parser.add_argument(
        "--negative",
        type=str,
        default="lowres, text, error, cropped, worst quality, low quality, jpeg artifacts, signature, watermark",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--guidance", type=float, default=7.5)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--out", type=str, default="sd21_out.png")
    parser.add_argument("--provider", type=str, default="cpu", choices=["cpu", "cuda", "dml"])
    parser.add_argument("--tokenizer_dir", type=str, default=None, help="Path to local tokenizer directory")
    parser.add_argument("--vae_scale", type=float, default=0.18215,
                        help="VAE latent scaling_factor (default 0.18215). Use 1.0 to disable.")
    parser.add_argument(
        "--model_root",
        type=str,
        default=None,
        help="Root directory of ONNX models (contains text_encoder/, unet/, vae_decoder/). "
             "If not set, defaults to the script directory.",
    )
    args = parser.parse_args()

    # Provider selection
    if args.provider == "cuda":
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    elif args.provider == "dml":
        providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
    else:
        providers = ["CPUExecutionProvider"]

    script_dir = Path(__file__).resolve().parent
    model_root = Path(args.model_root).expanduser().resolve() if args.model_root else script_dir
    print(f"[ModelRoot] Using model root: {model_root}")

    pipe = SD21OnnxPipeline(
        model_root=model_root,
        providers=providers,
        tokenizer_dir=args.tokenizer_dir,
        vae_scaling_factor=args.vae_scale,
    )

    image = pipe(
        prompt=args.prompt,
        negative_prompt=args.negative,
        seed=args.seed if args.seed >= 0 else int(np.random.randint(0, 2**31 - 1)),
        steps=args.steps,
        guidance=args.guidance,
        height=args.height,
        width=args.width,
    )

    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    print(f"[OK] Saved image to: {out_path}")

if __name__ == "__main__":
    main()