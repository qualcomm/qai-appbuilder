//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Tensor;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Image-based YOLOX object-detection sample with visible delegate reporting. */
public final class MainActivity extends Activity {
    private static final int PICK_IMAGE_REQUEST = 1001;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private DetectionView preview;
    private TextView status;
    private Button detectButton;
    private TFLiteModelRunner runner;
    private Bitmap selectedBitmap;
    private String[] labels = new String[0];
    private OutputTensors outputTensors;

    private static final class OutputTensors {
        final int detectionCount;
        final YoloXDetector.Quantization boxQuantization;
        final YoloXDetector.Quantization scoreQuantization;

        OutputTensors(int detectionCount, YoloXDetector.Quantization boxQuantization,
                      YoloXDetector.Quantization scoreQuantization) {
            this.detectionCount = detectionCount;
            this.boxQuantization = boxQuantization;
            this.scoreQuantization = scoreQuantization;
        }
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        labels = loadLabels();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 24, 24, 24);

        TextView title = new TextView(this);
        title.setText("TFLite YOLOX Object Detection");
        title.setTextSize(20.0f);
        root.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        preview = new DetectionView(this);
        root.addView(preview, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1.0f));

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        Button pickButton = new Button(this);
        pickButton.setText("Pick image");
        detectButton = new Button(this);
        detectButton.setText("Run detection");
        detectButton.setEnabled(false);
        actions.addView(pickButton, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1.0f));
        actions.addView(detectButton, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1.0f));
        root.addView(actions, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        status = new TextView(this);
        status.setText("Loading model.tflite on QNN NPU. Pick an image to begin...");
        status.setTextSize(14.0f);
        root.addView(status, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(root);

        pickButton.setOnClickListener(view -> pickImage());
        detectButton.setOnClickListener(view -> runDetection());

        executor.execute(() -> {
            TFLiteModelRunner loadedRunner = null;
            try {
                loadedRunner = new TFLiteModelRunner(this, "model.tflite");
                OutputTensors loadedOutputTensors = validateYoloXModel(loadedRunner);
                runner = loadedRunner;
                outputTensors = loadedOutputTensors;
                runOnUiThread(() -> {
                    StringBuilder message = new StringBuilder();
                    message.append("provider=").append(runner.getProviderMode()).append('\n');
                    if (!"qnn-npu".equals(runner.getProviderMode())) {
                        message.append("qnn=").append(runner.getQnnDiagnostic()).append('\n');
                    }
                    message.append("Ready. Choose an image and run detection.");
                    status.setText(message);
                    detectButton.setEnabled(selectedBitmap != null);
                });
            } catch (Exception error) {
                if (loadedRunner != null) loadedRunner.close();
                runOnUiThread(() -> status.setText(
                        "Unsupported TFLite model:\n" + error.getMessage()));
            }
        });
    }

    private String[] loadLabels() {
        try (InputStream input = getAssets().open("labels.txt");
             BufferedReader reader = new BufferedReader(new InputStreamReader(
                     input, StandardCharsets.UTF_8))) {
            return reader.lines().toArray(String[]::new);
        } catch (IOException error) {
            return new String[0];
        }
    }

    private void pickImage() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("image/*");
        startActivityForResult(intent, PICK_IMAGE_REQUEST);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK_IMAGE_REQUEST || resultCode != RESULT_OK
                || data == null || data.getData() == null) return;
        Uri imageUri = data.getData();
        try (InputStream input = getContentResolver().openInputStream(imageUri)) {
            Bitmap bitmap = BitmapFactory.decodeStream(input);
            if (bitmap == null) throw new IOException("Unable to decode selected image");
            selectedBitmap = bitmap.getConfig() == Bitmap.Config.ARGB_8888
                    ? bitmap : bitmap.copy(Bitmap.Config.ARGB_8888, false);
            preview.setBitmap(selectedBitmap);
            detectButton.setEnabled(runner != null);
            status.setText("Image selected. Tap Run detection.");
        } catch (IOException error) {
            status.setText("Image load failed: " + error.getMessage());
        }
    }

    private void runDetection() {
        if (runner == null || selectedBitmap == null || outputTensors == null) return;
        final Bitmap image = selectedBitmap;
        detectButton.setEnabled(false);
        status.setText("Running YOLOX on " + runner.getProviderMode() + "...");
        executor.execute(() -> {
            try {
                Bitmap argb = image.getConfig() == Bitmap.Config.ARGB_8888
                        ? image : image.copy(Bitmap.Config.ARGB_8888, false);
                int[] pixels = new int[argb.getWidth() * argb.getHeight()];
                argb.getPixels(pixels, 0, argb.getWidth(), 0, 0,
                        argb.getWidth(), argb.getHeight());
                YoloXDetector.PreprocessedImage prepared = YoloXDetector.preprocess(
                        pixels, argb.getWidth(), argb.getHeight());
                ByteBuffer input = ByteBuffer.allocateDirect(prepared.rgb.length)
                        .order(ByteOrder.nativeOrder());
                input.put(prepared.rgb).rewind();

                byte[][][] boxes = new byte[1][outputTensors.detectionCount][4];
                byte[][] scores = new byte[1][outputTensors.detectionCount];
                byte[][] classes = new byte[1][outputTensors.detectionCount];
                Map<Integer, Object> outputs = new HashMap<>();
                outputs.put(0, boxes);
                outputs.put(1, scores);
                outputs.put(2, classes);

                long started = System.nanoTime();
                runner.run(input, outputs);
                long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
                List<YoloXDetector.Detection> detections = YoloXDetector.decode(
                        boxes, scores, classes, outputTensors.boxQuantization,
                        outputTensors.scoreQuantization, prepared.transform,
                        argb.getWidth(), argb.getHeight(),
                        YoloXDetector.DEFAULT_CONFIDENCE);
                runOnUiThread(() -> {
                    preview.setDetections(detections, labels);
                    status.setText("provider=" + runner.getProviderMode()
                            + "\nfound=" + detections.size()
                            + "  inference=" + elapsedMs + " ms");
                    detectButton.setEnabled(true);
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    status.setText("Detection failed:\n" + error.getMessage());
                    detectButton.setEnabled(true);
                });
            }
        });
    }

    private static OutputTensors validateYoloXModel(TFLiteModelRunner modelRunner) {
        if (modelRunner.getInputCount() != 1 || modelRunner.getOutputCount() != 3) {
            throw new IllegalArgumentException("Expected one input and three output tensors");
        }
        validateTensor("input", modelRunner.getInputTensor(0), new int[]{1, 640, 640, 3});
        Tensor boxes = modelRunner.getOutputTensor(0);
        Tensor scores = modelRunner.getOutputTensor(1);
        Tensor classes = modelRunner.getOutputTensor(2);
        int[] boxShape = validateTensor("boxes", boxes, null);
        int[] scoreShape = validateTensor("scores", scores, null);
        int[] classShape = validateTensor("classes", classes, null);
        if (boxShape.length != 3 || boxShape[0] != 1 || boxShape[1] <= 0 || boxShape[2] != 4
                || scoreShape.length != 2 || scoreShape[0] != 1 || scoreShape[1] != boxShape[1]
                || classShape.length != 2 || classShape[0] != 1 || classShape[1] != boxShape[1]) {
            throw new IllegalArgumentException("Expected UINT8 outputs [1,N,4], [1,N], [1,N]");
        }
        return new OutputTensors(boxShape[1], quantizationFor("boxes", boxes),
                quantizationFor("scores", scores));
    }

    private static int[] validateTensor(String name, Tensor tensor, int[] expectedShape) {
        if (tensor.dataType() != DataType.UINT8) {
            throw new IllegalArgumentException(name + " tensor must use UINT8, found "
                    + tensor.dataType());
        }
        int[] shape = tensor.shape();
        if (expectedShape != null && !java.util.Arrays.equals(shape, expectedShape)) {
            throw new IllegalArgumentException(name + " tensor must have shape [1,640,640,3], found "
                    + TFLiteModelRunner.tensorSummary(tensor));
        }
        return shape;
    }

    private static YoloXDetector.Quantization quantizationFor(String name, Tensor tensor) {
        Tensor.QuantizationParams params = tensor.quantizationParams();
        if (params.getScale() <= 0.0f) {
            throw new IllegalArgumentException(name + " tensor has no valid quantization metadata");
        }
        return new YoloXDetector.Quantization(params.getScale(), params.getZeroPoint());
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        if (runner != null) runner.close();
        super.onDestroy();
    }
}
