#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

include(ExternalProject)

# ARM64 上深层路径的 out-of-source try_compile/try_run 会在 CMakeTestCCompiler 等检测阶段
# 触发 MSBuild tlog 路径失败(FTK1011)。已实测确认：真正根因是 Visual Studio 生成器背后 MSBuild
# 用于增量构建依赖跟踪的 FileTracker 组件(Tracker.exe / FileTracker*.dll)从未适配 Windows 长路径特性，
# 不受注册表 LongPathsEnabled 开关影响，仍固守传统 ~260 字符路径限制；并非 cl.exe/clang 谁支持长路径的问题。
# 对只触发一次基本编译器检测的子构建(Libappbuilder)，可以预先声明 CMAKE_C_COMPILER_WORKS=1 跳过该检测；
# 对额外还会触发 test_big_endian()/check_include_file()/check_c_source_compiles()/check_c_source_runs()
# 等多次独立 try_compile/try_run 的子构建(Libsamplerate)，通过预置对应的缓存变量(详见其定义处注释)
# 逐一跳过，两者均可使用默认生成器（继承顶层 Visual Studio + cl.exe）；其余子构建（MNN、llama.cpp，
# 以及 llama.cpp GPU 后端依赖的 OpenCLHeaders/OpenCLICDLoader）仍使用 Ninja + LLVM Clang 工具链
# (VS 自带的 ARM64 Clang)，因为它们不走 MSBuild/FileTracker，可以绕开该限制。
set(VS_VC_PATH "C:/Program Files/Microsoft Visual Studio/2022/Community/VC")
set(VS_VC_BUILD_TOOL_PATH "C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC")
# 依次尝试 Community 与 Build Tools 两种安装形态的 VC 根目录（Community 优先，找不到再退到
# Build Tools），覆盖“机器上只装了 Visual Studio Build Tools 而非完整 Community”这种常见环境
# 差异——此前 VS_VC_BUILD_TOOL_PATH 定义后从未被实际使用，导致只装了 Build Tools 的机器上
# clang/lld 与 libomp140.aarch64.dll 的探测都固定失败在 VS_VC_PATH 这一条路径上。
set(VS_VC_SEARCH_PATHS ${VS_VC_PATH} ${VS_VC_BUILD_TOOL_PATH})
if (MSVC)
    set(MSVC_CLANG_COMPILER "")
    set(MSVC_CLANG_LINKER "")
    foreach (_vs_vc_candidate ${VS_VC_SEARCH_PATHS})
        if (NOT MSVC_CLANG_COMPILER AND EXISTS "${_vs_vc_candidate}/Tools/Llvm/ARM64/bin/clang.exe")
            set(MSVC_CLANG_COMPILER ${_vs_vc_candidate}/Tools/Llvm/ARM64/bin/clang.exe)
            set(MSVC_CLANG_LINKER ${_vs_vc_candidate}/Tools/Llvm/ARM64/bin/lld.exe)
        endif ()
    endforeach ()
    if (NOT MSVC_CLANG_COMPILER)
        # 两条候选路径下都没找到时，退回 VS_VC_PATH 下的路径（保留原有行为），让后续
        # 构建阶段针对该路径给出的报错更直观、可定位。
        set(MSVC_CLANG_COMPILER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/clang.exe)
        set(MSVC_CLANG_LINKER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/lld.exe)
    endif ()
endif ()

# Library naming differs on each platform:
#   Windows : "Genie.dll" / "Genie.lib"
#   Linux   : "libGenie.so"
# We use LIB_PREFIX + name + DLL_EXT to assemble the file names uniformly.
if (MSVC)
    set(DLL_EXT ".dll")
    set(EXE_EXT ".exe")
    set(LIB_PREFIX "")
    set(QNN_PLATFORM "aarch64-windows-msvc")
elseif (UNIX)
    # Native Linux (typically aarch64-oe-linux-gcc11.2 from QAIRT)
    set(EXE_EXT "")
    set(DLL_EXT ".so")
    set(LIB_PREFIX "lib")
    if (NOT DEFINED QNN_PLATFORM)
        set(QNN_PLATFORM "aarch64-oe-linux-gcc11.2")
    endif ()
else ()
    message(FATAL_ERROR "only Windows / Linux platforms are supported")
endif ()

#  Define EXTERNAL_BIN and PATH
set(LIBAPPBUILDER_ROOT ${G_EXTERNAL_DIR}/../../../..)
# Normalize QNN_SDK_ROOT to ensure it ends with a slash
file(TO_CMAKE_PATH "$ENV{QNN_SDK_ROOT}" QNN_SDK_ROOT_DIR)
set(QNN_BIN_PATH "${QNN_SDK_ROOT_DIR}/bin/${QNN_PLATFORM}")
set(QNN_LIB_PATH "${QNN_SDK_ROOT_DIR}/lib/${QNN_PLATFORM}")
set(_QNN_RUNTIME_FILES
        ${QNN_BIN_PATH}/genie-t2t-run${EXE_EXT}
        ${QNN_LIB_PATH}/${LIB_PREFIX}Genie${DLL_EXT}
        ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtp${DLL_EXT}
        ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtpNetRunExtensions${DLL_EXT}
        ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtpPrepare${DLL_EXT}
        ${QNN_LIB_PATH}/${LIB_PREFIX}QnnSystem${DLL_EXT}
)
# Per-DSP-architecture runtime files (Stub / Skel / .cat). QNN_STUB_VERSION may
# list more than one Hexagon architecture (e.g. v73;v81) so the packaged
# service can run on multiple NPU generations.
foreach (_qnn_stub_ver ${QNN_STUB_VERSION})
    set(_QNN_STUB_PATH "${QNN_SDK_ROOT_DIR}/lib/hexagon-${_qnn_stub_ver}/unsigned")
    list(APPEND _QNN_RUNTIME_FILES
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtp${_qnn_stub_ver}Stub${DLL_EXT}
            ${_QNN_STUB_PATH}/libQnnHtp${_qnn_stub_ver}Skel.so
            ${_QNN_STUB_PATH}/libqnnhtp${_qnn_stub_ver}.cat
    )
endforeach ()
list(APPEND _QNN_RUNTIME_FILES
        ${QNN_SDK_ROOT_DIR}/examples/Genie/configs/htp_backend_ext_config.json
)
if (UNIX)
    # Different QAIRT SDK releases don't always ship the exact same set of runtime
    # files (e.g. .cat files, some stub libs); filter out missing ones instead of
    # letting a hard-coded list break the copy step for a slightly different SDK layout.
    set(EXTERNAL_BIN "")
    foreach (_qnn_file ${_QNN_RUNTIME_FILES})
        if (EXISTS ${_qnn_file})
            list(APPEND EXTERNAL_BIN ${_qnn_file})
        else ()
            message(STATUS "[platform] Skipping missing optional QNN runtime file: ${_qnn_file}")
        endif ()
    endforeach ()
else ()
    set(EXTERNAL_BIN ${_QNN_RUNTIME_FILES})
endif ()

# Define EXTERNAL_LIB and PATH
if (MSVC)
    # On Windows the linker takes "Genie.dll" / "Genie.lib" by full file name.
    set(EXTERNAL_LIBS Genie)
else ()
    # On Linux the linker prefers the un-decorated lib name (-lGenie).
    set(EXTERNAL_LIBS Genie)
endif ()
if (UNIX)
    list(APPEND EXTERNAL_LIBS pthread dl)
endif ()
set(EXTERNAL_LIB_PATH ${QNN_LIB_PATH})

# mbedTLS is fetched at build time from its git repository (like the OpenCL
# headers/loader) instead of being consumed from the vendored External/mbedtls
# checkout. mbedTLS relies on its own git submodules (framework/), which a plain
# source archive would not include, so it is pulled with recursive submodules.
set(MBEDTLS_SRC_DIR ${CMAKE_BINARY_DIR}/mbedtls-src)

#  Define EXTERNAL_HEADER
set(EXTERNAL_HEADER_PATH
        ${G_EXTERNAL_DIR}
        ${G_EXTERNAL_DIR}/LibrosaCpp
        ${G_EXTERNAL_DIR}/libsamplerate/include
        ${G_EXTERNAL_DIR}/dr_libs
        ${G_EXTERNAL_DIR}/stb
        ${G_EXTERNAL_DIR}/cpp-httplib
        ${G_EXTERNAL_DIR}/json/single_include
        ${G_EXTERNAL_DIR}/CLI11/include
        ${G_EXTERNAL_INCLUDE_PATH}/Genie
        $ENV{QNN_SDK_ROOT}/include/Genie
        ${LIBAPPBUILDER_ROOT}/src
        ${MBEDTLS_SRC_DIR}/include
)


if (MSVC)
    add_definitions(-DUSE_WINHTTP)
    list(APPEND EXTERNAL_LIBS winhttp)
elseif (UNIX)
    add_definitions(-DCPPHTTPLIB_MBEDTLS_SUPPORT)
    # Keep the mbedTLS out-of-source build directory *outside* the freshly-cloned
    # source tree. Some ExternalProject/git combinations do not auto-create a
    # BINARY_DIR that sits under SOURCE_DIR (git checkout of the empty subdir is
    # skipped), which historically caused the configure step to fail with
    # `cd: can't cd to <SOURCE_DIR>/build`.
    set(MBEDTLS_BUILD_DIR ${CMAKE_BINARY_DIR}/mbedtls-build)
    set(MBEDTLS_LIB_PATH ${MBEDTLS_BUILD_DIR}/library)
    list(APPEND EXTERNAL_LIB_PATH ${MBEDTLS_LIB_PATH})
    list(APPEND EXTERNAL_LIBS mbedtls mbedx509 mbedcrypto)

    # mbedTLS pulls in its own submodules (framework/), so GIT_SUBMODULES_RECURSE is on.
    ExternalProject_Add(Libmbedtls
            GIT_REPOSITORY https://github.com/Mbed-TLS/mbedtls.git
            GIT_TAG mbedtls-3.6.5
            GIT_SHALLOW ON
            GIT_SUBMODULES_RECURSE ON
            SOURCE_DIR ${MBEDTLS_SRC_DIR}
            BINARY_DIR ${MBEDTLS_BUILD_DIR}
            CMAKE_ARGS
            -DCMAKE_BUILD_TYPE=Release
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            -DENABLE_TESTING=OFF
            -DENABLE_PROGRAMS=OFF
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE OFF
    )
endif ()


if (MSVC)
    # ExternalProject 对子构建目录执行独立的 CMakeTestCCompiler 检测时，会在
    # <BINARY_DIR>/CMakeFiles/CMakeScratch/TryCompile-xxxx/... 下生成比顶层项目自己构建更深的
    # 临时路径，实测会触发历史 FTK1011(FileTracker tlog 路径过长)问题。顶层项目自己
    # 不触发这个问题是因为它缓存了 CMAKE_C_COMPILER_WORKS/CMAKE_CXX_COMPILER_WORKS，跳过了
    # 这一步独立 TryCompile。另外，Libappbuilder 自己的 CMakeLists.txt 硬编码了 /MD /O2 等
    # MSVC 风格开关，而其 LibAppBuilder.cpp 里几个 ModelInitialize 的类外定义与头文件声明
    # 参数不完全匹配(缺少 deviceID/coreIdsStr 两个尾部默认参数)，这是 cl.exe 宽容但 clang(-cl)
    # 拒绝的遗留非标准写法，因此无法改用 Ninja+Clang(-cl)。因此采用默认生成器(cl.exe)，
    # 并显式预先声明 CMAKE_C_COMPILER_WORKS/CMAKE_CXX_COMPILER_WORKS 为已知可用(该编译器已在
    # 顶层项目自己的构建中得到验证)，跳过导致 FTK1011 的 TryCompile 检测，而不需要回退到
    # Ninja + Clang。
    ExternalProject_Add(Libappbuilder
            SOURCE_DIR ${LIBAPPBUILDER_ROOT}
            BINARY_DIR ${CMAKE_BINARY_DIR}/libappbuilder-build
            CMAKE_ARGS
            -DCMAKE_C_COMPILER_WORKS=1
            -DCMAKE_CXX_COMPILER_WORKS=1
            -DCMAKE_BUILD_TYPE=Release
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE OFF
    )

    list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/lib/Release)
    list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/lib/Release/libappbuilder${DLL_EXT})
    list(APPEND EXTERNAL_LIBS libappbuilder)
elseif (UNIX)
    # Native Linux: build libappbuilder.so via the top-level CMake project
    ExternalProject_Add(Libappbuilder
            SOURCE_DIR ${LIBAPPBUILDER_ROOT}
            BINARY_DIR ${CMAKE_BINARY_DIR}/libappbuilder-build
            CMAKE_ARGS
            -DCMAKE_BUILD_TYPE=Release
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE OFF
    )

    list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/lib)
    list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/lib/libappbuilder${DLL_EXT})
    list(APPEND EXTERNAL_LIBS appbuilder)
endif ()
list(APPEND EXTERNAL_LIBS samplerate)

if (MSVC)
    # libsamplerate 自己的 CMakeLists.txt/src/CMakeLists.txt/cmake/ClipMode.cmake 除基本编译器检测外，
    # 还会独立触发 test_big_endian()、3 次 check_include_file()、check_c_source_compiles()(HAVE_VISIBILITY)、
    # check_c_source_runs()(CPU_CLIPS_POSITIVE/NEGATIVE) 等多次 try_compile/try_run，每一次都可能命中
    # FileTracker 的路径限制(FTK1011)。以下 10 个缓存变量均已在本机 ARM64/MSVC 工具链上实测验证取值，
    # 预置后可让这些检测全部走快速路径、不再实际触发 try_compile/try_run，从而可以使用默认生成器
    # （继承顶层 Visual Studio + cl.exe），与 Libappbuilder 一致。
    ExternalProject_Add(Libsamplerate
            SOURCE_DIR ${G_EXTERNAL_DIR}/libsamplerate
            BINARY_DIR ${CMAKE_BINARY_DIR}/libsamplerate-build
            CMAKE_ARGS
            -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            -DLIBSAMPLERATE_EXAMPLES=OFF
            -DBUILD_TESTING=OFF
            -DCMAKE_C_COMPILER_WORKS=1
            -DCMAKE_C_ABI_COMPILED=1
            -DCMAKE_C_BYTE_ORDER=LITTLE_ENDIAN
            -DCMAKE_SIZEOF_VOID_P=8
            -DHAVE_STDBOOL_H=1
            -DHAVE_UNISTD_H=0
            -DHAVE_IMMINTRIN_H=0
            -DHAVE_VISIBILITY=0
            -DCPU_CLIPS_POSITIVE=1
            -DCPU_CLIPS_NEGATIVE=1
            BUILD_IN_SOURCE OFF
    )
else ()
    # Linux: use the standard system GCC/Clang toolchain, no forced Ninja generator.
    ExternalProject_Add(Libsamplerate
            SOURCE_DIR ${G_EXTERNAL_DIR}/libsamplerate
            BINARY_DIR ${CMAKE_BINARY_DIR}/libsamplerate-build
            CMAKE_ARGS
            -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            -DLIBSAMPLERATE_EXAMPLES=OFF
            -DBUILD_TESTING=OFF
            BUILD_IN_SOURCE OFF
    )
endif ()
list(APPEND EXTERNAL_LIB_PATH ${CMAKE_BINARY_DIR}/lib)

if (MSVC)
    if (USE_MNN)
        list(APPEND EXTERNAL_HEADER_PATH
                ${G_EXTERNAL_DIR}/MNN/transformers/llm/engine/include
                ${G_EXTERNAL_DIR}/MNN/include/
        )

        ExternalProject_Add(Libmnn
                SOURCE_DIR ${G_EXTERNAL_DIR}/MNN
                BINARY_DIR ${CMAKE_BINARY_DIR}/mnn-build
                CMAKE_GENERATOR Ninja
                CMAKE_ARGS
                -DCMAKE_SYSTEM_NAME=Windows
                -DCMAKE_SYSTEM_PROCESSOR=ARM64
                -DCMAKE_C_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_CXX_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_LINKER=${MSVC_CLANG_LINKER}
                -DLLM_SUPPORT_VISION=ON
                -DMNN_LOW_MEMORY=true
                -DMNN_CPU_WEIGHT_DEQUANT_GEMM=true
                -DMNN_BUILD_LLM=true
                -DMNN_SUPPORT_TRANSFORMER_FUSE=true
                -DMNN_USE_SSE=OFF
                -DMNN_BUILD_TOOLS=OFF
                -DCMAKE_BUILD_TYPE=Release
                -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
                -DMNN_KLEIDIAI=FALSE
                BUILD_IN_SOURCE OFF
                INSTALL_COMMAND ""
        )
        list(APPEND EXTERNAL_LIBS ${CMAKE_BINARY_DIR}/mnn-build/MNN.lib)
        list(APPEND EXTERNAL_BIN ${CMAKE_BINARY_DIR}/mnn-build/MNN.dll)
    endif ()

    if (USE_GGUF)
        # OpenCL/Adreno GPU 后端所需的 Khronos 头文件 + ICD Loader，构建时从 GitHub 拉取，
        # 装到统一的 opencl-install 前缀下，供 llama.cpp 的 find_package(OpenCL) 链接期使用。
        set(OPENCL_INSTALL_PREFIX ${CMAKE_BINARY_DIR}/opencl-install)
        # 直连 github.com 的 git clone 在本环境下不稳定（TLS 连接经常超时），改用
        # codeload.github.com 的源码归档下载（同一份源码，仅换传输方式），规避该问题。
        # 保留 Ninja + Clang 配置：OpenCL-Headers/OpenCL-ICD-Loader 自身的 try_compile
        # 检测尚未逐一核实预置缓存变量方案（不同于已验证的 Libsamplerate），暂不改动。
        # 注意：OpenCL-Headers/OpenCL-ICD-Loader 自身的 CMakeLists.txt 中，"是否构建 tests/"
        # 的判断条件是 `if(CMAKE_PROJECT_NAME STREQUAL PROJECT_NAME OR <XXX_BUILD_TESTING>) AND BUILD_TESTING`。
        # 由于二者各自都是通过 ExternalProject_Add 发起的独立顶层 CMake 配置(不是 add_subdirectory 嵌入)，
        # `CMAKE_PROJECT_NAME STREQUAL PROJECT_NAME` 恒为真，导致它们各自的 `OPENCL_HEADERS_BUILD_TESTING`/
        # `OPENCL_ICD_LOADER_BUILD_TESTING` 选项(默认已是 OFF)完全不起作用——只要 `include(CTest)` 把
        # `BUILD_TESTING` 默认置为 ON，就会 add_subdirectory(tests)，编译出数百个 tests/lang_c、tests/lang_cpp
        # 下的 cl_*.exe 测试可执行文件。显式传入 `-DBUILD_TESTING=OFF` 作为缓存变量可阻止 CTest 的
        # option() 覆盖它，从根本上跳过这些测试目标；本项目只需要头文件与 ICD Loader 库本身。
        ExternalProject_Add(OpenCLHeaders
                URL https://codeload.github.com/KhronosGroup/OpenCL-Headers/zip/refs/tags/v2024.10.24
                # 显式声明按提取时间戳(而不是归档内时间戳)标记已下载文件,消除 CMP0135 policy 警告，
                # 同时避免依赖 cmake_minimum_required 版本判断该策略是否存在。
                DOWNLOAD_EXTRACT_TIMESTAMP TRUE
                BINARY_DIR ${CMAKE_BINARY_DIR}/opencl-headers-build
                CMAKE_GENERATOR Ninja
                CMAKE_ARGS
                -DCMAKE_C_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_INSTALL_PREFIX=${OPENCL_INSTALL_PREFIX}
                -DBUILD_TESTING=OFF
                BUILD_IN_SOURCE OFF
        )
        ExternalProject_Add(OpenCLICDLoader
                URL https://codeload.github.com/KhronosGroup/OpenCL-ICD-Loader/zip/refs/tags/v2024.10.24
                DOWNLOAD_EXTRACT_TIMESTAMP TRUE
                BINARY_DIR ${CMAKE_BINARY_DIR}/opencl-icd-loader-build
                DEPENDS OpenCLHeaders
                CMAKE_GENERATOR Ninja
                CMAKE_ARGS
                -DCMAKE_C_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_LINKER=${MSVC_CLANG_LINKER}
                -DCMAKE_INSTALL_PREFIX=${OPENCL_INSTALL_PREFIX}
                -DCMAKE_PREFIX_PATH=${OPENCL_INSTALL_PREFIX}
                -DBUILD_TESTING=OFF
                # cllayerinfo 是一个独立的诊断命令行工具(依赖 ENABLE_OPENCL_LAYERS，默认 ON)，
                # 与 llama.cpp/ggml-opencl 链接所需的 OpenCL ICD Loader 库本身无关，一并关闭。
                -DENABLE_OPENCL_LAYERINFO=OFF
                # 显式只构建 OpenCL Loader 库本身(target 名为 OpenCL)，不构建 cllayerinfo/测试等
                # 其它目标，即使上游未来新增默认开启的目标也不会被意外拉入构建。
                BUILD_COMMAND ${CMAKE_COMMAND} --build <BINARY_DIR> --target OpenCL
                BUILD_IN_SOURCE OFF
        )

        ExternalProject_Add(AdrenoOpenCLKernels
                URL https://apigwx-aws.qualcomm.com/qsc/public/v1/api/download/software/tools/Adreno_Kernel_Library_GGML/Windows/0.0.7/adreno-opencl-kernels-0.0.7.zip
                DOWNLOAD_EXTRACT_TIMESTAMP TRUE
                SOURCE_DIR ${CMAKE_BINARY_DIR}/adreno-opencl-kernels-src
                CONFIGURE_COMMAND ""
                BUILD_COMMAND ""
                INSTALL_COMMAND ""
        )
        list(APPEND EXTERNAL_BIN ${CMAKE_BINARY_DIR}/adreno-opencl-kernels-src/adreno-opencl-kernels.dll)

        # ggml-cpu 后端在 ARM64 上不支持 MSVC(cl.exe),需要 Clang,与 MNN 保持一致的 Ninja + clang-cl 工具链
        ExternalProject_Add(Libllama.cpp
                SOURCE_DIR ${G_EXTERNAL_DIR}/llama.cpp
                BINARY_DIR ${CMAKE_BINARY_DIR}/llama-cpp-build
                DEPENDS OpenCLICDLoader
                CMAKE_GENERATOR Ninja
                CMAKE_ARGS
                -DCMAKE_SYSTEM_NAME=Windows
                -DCMAKE_SYSTEM_PROCESSOR=ARM64
                -DCMAKE_C_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_CXX_COMPILER=${MSVC_CLANG_COMPILER}
                -DCMAKE_LINKER=${MSVC_CLANG_LINKER}
                -DCMAKE_BUILD_TYPE=Release
                -DCMAKE_POSITION_INDEPENDENT_CODE=ON
                -DCMAKE_PREFIX_PATH=${OPENCL_INSTALL_PREFIX}
                # ARM64 向量化/快速数学编译选项，用于加速 ggml CPU 侧计算(Ninja + Clang 工具链)
                "-DCMAKE_C_FLAGS=-march=armv8.7-a -fvectorize -ffp-model=fast -fno-finite-math-only"
                "-DCMAKE_CXX_FLAGS=-march=armv8.7-a -fvectorize -ffp-model=fast -fno-finite-math-only"
                -DGGML_OPENCL=ON
                # 预编译 Adreno OpenCL kernel 支持，详见 playbook 3.3.5
                -DGGML_OPENCL_USE_ADRENO_BIN_KERNELS=ON
                -DLLAMA_CURL=OFF
                -DLLAMA_HTTPLIB=OFF
                -DLLAMA_BUILD_SERVER=OFF
                -DLLAMA_BUILD_TESTS=OFF
                -DLLAMA_BUILD_TOOLS=ON
                -DLLAMA_BUILD_EXAMPLES=OFF
                # --target llama-completion：诊断/性能对比工具，用途详见 playbook 3.3.5
                BUILD_COMMAND ${CMAKE_COMMAND} --build <BINARY_DIR> --target ggml-base --target ggml --target ggml-cpu --target ggml-opencl --target llama --target llama-common --target mtmd --target llama-completion
                BUILD_IN_SOURCE OFF
                INSTALL_COMMAND ""
        )
        list(APPEND EXTERNAL_HEADER_PATH
                ${G_EXTERNAL_DIR}/llama.cpp/include
                ${G_EXTERNAL_DIR}/llama.cpp/common
                ${G_EXTERNAL_DIR}/llama.cpp/ggml/include
                ${G_EXTERNAL_DIR}/llama.cpp/tools/mtmd
        )
        list(APPEND EXTERNAL_LIB_PATH
                ${CMAKE_BINARY_DIR}/llama-cpp-build/src
                ${CMAKE_BINARY_DIR}/llama-cpp-build/common
                ${CMAKE_BINARY_DIR}/llama-cpp-build/ggml/src
                ${CMAKE_BINARY_DIR}/llama-cpp-build/ggml/src/ggml-opencl
                ${CMAKE_BINARY_DIR}/llama-cpp-build/tools/mtmd
        )
        list(APPEND EXTERNAL_LIBS llama-common llama ggml ggml-cpu ggml-base ggml-opencl)
        list(APPEND EXTERNAL_BIN
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/llama.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/ggml.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/ggml-cpu.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/ggml-base.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/ggml-opencl.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/mtmd.dll
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/llama-common.dll
                # llama-completion.exe + llama-completion-impl.dll，详见 playbook 3.3.5
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/llama-completion.exe
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin/llama-completion-impl.dll
        )
        # ggml-cpu.dll 依赖 LLVM OpenMP 运行时(libomp140.aarch64.dll)，随 VS ARM64 LLVM 工具链
        # 分发但不在 PATH 中；多 MSVC 工具集版本共存时的选取踩坑详见 playbook 3.3.5。
        # 依次在 VS_VC_SEARCH_PATHS(Community 优先、Build Tools 兜底)下逐一 GLOB 搜索，
        # 避免只装了 Build Tools(而非完整 Community)时固定使用 VS_VC_PATH 而搜索落空。
        set(LIBOMP_DLL_CANDIDATES "")
        foreach (_vs_vc_candidate ${VS_VC_SEARCH_PATHS})
            file(GLOB _libomp_dll_found "${_vs_vc_candidate}/Redist/MSVC/*/debug_nonredist/arm64/Microsoft.VC*.OpenMP.LLVM/libomp140.aarch64.dll")
            list(APPEND LIBOMP_DLL_CANDIDATES ${_libomp_dll_found})
        endforeach ()
        if (NOT LIBOMP_DLL_CANDIDATES)
            message(FATAL_ERROR "libomp140.aarch64.dll not found under any of: ${VS_VC_SEARCH_PATHS} "
                    "(pattern: <VC_ROOT>/Redist/MSVC/*/debug_nonredist/arm64/Microsoft.VC*.OpenMP.LLVM/). "
                    "Install the ARM64 LLVM OpenMP redistributable via Visual Studio Installer, or update VS_VC_PATH/VS_VC_BUILD_TOOL_PATH in platform.cmake.")
        endif ()
        list(SORT LIBOMP_DLL_CANDIDATES COMPARE NATURAL)
        list(POP_BACK LIBOMP_DLL_CANDIDATES LIBOMP_DLL)
        message(STATUS "Selected libomp140.aarch64.dll: ${LIBOMP_DLL}")
        list(APPEND EXTERNAL_BIN ${LIBOMP_DLL})
    endif ()
endif ()