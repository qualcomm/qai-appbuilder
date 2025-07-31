# GenieFletUI Android guide

GenieFletUI is an AI chat assistant. This is Android version.

## Environment setup

Step 1. Install python 3.12 or above.<br>
Step 2. Install necessary python module. Recommend to setup python virtual environment and intall modules with below commands.<br>
```
python -m venv flet_android_env
flet_android_env\Scripts\activate
pip install flet openai
```

## How to run in python environment

Step 1. Start cmd command prompt window and active python virtual environment with below command:
```
<flet_android_env path>\Scripts\activate
```
Step 2. Go to ai-engine-direct-helper samples path and start GenieFletUI application with below command:
```
cd <your path>\ai-engine-direct-helper-2.34\samples
python fletui\GenieFletUI\android\GenieFletUI.py
```

## How to build APK.
Building APK is a bit complicated. Recommend to build a simplest flet app firstly on Linux OS (building APK on Linux is more stable than on Windows). Refer to [BuildAPKGuide](buildapkguide/BuildAPKGuide.md). It will verify the building environment is good. And then replace your .py file to build your application APK.
