//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

public class GenieReceiver extends BroadcastReceiver {

    private static final String TAG = "GenieReceiver";
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent.getAction().equals("com.example.genieapiservice.UPDATE_WEBVIEW")) {
            String msg = intent.getStringExtra("service_msg");
            MainActivity.updateWebView(msg);
            LogUtils.logDebug(TAG,"action = " + intent.getAction() + " msg = " + msg,LogUtils.LOG_DEBUG);
        }
    }
}