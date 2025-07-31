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
First, create a directory named beit at any location (e.g., C:\ or D:\). Inside the beit/ folder, create a folder named **3rd/** .
The final project structure should look like this:
```plaintext
beit/
├── 3rd/              # Third-party dependencies (e.g., QNN SDK, py3_wget)
├── models/           # Model files (e.g., .bin, .json)
├── qai_libs/         # QNN SDK runtime libraries (e.g., HTP backend)
├── Release/          # Directory generated after successful compilation; contains the beit.exe binary.
├── beit.cpp          # C++ inference main program
├── input.jpg         # Sample input jpg
├── CMakeLists        # CMake configuration 
├── other             # Other files
└── README.md         # Project documentation
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
Copy the entire **qai_libs/** folder into your local beit/ project directory.

#### Step 2: Download BEit file
The essential files needed for BEiT model inference are already downloaded when you run beit.py after launching the Python environment via **8.Start_PythonEnv.bat**.
So, there’s no need to re-download them—you just need to locate and copy them into the correct locations.
Open a terminal in C:\ai-hub\，and run:
```
.\8.Start_PythonEnv.bat
pip install py3-wget
cd ../ai-engine-direct-helper/samples
python python\beit\beit.py
```
**Note: All operations in this step are performed inside the virtual environment py312, which is automatically created and activated by 8.Start_PythonEnv.bat.**
Then navigate to:
```
ai-engine-direct-helper/samples/python/beit/
```
Copy the following files into your local beit/ directory:
* **models/** folder (contains the BEIT model and imagenet_labels.txt files)
* **input.jpg** (sample image for inference)

Place all files in the appropriate directories under your local beit/ project folder to ensure the inference script can access them correctly.


#### Step 3: Download QAI_AppBuilder

Click here to download [QAI_AppBuilder-win_arm64-QNN2.34.0-Release](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAI_AppBuilder-win_arm64-QNN2.34.0-Release.zip).Unzip the contents of the archive into the 3rd/ folder inside your beit/ project directory. The structure should look like:
```plaintext
beit/
├── 3rd/
│   └── QAI_AppBuilder/
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
* The **beit/** directory contains all required files, including:
  * CMakeLists.txt (see configuration below)
  * beit.cpp and any additional source files
  * All required model files and DLLs are in their correct locations
* All DLL paths, model/image paths, and other hardcoded paths in your beit.cpp and CMakeLists.txt are updated to match your environment.The specific steps to update these paths are provided below.
#### Step 1: Setup CMakeLists.txt and Source Code
##### 1. Configure CMakeLists.txt
Make sure to modify the following path settings in your CMakeLists.txt file to match your local environment:
```
# ❗ Replace these with the actual paths where your OpenCV is installed:
set(OpenCV_DIR "C:/vcpkg/installed/arm64-windows/share/opencv4")
set(APPBUILDER_DIR "${CMAKE_SOURCE_DIR}/3rd/QAI_AppBuilder-win_arm64-QNN2.34.0-Release")
set(APPBUILDER_DLL "${APPBUILDER_DIR}/libappbuilder.dll")
```
* These paths configure the include directories, DLL binaries, and static library locations for OpenCV. If your setup differs, be sure to update accordingly.
* If OpenCV is installed via vcpkg, then set:
  ```
  set(OpenCV_DIR "path/to/vcpkg/installed/arm64-windows/share/opencv4")
   ```
  If OpenCV is installed via GitHub (manual build), then set:
  ```
  set(OpenCV_DIR "path/to/opencv/build_msvc")
  ```
###### 2. Update Paths in Source Code
Inside your beit.cpp file, modify the hardcoded paths to match the location of your model and runtime libraries. For example:
```
// ❗ Update these paths to reflect your actual file locations
std::string model_path = "../models/beit-beit-qualcomm_snapdragon_x_elite-float.bin";
std::string  backendLib = "../qai_libs/QnnHtp.dll";
std::string  systemLib = "../qai_libs/QnnSystem.dll";
```
```
std::string image_path = "../input.jpg";
std::string json_path = "../models/imagenet_labels.json";
```
* These paths are currently relative paths, which means they are resolved relative to the location of the executable (beit.exe) generated in the Release/ directory.
If your files are not placed in the expected locations relative to Release/, you will need to adjust these paths accordingly or use absolute paths instead.

#####  3. Build the Project with CMake 
In the Windows terminal, run the following command from the project root to configure the build:
```bash
cmake -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake -DCMAKE_BUILD_TYPE=Release
```
After running this command, a file named beit.sln will appear in the directory.
#### Step 2: Build the Application
Open beit.sln with visual studio. Select Release/ARM64. Then Build-build solution
#### Step 3: Run the Application
In the Windows terminal, navigate to the Release folder and run:
```
cd Release
./beit.exe
```
#### Step 4: Verifying Successful Execution
If the service prints output similar to the following, it indicates that beit.exe has run successfully:
```
"Miniature Poodle",: 62.0111%
"Toy Poodle",: 28.6134%
"Standard Poodle",: 0.724766%
"Yorkshire Terrier",: 0.200868%
"Norwich Terrier",: 0.128179%
```