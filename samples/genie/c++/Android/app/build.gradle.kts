import org.gradle.api.tasks.Copy
import java.io.File

// Use absolute path to build_android output directory
// From app/build.gradle.kts (Android/app/), go up 2 levels to samples/genie/c++, then to build_android
val buildOutputDir = file("../../build_android/output/libs/arm64-v8a")

val sourceFiles = listOf(
    "libJNIGenieAPIService.so",
    "libGenieAPIService.so",
    "libappbuilder.so",
    "libsamplerate.so",
    "libcurl.so",
    "libGenie.so",
    "libQnnHtp.so",
    "libQnnHtpNetRunExtensions.so",
    "libQnnHtpPrepare.so",
    "libQnnHtpV79.so",
    "libQnnHtpV79Skel.so",
    "libQnnHtpV79Stub.so",
    "libQnnHtpV81.so",
    "libQnnHtpV81Skel.so",
    "libQnnHtpV81Stub.so",
    "libQnnSystem.so"
)

val libsDir = file("libs/arm64-v8a")

println("Build output directory: ${buildOutputDir.absolutePath}")
println("Libs directory: ${libsDir.absolutePath}")

val copyHttpServiceTask = tasks.register<Copy>("copyHttpService") {
    from(buildOutputDir) {
        include(sourceFiles)
    }
    into(libsDir)
    
    doFirst {
        println("Copying libraries from: ${buildOutputDir.absolutePath}")
        println("Copying libraries to: ${libsDir.absolutePath}")
        if (!buildOutputDir.exists()) {
            throw GradleException("Build output directory does not exist: ${buildOutputDir.absolutePath}")
        }
        sourceFiles.forEach { fileName ->
            val sourceFile = File(buildOutputDir, fileName)
            if (!sourceFile.exists()) {
                println("WARNING: Source file not found: ${sourceFile.absolutePath}")
            } else {
                println("Found: ${fileName} (${sourceFile.length()} bytes)")
            }
        }
    }
    
    doLast {
        println("Copied ${outputs.files.files.size} library files")
    }
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
        checkReleaseBuilds = false
        abortOnError = false
    }
    signingConfigs {
        create("release") {
            storeFile = file("C:\\work\\Android\\genieapiservice")
            storePassword = "123456"
            keyAlias = "key0"
            keyPassword = "123456"
        }
    }
    
    defaultConfig {
        applicationId = "com.example.genieapiservice"
        minSdk = 30
        targetSdk = 31
        versionCode = 1
        versionName = "2.0.0"

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
            signingConfig = signingConfigs.getByName("release")
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
    implementation(libs.activity)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation(libs.junit)
    androidTestImplementation(libs.ext.junit)
    androidTestImplementation(libs.espresso.core)
}