val libsDir = file("libs/arm64-v8a")

plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "com.example.superresolution"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.superresolution"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "2.0.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        externalNativeBuild {
            cmake {
                cppFlags += "-std=c++17"
                arguments += listOf(
                    "-DANDROID_ABI=arm64-v8a",
                    "-DANDROID_STL=c++_shared"
                )
            }
        }
        ndk {
            abiFilters.add("arm64-v8a")
        }
        sourceSets {
            getByName("main") {
                jniLibs.srcDir("libs")
            }
        }
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
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
    buildFeatures {
        viewBinding = true
    }

    packaging {
        jniLibs.useLegacyPackaging = true
    }
}

dependencies {

    implementation(libs.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    testImplementation(libs.junit)
    androidTestImplementation(libs.ext.junit)
    androidTestImplementation(libs.espresso.core)
}