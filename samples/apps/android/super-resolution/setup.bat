@echo off
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo ============================================================
echo Step 0: Check and install Android Studio
echo ============================================================

set AS_INSTALLED=0

REM Check common Android Studio installation paths
if exist "%PROGRAMFILES%\Android\Android Studio\bin\studio64.exe" set AS_INSTALLED=1
if exist "%PROGRAMFILES%\Android\Android Studio\bin\studio.exe"   set AS_INSTALLED=1
if exist "%LOCALAPPDATA%\Programs\Android Studio\bin\studio64.exe" set AS_INSTALLED=1
if exist "%LOCALAPPDATA%\Programs\Android Studio\bin\studio.exe"   set AS_INSTALLED=1

REM Also check via registry (machine-wide and per-user installs)
if "%AS_INSTALLED%"=="0" (
    reg query "HKLM\SOFTWARE\Android Studio" >nul 2>&1 && set AS_INSTALLED=1
)
if "%AS_INSTALLED%"=="0" (
    reg query "HKCU\SOFTWARE\Android Studio" >nul 2>&1 && set AS_INSTALLED=1
)

if "%AS_INSTALLED%"=="1" (
    echo Android Studio is already installed. Skipping installation.
) else (
    echo Android Studio is not detected. Downloading and installing...

    set AS_INSTALLER=android-studio-installer.exe
    set AS_URL=https://redirector.gvt1.com/edgedl/android/studio/install/2024.3.2.14/android-studio-2024.3.2.14-windows.exe

    if not exist "!AS_INSTALLER!" (
        echo Downloading Android Studio installer...
        curl -L --ssl-no-revoke -o "!AS_INSTALLER!" "!AS_URL!"
        if errorlevel 1 (
            echo ERROR: Failed to download Android Studio installer.
            echo Please download and install Android Studio manually from:
            echo   https://developer.android.com/studio
            exit /b 1
        )
    ) else (
        echo Android Studio installer already downloaded, skipping download.
    )

    echo Running Android Studio installer silently...
    echo NOTE: This may take several minutes. Please wait...
    "!AS_INSTALLER!" /S
    if errorlevel 1 (
        echo ERROR: Android Studio installation failed.
        echo Please install Android Studio manually from:
        echo   https://developer.android.com/studio
        del /F /Q "!AS_INSTALLER!"
        exit /b 1
    )

    del /F /Q "!AS_INSTALLER!"
    echo Android Studio installation complete.
)

echo.
echo ============================================================
echo Step 1: Download and extract QAIRT SDK
echo ============================================================

set QAIRT_ZIP=QAIRT_v2.48.40.260702.zip
set QAIRT_URL=https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/QAIRT_v2.48.40.260702.zip
set EXTERNAL_DIR=app\src\main\cpp\External
set LIBS_DIR=app\libs

if not exist "%QAIRT_ZIP%" (
    echo Downloading %QAIRT_ZIP% ...
    curl -L --ssl-no-revoke -o "%QAIRT_ZIP%" "%QAIRT_URL%"
    if errorlevel 1 (
        echo ERROR: Failed to download %QAIRT_ZIP%
        exit /b 1
    )
) else (
    echo %QAIRT_ZIP% already exists, skipping download.
)

echo Extracting %QAIRT_ZIP% ...
powershell -NoProfile -Command "Expand-Archive -Path '%QAIRT_ZIP%' -DestinationPath 'qairt_tmp' -Force"
if errorlevel 1 (
    echo ERROR: Failed to extract %QAIRT_ZIP%
    exit /b 1
)

echo Copying QAIAppBuilder to %EXTERNAL_DIR% ...
if not exist "%EXTERNAL_DIR%" mkdir "%EXTERNAL_DIR%"
xcopy /E /I /Y "qairt_tmp\QAIRT_v2.48.40.260702\QAIAppBuilder" "%EXTERNAL_DIR%\QAIAppBuilder"
if errorlevel 1 (
    echo ERROR: Failed to copy QAIAppBuilder
    exit /b 1
)

echo Copying arm64-v8a to %LIBS_DIR% ...
if not exist "%LIBS_DIR%" mkdir "%LIBS_DIR%"
xcopy /E /I /Y "qairt_tmp\QAIRT_v2.48.40.260702\lib\arm64-v8a" "%LIBS_DIR%\arm64-v8a"
if errorlevel 1 (
    echo ERROR: Failed to copy arm64-v8a
    exit /b 1
)

echo Cleaning up QAIRT temporary files ...
rmdir /S /Q qairt_tmp
del /F /Q "%QAIRT_ZIP%"

echo QAIRT SDK setup complete.

echo.
echo ============================================================
echo Step 2: Clone xtensor and xtl
echo ============================================================

cd "%EXTERNAL_DIR%"

if not exist "xtensor" (
    echo Cloning xtensor 0.25.0 ...
    git clone https://github.com/xtensor-stack/xtensor.git -b 0.25.0
    if errorlevel 1 (
        echo ERROR: Failed to clone xtensor
        exit /b 1
    )
) else (
    echo xtensor already exists, skipping clone.
)

if not exist "xtl" (
    echo Cloning xtl 0.7.7 ...
    git clone https://github.com/xtensor-stack/xtl.git -b 0.7.7
    if errorlevel 1 (
        echo ERROR: Failed to clone xtl
        exit /b 1
    )
) else (
    echo xtl already exists, skipping clone.
)

cd /d "%SCRIPT_DIR%"

echo xtensor and xtl setup complete.

echo.
echo ============================================================
echo Step 3: Download and extract OpenCV Android SDK
echo ============================================================

set OPENCV_ZIP=opencv-4.12.0-android-sdk.zip
set OPENCV_URL=https://github.com/opencv/opencv/releases/download/4.12.0/opencv-4.12.0-android-sdk.zip

if not exist "%OPENCV_ZIP%" (
    echo Downloading %OPENCV_ZIP% ...
    curl -L --ssl-no-revoke -o "%OPENCV_ZIP%" "%OPENCV_URL%"
    if errorlevel 1 (
        echo ERROR: Failed to download %OPENCV_ZIP%
        exit /b 1
    )
) else (
    echo %OPENCV_ZIP% already exists, skipping download.
)

echo Extracting %OPENCV_ZIP% ...
powershell -NoProfile -Command "Expand-Archive -Path '%OPENCV_ZIP%' -DestinationPath 'opencv_tmp' -Force"
if errorlevel 1 (
    echo ERROR: Failed to extract %OPENCV_ZIP%
    exit /b 1
)

echo Copying OpenCV native to %EXTERNAL_DIR%\OpenCV ...
xcopy /E /I /Y "opencv_tmp\OpenCV-android-sdk\sdk\native" "%EXTERNAL_DIR%\OpenCV"
if errorlevel 1 (
    echo ERROR: Failed to copy OpenCV native
    exit /b 1
)

echo Cleaning up OpenCV temporary files ...
rmdir /S /Q opencv_tmp
del /F /Q "%OPENCV_ZIP%"

echo OpenCV setup complete.

echo.
echo ============================================================
echo All dependencies set up successfully!
echo ============================================================

endlocal
