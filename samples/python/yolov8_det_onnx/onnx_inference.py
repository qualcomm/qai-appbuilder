
from qai_appbuilder.aipc import patch_onnxruntime_to_qnn
patch_onnxruntime_to_qnn()
import onnxruntime as ort
import numpy as np
import cv2
import time


# ------------------------- basic utils -------------------------
def preprocess(image_path, input_size=(640, 640)):
    start_time = time.time()
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Image not found at {image_path}")

    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img_resized = cv2.resize(img_rgb, input_size)

    img_data = img_resized.astype(np.float32) / 255.0
    img_data = np.transpose(img_data, (2, 0, 1))  # HWC -> CHW
    img_data = np.expand_dims(img_data, axis=0)   # add batch

    elapsed_time = (time.time() - start_time) * 1000
    return img_data, img, elapsed_time


def sigmoid(x):
    x = np.asarray(x, dtype=np.float32)
    return 1.0 / (1.0 + np.exp(-x))


def class_id_name(class_id):
    # COCO 80 classes (YOLOv8 default)
    classes = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
        "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
        "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
        "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
        "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush"
    ]
    cid = int(class_id)
    if 0 <= cid < len(classes):
        return classes[cid]
    return str(cid)


def inference_all_outputs(session, input_data, debug=True):
    start_time = time.time()
    input_name = session.get_inputs()[0].name

    output_names = [o.name for o in session.get_outputs()]
    outputs = session.run(output_names, {input_name: input_data})
    elapsed_time = (time.time() - start_time) * 1000

    out_map = {n: np.asarray(t) for n, t in zip(output_names, outputs)}

    if debug:
        print("[DEBUG] output names:", output_names)
        for n in output_names:
            print(f"[DEBUG] output {n} shape: {out_map[n].shape}")

    return out_map, elapsed_time


def squeeze_batch(a):
    a = np.asarray(a)
    if a.ndim >= 1 and a.shape[0] == 1:
        return a[0]
    return a


# ------------------------- mode A: QNN 3-outputs (boxes/scores/class_idx) -------------------------
def postprocess_qnn_3outputs(out_map, original_img, input_size=(640, 640),
                            conf_threshold=0.25, iou_threshold=0.45, debug=True):
    # YOLOv8 sometimes produces 3 output tensors (boxes, scores, class indices) in pipelines.
    boxes = squeeze_batch(out_map["boxes"])        # (N,4)
    scores = squeeze_batch(out_map["scores"])      # (N,)
    class_idx = squeeze_batch(out_map["class_idx"])  # (N,)

    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    class_idx = np.asarray(class_idx, dtype=np.int32).reshape(-1)

    if debug:
        print("[DEBUG] QNN3: boxes/scores/class_idx:", boxes.shape, scores.shape, class_idx.shape)
        print(f"[DEBUG] QNN3: scores range min={scores.min():.6f}, max={scores.max():.6f}")

    original_h, original_w = original_img.shape[:2]
    x_factor = original_w / input_size[0]
    y_factor = original_h / input_size[1]

    # IMPORTANT: many pipelines export boxes as XYXY already (xmin,ymin,xmax,ymax). Others may be XYWH.
    # We'll infer format by checking if (x2>x1,y2>y1) holds for most boxes.
    dx = boxes[:, 2] - boxes[:, 0]
    dy = boxes[:, 3] - boxes[:, 1]
    xyxy_ratio = np.mean((dx > 0) & (dy > 0))
    fmt = "xyxy" if xyxy_ratio > 0.7 else "xywh_center"
    if debug:
        print("[DEBUG] QNN3: inferred boxes format:", fmt)

    if fmt == "xyxy":
        x1 = boxes[:, 0] * x_factor
        y1 = boxes[:, 1] * y_factor
        x2 = boxes[:, 2] * x_factor
        y2 = boxes[:, 3] * y_factor
    else:
        x = boxes[:, 0] * x_factor
        y = boxes[:, 1] * y_factor
        w = boxes[:, 2] * x_factor
        h = boxes[:, 3] * y_factor
        x1 = x - w / 2.0
        y1 = y - h / 2.0
        x2 = x + w / 2.0
        y2 = y + h / 2.0

    keep = scores >= conf_threshold
    if debug:
        print(f"[DEBUG] QNN3: candidates after conf filter: {int(np.sum(keep))} (conf={conf_threshold})")
    if not np.any(keep):
        return original_img.copy(), 0, 0.0

    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    s = scores[keep].tolist()
    c = class_idx[keep]

    rects = []
    for i in range(len(s)):
        left = int(x1[i])
        top = int(y1[i])
        width = int(x2[i] - x1[i])
        height = int(y2[i] - y1[i])
        rects.append([left, top, width, height])

    indices = cv2.dnn.NMSBoxes(rects, s, float(conf_threshold), float(iou_threshold))
    result_img = original_img.copy()
    if len(indices) > 0:
        if isinstance(indices, np.ndarray):
            indices = indices.flatten()
        for idx in indices:
            left, top, width, height = rects[idx]
            right = left + max(0, width)
            bottom = top + max(0, height)

            left = max(0, min(original_w - 1, left))
            top = max(0, min(original_h - 1, top))
            right = max(0, min(original_w - 1, right))
            bottom = max(0, min(original_h - 1, bottom))

            cv2.rectangle(result_img, (left, top), (right, bottom), (0, 255, 0), 2)
            label = f"{class_id_name(c[idx])}: {s[idx]:.2f}"
            cv2.putText(result_img, label, (left, max(0, top - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        return result_img, len(indices), 0.0

    return result_img, 0, 0.0


# ------------------------- mode B: ORT single output0 (1,84,8400) -------------------------
def postprocess_output0_yolov8(out0, original_img, input_size=(640, 640),
                              conf_threshold=0.25, iou_threshold=0.45, debug=True):
    """
    Standard YOLOv8 ONNX often returns output0 shape (1,84,8400).
    Format: [x,y,w,h, cls0..cls79] (84 = 4 + 80). (Some exports include obj; yours is 84 so no obj.)
    """
    start_time = time.time()

    out = np.asarray(out0)
    if out.ndim != 3 or out.shape[0] != 1:
        raise ValueError(f"Unexpected output0 shape: {out.shape}")
    # (1,84,8400) -> (8400,84)
    out = out[0].transpose(1, 0)

    if debug:
        print("[DEBUG] output0 normalized:", out.shape,
              f"range min={float(out.min()):.6f}, max={float(out.max()):.6f}")

    boxes_xywh = out[:, 0:4].astype(np.float32)
    cls_raw = out[:, 4:].astype(np.float32)  # (8400,80)

    # If class scores are logits (range not in [0,1]), apply sigmoid
    cls_min, cls_max = float(cls_raw.min()), float(cls_raw.max())
    if debug:
        print(f"[DEBUG] output0 cls range before activation: min={cls_min:.6f}, max={cls_max:.6f}")
    if cls_max > 1.5 or cls_min < -0.5:
        cls = sigmoid(cls_raw)
        if debug:
            print("[DEBUG] output0: applied sigmoid to cls scores")
    else:
        cls = cls_raw

    class_ids = np.argmax(cls, axis=1).astype(np.int32)
    scores = cls[np.arange(cls.shape[0]), class_ids].astype(np.float32)

    original_h, original_w = original_img.shape[:2]
    x_factor = original_w / input_size[0]
    y_factor = original_h / input_size[1]

    x = boxes_xywh[:, 0]
    y = boxes_xywh[:, 1]
    w = boxes_xywh[:, 2]
    h = boxes_xywh[:, 3]

    left = ((x - w / 2) * x_factor).astype(np.int32)
    top = ((y - h / 2) * y_factor).astype(np.int32)
    width = (w * x_factor).astype(np.int32)
    height = (h * y_factor).astype(np.int32)

    keep = scores >= conf_threshold
    if debug:
        print(f"[DEBUG] output0: candidates after conf filter: {int(np.sum(keep))} (conf={conf_threshold})")
    if not np.any(keep):
        elapsed_time = (time.time() - start_time) * 1000
        return original_img.copy(), 0, elapsed_time

    rects = []
    sc = []
    cid = []
    for i in np.where(keep)[0]:
        rects.append([int(left[i]), int(top[i]), int(width[i]), int(height[i])])
        sc.append(float(scores[i]))
        cid.append(int(class_ids[i]))

    indices = cv2.dnn.NMSBoxes(rects, sc, float(conf_threshold), float(iou_threshold))

    result_img = original_img.copy()
    if len(indices) > 0:
        if isinstance(indices, np.ndarray):
            indices = indices.flatten()
        for idx in indices:
            l, t, ww, hh = rects[idx]
            r = l + max(0, ww)
            b = t + max(0, hh)

            l = max(0, min(original_w - 1, l))
            t = max(0, min(original_h - 1, t))
            r = max(0, min(original_w - 1, r))
            b = max(0, min(original_h - 1, b))

            cv2.rectangle(result_img, (l, t), (r, b), (0, 255, 0), 2)
            label = f"{class_id_name(cid[idx])}: {sc[idx]:.2f}"
            cv2.putText(result_img, label, (l, max(0, t - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        elapsed_time = (time.time() - start_time) * 1000
        return result_img, len(indices), elapsed_time

    elapsed_time = (time.time() - start_time) * 1000
    return result_img, 0, elapsed_time


# ------------------------- main -------------------------
def main():
    model_path = "yolov8n.onnx"
    image_path = "input.jpg"
    output_path = "output_onnx.jpg"

    session = ort.InferenceSession(model_path)

    # 1) preprocess
    input_data, original_img, pre_time = preprocess(image_path)

    # 2) inference (all outputs)
    out_map, inf_time = inference_all_outputs(session, input_data, debug=True)

    # 3) dispatch postprocess depending on outputs
    post_t0 = time.time()
    if all(k in out_map for k in ("boxes", "scores", "class_idx")):
        # QNN wrapper style: 3 output tensors (boxes/scores/class_idx).
        result_img, num_dets, _ = postprocess_qnn_3outputs(
            out_map, original_img,
            input_size=(640, 640),
            conf_threshold=0.25,
            iou_threshold=0.45,
            debug=True
        )
    elif "output0" in out_map and out_map["output0"].shape == (1, 84, 8400):
        # ORT standard: single output0 (1,84,8400).
        result_img, num_dets, _ = postprocess_output0_yolov8(
            out_map["output0"], original_img,
            input_size=(640, 640),
            conf_threshold=0.25,
            iou_threshold=0.45,
            debug=True
        )
    else:
        raise ValueError(f"Unknown outputs: {list(out_map.keys())} with shapes {[out_map[k].shape for k in out_map]}")

    post_time = (time.time() - post_t0) * 1000

    # Save
    cv2.imwrite(output_path, result_img)
    print(f"Results saved to {output_path}")

    print(f"\n--- Timing Results ---")
    print(f"Preprocess: {pre_time:.2f} ms")
    print(f"Inference: {inf_time:.2f} ms")
    print(f"Postprocess: {post_time:.2f} ms")
    print(f"Total: {pre_time + inf_time + post_time:.2f} ms")
    print(f"----------------------")
    print(f"Detected {num_dets} objects.")


if __name__ == "__main__":
    main()