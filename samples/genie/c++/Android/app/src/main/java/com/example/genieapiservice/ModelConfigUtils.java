//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import java.io.File;

public class ModelConfigUtils {

    // 优先返回 modelDir/genie_config.json（若存在），否则回退返回 modelDir/config.json，
    // 与 native 侧 File::ResolveModelConfigPath 语义对齐。
    public static File resolveConfigFile(String modelDir) {
        File genieConfig = new File(modelDir, "genie_config.json");
        if (genieConfig.exists()) {
            return genieConfig;
        }
        return new File(modelDir, "config.json");
    }
}
