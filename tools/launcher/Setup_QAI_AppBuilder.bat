@echo off
REM Run as administrator
REM Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This script requires administrator privileges. Please right-click and run as administrator.
    pause
    exit /b
)

REM Step 1: Check Git version
echo Checking Git version...
git --version > temp_git_version.txt 2>nul
findstr /R "^git version 2\.[4-9][3-9]\." temp_git_version.txt >nul
if %errorlevel%==0 (
    echo Git version is 2.43.0 or higher. Skipping installation.
    del temp_git_version.txt
) else (
    echo Installing Git...
    powershell -Command "Invoke-WebRequest -Uri https://github.com/git-for-windows/git/releases/download/v2.43.0.windows.1/Git-2.43.0-64-bit.exe -OutFile git-installer.exe"
    start /wait git-installer.exe /VERYSILENT /NORESTART
    del git-installer.exe
)
del temp_git_version.txt 2>nul

REM Step 2: Install Python 3.12.8 x64
echo Checking Python version...
python --version > temp_py_version.txt 2>nul
findstr /C:"Python 3.12.8" temp_py_version.txt >nul
if %errorlevel%==0 (
    echo Python 3.12.8 is already installed. Skipping installation.
    del temp_py_version.txt
    goto CheckVC
)
del temp_py_version.txt
echo Installing Python 3.12.8 x64...
powershell -Command "Invoke-WebRequest -Uri https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe -OutFile python-installer.exe"
start /wait python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
del python-installer.exe

:CheckVC
REM Step 3: Install Visual C++ Redistributable
echo Checking Visual C++ Redistributable...
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" >nul 2>&1
if %errorlevel%==0 (
    echo Visual C++ Redistributable is already installed. Skipping installation.
    goto InstallPythonPackages
)
echo Installing Visual C++ Redistributable...
powershell -Command "Invoke-WebRequest -Uri https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile vc_redist.x64.exe"
start /wait vc_redist.x64.exe /quiet /norestart
del vc_redist.x64.exe

:InstallPythonPackages
REM Step 4: Install Python dependencies
echo Installing Python packages...
pip install requests wget tqdm importlib-metadata qai-hub

REM Step 5: Clone QAI AppBuilder repository
if exist ai-engine-direct-helper (
    echo Repository 'ai-engine-direct-helper' already exists. Skipping clone.
) else (
    echo Cloning QAI AppBuilder repository...
    git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
)

REM Step 6: Run setup script
echo Running setup script...
if exist "ai-engine-direct-helper\samples\python\setup.py" (
    pushd ai-engine-direct-helper\samples
    python python\setup.py
    popd
) else (
    echo ERROR: setup.py not found in expected location.
)

echo Setup complete.
pause



