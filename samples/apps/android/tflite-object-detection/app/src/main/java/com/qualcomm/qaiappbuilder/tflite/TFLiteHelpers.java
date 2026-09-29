//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.qualcomm.qaiappbuilder.tflite;

import android.content.res.AssetFileDescriptor;
import android.content.res.AssetManager;
import android.util.Log;
import android.util.Pair;

import com.qualcomm.qti.QnnDelegate;

import org.tensorflow.lite.Delegate;
import org.tensorflow.lite.Interpreter;
import org.tensorflow.lite.gpu.GpuDelegate;
import org.tensorflow.lite.gpu.GpuDelegateFactory;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.MappedByteBuffer;
import java.nio.channels.FileChannel;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/** Delegate creation and fallback policy for the Java TFLite Android path. */
public final class TFLiteHelpers {
    private static final String TAG = "QaiAppBuilderTFLite";

    public enum DelegateType { QNN_NPU, GPUv2 }

    private TFLiteHelpers() {}

    public static Pair<Interpreter, Map<DelegateType, Delegate>> createInterpreter(
            MappedByteBuffer model,
            String nativeLibraryDir,
            String cacheDir,
            String modelToken,
            int cpuThreads) {
        Map<DelegateType, Delegate> delegates = new HashMap<>();
        Set<DelegateType> attempted = new HashSet<>();
        DelegateType[][] priority = {
                {DelegateType.QNN_NPU, DelegateType.GPUv2},
                {DelegateType.GPUv2},
                {}
        };

        for (DelegateType[] requested : priority) {
            for (DelegateType type : requested) {
                if (!attempted.add(type)) continue;
                Delegate delegate = createDelegate(type, nativeLibraryDir, cacheDir, modelToken);
                if (delegate != null) delegates.put(type, delegate);
            }
            boolean available = Arrays.stream(requested).allMatch(delegates::containsKey);
            if (!available) continue;

            Interpreter interpreter = createInterpreter(model, requested, delegates, cpuThreads);
            if (interpreter == null) continue;

            Set<DelegateType> used = new HashSet<>(Arrays.asList(requested));
            delegates.keySet().removeIf(type -> {
                if (used.contains(type)) return false;
                Delegate delegate = delegates.get(type);
                if (delegate != null) delegate.close();
                return true;
            });
            return new Pair<>(interpreter, delegates);
        }
        throw new IllegalStateException("Unable to create a TFLite interpreter with QNN, GPU, or CPU fallback");
    }

    private static Interpreter createInterpreter(
            MappedByteBuffer model,
            DelegateType[] requested,
            Map<DelegateType, Delegate> delegates,
            int cpuThreads) {
        Interpreter.Options options = new Interpreter.Options();
        options.setRuntime(Interpreter.Options.TfLiteRuntime.FROM_APPLICATION_ONLY);
        options.setUseNNAPI(false);
        options.setUseXNNPACK(true);
        options.setNumThreads(Math.max(1, cpuThreads));
        for (DelegateType type : requested) options.addDelegate(delegates.get(type));
        try {
            Interpreter interpreter = new Interpreter(model, options);
            interpreter.allocateTensors();
            return interpreter;
        } catch (RuntimeException e) {
            Log.e(TAG, "Interpreter creation failed for "
                    + Arrays.stream(requested).map(Enum::name).collect(Collectors.joining(",")), e);
            return null;
        }
    }

    public static String providerMode(Map<DelegateType, Delegate> delegates) {
        if (delegates.containsKey(DelegateType.QNN_NPU)) return "qnn-npu";
        if (delegates.containsKey(DelegateType.GPUv2)) return "gpu";
        return "cpu";
    }

    private static Delegate createDelegate(DelegateType type, String nativeLibraryDir,
                                           String cacheDir, String modelToken) {
        try {
            if (type == DelegateType.QNN_NPU) {
                QnnDelegate.Options options = new QnnDelegate.Options();
                options.setSkelLibraryDir(nativeLibraryDir);
                options.setLogLevel(QnnDelegate.Options.LogLevel.LOG_LEVEL_WARN);
                options.setCacheDir(cacheDir);
                options.setModelToken(modelToken);
                if (QnnDelegate.checkCapability(QnnDelegate.Capability.DSP_RUNTIME)) {
                    options.setBackendType(QnnDelegate.Options.BackendType.DSP_BACKEND);
                    options.setDspOptions(
                            QnnDelegate.Options.DspPerformanceMode.DSP_PERFORMANCE_BURST,
                            QnnDelegate.Options.DspPdSession.DSP_PD_SESSION_ADAPTIVE);
                } else {
                    boolean fp16 = QnnDelegate.checkCapability(QnnDelegate.Capability.HTP_RUNTIME_FP16);
                    boolean quant = QnnDelegate.checkCapability(QnnDelegate.Capability.HTP_RUNTIME_QUANTIZED);
                    if (!fp16 && !quant) return null;
                    options.setBackendType(QnnDelegate.Options.BackendType.HTP_BACKEND);
                    options.setHtpPerformanceMode(
                            QnnDelegate.Options.HtpPerformanceMode.HTP_PERFORMANCE_BURST);
                    options.setHtpUseConvHmx(
                            QnnDelegate.Options.HtpUseConvHmx.HTP_CONV_HMX_ON);
                    if (fp16) {
                        options.setHtpPrecision(QnnDelegate.Options.HtpPrecision.HTP_PRECISION_FP16);
                    }
                }
                return new QnnDelegate(options);
            }
            GpuDelegateFactory.Options options = new GpuDelegateFactory.Options();
            options.setInferencePreference(
                    GpuDelegateFactory.Options.INFERENCE_PREFERENCE_SUSTAINED_SPEED);
            options.setPrecisionLossAllowed(true);
            options.setSerializationParams(cacheDir, modelToken);
            return new GpuDelegate(options);
        } catch (Exception e) {
            Log.w(TAG, type + " delegate unavailable; trying fallback", e);
            return null;
        }
    }

    public static Pair<MappedByteBuffer, String> loadModelFile(
            AssetManager assets, String filename) throws IOException, NoSuchAlgorithmException {
        AssetFileDescriptor descriptor = assets.openFd(filename);
        try (FileInputStream input = new FileInputStream(descriptor.getFileDescriptor())) {
            FileChannel channel = input.getChannel();
            long offset = descriptor.getStartOffset();
            long length = descriptor.getDeclaredLength();
            MappedByteBuffer model = channel.map(FileChannel.MapMode.READ_ONLY, offset, length);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            input.skip(offset);
            try (DigestInputStream stream = new DigestInputStream(input, digest)) {
                byte[] buffer = new byte[8192];
                long remaining = length;
                while (remaining > 0) {
                    int read = stream.read(buffer, 0, (int) Math.min(buffer.length, remaining));
                    if (read < 0) throw new IOException("Unexpected end of model asset: " + filename);
                    remaining -= read;
                }
            }
            StringBuilder token = new StringBuilder();
            for (byte value : digest.digest()) token.append(String.format("%02x", value));
            return new Pair<>(model, token.toString());
        } finally {
            descriptor.close();
        }
    }
}
