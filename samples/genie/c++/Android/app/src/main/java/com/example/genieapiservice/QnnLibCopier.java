//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import android.content.Context;
import android.content.SharedPreferences;
import android.system.Os;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;

class QnnLibCopier {
    private static final String TAG = "QnnLibCopier";
    // Increment when copy logic changes to force a re-copy on next app start.
    private static final int COPY_VERSION = 7;
    private static final String PREF_COPY_VERSION = "qnn_libs_copy_version";

    // libQnnHtpV81Skel.so contains a firmware-specific CRC embedded by the Qualcomm
    // toolchain. build.gradle must set keepDebugSymbols for skel libs to prevent
    // Android build tools from stripping the binary and breaking the DSP CRC check.
    private static final String[] LIBS_TO_COPY = {
        "libGenie.so",
        "libQnnHtp.so",
        "libQnnHtpNetRunExtensions.so",
        "libQnnHtpV81Skel.so",
        "libQnnHtpV81Stub.so",
        "libQnnSystem.so",
        "libQnnHtpV79Skel.so",
        "libQnnHtpV79Stub.so"
    };

    static void copyIfNeeded(Context context) {
        SharedPreferences prefs = context.getSharedPreferences("AppPrefs", Context.MODE_PRIVATE);
        if (prefs.getInt(PREF_COPY_VERSION, 0) >= COPY_VERSION) {
            return;
        }
        String nativeLibDir = context.getApplicationInfo().nativeLibraryDir;
        File targetDir = new File("/data/local/tmp/qnn");
        if (!targetDir.exists() && !targetDir.mkdirs()) {
            LogUtils.logDebug(TAG, "Failed to create dir: " + targetDir.getAbsolutePath(), LogUtils.LOG_ERROR);
            return;
        }
        try {
            Os.chmod(targetDir.getAbsolutePath(), 0777);
        } catch (Exception e) {
            LogUtils.logDebug(TAG, "chmod dir failed: " + e.getMessage(), LogUtils.LOG_ERROR);
        }
        runCmd(new String[]{"chcon", "u:object_r:shell_data_file:s0", targetDir.getAbsolutePath()});

        for (String libName : LIBS_TO_COPY) {
            copyLib(new File(nativeLibDir, libName), new File(targetDir, libName));
        }

        prefs.edit().putInt(PREF_COPY_VERSION, COPY_VERSION).apply();
        LogUtils.logDebug(TAG, "QNN libs copy completed (v" + COPY_VERSION + ").", LogUtils.LOG_DEBUG);
    }

    private static void copyLib(File srcFile, File dstFile) {
        String libName = srcFile.getName();
        if (!srcFile.exists()) {
            LogUtils.logDebug(TAG, "Source not found: " + srcFile.getAbsolutePath(), LogUtils.LOG_DEBUG);
            return;
        }
        try {
            Process proc = Runtime.getRuntime().exec(
                    new String[]{"cp", srcFile.getAbsolutePath(), dstFile.getAbsolutePath()});
            StringBuilder err = readStream(proc.getErrorStream());
            int exit = proc.waitFor();
            if (exit != 0) {
                LogUtils.logDebug(TAG, "cp failed: " + libName + " exit=" + exit + " " + err, LogUtils.LOG_ERROR);
                return;
            }
        } catch (Exception e) {
            LogUtils.logDebug(TAG, "cp exception: " + libName + " " + e.getMessage(), LogUtils.LOG_ERROR);
            return;
        }
        try {
            Os.chmod(dstFile.getAbsolutePath(), 0777);
        } catch (Exception e) {
            LogUtils.logDebug(TAG, "chmod failed: " + libName + " " + e.getMessage(), LogUtils.LOG_ERROR);
        }
        runCmd(new String[]{"chcon", "u:object_r:shell_data_file:s0", dstFile.getAbsolutePath()});
    }

    private static void runCmd(String[] cmd) {
        try {
            Runtime.getRuntime().exec(cmd).waitFor();
        } catch (Exception ignored) {}
    }

    private static StringBuilder readStream(java.io.InputStream is) {
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(is))) {
            String line;
            while ((line = r.readLine()) != null) sb.append(line);
        } catch (Exception ignored) {}
        return sb;
    }
}
