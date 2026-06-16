//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import android.app.Application;
import android.content.pm.PackageManager;

public class GenieApplication extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        if (getPackageManager().hasSystemFeature(PackageManager.FEATURE_AUTOMOTIVE)) {
            QnnLibCopier.copyIfNeeded(getApplicationContext());
        }
    }
}
