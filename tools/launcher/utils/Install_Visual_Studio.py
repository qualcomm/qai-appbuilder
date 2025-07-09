import os
import subprocess
from pathlib import Path
from Install_Helper import *

VS_STUDIO_URL = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
VS_DOWNLOAD_PATH = Path(".") / "tools" / "vs_BuildTools.exe"

def install_vs_studio():
    try:
        print("Installing Visual Studio Build Tools...")
        
        args = [str(VS_DOWNLOAD_PATH), "--passive", "--wait", 
            "--add", "Microsoft.VisualStudio.Component.Windows11SDK.22621",
            "--add", "Microsoft.VisualStudio.Component.VC.14.34.17.4.x86.x64",
            "--add", "Microsoft.VisualStudio.Component.VC.CMake.Project"
        ]

        result = subprocess.run(args, check=True)
        
        if result.returncode == 0:
            print("Visual Studio Build Tools installed successfully.")
            return True
        else:
            print(f"Install failed with return code: {result.returncode}")
            return False

    except subprocess.CalledProcessError as e:
        print(f"Install failed: {e}")
        return False
    except Exception as e:
        print(f"Unknown error occurred: {e}")
        return False

def download_and_install_vs():
    print("Downloading the Visual Studio...")
    
    if download_file_with_progress(VS_STUDIO_URL, VS_DOWNLOAD_PATH):
        print("Installing Visual Studio...")
        if install_vs_studio():
            print("Visual Studio installed successfully.")
            return True
        else:
            print(f"Visual Studio installation failed from: {VS_DOWNLOAD_PATH}")
            return False
    else:
        print(f"Visual Studio download failed. Download it manually from: {VS_STUDIO_URL}")
        return False

def main():
    if download_and_install_vs():
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
