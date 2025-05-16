import org.gradle.api.tasks.Copy
import java.io.File

val sourceFiles = listOf(
    file("..\\..\\Service\\libs\\arm64-v8a\\libJNIGenieAPIService.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libGenie.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnHtp.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnHtpNetRunExtensions.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnHtpPrepare.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnHtpV79Skel.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnHtpV79Stub.so"),
    file("..\\..\\Service\\libs\\arm64-v8a\\libQnnSystem.so"),
)

val libsDir = file("libs/arm64-v8a")

println("sourceFiles path: ${sourceFiles}")
println("libsDir path: ${libsDir.absolutePath}")

val copyHttpServiceTask = tasks.register<Copy>("copyHttpService") {
    from(sourceFiles)
    into(libsDir)
}

tasks.preBuild {
    dependsOn(copyHttpServiceTask)
}

plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "com.example.genieapiservice"
    compileSdk = 35

    lint {
        baseline = file("lint-baseline.xml")
    }
    defaultConfig {
        applicationId = "com.example.genieapiservice"
        minSdk = 28
        targetSdk = 28
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        externalNativeBuild {
            cmake {
                cppFlags += "-std=c++14"
                arguments("-DANDROID_ABI=arm64-v8a")
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