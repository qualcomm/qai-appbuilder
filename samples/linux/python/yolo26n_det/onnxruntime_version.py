import argparse

import cv2
import numpy as np
import onnxruntime as ort


IMAGE_SIZE = 640  # model expects [1,3,640,640]

# define class type (COCO 80)
class_map = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    9: "traffic light",
    10: "fire hydrant",
    11: "stop sign",
    12: "parking meter",
    13: "bench",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    29: "frisbee",
    30: "skis",
    31: "snowboard",
    32: "sports ball",
    33: "kite",
    34: "baseball bat",
    35: "baseball glove",
    36: "skateboard",
    37: "surfboard",
    38: "tennis racket",
    39: "bottle",
    40: "wine glass",
    41: "cup",
    42: "fork",
    43: "knife",
    44: "spoon",
    45: "bowl",
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
    56: "chair",
    57: "couch",
    58: "potted plant",
    59: "bed",
    60: "dining table",
    61: "toilet",
    62: "tv",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    68: "microwave",
    69: "oven",
    70: "toaster",
    71: "sink",
    72: "refrigerator",
    73: "book",
    74: "clock",
    75: "vase",
    76: "scissors",
    77: "teddy bear",
    78: "hair drier",
    79: "toothbrush"
}


def _select_providers() -> list[str]:
    available = ort.get_available_providers()
    preferred = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return [p for p in preferred if p in available] or available


def _load_image(image_path: str) -> tuple[np.ndarray, np.ndarray]:
    img0 = cv2.imread(image_path)
    if img0 is None:
        raise FileNotFoundError(f"Failed to read image: {image_path}")

    img = cv2.resize(img0, (IMAGE_SIZE, IMAGE_SIZE), interpolation=cv2.INTER_LINEAR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[None]  # NCHW
    return img0, img


def _draw_xyxy(
    frame_bgr: np.ndarray,
    xyxy_640: np.ndarray,
    score: float,
    class_id: int,
    color: tuple[int, int, int] = (0, 255, 0),
    thickness: int = 2,
) -> None:
    h, w = frame_bgr.shape[:2]
    scale_x = w / IMAGE_SIZE
    scale_y = h / IMAGE_SIZE

    x1, y1, x2, y2 = xyxy_640.tolist()
    x1 = int(max(0.0, min(IMAGE_SIZE, x1)) * scale_x)
    y1 = int(max(0.0, min(IMAGE_SIZE, y1)) * scale_y)
    x2 = int(max(0.0, min(IMAGE_SIZE, x2)) * scale_x)
    y2 = int(max(0.0, min(IMAGE_SIZE, y2)) * scale_y)

    cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, thickness)
    class_name = class_map.get(class_id, "Unknown")
    label = f"{score:.2f} {class_name}"
    cv2.putText(
        frame_bgr,
        label,
        (x1, max(0, y1 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
        cv2.LINE_AA,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="YOLO26 ONNXRuntime inference (no NMS).")
    parser.add_argument("--model", default="yolo26n.onnx", help="Path to YOLO26 ONNX model")
    parser.add_argument("--image", default="input.jpg", help="Path to input image")
    parser.add_argument("--output", default="output.jpg", help="Path to save visualization")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    args = parser.parse_args()

    providers = _select_providers()
    try:
        sess = ort.InferenceSession(args.model, providers=providers)
    except Exception:
        sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
        providers = ["CPUExecutionProvider"]

    input_name = sess.get_inputs()[0].name

    img0, inp = _load_image(args.image)
    (out,) = sess.run(None, {input_name: inp})

    det = np.asarray(out, dtype=np.float32)[0]  # [300,6]
    scores = det[:, 4]
    keep = scores >= float(args.conf)
    det = det[keep]
    det = det[np.argsort(-det[:, 4])]

    for x1, y1, x2, y2, score, cls in det:
        _draw_xyxy(img0, np.array([x1, y1, x2, y2], dtype=np.float32), float(score), int(cls))

    if not cv2.imwrite(args.output, img0):
        raise RuntimeError(f"Failed to write image: {args.output}")

    print(f"onnxruntime={ort.__version__} providers={providers}")
    print(f"detections={len(det)} saved={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
