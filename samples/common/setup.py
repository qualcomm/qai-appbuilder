# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import argparse
import sys
import platform
import install

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--qnn-sdk-version", default=install.DEFAULT_SDK_VER, type=str)
    parser.add_argument("--dsp-arch", default=73, type=int)
    args = parser.parse_args()

    qnn_sdk_version = args.qnn_sdk_version
    dsp_arch = args.dsp_arch

    lib_arch = "aarch64-windows-msvc"
    machine = platform.machine()
    sysinfo = sys.version
    if machine == "AMD64" or "AMD64" in sysinfo:
        lib_arch = install.DEFAULT_LIB_ARCH

    star_number = 88

    print()
    print(star_number * "*")
    print("*                 You can press [Ctrl+C] to interrupt the current task.                *")
    print("*    If the downloading is interrupted, you can re-execute this script to continue.    *")
    print("*                    [Support Resume Broken Download]                                  *")
    print(star_number * "*")
    print()

    try:
        install.install_download_tools()

        install.install_qai_appbuilder(qnn_sdk_version, lib_arch)

        print()
        print(star_number * "*")
        print("*                           [Installation Succeeded.]                                  *")
        print(star_number * "*")
        print()
    except KeyboardInterrupt:
        pass
