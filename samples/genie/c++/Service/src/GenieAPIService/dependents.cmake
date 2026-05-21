include(ExternalProject)

# Library naming differs on each platform:
#   Windows : "Genie.dll" / "Genie.lib"
#   Linux   : "libGenie.so"
#   Android : "libGenie.so"
# We use LIB_PREFIX + name + DLL_EXT to assemble the file names uniformly.
if (MSVC)
    set(DLL_EXT ".dll")
    set(EXE_EXT ".exe")
    set(LIB_PREFIX "")
    set(QNN_PLATFORM "aarch64-windows-msvc")
elseif (ANDROID)
    set(EXE_EXT ".so")
    set(DLL_EXT ".so")
    set(LIB_PREFIX "lib")
    set(QNN_PLATFORM "aarch64-android")
elseif (UNIX AND NOT ANDROID)
    # Native Linux (typically aarch64-oe-linux-gcc11.2 from QAIRT). The Android
    # CMake/NDK path is handled by the elseif(ANDROID) branch above and is
    # intentionally not affected here.
    set(EXE_EXT "")
    set(DLL_EXT ".so")
    set(LIB_PREFIX "lib")
    if (NOT DEFINED QNN_PLATFORM)
        set(QNN_PLATFORM "aarch64-oe-linux-gcc11.2")
    endif ()
else ()
    message(FATAL_ERROR "only Windows / Linux / Android platforms are supported")
endif ()

#  Define EXTERNAL_BIN and PATH
set(LIBAPPBUILDER_ROOT ${G_EXTERNAL_DIR}/../../../..)

if (MSVC)
    # Keep the original Windows-style paths (back-slash separators are OK on Windows)
    set(QNN_BIN_PATH $ENV{QNN_SDK_ROOT}bin\\${QNN_PLATFORM})
    set(QNN_LIB_PATH $ENV{QNN_SDK_ROOT}lib\\${QNN_PLATFORM})
    set(QNN_STUB_PATH $ENV{QNN_SDK_ROOT}lib\\hexagon-${QNN_STUB_VERSION}\\unsigned)
    set(EXTERNAL_BIN
            ${QNN_BIN_PATH}\\genie-t2t-run${EXE_EXT}
            ${QNN_LIB_PATH}\\Genie${DLL_EXT}
            ${QNN_LIB_PATH}\\QnnHTP${DLL_EXT}
            ${QNN_LIB_PATH}\\QnnHtpNetRunExtensions${DLL_EXT}
            ${QNN_LIB_PATH}\\QnnHtpPrepare${DLL_EXT}
            ${QNN_LIB_PATH}\\QnnSystem${DLL_EXT}
            ${QNN_LIB_PATH}\\QnnHtp${QNN_STUB_VERSION}Stub${DLL_EXT}
            ${QNN_STUB_PATH}\\libQnnHtp${QNN_STUB_VERSION}Skel.so
            ${QNN_STUB_PATH}\\libqnnhtp${QNN_STUB_VERSION}.cat
            $ENV{QNN_SDK_ROOT}examples\\Genie\\configs\\htp_backend_ext_config.json
    )
else ()
    # Linux / Android: use forward-slashes.
    # On Linux, QAIRT names its DSP-stub / skel libraries with an UPPERCASE
    # version tag (libQnnHtpV73Stub.so, libQnnHtpV73Skel.so) while the
    # hexagon directory itself uses the lowercase tag (hexagon-v73). The
    # filesystem is case-sensitive, so we must use the proper case for each.
    string(TOUPPER "${QNN_STUB_VERSION}" QNN_STUB_VERSION_UPPER)

    set(QNN_BIN_PATH $ENV{QNN_SDK_ROOT}/bin/${QNN_PLATFORM})
    set(QNN_LIB_PATH $ENV{QNN_SDK_ROOT}/lib/${QNN_PLATFORM})
    set(QNN_STUB_PATH $ENV{QNN_SDK_ROOT}/lib/hexagon-${QNN_STUB_VERSION}/unsigned)

    # Some files in the SDK are optional - they may or may not be shipped in a
    # given QAIRT release. Build the candidate list and filter out missing
    # entries instead of failing the post-build copy step.
    set(_optional_bins
            ${QNN_BIN_PATH}/genie-t2t-run${EXE_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}Genie${DLL_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtp${DLL_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtpNetRunExtensions${DLL_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtpPrepare${DLL_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnSystem${DLL_EXT}
            ${QNN_LIB_PATH}/${LIB_PREFIX}QnnHtp${QNN_STUB_VERSION_UPPER}Stub${DLL_EXT}
            ${QNN_STUB_PATH}/libQnnHtp${QNN_STUB_VERSION_UPPER}Skel.so
            ${QNN_STUB_PATH}/libqnnhtp${QNN_STUB_VERSION}.cat
            $ENV{QNN_SDK_ROOT}/examples/Genie/configs/htp_backend_ext_config.json
    )

    set(EXTERNAL_BIN "")
    foreach(_f IN LISTS _optional_bins)
        if (EXISTS "${_f}")
            list(APPEND EXTERNAL_BIN "${_f}")
        else ()
            message(STATUS "QNN runtime file not present, will skip: ${_f}")
        endif ()
    endforeach()
endif ()

# Define EXTERNAL_LIB and PATH
if (MSVC)
    # On Windows the linker takes "Genie.dll" / "Genie.lib" by full file name.
    set(EXTERNAL_LIBS Genie${DLL_EXT})
else ()
    # On Linux / Android the linker prefers the un-decorated lib name (-lGenie).
    set(EXTERNAL_LIBS Genie)
endif ()
if (ANDROID)
    list(APPEND EXTERNAL_LIBS log)
endif ()
if (UNIX AND NOT ANDROID)
    # Linux-only: link against POSIX threading and dlopen.
    list(APPEND EXTERNAL_LIBS pthread dl)
endif ()
set(EXTERNAL_LIB_PATH ${QNN_LIB_PATH})

# Locate the CLI11 header directory. The repository ships two submodules
# (External/CLI11 and External/cli11) for historical reasons; pick whichever
# is actually populated. Required so case-sensitive Linux filesystems and
# selectively-cloned trees both work.
set(_CLI11_DIR "")
foreach(_cand "${G_EXTERNAL_DIR}/CLI11/include" "${G_EXTERNAL_DIR}/cli11/include")
    if (EXISTS "${_cand}/CLI/CLI.hpp")
        set(_CLI11_DIR "${_cand}")
        break()
    endif ()
endforeach()
if (_CLI11_DIR STREQUAL "")
    message(FATAL_ERROR
        "CLI11 header (CLI/CLI.hpp) not found under ${G_EXTERNAL_DIR}/CLI11 or "
        "${G_EXTERNAL_DIR}/cli11. Run:  git submodule update --init --recursive"
    )
endif ()

#  Define EXTERNAL_HEADER
set(EXTERNAL_HEADER_PATH
        ${G_EXTERNAL_DIR}
        ${G_EXTERNAL_DIR}/LibrosaCpp
        ${G_EXTERNAL_DIR}/libsamplerate/include
        ${G_EXTERNAL_DIR}/dr_libs
        ${G_EXTERNAL_DIR}/stb
        ${G_EXTERNAL_DIR}/cpp-httplib
        ${G_EXTERNAL_DIR}/json/single_include
        ${_CLI11_DIR}
        ${G_EXTERNAL_INCLUDE_PATH}/Genie
        $ENV{QNN_SDK_ROOT}/include/Genie
        ${LIBAPPBUILDER_ROOT}/src
)

if (MSVC)
    ExternalProject_Add(Libappbuilder
            SOURCE_DIR ${LIBAPPBUILDER_ROOT}
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE ON
    )

    list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/lib/Release)
    list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/lib/Release/libappbuilder${DLL_EXT})
    list(APPEND EXTERNAL_LIBS libappbuilder)

elseif (ANDROID)
    ExternalProject_Add(Libappbuilder
            SOURCE_DIR ${LIBAPPBUILDER_ROOT}
            CONFIGURE_COMMAND ""
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE ON
    )

    list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/libs/arm64-v8a)
    list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/libs/arm64-v8a/libappbuilder${DLL_EXT})
    list(APPEND EXTERNAL_LIBS appbuilder)

elseif (UNIX AND NOT ANDROID)
    # Native Linux: build libappbuilder.so via the top-level CMake project.
    # Android already has its own elseif(ANDROID) branch above that is left
    # untouched.
    ExternalProject_Add(Libappbuilder
            SOURCE_DIR ${LIBAPPBUILDER_ROOT}
            CMAKE_ARGS
                -DCMAKE_BUILD_TYPE=Release
                -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            INSTALL_COMMAND ""
            BUILD_IN_SOURCE ON
    )

    list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/lib)
    list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/lib/libappbuilder${DLL_EXT})
    list(APPEND EXTERNAL_LIBS appbuilder)
endif ()
list(APPEND EXTERNAL_LIBS samplerate)

if (MSVC)
    # Windows path keeps its original BUILD_IN_SOURCE behaviour.
    ExternalProject_Add(Libsamplerate
            SOURCE_DIR ${G_EXTERNAL_DIR}/libsamplerate
            CMAKE_ARGS
            -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON
            -DLIBSAMPLERATE_EXAMPLES=OFF
            -DBUILD_TESTING=OFF
            BUILD_IN_SOURCE ON
    )

    list(APPEND EXTERNAL_LIBS samplerate)
elseif (UNIX AND NOT ANDROID)
    # On Linux we use an out-of-source build for libsamplerate to avoid the
    # known issue where the in-source CMake configure resolves "add_subdirectory(src)"
    # against the wrong working directory under some CMake versions.
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

    list(APPEND EXTERNAL_LIBS samplerate)
endif ()

set(VS_VC_PATH "C:/Program Files/Microsoft Visual Studio/2022/Community/VC")
set(VS_VC_BUILD_TOOL_PATH "C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC")

if (MSVC)
    if (USE_MNN)
        set(MSVC_CLANG_COMPILER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/clang.exe)
        set(MSVC_CLANG_LINKER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/lld.exe)
            ExternalProject_Add(Libmnn
                    SOURCE_DIR ${G_EXTERNAL_DIR}/mnn
                    CMAKE_GENERATOR Ninja
                    CMAKE_ARGS
                    -DCMAKE_C_COMPILER=${MSVC_CLANG_COMPILER}
                    -DCMAKE_CXX_COMPILER=${MSVC_CLANG_COMPILER}
                    -DCMAKE_LINKER=${MSVC_CLANG_LINKER}
                    -DLLM_SUPPORT_VISION=ON
                    -DMNN_BUILD_OPENCV=ON
                    -DMNN_IMGCODECS=ON
                    -DLLM_SUPPORT_AUDIO=ON
                    -DMNN_BUILD_AUDIO=ON
                    -DMNN_LOW_MEMORY=true
                    -DMNN_CPU_WEIGHT_DEQUANT_GEMM=true
                    -DMNN_BUILD_LLM=true
                    -DMNN_SUPPORT_TRANSFORMER_FUSE=true
                    -DCMAKE_BUILD_TYPE=Release
                    -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
                    -DMNN_KLEIDIAI=FALSE
                    BUILD_IN_SOURCE ON
            )
        list(APPEND EXTERNAL_LIBS MNN)
        list(APPEND EXTERNAL_BIN ${G_EXTERNAL_BIN_PATH}/mnn.dll)
    endif ()

    if (USE_GGUF)
            ExternalProject_Add(Libllama.cpp
                    SOURCE_DIR ${G_EXTERNAL_DIR}/llama.cpp
                    CMAKE_GENERATOR "Visual Studio 17 2022"
                    CMAKE_GENERATOR_TOOLSET ClangCL
                    CMAKE_ARGS
                    -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
                    -DLLAMA_CURL=OFF
                    -DLLAMA_HTTPLIB=OFF
                    -DLLAMA_BUILD_SERVER=OFF
                    -DLLAMA_BUILD_TESTS=OFF
                    -DLLAMA_BUILD_TOOLS=OFF
                    -DLLAMA_BUILD_EXAMPLES=OFF
                    BUILD_IN_SOURCE ON
            )
        # common part
        list(APPEND EXTERNAL_HEADER_PATH ${G_EXTERNAL_DIR}/llama.cpp/common)
        list(APPEND EXTERNAL_LIB_PATH ${G_EXTERNAL_DIR}/llama.cpp/common/Release)

        if (EXISTS ${VS_VC_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt)
            file(STRINGS ${VS_VC_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt VC_VERSION LIMIT_COUNT 1)
        else ()
            file(STRINGS ${VS_VC_BUILD_TOOL_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt VC_VERSION LIMIT_COUNT 1)
        endif ()

        list(APPEND EXTERNAL_LIBS common llama ggml ggml-cpu ggml-base)
        list(APPEND EXTERNAL_BIN
                ${G_EXTERNAL_BIN_PATH}/llama.dll
                ${G_EXTERNAL_BIN_PATH}/ggml.dll
                ${G_EXTERNAL_BIN_PATH}/ggml-cpu.dll
                ${G_EXTERNAL_BIN_PATH}/ggml-base.dll
                ${VS_VC_PATH}/Redist/MSVC/${VC_VERSION}/debug_nonredist/arm64/Microsoft.VC143.OpenMP.LLVM/libomp140.aarch64.dll
        )
    endif ()
endif ()

if (UNIX AND NOT ANDROID)
    if (USE_GGUF)
        ExternalProject_Add(Libllama.cpp
                SOURCE_DIR ${G_EXTERNAL_DIR}/llama.cpp
                BINARY_DIR ${CMAKE_BINARY_DIR}/llama-cpp-build
                CMAKE_ARGS
                -DCMAKE_BUILD_TYPE=Release
                -DCMAKE_POSITION_INDEPENDENT_CODE=ON
                -DLLAMA_CURL=OFF
                -DLLAMA_HTTPLIB=OFF
                -DLLAMA_BUILD_SERVER=OFF
                -DLLAMA_BUILD_TESTS=OFF
                -DLLAMA_BUILD_TOOLS=OFF
                -DLLAMA_BUILD_EXAMPLES=OFF
                BUILD_IN_SOURCE OFF
                INSTALL_COMMAND ""
        )
        # Headers: llama.h in include/, common headers in common/, ggml in ggml/include/
        list(APPEND EXTERNAL_HEADER_PATH
                ${G_EXTERNAL_DIR}/llama.cpp/include
                ${G_EXTERNAL_DIR}/llama.cpp/common
                ${G_EXTERNAL_DIR}/llama.cpp/ggml/include
        )
        # Library search paths (out-of-source build).
        # llama.cpp places shared libs in bin/, static libs in their source dirs.
        list(APPEND EXTERNAL_LIB_PATH
                ${CMAKE_BINARY_DIR}/llama-cpp-build/bin
                ${CMAKE_BINARY_DIR}/llama-cpp-build/common
        )
        list(APPEND EXTERNAL_LIBS common llama ggml ggml-cpu ggml-base)
    endif ()
endif ()
