//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import android.os.Environment;
import android.util.Log;

import java.io.File;

public class LogUtils {

    public static final int LOG_ERROR = 1;
    public static final int LOG_WARNING= 2;
    public static final int LOG_DEBUG = 3;
    public static boolean enableLog = true;
    //public static String LOG_DIRECTORY = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath()+ File.separator + "Logs";
    public static String LOG_DIRECTORY = "";
    public static void logDebug(String tag,String log, int level) {
        if (!enableLog) {
            return;
        }
        switch (level) {
            case 1:
                Log.e(tag,log);
                break;
            case 2:
                Log.w(tag,log);
                break;
            case 3:
                Log.d(tag,log);
                break;
        }
    }
}
