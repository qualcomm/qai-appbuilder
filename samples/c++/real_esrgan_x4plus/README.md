<br>

<div align="center">
  <h3>Run the model locally on NPU, deploy the AI-Hub model quickly.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks.
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction
This guide helps developers setup C++ environment for using QAI AppBuilder to run sample code on Windows on Snapdragon (WoS) platforms.You should install [Visual Studio 2022 or higher](https://visualstudio.microsoft.com/vs/) ,[Cmake](https://cmake.org/download/) and [Git](https://git-scm.com/downloads/win).
During the  Visual Studio installation process, you will be asked to choose the workloads you want and customize your installation. 
**At a minimum, selmake sure to check：**
* **Desktop development with C++**.

##  1.File Download & Setup Instructions
First, create a directory named `real_esrgan_x4plus` at any location (e.g., ai-engine-direct-helper\samples\c++\real_esrgan_x4plus). 
The final project structure should look like this:
```plaintext
real_esrgan_x4plus/
├── QAI_AppBuilder-win_arm64-QNN2.34.0-Release/   # QNN SDK
├── models/                                       # Model files (e.g., .bin)
├── qai_libs/                                     # QNN SDK runtime libraries (e.g., HTP backend)
├── build/                                        # Directory generated after successful compilation; contains the real_esrgan_x4plus.exe binary.
├── real_esrgan_x4plus.cpp                        # C++ inference main program
├── input.jpg                                     # Sample input jpg
├── CMakeLists.txt                                # CMake configuration 
└── README.md                                     # Project documentation
```
**NOTE: During runtime, the Release/ directory serves as the working directory. All relative paths within the application are resolved with respect to this directory.**
#### Step 1: Download QAI Launcher
Open a terminal and run:
```
mkdir C:\ai-hub\
cd C:\ai-hub\
```
Click here to download [QAI_Launcher_v1.0.0.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAI_Launcher_v1.0.0.zip). Unzip the downloaded file into the **C:\ai-hub** directory.
Run the following script to install all required dependencies and tools:
```
.\1.Install_QAI_AppBuilder.bat
```
Then navigate to:
```
ai-engine-direct-helper/samples/
```
Copy the entire **`qai_libs`/** folder into your local `real_esrgan_x4plus`/ project directory.

#### Step 2: Download Real_Esrgan_X4plus file
The essential files needed for `real_esrgan_x4plus` model inference are already downloaded when you run `real_esrgan_x4plus.py` after launching the Python environment via **8.Start_PythonEnv.bat**.
So, there’s no need to re-download them—you just need to locate and copy them into the correct locations.
Open a terminal in C:\ai-hub\，and run:
```
.\8.Start_PythonEnv.bat
pip install py3-wget
cd ../ai-engine-direct-helper/samples
python python\real_esrgan_x4plus\real_esrgan_x4plus.py
```
**Note: All operations in this step are performed inside the virtual environment py312, which is automatically created and activated by 8.Start_PythonEnv.bat.**
Then navigate to:
```
ai-engine-direct-helper/samples/python/real_esrgan_x4plus/
```
Copy the following files into your local `real_esrgan_x4plus`/ directory:
* **`models`/** folder (contains the real_esrgan_x4plus model (`real_esrgan_x4plus.bin`) files)
* **`input.jpg`** (sample image for inference)

Place all files in the appropriate directories under your local `real_esrgan_x4plus`/ project folder to ensure the inference script can access them correctly.


#### Step 3: Download QAI_AppBuilder

Click here to download [QAI_AppBuilder-win_arm64-QNN2.34.0-Release](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAI_AppBuilder-win_arm64-QNN2.34.0-Release.zip).Unzip the contents of the archive into the `QAI_AppBuilder-win_arm64-QNN2.34.0-Release`/ folder inside your `real_esrgan_x4plus`/ project directory. The structure should look like:
```plaintext
real_esrgan_x4plus/
├── QAI_AppBuilder-win_arm64-QNN2.34.0-Release/
```



## 2.Install Dependencies
#### Note:
All the following operations are not performed inside the py312 virtual environment.
If you're running the commands directly from this README, make sure to launch your command prompt from the root of drive C (C:\) before executing the steps below.If you're not in C:\, you will need to modify the command paths accordingly.
### 2.1 Setup vcpkg

#### 1.Clone the vcpkg Repository

```bash
git clone https://github.com/microsoft/vcpkg.git
cd vcpkg
.\bootstrap-vcpkg.bat
```
#### 2.Add to PATH
Add the full path to the vcpkg directory (e.g., C:\vcpkg) to your system PATH so you can run vcpkg from any terminal.

### 2.2 Setup xtensor
Run below command in Windows terminal:
```
vcpkg install xtensor
```
* If the download is too slow, you can download via the link.Put all the downloaded files in the the vcpkg directory (e.g., C:\vcpkg\dowloads).Then run the command again.


### 2.3 How to build opencv4:arm64-windows
#### Using vcpkg
##### 1.
Run following commands in Windows terminal:
```
vcpkg install opencv4[core,win32ui,webp,tiff,thread,quirc,png,jpeg,intrinsics,highgui,gapi,fs,dshow,calib3d]:arm64-windows
```
##### 2.
Go to the C:\vcpkg\ports\opencv4\ directory and open the **portfile.cmake** file.
After line 348, add the following line:
```
-DCPU_BASELINE=NEON
```
So that the section looks like this:
```
vcpkg_cmake_configure(
    SOURCE_PATH "${SOURCE_PATH}"
    OPTIONS
        ###### Verify that required components and only those are enabled
        -DENABLE_CONFIG_VERIFICATION=ON
        ###### opencv cpu recognition is broken, always using host and not target: here we bypass that
        -DOPENCV_SKIP_SYSTEM_PROCESSOR_DETECTION=TRUE
        -DAARCH64=${TARGET_IS_AARCH64}
        -DX86_64=${TARGET_IS_X86_64}
        -DX86=${TARGET_IS_X86}
        -DCPU_BASELINE=NEON        # <-- NEWLY ADDED LINE (IMPORTANT)
```
##### 3.
Then run below command in Windows terminal:
```
vcpkg install opencv4[core,calib3d,directml,dshow,fs,gapi,highgui,intrinsics,jpeg,msmf,png,quirc,thread,tiff,webp,win32ui]:arm64-windows --editable  --recurse
```

### 3.Run Application
##### Preparation
Before building and running the application, ensure that:
* The **real_esrgan_x4plus/** directory contains all required files, including:
  * `CMakeLists.txt` (see configuration below)
  * `real_esrgan_x4plus.cpp` and any additional source files
  * All required model files and DLLs are in their correct locations
* All DLL paths, model/image paths, and other hardcoded paths in your `real_esrgan_x4plus.cpp` and CMakeLists.txt are updated to match your environment.The specific steps to update these paths are provided below.
#### Step 1: Setup CMakeLists.txt and Source Code
##### 1. Configure `CMakeLists.txt`
Make sure to modify the following path settings in your CMakeLists.txt file to match your local environment:
```
# ❗ Replace these with the actual paths where your OpenCV is installed:
set(OPENCV_ROOT ${CMAKE_SOURCE_DIR}/opencv)
include_directories(${OPENCV_ROOT}/include)
link_directories(${OPENCV_ROOT}/Release)
link_directories(${OPENCV_ROOT}/lib)
```
* These paths configure the include directories, DLL binaries, and static library locations for OpenCV. If your setup differs, be sure to update accordingly.
* If OpenCV is installed via vcpkg, The following CMake code will automatically locate the path to the OpenCV library.
  ```
  find_package(OpenCV REQUIRED COMPONENTS core imgproc highgui imgcodecs videoio)
  ```
###### 2. Update Paths in Source Code
Inside your `real_esrgan_x4plus.cpp` file, modify the hardcoded paths to match the location of your model and runtime libraries. For example:
```
// ❗ Update these paths to reflect your actual file locations
fs::path execution_ws = fs::current_path();
fs::path backend_lib_path = execution_ws / "QnnHtp.dll";
fs::path system_lib_path = execution_ws / "QnnSystem.dll";
fs::path model_path = execution_ws / (MODEL_NAME + ".bin");
fs::path input_path = execution_ws / "input.jpg";
fs::path output_path = execution_ws / "output.jpg";
```

* These paths are currently relative paths, which means they are resolved relative to the location of the executable (`real_esrgan_x4plus.exe`) generated in the Release/ directory.
If your files are not placed in the expected locations relative to Release/, you will need to adjust these paths accordingly or use absolute paths instead.

#####  3. Build the Project with CMake 
In the Windows terminal, run the following command from the project root to configure the build:
```bash
cmake -B "%BUILD_DIR%" ^
      -G %GENERATOR% ^
      -A ARM64 ^
      -DCMAKE_BUILD_TYPE=Release ^
      -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=bin
```
#### Step 2: Build the Application
```bash
cmake --build "%BUILD_DIR%" --config Release
```
#### Step 3: Run the Application
In the Windows terminal, navigate to the Release folder and run:
```
cd Release
./WoA_Real_Esrgan_x4plus.exe
```
#### Step 4: Verifying Successful Execution
If the following files are generated, it indicates that `WoA_Real_Esrgan_x4plus.exe` has run successfully:
```
output.jpg
```
To simplify the operation flow, you can replace 'Run Application' by copying the following script(`clean_config_build_run.bat`) to the `real_esrgan_x4plus` folder and running it with administrator privileges. This script will automatically execute CMake's configuration and build process, then run the generated `WoA_Real_Esrgan_x4plus.exe`.
```
REM ==================================================================
REM Automated Build Script for real_esrgan_x4plus
REM Output: build/bin/Release/real_esrgan_x4plus.exe
REM ==================================================================

set ROOT_DIR=%~dp0
set BUILD_DIR=%ROOT_DIR%build
set PROJECT_DIR=%ROOT_DIR%
set OUTPUT_DIR=%BUILD_DIR%\bin
set PROJECT_NAME=WoA_Real_Esrgan_x4plus
set GENERATOR="Visual Studio 17 2022"

REM ------------------------------------------------------------------
REM 1. Clean previous builds
REM ------------------------------------------------------------------
echo Cleaning previous builds...
if exist "%BUILD_DIR%" (
    rmdir /s /q "%BUILD_DIR%" 2>nul
    if exist "%BUILD_DIR%" (
        echo [ERROR] Failed to remove %BUILD_DIR% 
        echo         Make sure no programs are using it and try again
        exit /b 1
    )
    echo ✓ Removed existing build directory
) else (
    echo ✓ No existing build to clean (first run)
)

REM ------------------------------------------------------------------
REM 2. Create build directory
REM ------------------------------------------------------------------
mkdir "%BUILD_DIR%" 2>nul

REM ------------------------------------------------------------------
REM 3. Configure CMake
REM ------------------------------------------------------------------
echo Configuring CMake for %PROJECT_NAME%...
cd "%PROJECT_DIR%"
call cmake -B "%BUILD_DIR%" ^
    -G %GENERATOR% ^
    -A ARM64 ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=bin

if errorlevel 1 (
    echo [ERROR] CMake configuration failed
    exit /b 1
)
echo ✓ CMake configuration successful

REM ------------------------------------------------------------------
REM 4. Build the project (generate real_esrgan_x4plus.exe)
REM ------------------------------------------------------------------
echo Building %PROJECT_NAME%...
call cmake --build "%BUILD_DIR%" --config Release

if errorlevel 1 (
    echo [ERROR] Build failed
    exit /b 1
)

REM Verify real_esrgan_x4plus.exe exists
if not exist "%OUTPUT_DIR%\Release\%PROJECT_NAME%.exe" (
    echo [ERROR] Target not found: %OUTPUT_DIR%\Release\%PROJECT_NAME%.exe
    exit /b 1
)
echo ✓ Compilation successful! Output in: %OUTPUT_DIR%\Release

REM ------------------------------------------------------------------
REM 5. Display build results
REM ------------------------------------------------------------------
cd ..
echo.
echo ====== BUILD OUTPUT ======
echo.
echo REAL_ESRGAN_X4PLUS Output:
echo   %CD%\%OUTPUT_DIR%\Release\%PROJECT_NAME%.exe
echo.
echo Build completed successfully!
echo Call %OUTPUT_DIR%\Release\%PROJECT_NAME%.exe

cd %OUTPUT_DIR%\Release
call %PROJECT_NAME%.exe
cd %PROJECT_DIR%

echo Press any key to exit...
pause >nul
```