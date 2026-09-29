//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

plugins {
    id("com.android.application")
}

android {
    namespace = "com.qualcomm.qaiappbuilder.tflite"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.qualcomm.qaiappbuilder.tflite"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk {
            abiFilters.add("arm64-v8a")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    sourceSets["main"].assets.srcDir("src/main/assets")

    // Keep the sample buildable from a clean checkout while making the
    // missing model error actionable instead of failing later at runtime.
    tasks.register("validateTfliteAsset") {
        doLast {
            val model = file("src/main/assets/model.tflite")
            if (!model.isFile) {
                throw GradleException(
                    "model.tflite is missing from app/src/main/assets/. " +
                        "Copy a compatible TFLite model there before building."
                )
            }
        }
    }
    tasks.named("preBuild").configure {
        dependsOn("validateTfliteAsset")
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.tensorflow:tensorflow-lite:2.16.1")
    implementation("org.tensorflow:tensorflow-lite-gpu:2.16.1")
    implementation("org.tensorflow:tensorflow-lite-gpu-api:2.16.1")
    implementation("org.tensorflow:tensorflow-lite-gpu-delegate-plugin:0.4.4")

    // Keep these versions coherent with the QAIRT/QNN stack on the target.
    implementation("com.qualcomm.qti:qnn-runtime:2.40.0")
    implementation("com.qualcomm.qti:qnn-litert-delegate:2.40.0")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
