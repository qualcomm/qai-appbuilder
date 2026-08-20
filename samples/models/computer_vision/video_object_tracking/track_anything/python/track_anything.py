# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "shared", "python"))
import install
import cv2
import numpy as np
import zipfile
from qai_appbuilder import OnnxRuntimeContext, PerfProfile

# Global variables for ROI selection
roi_points = []
selecting = False

# Root directory (the folder containing this script); videos and models are relative to it
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Model path: the extracted onnx files are placed under <root>/models
MODEL_DIR = os.path.join(ROOT_DIR, "models") + os.sep
# Video path: input videos are looked up in the root directory (no sub-directory)
VIDEO_DIR = ROOT_DIR + os.sep

# Track-Anything ONNX model download URL (Qualcomm AI Hub Models public asset bucket)
MODEL_ZIP_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/track_anything/releases/v0.58.0/track_anything-onnx-float.zip"
)
# The 4 onnx models required for inference (used to check whether the download is complete)
REQUIRED_ONNX = [
    "encode_key_with_shrinkage.onnx",
    "encode_key_without_shrinkage.onnx",
    "encode_value.onnx",
    "segment.onnx",
]

# Default input video (from the Qualcomm AI Hub Models public video_classifier assets).
# If the user does not select a video, this one is downloaded and used as the default input.
DEFAULT_VIDEO_NAME = "surfing_cutback.mp4"
DEFAULT_VIDEO_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/video_classifier/v1/surfing_cutback.mp4"
)


def ensure_models():
    """Ensure the 4 onnx models are present in MODEL_DIR; otherwise download the zip, extract it, and delete the zip."""
    os.makedirs(MODEL_DIR, exist_ok=True)
    if all(os.path.exists(os.path.join(MODEL_DIR, n)) for n in REQUIRED_ONNX):
        print(f"Models already exist: {MODEL_DIR}")
        return

    zip_path = os.path.join(MODEL_DIR, "track_anything-onnx-float.zip")
    print(f"Complete model set not found, downloading:\n  {MODEL_ZIP_URL}")
    fail = (f"\nFailed to download the Track-Anything model. "
            f"Please download it manually from {MODEL_ZIP_URL} and place it at {zip_path}.")
    ret = install.download_url(MODEL_ZIP_URL, zip_path, fail=fail)
    if not ret:
        exit()

    # Extract: flatten all files inside the zip (onnx / data / json) into MODEL_DIR
    print(f"Extracting to: {MODEL_DIR}")
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.infolist():
                if member.is_dir():
                    continue
                fname = os.path.basename(member.filename)
                if not fname:
                    continue
                with z.open(member) as src, \
                        open(os.path.join(MODEL_DIR, fname), "wb") as dst:
                    dst.write(src.read())
    finally:
        # Delete the downloaded zip file
        if os.path.exists(zip_path):
            os.remove(zip_path)
            print(f"Deleted downloaded archive: {zip_path}")

    missing = [n for n in REQUIRED_ONNX if not os.path.exists(os.path.join(MODEL_DIR, n))]
    if missing:
        raise FileNotFoundError(f"Model files still missing after extraction: {missing}")
    print("Models are ready.")


def ensure_default_video():
    """Ensure the default video exists in the root directory, downloading it if absent. Returns the local path."""
    local_path = os.path.join(VIDEO_DIR, DEFAULT_VIDEO_NAME)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        print(f"Default video already exists: {local_path}")
        return local_path

    os.makedirs(VIDEO_DIR, exist_ok=True)
    print(f"Default video not found, downloading from:\n  {DEFAULT_VIDEO_URL}")
    fail = (f"\nFailed to download the default video. "
            f"Please download it manually from {DEFAULT_VIDEO_URL} and place it at {local_path}.")
    ret = install.download_url(DEFAULT_VIDEO_URL, local_path, fail=fail)
    if not ret:
        exit()
    print(f"Download complete: {local_path}")
    return local_path


def play_video(path):
    """Open the given video in the OS default player (cross-platform, best-effort)."""
    if not os.path.exists(path):
        print(f"[WARN] Cannot play, file not found: {path}")
        return
    print(f"Opening output video in the default player: {path}")
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]  # Windows only
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"[WARN] Failed to auto-play the video ({e}). "
              f"Please open it manually: {path}")


# Use matplotlib for point selection (on Windows on ARM only opencv-python-headless is
# available, which has no GUI backend, so cv2.imshow/namedWindow cannot be used)
def select_points_matplotlib(first_frame):
    """Click to select tracking points on the first frame; close the window or press ENTER to confirm. Returns [(x, y), ...]."""
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt

    points = []
    # cv2 reads frames as BGR, matplotlib expects RGB
    frame_rgb = cv2.cvtColor(first_frame, cv2.COLOR_BGR2RGB)

    fig, ax = plt.subplots()
    ax.imshow(frame_rgb)
    ax.set_title("Click the target(s) to track, then press ENTER or close the window")

    def on_click(event):
        if event.inaxes != ax or event.xdata is None:
            return
        x, y = int(round(event.xdata)), int(round(event.ydata))
        points.append((x, y))
        ax.plot(x, y, "o", color="lime", markersize=8)
        ax.annotate(str(len(points)), (x + 10, y), color="lime")
        fig.canvas.draw_idle()
        print(f"Selected point {len(points)}: ({x}, {y})")

    def on_key(event):
        if event.key == "enter":
            plt.close(fig)

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show()  # blocks until the window is closed
    return points

# Generate an initial mask from point coordinates (simplified: Gaussian-blurred points)
def generate_initial_mask_from_points(points, frame_h, frame_w, target_h=320, target_w=576):
    """Generate an initial mask from point coordinates"""
    mask = np.zeros((frame_h, frame_w), dtype=np.float32)

    # Draw a circular region around each point
    for x, y in points:
        cv2.circle(mask, (x, y), radius=30, color=1.0, thickness=-1)

    # Apply Gaussian blur to smooth the boundary
    mask = cv2.GaussianBlur(mask, (31, 31), 0)
    mask = np.clip(mask, 0, 1)

    # Resize to the model input size
    mask = cv2.resize(mask, (target_w, target_h))
    return np.expand_dims(mask, axis=0)  # shape [1, 320, 576]

# Image preprocessing: resize, normalize, convert to RGB, NCHW format
def preprocess_image(image_path, frame=None, target_height=320, target_width=576):
    if frame is not None:
        img = frame
    else:
        img = cv2.imread(image_path)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    orig_h, orig_w = img_rgb.shape[:2]

    # Aspect-ratio-preserving resize
    scale = min(target_height / orig_h, target_width / orig_w)
    new_h, new_w = int(orig_h * scale), int(orig_w * scale)
    img_resized = cv2.resize(img_rgb, (new_w, new_h))

    # Pad to the target size
    pad_h = target_height - new_h
    pad_w = target_width - new_w
    img_padded = cv2.copyMakeBorder(img_resized, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=0)

    # Normalize to [0, 1] and convert to NCHW format
    img_tensor = img_padded.astype(np.float32) / 255.0
    img_tensor = np.transpose(img_tensor, (2, 0, 1))
    img_tensor = np.expand_dims(img_tensor, axis=0)

    return img_tensor, orig_h, orig_w, new_h, new_w

# Mask post-processing
def postprocess_mask(mask_tensor, orig_h, orig_w, new_h, new_w):
    # Remove the padded region
    mask = mask_tensor[1, :new_h, :new_w]
    # Resize to the original size
    mask = cv2.resize(mask, (orig_w, orig_h))
    # Binarize
    mask = (mask > 0.5).astype(np.uint8) * 255
    return mask

class TrackAnythingHTP:
    def __init__(self):
        # Initialize the OnnxRuntimeContext HTP runtime (use_cpu=False automatically uses HTP acceleration)
        self.runtime_ek_shrink = OnnxRuntimeContext(
            "encode_key_with_shrinkage",
            MODEL_DIR + "encode_key_with_shrinkage.onnx",
            use_cpu=False
        )
        self.runtime_ek_noshrink = OnnxRuntimeContext(
            "encode_key_without_shrinkage",
            MODEL_DIR + "encode_key_without_shrinkage.onnx",
            use_cpu=False
        )
        self.runtime_ev = OnnxRuntimeContext(
            "encode_value",
            MODEL_DIR + "encode_value.onnx",
            use_cpu=False
        )
        self.runtime_seg = OnnxRuntimeContext(
            "segment",
            MODEL_DIR + "segment.onnx",
            use_cpu=False
        )

        self.hidden_state = None
        self.num_objects = 1

        # ---- Memory bank (XMem working memory) ----
        # Each memory frame stores: memory key (CK,HW), shrinkage (HW,), value (CV,HW)
        self.mem_keys = []      # list of (CK, HW)
        self.mem_shrink = []    # list of (HW,)
        self.mem_values = []    # list of (CV, HW)
        self.mem_is_anchor = [] # whether it is the first-frame anchor (never evicted)
        self.frame_idx = 0
        self.mem_every = 5      # write to the memory bank every N frames
        self.max_frames = 8     # memory bank capacity limit (including the anchor)

    def _add_memory(self, key, shrinkage, value, is_anchor=False):
        """Write a frame's memory key / shrinkage / value into the memory bank and maintain capacity."""
        CK, H, W = key.shape[1], key.shape[2], key.shape[3]
        HW = H * W
        CV = value.shape[2]
        self.mem_keys.append(key.reshape(CK, HW).astype(np.float32))
        self.mem_shrink.append(shrinkage.reshape(HW).astype(np.float32))
        self.mem_values.append(value.reshape(CV, HW).astype(np.float32))
        self.mem_is_anchor.append(is_anchor)

        # When capacity is exceeded, evict the earliest non-anchor frame
        while len(self.mem_keys) > self.max_frames:
            drop = next((i for i, a in enumerate(self.mem_is_anchor) if not a), None)
            if drop is None:
                break
            for lst in (self.mem_keys, self.mem_shrink, self.mem_values, self.mem_is_anchor):
                del lst[drop]

    def _memory_readout(self, qk, qe):
        """XMem affinity matching: match the current frame's query key/selection against the memory bank and read out the value.

        qk: query key         (1, CK, H, W)
        qe: query selection    (1, CK, H, W)  per-channel anisotropic weights
        returns memory_readout (1, 1, CV, H, W)
        """
        CK, H, W = qk.shape[1], qk.shape[2], qk.shape[3]
        HW = H * W
        QK = qk.reshape(CK, HW).astype(np.float32)   # (CK, HW)
        QE = qe.reshape(CK, HW).astype(np.float32)   # (CK, HW)

        MK = np.concatenate(self.mem_keys, axis=1)    # (CK, N)
        MS = np.concatenate(self.mem_shrink, axis=0)  # (N,)
        MV = np.concatenate(self.mem_values, axis=1)  # (CV, N)

        # Anisotropic L2 similarity (larger is more similar); the selection-weighted form of -||mk-qk||^2
        MKt = MK.T                                    # (N, CK)
        a_sq = (MKt ** 2) @ QE                        # (N, HW)
        two_ab = 2.0 * (MKt @ (QK * QE))              # (N, HW)
        b_sq = (QE * QK ** 2).sum(axis=0, keepdims=True)  # (1, HW)
        sim = (-a_sq + two_ab - b_sq)                 # (N, HW)
        sim = sim * MS[:, None] / np.sqrt(CK)         # shrinkage scaling

        # Softmax over the memory dimension N to obtain the affinity
        sim -= sim.max(axis=0, keepdims=True)
        aff = np.exp(sim)
        aff /= (aff.sum(axis=0, keepdims=True) + 1e-8)  # (N, HW)

        # value readout: (CV, N) @ (N, HW) = (CV, HW)
        readout = MV @ aff                            # (CV, HW)
        return readout.reshape(1, 1, readout.shape[0], H, W).astype(np.float32)

    def initialize_first_frame(self, image_path, initial_mask, frame=None):
        """Initialize the first frame with the given initial mask; supports passing a frame directly or reading from a file."""
        if frame is not None:
            img_tensor, orig_h, orig_w, new_h, new_w = preprocess_image(None, frame)
        else:
            img_tensor, orig_h, orig_w, new_h, new_w = preprocess_image(image_path)

        # Set the HTP performance mode to BURST
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

        # Anchor-frame memory key encoding (with shrinkage, for affinity matching in later frames)
        outputs = self.runtime_ek_shrink.Inference([img_tensor])
        key, shrinkage, selection, f16 = outputs[0], outputs[1], outputs[2], outputs[3]
        print(f"DEBUG init: key shape: {key.shape}, shrinkage shape: {shrinkage.shape}")

        # Encode the value, initialize the hidden state, and write into the memory bank
        hidden_state_init = np.zeros((1, self.num_objects, 64, 20, 36), dtype=np.float32)
        outputs = self.runtime_ev.Inference([img_tensor, initial_mask, f16, hidden_state_init])
        pred_prob, value, hidden = outputs[0], outputs[1], outputs[2]
        print(f"DEBUG init: value shape: {value.shape}, hidden shape: {hidden.shape}")

        self.hidden_state = hidden
        self._add_memory(key, shrinkage, value, is_anchor=True)
        self.frame_idx = 0

        # Release the HTP performance mode
        PerfProfile.RelPerfProfileGlobal()

        return postprocess_mask(pred_prob, orig_h, orig_w, new_h, new_w)

    def track_next_frame(self, image_path, frame=None):
        """Track a subsequent frame; supports passing a frame directly or reading from a file."""
        if frame is not None:
            img_tensor, orig_h, orig_w, new_h, new_w = preprocess_image(None, frame)
        else:
            img_tensor, orig_h, orig_w, new_h, new_w = preprocess_image(image_path)

        # Set the HTP performance mode to BURST
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

        # Encode the current frame's query key (without shrinkage) and get the multi-scale features needed by the decoder
        outputs = self.runtime_ek_noshrink.Inference([img_tensor])
        qk, qe, f16, f8, f4 = outputs[0], outputs[1], outputs[2], outputs[3], outputs[4]

        # Affinity matching: read out the value corresponding to the current frame's target from the memory bank
        memory_readout = self._memory_readout(qk, qe)

        #print(f"DEBUG track: qk shape: {qk.shape}, mem frames: {len(self.mem_keys)}, "
        #      f"readout shape: {memory_readout.shape}")

        # Segmentation
        outputs = self.runtime_seg.Inference([f16, f8, f4, memory_readout, self.hidden_state])
        pred_prob, hidden = outputs[0], outputs[1]
        self.hidden_state = hidden
        self.frame_idx += 1

        # Periodically write the current frame into the memory bank (using the predicted mask as this frame's value basis)
        if self.frame_idx % self.mem_every == 0:
            pred_prob_input = pred_prob[1:].astype(np.float32)  # (1, 320, 576) target probability
            outputs = self.runtime_ek_shrink.Inference([img_tensor])
            m_key, m_shrink = outputs[0], outputs[1]
            outputs = self.runtime_ev.Inference([img_tensor, pred_prob_input, f16, self.hidden_state])
            _, new_value, ev_hidden = outputs[0], outputs[1], outputs[2]
            self._add_memory(m_key, m_shrink, new_value)
            self.hidden_state = ev_hidden  # deep update of the sensory memory by the value encoder

        # Release the HTP performance mode
        PerfProfile.RelPerfProfileGlobal()

        return postprocess_mask(pred_prob, orig_h, orig_w, new_h, new_w)

    def release(self):
        """Release runtime resources"""
        del self.runtime_ek_shrink
        del self.runtime_ek_noshrink
        del self.runtime_ev
        del self.runtime_seg

if __name__ == "__main__":
    # Ensure the models are in place (download the zip, extract to models/ and delete the zip if absent)
    ensure_models()

    # List the available videos in the root directory (excluding tracking result outputs)
    videos = [f for f in os.listdir(VIDEO_DIR)
              if f.lower().endswith('.mp4') and not f.startswith('tracked_')]
    print("Available videos:")
    for i, v in enumerate(videos):
        print(f"[{i+1}] {v}")
    print("(Press ENTER without a selection to use the default surfing_cutback.mp4)")

    raw = input("Enter the number of the video to process: ").strip()
    if raw == "" or len(videos) == 0:
        # User made no selection (or no video in root): use (download if needed) the default video
        video_path = ensure_default_video()
        print(f"Using default video: {video_path}")
    else:
        try:
            sel = int(raw) - 1
            if not (0 <= sel < len(videos)):
                raise ValueError
            video_path = os.path.join(VIDEO_DIR, videos[sel])
        except ValueError:
            print("Invalid input, using the default video: surfing_cutback.mp4")
            video_path = ensure_default_video()

    video_name = os.path.basename(video_path)
    output_path = os.path.join(VIDEO_DIR, f"tracked_{video_name}")

    # Open the video
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Video info: {frame_w}x{frame_h}, {fps}fps, {total_frames} frames total")

    # Read the first frame to select the ROI
    ret, first_frame = cap.read()
    if not ret:
        print("Failed to read the video")
        exit()

    # Point selection window (uses matplotlib, compatible with opencv-python-headless)
    print("\nInstructions:")
    print("1. Click the target you want to track in the pop-up window (you can click multiple points)")
    print("2. Press ENTER or close the window to confirm your selection")
    print("3. For the surfing video, click on the surfer's body")
    roi_points = select_points_matplotlib(first_frame)

    if len(roi_points) == 0:
        print("No point selected")
        exit()
    print(f"Selected {len(roi_points)} points: {roi_points}")

    # Generate the initial mask
    initial_mask = generate_initial_mask_from_points(roi_points, frame_h, frame_w)

    # Initialize the tracker
    print("Loading models to HTP...")
    tracker = TrackAnythingHTP()

    # Initialize the first frame
    print("Initializing first-frame tracking...")
    first_mask = tracker.initialize_first_frame(None, initial_mask, first_frame)

    # Create the output video
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (frame_w, frame_h))

    # Overlay the first-frame result
    first_frame[first_mask > 0] = first_frame[first_mask > 0] * 0.5 + np.array([0, 0, 255], dtype=np.uint8) * 0.5
    out.write(first_frame)

    # Track frame by frame
    print("Tracking started... press q to exit early")
    frame_count = 1
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Track the current frame
        mask = tracker.track_next_frame(None, frame)

        # Overlay the mask onto the original frame (semi-transparent red)
        frame[mask > 0] = frame[mask > 0] * 0.5 + np.array([0, 0, 255], dtype=np.uint8) * 0.5

        # Write the output
        out.write(frame)

        # Show progress
        frame_count += 1
        if frame_count % 10 == 0:
            print(f"Progress: {frame_count}/{total_frames} ({frame_count/total_frames*100:.1f}%)")

        # Real-time preview (headless OpenCV has no GUI, skipped; results are still written to the output video)
        # cv2.imshow("Tracking (press q to exit)", frame)
        # if cv2.waitKey(1) & 0xFF == ord('q'):
        #     break

    # Release resources
    cap.release()
    out.release()
    # cv2.destroyAllWindows()  # headless OpenCV has no GUI, skipped
    tracker.release()

    print(f"\nTracking complete! Output video saved to: {output_path}")
    print("Track-Anything HTP inference finished")

    # Auto-play the resulting output video in the OS default player
    play_video(output_path)
