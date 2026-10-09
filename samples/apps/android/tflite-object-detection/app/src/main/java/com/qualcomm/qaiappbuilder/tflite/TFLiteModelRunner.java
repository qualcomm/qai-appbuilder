//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import android.content.Context;
import android.util.Pair;

import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Delegate;
import org.tensorflow.lite.Interpreter;
import org.tensorflow.lite.Tensor;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.MappedByteBuffer;
import java.util.Map;

/** Reusable Java TFLite runner. Pre/post-processing remains application-owned. */
public final class TFLiteModelRunner implements AutoCloseable {
    private final Interpreter interpreter;
    private final Map<TFLiteHelpers.DelegateType, Delegate> delegates;
    private final String providerMode;
    private final String qnnDiagnostic;
    private boolean closed;

    public TFLiteModelRunner(Context context, String modelAsset) throws Exception {
        Pair<MappedByteBuffer, String> loaded = TFLiteHelpers.loadModelFile(
                context.getAssets(), modelAsset);
        Pair<Interpreter, Map<TFLiteHelpers.DelegateType, Delegate>> created =
                TFLiteHelpers.createInterpreter(
                        loaded.first,
                        context.getApplicationInfo().nativeLibraryDir,
                        context.getCacheDir().getAbsolutePath(),
                        loaded.second,
                        Math.max(1, Runtime.getRuntime().availableProcessors() / 2));
        interpreter = created.first;
        delegates = created.second;
        providerMode = TFLiteHelpers.providerMode(delegates);
        qnnDiagnostic = TFLiteHelpers.qnnDiagnostic();
    }

    public String getProviderMode() {
        return providerMode;
    }

    public String getQnnDiagnostic() {
        return qnnDiagnostic;
    }

    public int getInputCount() {
        ensureOpen();
        return interpreter.getInputTensorCount();
    }

    public int getOutputCount() {
        ensureOpen();
        return interpreter.getOutputTensorCount();
    }

    public Tensor getInputTensor(int index) {
        ensureOpen();
        return interpreter.getInputTensor(index);
    }

    public Tensor getOutputTensor(int index) {
        ensureOpen();
        return interpreter.getOutputTensor(index);
    }

    public void run(ByteBuffer input, Map<Integer, Object> outputs) {
        ensureOpen();
        interpreter.runForMultipleInputsOutputs(new Object[]{input}, outputs);
    }

    public void run(Object input, Object output) {
        ensureOpen();
        interpreter.run(input, output);
    }

    public static String tensorSummary(Tensor tensor) {
        DataType type = tensor.dataType();
        int[] shape = tensor.shape();
        StringBuilder result = new StringBuilder(type.toString()).append(" ");
        result.append("[");
        for (int i = 0; i < shape.length; ++i) {
            if (i != 0) result.append(", ");
            result.append(shape[i]);
        }
        return result.append("]").toString();
    }

    private void ensureOpen() {
        if (closed) throw new IllegalStateException("TFLite model runner is closed");
    }

    @Override
    public void close() {
        if (closed) return;
        closed = true;
        interpreter.close();
        for (Delegate delegate : delegates.values()) delegate.close();
    }
}
