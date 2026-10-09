//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.View;

import java.util.Collections;
import java.util.List;

/** Displays the selected image and YOLOX detections in the same coordinate space. */
public final class DetectionView extends View {
    private final Paint imagePaint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Paint boxPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private Bitmap bitmap;
    private List<YoloXDetector.Detection> detections = Collections.emptyList();
    private String[] labels = new String[0];

    public DetectionView(Context context) {
        super(context);
        setBackgroundColor(Color.rgb(24, 24, 24));
        boxPaint.setStyle(Paint.Style.STROKE);
        boxPaint.setStrokeWidth(5.0f);
        boxPaint.setColor(Color.rgb(0, 220, 80));
        textPaint.setColor(Color.WHITE);
        textPaint.setTextSize(32.0f);
        textPaint.setStyle(Paint.Style.FILL);
        setContentDescription("Object detection preview");
    }

    public void setBitmap(Bitmap bitmap) {
        this.bitmap = bitmap;
        this.detections = Collections.emptyList();
        invalidate();
    }

    public void setDetections(List<YoloXDetector.Detection> detections, String[] labels) {
        this.detections = detections == null ? Collections.emptyList() : detections;
        this.labels = labels == null ? new String[0] : labels;
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        if (bitmap == null || bitmap.isRecycled()) return;

        float scale = Math.min((float) getWidth() / bitmap.getWidth(),
                (float) getHeight() / bitmap.getHeight());
        float offsetX = (getWidth() - bitmap.getWidth() * scale) / 2.0f;
        float offsetY = (getHeight() - bitmap.getHeight() * scale) / 2.0f;
        canvas.save();
        canvas.translate(offsetX, offsetY);
        canvas.scale(scale, scale);
        canvas.drawBitmap(bitmap, 0, 0, imagePaint);

        for (YoloXDetector.Detection detection : detections) {
            RectF box = new RectF(detection.left, detection.top,
                    detection.right, detection.bottom);
            canvas.drawRect(box, boxPaint);
            String label = detection.classIndex < labels.length
                    ? labels[detection.classIndex] : "class " + detection.classIndex;
            String text = label + " " + Math.round(detection.score * 100) + "%";
            float textY = Math.max(34.0f, box.top - 8.0f);
            canvas.drawText(text, box.left, textY, textPaint);
        }
        canvas.restore();
    }
}
