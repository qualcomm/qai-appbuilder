//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/** YOLOX image preprocessing and decoding for a UINT8 w8a8 model. */
public final class YoloXDetector {
    public static final int MODEL_SIZE = 640;
    public static final float DEFAULT_CONFIDENCE = 0.35f;
    private static final float NMS_IOU = 0.45f;
    private static final int MAX_DETECTIONS = 20;

    private YoloXDetector() {}

    public static final class Transform {
        public final float scale;
        public final float padX;
        public final float padY;

        public Transform(float scale, float padX, float padY) {
            this.scale = scale;
            this.padX = padX;
            this.padY = padY;
        }
    }

    public static final class PreprocessedImage {
        public final byte[] rgb;
        public final Transform transform;

        private PreprocessedImage(byte[] rgb, Transform transform) {
            this.rgb = rgb;
            this.transform = transform;
        }
    }

    public static final class Detection {
        public final float left;
        public final float top;
        public final float right;
        public final float bottom;
        public final float score;
        public final int classIndex;

        private Detection(float left, float top, float right, float bottom,
                          float score, int classIndex) {
            this.left = left;
            this.top = top;
            this.right = right;
            this.bottom = bottom;
            this.score = score;
            this.classIndex = classIndex;
        }
    }

    /** Quantization metadata read from the loaded model's output tensor. */
    public static final class Quantization {
        public final float scale;
        public final int zeroPoint;

        public Quantization(float scale, int zeroPoint) {
            if (scale <= 0.0f) {
                throw new IllegalArgumentException("Tensor quantization scale must be positive");
            }
            this.scale = scale;
            this.zeroPoint = zeroPoint;
        }
    }

    /** Converts ARGB pixels to the model's letterboxed, RGB UINT8 input. */
    public static PreprocessedImage preprocess(int[] argb, int width, int height) {
        return preprocess(argb, width, height, MODEL_SIZE);
    }

    static PreprocessedImage preprocess(int[] argb, int width, int height, int targetSize) {
        if (argb.length != width * height || width <= 0 || height <= 0) {
            throw new IllegalArgumentException("Invalid source image dimensions");
        }
        float scale = Math.min((float) targetSize / width, (float) targetSize / height);
        int resizedWidth = Math.max(1, Math.round(width * scale));
        int resizedHeight = Math.max(1, Math.round(height * scale));
        float padX = (targetSize - resizedWidth) / 2.0f;
        float padY = (targetSize - resizedHeight) / 2.0f;
        byte[] rgb = new byte[targetSize * targetSize * 3];

        for (int y = 0; y < targetSize; ++y) {
            for (int x = 0; x < targetSize; ++x) {
                int destination = (y * targetSize + x) * 3;
                float resizedX = x - padX;
                float resizedY = y - padY;
                if (resizedX < 0 || resizedY < 0
                        || resizedX >= resizedWidth || resizedY >= resizedHeight) {
                    rgb[destination] = (byte) 114;
                    rgb[destination + 1] = (byte) 114;
                    rgb[destination + 2] = (byte) 114;
                    continue;
                }
                int sourceX = Math.min(width - 1,
                        Math.max(0, (int) (resizedX * width / resizedWidth)));
                int sourceY = Math.min(height - 1,
                        Math.max(0, (int) (resizedY * height / resizedHeight)));
                int pixel = argb[sourceY * width + sourceX];
                rgb[destination] = (byte) ((pixel >> 16) & 0xff);
                rgb[destination + 1] = (byte) ((pixel >> 8) & 0xff);
                rgb[destination + 2] = (byte) (pixel & 0xff);
            }
        }
        return new PreprocessedImage(rgb, new Transform(scale, padX, padY));
    }

    /** Decodes quantized [x1,y1,x2,y2], score, and class-index tensors. */
    public static List<Detection> decode(
            byte[][][] boxes,
            byte[][] scores,
            byte[][] classes,
            Quantization boxQuantization,
            Quantization scoreQuantization,
            Transform transform,
            int sourceWidth,
            int sourceHeight,
            float confidenceThreshold) {
        if (boxes.length == 0 || scores.length == 0 || classes.length == 0) {
            return Collections.emptyList();
        }
        int count = Math.min(boxes[0].length,
                Math.min(scores[0].length, classes[0].length));
        List<Detection> candidates = new ArrayList<>();
        for (int i = 0; i < count; ++i) {
            if (boxes[0][i].length < 4) {
                continue;
            }
            float score = dequantize(scores[0][i], scoreQuantization);
            if (score < confidenceThreshold) continue;

            float x1 = dequantize(boxes[0][i][0], boxQuantization);
            float y1 = dequantize(boxes[0][i][1], boxQuantization);
            float x2 = dequantize(boxes[0][i][2], boxQuantization);
            float y2 = dequantize(boxes[0][i][3], boxQuantization);
            float left = clamp((x1 - transform.padX) / transform.scale, 0, sourceWidth);
            float top = clamp((y1 - transform.padY) / transform.scale, 0, sourceHeight);
            float right = clamp((x2 - transform.padX) / transform.scale, 0, sourceWidth);
            float bottom = clamp((y2 - transform.padY) / transform.scale, 0, sourceHeight);
            if (right <= left || bottom <= top) continue;
            candidates.add(new Detection(left, top, right, bottom, score,
                    classes[0][i] & 0xff));
        }

        candidates.sort(Comparator.comparingDouble((Detection value) -> value.score).reversed());
        List<Detection> result = new ArrayList<>();
        for (Detection candidate : candidates) {
            boolean suppressed = false;
            for (Detection selected : result) {
                if (candidate.classIndex == selected.classIndex
                        && intersectionOverUnion(candidate, selected) > NMS_IOU) {
                    suppressed = true;
                    break;
                }
            }
            if (!suppressed) {
                result.add(candidate);
                if (result.size() == MAX_DETECTIONS) break;
            }
        }
        return result;
    }

    private static float dequantize(byte value, Quantization quantization) {
        return ((value & 0xff) - quantization.zeroPoint) * quantization.scale;
    }

    private static float clamp(float value, float low, float high) {
        return Math.max(low, Math.min(high, value));
    }

    private static float intersectionOverUnion(Detection first, Detection second) {
        float left = Math.max(first.left, second.left);
        float top = Math.max(first.top, second.top);
        float right = Math.min(first.right, second.right);
        float bottom = Math.min(first.bottom, second.bottom);
        float intersection = Math.max(0, right - left) * Math.max(0, bottom - top);
        float firstArea = (first.right - first.left) * (first.bottom - first.top);
        float secondArea = (second.right - second.left) * (second.bottom - second.top);
        return intersection / (firstArea + secondArea - intersection);
    }
}
