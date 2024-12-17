#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# Compile Commands: 
# Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.28.0.241029\
# python setup.py bdist_wheel

import os
import platform
import re
import subprocess
import sys
from pathlib import Path
import shutil
import zipfile

from setuptools import Extension, setup, find_packages
from setuptools.command.build_ext import build_ext

VERSION = "2.28.0"
CONFIG = "Release"  # Release, RelWithDebInfo
package_name = "qai_appbuilder"

machine = platform.machine()
sysinfo = sys.version

generate = "-G \"Visual Studio 17 2022\""
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
PACKAGE_ZIP  = "QAI_AppBuilder-win_arm64-QNN" + VERSION + "-" + CONFIG + ".zip"
if arch == "ARM64EC":
    PACKAGE_ZIP  = "QAI_AppBuilder-win_arm64ec-QNN" + VERSION + "-" + CONFIG + ".zip"
elif arch == "aarch64":
    PACKAGE_ZIP  = "QAI_AppBuilder-linux_arm64-QNN" + VERSION + "-" + CONFIG + ".zip"

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
    if os.path.exists(binary_path + "/libappbuilder.pdb"):
        os.remove(binary_path + "/libappbuilder.pdb")
    if os.path.exists(binary_path + "/libappbuilder.so"):
        os.remove(binary_path + "/libappbuilder.so")

def build_cmake():
    if not os.path.exists("build"):
        os.mkdir("build")
    os.chdir("build")

    subprocess.run("cmake .. " + generate, shell=True)
    subprocess.run("cmake --build ./ --config " + CONFIG, shell=True)
    os.chdir("../")

    if os.path.exists("lib/" + CONFIG + "/QAIAppSvc.exe"):
        shutil.copy("lib/" + CONFIG +"/libappbuilder.dll", binary_path)
        shutil.copy("lib/" + CONFIG + "/QAIAppSvc.exe", binary_path)
    if os.path.exists("lib/" + CONFIG + "/libappbuilder.pdb"):
        shutil.copy("lib/" + CONFIG + "/libappbuilder.pdb", binary_path)
    if os.path.exists("lib/" + "libappbuilder.so"):
        shutil.copy("lib/" + "libappbuilder.so", binary_path)

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

        cmake_args = f" -DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}" + f" -DPYTHON_EXECUTABLE={sys.executable}" + f" -DCMAKE_BUILD_TYPE={cfg}"  # not used on MSVC, but no harm

        build_args = ""
        #if "CMAKE_ARGS" in os.environ:
        #    cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # We pass in the version to C++. You might not need to.
        cmake_args += f" -DVERSION_INFO={self.distribution.get_version()}"

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

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name=package_name,
    version=VERSION,
    packages=[package_name],
    package_dir={'': 'script'},
    package_data={"": ["*.dll", "*.pdb", "*.exe", "*.so"]},
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
    python_requires='>=3.8',
    install_requires=['pybind11>=2.11.1'
                      ],
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Qualcomm CE',
        'License :: OSI Approved :: BSD License',
        'Operating System :: Windows On Snapdragon"',
        'Programming Language :: Python :: 3.11',
    ],
)

build_release()
build_clean()
