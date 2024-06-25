#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import os
import re
import subprocess
import sys
from pathlib import Path
import shutil
import zipfile

from setuptools import Extension, setup, find_packages
from setuptools.command.build_ext import build_ext

VERSION = "2.23.0"
CONFIG = "Release"  # Release, RelWithDebInfo

package_name = "qnnhelper"
python_path = "QNNHelper"
PACKAGE_ZIP  = "QNNHelper-win_arm64-QNN" + VERSION + "-" + CONFIG + ".zip"

def zip_package(dirpath, outFullName):
    zip = zipfile.ZipFile(outFullName, "w", zipfile.ZIP_DEFLATED)
    for path, dirnames, filenames in os.walk(dirpath):
        fpath = path.replace(dirpath, '')
        for filename in filenames:
            zip.write(os.path.join(path, filename), os.path.join(fpath, filename))
    zip.close()

def build_clean():
    shutil.rmtree("qnnhelper.egg-info")
    shutil.rmtree("build")
    shutil.rmtree("lib")
    os.remove(python_path + "/libqnnhelper.dll")
    os.remove(python_path + "/SvcQNNHelper.exe")
    if os.path.exists(python_path + "/libqnnhelper.pdb"):
        os.remove(python_path + "/libqnnhelper.pdb")

def build_cmake():
    if not os.path.exists("build"):
        os.mkdir("build")
    os.chdir("build")

    vs_version = " \"Visual Studio 17 2022\""

    subprocess.run("cmake .. -G " + vs_version + " -A ARM64")
    subprocess.run("cmake --build ./ --config " + CONFIG)
    os.chdir("../")

    shutil.copy("lib/" + CONFIG +"/libqnnhelper.dll", python_path)
    shutil.copy("lib/" + CONFIG + "/SvcQNNHelper.exe", python_path)
    if os.path.exists("lib/" + CONFIG + "/libqnnhelper.pdb"):
        shutil.copy("lib/" + CONFIG + "/libqnnhelper.pdb", python_path)

build_cmake()

# build release package for C++ based application.
def build_release():
    tmp_path = "lib/package"
    include_path = "lib/package/include"
    if not os.path.exists(tmp_path):
        os.mkdir(tmp_path)
    shutil.copy("lib/" + CONFIG + "/libqnnhelper.dll", tmp_path)
    shutil.copy("lib/" + CONFIG + "/libqnnhelper.lib", tmp_path)
    if os.path.exists("lib/" + CONFIG + "/libqnnhelper.pdb"):
        shutil.copy("lib/" + CONFIG + "/libqnnhelper.pdb", tmp_path)
    shutil.copy("lib/" + CONFIG + "/SvcQNNHelper.exe", tmp_path)
    if os.path.exists("lib/" + CONFIG + "/SvcQNNHelper.pdb"):
        shutil.copy("lib/" + CONFIG + "/SvcQNNHelper.pdb", tmp_path)

    if not os.path.exists(include_path):
        os.mkdir(include_path)
    shutil.copy("LibQNNHelper/src/LibQNNHelper.hpp", include_path)

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

        cmake_args = [
            f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
            f"-DCMAKE_BUILD_TYPE={cfg}",  # not used on MSVC, but no harm
        ]
        build_args = []
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # We pass in the version to C++. You might not need to.
        cmake_args += [f"-DVERSION_INFO={self.distribution.get_version()}"]

        # Single config generators are handled "normally"
        single_config = any(x in cmake_generator for x in {"NMake", "Ninja"})

        # CMake allows an arch-in-generator style for backward compatibility
        contains_arch = any(x in cmake_generator for x in {"ARM", "Win64"})

        if not single_config and not contains_arch:
            cmake_args += ["-A", "ARM64"]

        # Multi-config generators have a different way to specify configs
        if not single_config:
            cmake_args += [
                f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{cfg.upper()}={extdir}"
            ]
            build_args += ["--config", cfg]

        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            if hasattr(self, "parallel") and self.parallel:
                # CMake 3.12+ only.
                build_args += [f"-j{self.parallel}"]

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        subprocess.run(
            ["cmake", ext.sourcedir, *cmake_args], cwd=build_temp, check=True
        )
        subprocess.run(
            ["cmake", "--build", ".", *build_args], cwd=build_temp, check=True
        )

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name=package_name,
    version=VERSION,
    packages=[package_name],
    package_data={"": ["*.dll", "*.pdb", "*.exe"]},
    ext_modules=[CMakeExtension("qnnhelper.pyqnnhelper", "PyQNNHelper")],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
    description='QNNHelper is Python & C++ extension that simplifies the process of developing AI prototype & App on WoS. It provides several APIs for running QNN models in WoS CPU & HTP, making it easier to manage AI models.',
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
