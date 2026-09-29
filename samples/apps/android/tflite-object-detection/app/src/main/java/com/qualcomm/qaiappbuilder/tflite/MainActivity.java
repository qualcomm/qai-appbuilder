//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

import org.tensorflow.lite.Tensor;

/** Minimal model smoke-test UI; model preprocessing belongs to the caller. */
public final class MainActivity extends Activity {
    private TFLiteModelRunner runner;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        TextView status = new TextView(this);
        status.setPadding(32, 32, 32, 32);
        status.setText("Loading model.tflite...");
        setContentView(status);

        new Thread(() -> {
            try {
                runner = new TFLiteModelRunner(this, "model.tflite");
                StringBuilder text = new StringBuilder();
                text.append("provider=").append(runner.getProviderMode()).append('\n');
                text.append("inputs=").append(runner.getInputCount()).append('\n');
                for (int i = 0; i < runner.getInputCount(); ++i) {
                    Tensor tensor = runner.getInputTensor(i);
                    text.append("input[").append(i).append("] ")
                            .append(TFLiteModelRunner.tensorSummary(tensor)).append('\n');
                }
                text.append("outputs=").append(runner.getOutputCount()).append('\n');
                for (int i = 0; i < runner.getOutputCount(); ++i) {
                    Tensor tensor = runner.getOutputTensor(i);
                    text.append("output[").append(i).append("] ")
                            .append(TFLiteModelRunner.tensorSummary(tensor)).append('\n');
                }
                runOnUiThread(() -> status.setText(text));
            } catch (Exception error) {
                runOnUiThread(() -> status.setText(
                        "TFLite model load failed:\n" + error.getMessage()));
            }
        }).start();
    }

    @Override
    protected void onDestroy() {
        if (runner != null) runner.close();
        super.onDestroy();
    }
}
