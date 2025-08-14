<br>

<div align="center">
  <h3> Building OpenCV for ARM64 on Windows</h3>
</div>
<br>
This guide provides step-by-step instructions for building and installing OpenCV for ARM64 architecture on Windows. You can choose between two methods:

* Using the vcpkg package manager
* Building from source via GitHub

## Prerequisites 
Before you begin, make sure the following tools are installed on your system:
* [Cmake](https://cmake.org/download/): Required to configure and generate build files. 
*  [Visual Studio 2022 or higher](https://visualstudio.microsoft.com/vs/): Make sure to install the Desktop development with C++ workload during setup.
* [Git](https://git-scm.com/downloads/win): Used to clone repositories from GitHub.
## 1.Setup OpenCV
### 1.1 Install OpenCV via vcpkg
#### 1.Clone the vcpkg Repository

```bash
git clone https://github.com/microsoft/vcpkg.git
cd vcpkg
.\bootstrap-vcpkg.bat
```
#### 2.Add to PATH
Add the full path to the vcpkg directory (e.g., C:\vcpkg) to your system PATH so you can run vcpkg from any terminal.

#### 3 Build OpenCV for ARM64
##### Step 1: Install OpenCV with required features
Run following commands in Windows terminal:
```
vcpkg install opencv4[core,win32ui,webp,tiff,thread,quirc,png,jpeg,intrinsics,highgui,gapi,fs,dshow,calib3d]:arm64-windows
```
##### Step 2: Modify portfile.cmake
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
##### Step 3: Reinstall OpenCV with editable mode
Then run below command in Windows terminal:
```
vcpkg install opencv4[core,calib3d,directml,dshow,fs,gapi,highgui,intrinsics,jpeg,msmf,png,quirc,thread,tiff,webp,win32ui]:arm64-windows --editable  --recurse
```
### 1.2 Build OpenCV from Source (GitHub）

Run following commands in Windows terminal:
```bash
# Clone the OpenCV repository
git clone https://github.com/opencv/opencv
cd opencv

# Checkout the specific version
git checkout tags/4.10.0

# Create and enter the build directory
mkdir build_msvc
cd build_msvc

# Configure the build with CMake
cmake -S .. -G "Visual Studio 17 2022" -A ARM64 -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DBUILD_EXAMPLES=OFF -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_DOCS=OFF -DBUILD_opencv_world=OFF -DWITH_CUDA=OFF -DWITH_OPENCL=OFF -DWITH_OPENVINO=OFF -DWITH_TBB=OFF

# Build the project
cmake --build . --config Release
cmake --build . --target INSTALL --config Release
```
### 2.Common Error
Regardless of the method used, you may encounter the following error:
```
C:\Program Files (x86)\Windows Kits\10\include\10.0.26100.0\ucrt\wchar.h(254): 
error C2664: '__n64 __uint64x1_t_to_n64(uint64x1_t)': 
cannot convert argument 1 from 'uint16x4_t' to 'uint64x1_t'
... arm64_neon.h(1957): see declaration of '__uint64x1_t_to_n64'
```

### Solution（Choose One）
To resolve this issue, you can use either of the following methods:
#### Option 1: Upgrade the Windows SDK
Upgrade the [Windows SDK](https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/) to version 10.0.26100.4188 or later (preferably the latest, such as 4654).Download the installer and run it to complete the upgrade.

#### Option 2: Apply a Patch
Manually modify the following file:
```
C:\Program Files (x86)\Windows Kits\10\include\10.0.26100.0\ucrt\wchar.h
```
Make the following changes:
```
@@ -228,7 +228,7 @@ typedef wchar_t _Wint_t;
         unsigned long Index = 0;
         wchar_t const* S = _S;
 
-    #if defined(_M_ARM64) || defined(_M_ARM64EC) || defined(_M_HYBRID_X86_ARM64) // ← Original line
+    #if 0//defined(_M_ARM64) || defined(_M_ARM64EC) || defined(_M_HYBRID_X86_ARM64) // ← Modified line
         if (_N >= 4)
         {
             uint16x8_t V2 = vdupq_n_u16(_C);
@@ -352,7 +352,7 @@ typedef wchar_t _Wint_t;
         wchar_t const* S1 = _S1;
         wchar_t const* S2 = _S2;
 
-    #if defined(_M_ARM64) || defined(_M_ARM64EC) || defined(_M_HYBRID_X86_ARM64) // ← Original line
+    #if 0//defined(_M_ARM64) || defined(_M_ARM64EC) || defined(_M_HYBRID_X86_ARM64) // ← Modified line
 
         while (Count + 8 <= _N)
         {
```
After making these changes, be sure to save the file before recompiling.