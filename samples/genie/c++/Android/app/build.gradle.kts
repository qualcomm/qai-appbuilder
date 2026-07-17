import org.gradle.api.tasks.Copy
import java.io.File

// Must stay in sync with the QNN_STUB_VERSION build_android.bat actually built (default
// "v79;v81", ';'-separated for multiple Hexagon architectures); pass
// -PqnnStubVersion=<vNN>[;<vNN>...] to gradlew to override.
val qnnStubVersion = (project.findProperty("qnnStubVersion") as String?) ?: "v79;v81"
val qnnStubVersions = qnnStubVersion.split(";").map { it.trim() }.filter { it.isNotEmpty() }

// From app/build.gradle.kts (Android/app/), go up 2 levels to samples/genie/c++, then into
// Service/build-android (build_android.bat lives in Service and resolves all its paths from
// there, mirroring how build_linux.sh keeps its own build-linux output inside Service too).
val buildOutputDir = file("../../Service/build-android/output/libs/arm64-v8a")

val sourceFiles = listOf(
    "libJNIGenieAPIService.so",
    "libGenieAPIService.so",
    // libappbuilder/libsamplerate are LOCAL_SHARED_LIBRARIES of the two modules above
    // (see Service/scripts/Android.mk) and must ship alongside them or the dynamic
    // linker refuses to load either .so at process start.
    "libappbuilder.so",
    "libsamplerate.so",
    "libGenie.so",
    "libQnnHtp.so",
    "libQnnSystem.so",
    "libQnnHtpNetRunExtensions.so",
    "libQnnHtpPrepare.so"
) + qnnStubVersions.flatMap { ver ->
    val tag = "V" + ver.removePrefix("v")
    listOf(
        "libQnnHtp${tag}Stub.so",
        "libQnnHtp${tag}Skel.so",
        "libqnnhtp${ver}.cat"
    )
}

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
        // Skel libraries contain a DSP-firmware-specific CRC embedded by the Qualcomm
        // toolchain. Stripping changes the binary and breaks CRC validation at runtime.
        jniLibs.keepDebugSymbols += setOf(
            "*/arm64-v8a/*Skel.so"
        )
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
