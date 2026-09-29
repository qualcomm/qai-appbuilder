//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.Context;

import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

/** Device test. Add app/src/main/assets/model.tflite before running it. */
@RunWith(AndroidJUnit4.class)
public final class TFLiteModelRunnerTest {
    @Test
    public void loadsModelReportsProviderAndClosesIdempotently() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        try (TFLiteModelRunner runner = new TFLiteModelRunner(context, "model.tflite")) {
            assertNotNull(runner.getProviderMode());
            assertTrue(runner.getProviderMode().equals("qnn-npu")
                    || runner.getProviderMode().equals("gpu")
                    || runner.getProviderMode().equals("cpu"));
            assertTrue(runner.getInputCount() > 0);
            assertTrue(runner.getOutputCount() > 0);
        }
    }
}
