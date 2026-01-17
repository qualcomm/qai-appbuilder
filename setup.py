#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# Compile Commands:
# [windows]
# Set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.42.0.251225/
# Set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.38.0.250901/
# python setup.py bdist_wheel
# [linux]
# export QNN_SDK_ROOT=~/QAIRT/2.38.0.250901/
# python setup.py bdist_wheel --hexagonarch 81
# python setup.py bdist_wheel --hexagonarch 81 --toolchains aarch64-windows-msvc

import os
import platform
import re
import subprocess
import sys
from pathlib import Path
import shutil
import zipfile
from shlex import quote

from setuptools import Extension, setup, find_packages
from setuptools.command.build_ext import build_ext

VERSION = "2.42.0"
CONFIG = "Release"  # Release, RelWithDebInfo
package_name = "qai_appbuilder"

machine = platform.machine()
sysinfo = sys.version

generate = "-G \"Visual Studio 17 2022\""

# -----------------------------------------------------------------------------
# For "x64 Python + ARM64EC extension" builds:
# - CMake FindPython (new mode) enforces interpreter architecture == target arch
#   when both Interpreter and Development are requested, which fails here.
#   (ARM64EC target + x64 python.exe => "Wrong architecture").
#   (https://learn.arm.com/learning-paths/laptops-and-desktops/win_arm64ec_porting/how-to-2/)[5](https://getdocs.org/Cmake/docs/3.21/module/findpython)
# - pybind11 provides a compat/classic mode to avoid the new FindPython path,
#   selectable via PYBIND11_FINDPYTHON=COMPAT.
#   (https://stackoverflow.com/questions/64632484/cmake-python-cannot-use-the-interpreter)[4](https://github.com/msys2/MINGW-packages/issues/20569)
# - We also pass multiple Python executable variables to satisfy different
#   discovery code paths (Python_EXECUTABLE / Python3_EXECUTABLE / PYTHON_EXECUTABLE). 
#   (https://learn.arm.com/learning-paths/laptops-and-desktops/win_arm64ec_porting/how-to-2/)
# -----------------------------------------------------------------------------
def _cmake_python_hints():
    py = os.environ.get("PYTHON_X64_EXECUTABLE", sys.executable)
    # Quote for safety when paths include spaces.
    pyq = quote(py)
    # Force pybind11 to use classic/compat mode (avoid FindPython arch check).
    # See pybind11 CMake docs for PYBIND11_FINDPYTHON=COMPAT. [3](https://stackoverflow.com/questions/64632484/cmake-python-cannot-use-the-interpreter)[4](https://github.com/msys2/MINGW-packages/issues/20569)
    hints = []
    hints.append(f" -DPYBIND11_FINDPYTHON=COMPAT")
    # Make sure pybind11 classic mode finds the right version when multiple Pythons exist.
    hints.append(f" -DPYBIND11_PYTHON_VERSION={sys.version_info.major}.{sys.version_info.minor}")
    # Provide Python executable under both "new" and "old" variable names.
    hints.append(f" -DPython_EXECUTABLE={pyq}")
    hints.append(f" -DPython3_EXECUTABLE={pyq}")
    hints.append(f" -DPYTHON_EXECUTABLE={pyq}")
    return "".join(hints)


arch = "ARM64"

if machine == "AMD64" or "AMD64" in sysinfo:
    arch = "ARM64EC"
    generate += " -A " + arch

if machine == "aarch64":
    arch = "aarch64"
    generate = ""

print("-- Arch: " + arch)

python_path = "script"
binary_path = python_path + "/" + package_name
qai_libs_path = binary_path + "/libs"
os.makedirs(qai_libs_path, exist_ok=True)
init_path = os.path.join(qai_libs_path, "__init__.py")
with open(init_path, "w") as f:
    f.write("# This file marks this directory as a Python package.\n")

PACKAGE_ZIP  = "QAI_AppBuilder-win_arm64-QNN" + VERSION + "-" + CONFIG + ".zip"
if arch == "ARM64EC":
    PACKAGE_ZIP  = "QAI_AppBuilder-win_arm64ec-QNN" + VERSION + "-" + CONFIG + ".zip"
elif arch == "aarch64":
    PACKAGE_ZIP  = "QAI_AppBuilder-linux_arm64-QNN" + VERSION + "-" + CONFIG + ".zip"

QNN_SDK_ROOT = os.environ.get("QNN_SDK_ROOT")
if QNN_SDK_ROOT is None:
    print('QNN_SDK_ROOT environmental variable not set')
    exit(1)

print("-- QNN_SDK_ROOT: ", QNN_SDK_ROOT)

toolchain = None
hexagonarch = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--toolchains':
        toolchain = sys.argv[i + 1]
        i += 2
    elif sys.argv[i] == '--hexagonarch':
        hexagonarch = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv  # Now safe for setuptools

if hexagonarch is None:
    if sys.platform.startswith('win'): 
        dsp_arch    = "81"  # 73 For X-Elite device of QAIRT prior to 2.38.1.
    else: # TODO: linux or android.
        dsp_arch    = "68"
else:
    dsp_arch = hexagonarch

def zip_package(dirpath, outFullName):
    zip = zipfile.ZipFile(outFullName, "w", zipfile.ZIP_DEFLATED)
    for path, dirnames, filenames in os.walk(dirpath):
        fpath = path.replace(dirpath, '')
        for filename in filenames:
            zip.write(os.path.join(path, filename), os.path.join(fpath, filename))
    zip.close()

def build_clean():
    shutil.rmtree(python_path + "/qai_appbuilder.egg-info")
    shutil.rmtree("build")
    shutil.rmtree("lib")
    if os.path.exists(binary_path + "/QAIAppSvc.exe"):
        os.remove(binary_path + "/libappbuilder.dll")
        os.remove(binary_path + "/QAIAppSvc.exe")
    if os.path.exists(binary_path + "/QAIAppSvc.pdb"):
        os.remove(binary_path + "/QAIAppSvc.pdb")
    if os.path.exists(binary_path + "/libappbuilder.pdb"):
        os.remove(binary_path + "/libappbuilder.pdb")
    if os.path.exists(binary_path + "/libappbuilder.so"):
        os.remove(binary_path + "/libappbuilder.so")
    if os.path.exists(binary_path + "/Genie.dll"):
        os.remove(binary_path + "/Genie.dll")
    shutil.rmtree(qai_libs_path)

def build_cmake():
    if not os.path.exists("build"):
        os.mkdir("build")
    os.chdir("build")

    # subprocess.run("cmake .. " + generate, shell=True)
    # Pass Python/pybind11 hints to avoid FindPython arch mismatch for x64 python + ARM64EC target. 
    subprocess.run("cmake .. " + generate + _cmake_python_hints(), shell=True)

    subprocess.run("cmake --build ./ --config " + CONFIG, shell=True)
    os.chdir("../")

    if os.path.exists("lib/" + CONFIG + "/QAIAppSvc.exe"):
        shutil.copy("lib/" + CONFIG +"/libappbuilder.dll", binary_path)
        shutil.copy("lib/" + CONFIG + "/QAIAppSvc.exe", binary_path)
    if os.path.exists("lib/" + CONFIG + "/QAIAppSvc.pdb"):
        shutil.copy("lib/" + CONFIG + "/QAIAppSvc.pdb", binary_path)
    if os.path.exists("lib/" + CONFIG + "/libappbuilder.pdb"):
        shutil.copy("lib/" + CONFIG + "/libappbuilder.pdb", binary_path)
    if os.path.exists("lib/" + "libappbuilder.so"):
        shutil.copy("lib/" + "libappbuilder.so", binary_path)

    if toolchain is None:
        if sys.platform.startswith('win'): # Copy Genie library to 'lib' folder for compiling GenieBuilder pyd.
            LIB_PATH = QNN_SDK_ROOT + "/lib/aarch64-windows-msvc"
            if arch == "ARM64EC": 
                LIB_PATH = QNN_SDK_ROOT + "/lib/arm64x-windows-msvc"
        else: # TODO: linux or android.
            if os.path.exists(os.path.join(QNN_SDK_ROOT, 'lib', 'aarch64-oe-linux-gcc11.2', 'libGenie.so')):
                LIB_PATH = os.path.join(QNN_SDK_ROOT, 'lib', 'aarch64-oe-linux-gcc11.2')
            elif os.path.exists(os.path.join(QNN_SDK_ROOT, 'lib', 'aarch64-android', 'libGenie.so')):
                LIB_PATH = os.path.join(QNN_SDK_ROOT, 'lib', 'aarch64-android')
            elif os.path.exists(os.path.join(QNN_SDK_ROOT, 'lib', 'libGenie.so')):
                LIB_PATH = os.path.join(QNN_SDK_ROOT, 'lib')
            else:
                raise Exception('Failed to find "libGenie.so" in /usr/lib')
    else:
        LIB_PATH = QNN_SDK_ROOT + f"/lib/{toolchain}"

    os.makedirs("lib/Release", exist_ok=True)
    if os.path.exists(LIB_PATH + "/Genie.dll"):
        shutil.copy(LIB_PATH + "/Genie.dll", binary_path)
        shutil.copy(LIB_PATH + "/Genie.dll", "lib/Release")

    if os.path.exists(LIB_PATH + "/Genie.lib"):
        shutil.copy(LIB_PATH + "/Genie.lib", "lib/Release")

    DSP_LIB_PATH = QNN_SDK_ROOT + f"/lib/hexagon-v{dsp_arch}/unsigned"

    if os.path.exists(DSP_LIB_PATH + f"/libqnnhtpV{dsp_arch}.cat"):
        shutil.copy(DSP_LIB_PATH + f"/libqnnhtpV{dsp_arch}.cat", qai_libs_path)

    if os.path.exists(DSP_LIB_PATH + f"/libQnnHtpV{dsp_arch}Skel.so"):
        shutil.copy(DSP_LIB_PATH + f"/libQnnHtpV{dsp_arch}Skel.so", qai_libs_path)
    
    if os.path.exists(LIB_PATH + "/QnnHtp.dll"):
        shutil.copy(LIB_PATH + "/QnnHtp.dll", qai_libs_path)

    if os.path.exists(LIB_PATH + "/QnnCpu.dll"):
        shutil.copy(LIB_PATH + "/QnnCpu.dll", qai_libs_path)

    if os.path.exists(LIB_PATH + "/QnnHtpNetRunExtensions.dll"):
        shutil.copy(LIB_PATH + "/QnnHtpNetRunExtensions.dll", qai_libs_path)

    if os.path.exists(LIB_PATH + "/QnnHtpPrepare.dll"):
        shutil.copy(LIB_PATH + "/QnnHtpPrepare.dll", qai_libs_path)

    if os.path.exists(LIB_PATH + f"/QnnHtpV{dsp_arch}Stub.dll"):
        shutil.copy(LIB_PATH + f"/QnnHtpV{dsp_arch}Stub.dll", qai_libs_path)

    if os.path.exists(LIB_PATH + "/QnnSystem.dll"):
        shutil.copy(LIB_PATH + "/QnnSystem.dll", qai_libs_path)

    #linux or android
    if os.path.exists(LIB_PATH + "/libGenie.so"):
        shutil.copy(LIB_PATH + "/libGenie.so", binary_path)
        shutil.copy(LIB_PATH + "/libGenie.so", "lib/Release")

    if os.path.exists(DSP_LIB_PATH + f"/libQnnHtpV{dsp_arch}Skel.so"):
        shutil.copy(DSP_LIB_PATH + f"/libQnnHtpV{dsp_arch}Skel.so", qai_libs_path)

    if os.path.exists(LIB_PATH + "/libQnnHtp.so"):
        shutil.copy(LIB_PATH + "/libQnnHtp.so", qai_libs_path)

    if os.path.exists(LIB_PATH + "/libQnnCpu.so"):
        shutil.copy(LIB_PATH + "/libQnnCpu.so", qai_libs_path)

    if os.path.exists(LIB_PATH + "/libQnnHtpNetRunExtensions.so"):
        shutil.copy(LIB_PATH + "/libQnnHtpNetRunExtensions.so", qai_libs_path)

    if os.path.exists(LIB_PATH + "/libQnnHtpPrepare.so"):
        shutil.copy(LIB_PATH + "/libQnnHtpPrepare.so", qai_libs_path)

    if os.path.exists(LIB_PATH + f"/libQnnHtpV{dsp_arch}Stub.so"):
        shutil.copy(LIB_PATH + f"/libQnnHtpV{dsp_arch}Stub.so", qai_libs_path)

    if os.path.exists(LIB_PATH + "/libQnnSystem.so"):
        shutil.copy(LIB_PATH + "/libQnnSystem.so", qai_libs_path)

build_cmake()

# build release package for C++ based application.
def build_release():
    tmp_path = "lib/package"
    include_path = "lib/package/include"
    if not os.path.exists(tmp_path):
        os.mkdir(tmp_path)

    if os.path.exists("lib/" + CONFIG + "/QAIAppSvc.exe"):
        shutil.copy("lib/" + CONFIG + "/libappbuilder.dll", tmp_path)
        shutil.copy("lib/" + CONFIG + "/libappbuilder.lib", tmp_path)
        shutil.copy("lib/" + CONFIG + "/QAIAppSvc.exe", tmp_path)

    if os.path.exists("lib/" + CONFIG + "/libappbuilder.pdb"):
        shutil.copy("lib/" + CONFIG + "/libappbuilder.pdb", tmp_path)
    if os.path.exists("lib/" + CONFIG + "/QAIAppSvc.pdb"):
        shutil.copy("lib/" + CONFIG + "/QAIAppSvc.pdb", tmp_path)

    if os.path.exists("lib/" + "/libappbuilder.so"):
        shutil.copy("lib/" + "/libappbuilder.so", tmp_path)

    if not os.path.exists(include_path):
        os.mkdir(include_path)
    shutil.copy("src/LibAppBuilder.hpp", include_path)
    shutil.copy("src/Lora.hpp", include_path)

    zip_package(tmp_path, "dist/" + PACKAGE_ZIP)

class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath(Path(sourcedir).resolve())

class CMakeBuild(build_ext):
    def build_extension(self, ext: CMakeExtension) -> None:
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()

        cfg = CONFIG

        cmake_generator = os.environ.get("CMAKE_GENERATOR", "")

        # cmake_args = f" -DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}" + f" -DPYTHON_EXECUTABLE={sys.executable}" + f" -DCMAKE_BUILD_TYPE={cfg}"  # not used on MSVC, but no harm
        # IMPORTANT:
        # - Do NOT rely on FindPython (new) here: it rejects x64 interpreter for ARM64EC target when Development is requested. 
        # - Force pybind11 compat mode and pass x64 python executable via multiple variables.
        cmake_args = f" -DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}" + f" -DCMAKE_BUILD_TYPE={cfg}" + _cmake_python_hints()  # not used on MSVC, but no harm

        build_args = ""

        # We pass in the version to C++. You might not need to.
        # cmake_args += f" -DVERSION_INFO={self.distribution.get_version()}"

        # Single config generators are handled "normally"
        single_config = any(x in cmake_generator for x in {"NMake", "Ninja"})

        # CMake allows an arch-in-generator style for backward compatibility
        contains_arch = any(x in cmake_generator for x in {"ARM", "Win64"})

        if not single_config and not contains_arch and not arch == "aarch64":
            cmake_args += " -A " + arch

        # Multi-config generators have a different way to specify configs
        if not single_config:
            cmake_args += f" -DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{cfg.upper()}={extdir}"
            build_args += " --config " + cfg

        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            if hasattr(self, "parallel") and self.parallel:
                # CMake 3.12+ only.
                build_args += f" -j{self.parallel}"

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        print(cmake_args)
        print(build_args)
        print(ext.sourcedir)

        subprocess.run("cmake " + ext.sourcedir + cmake_args, cwd=build_temp, check=True, shell=True)
        subprocess.run("cmake --build . " + build_args, cwd=build_temp, check=True, shell=True)

with open("README.md", "r", encoding="utf-8", errors="ignore") as fh:
    long_description = fh.read()

setup(
    name=package_name,
    version=VERSION,
    packages=find_packages(where="script"),
    package_dir={'': 'script'},
    package_data={"": ["*.dll", "*.pdb", "*.exe", "*.so", "*.cat"]},
    ext_modules=[CMakeExtension("qai_appbuilder.appbuilder", "pybind")],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
    description='AppBuilder is Python & C++ extension that simplifies the process of developing AI prototype & App on WoS. It provides several APIs for running QNN models in WoS CPU & HTP, making it easier to manage AI models.',
    long_description=long_description,
    long_description_content_type="text/markdown",
    url='https://github.com/quic/ai-engine-direct-helper',
    author='quic-zhanweiw',
    author_email='quic_zhanweiw@quicinc.com',
    license='BSD-3-Clause',
    python_requires='>=3.10',
    # install_requires=['pybind11>=2.13.6'],
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Qualcomm CE',
        'License :: OSI Approved :: BSD License',
        'Operating System :: Windows On Snapdragon"',
        'Programming Language :: Python :: 3.12',
    ],
)

build_release()
build_clean()
