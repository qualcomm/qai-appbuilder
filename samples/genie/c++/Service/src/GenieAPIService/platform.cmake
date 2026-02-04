include(ExternalProject)

if (MSVC)
    set(DLL_EXT ".dll")
    set(EXE_EXT ".exe")
    set(QNN_PLATFORM "aarch64-windows-msvc")
elseif (ANDROID)
    set(EXE_EXT ".so")
    set(DLL_EXT ".so")
    set(QNN_PLATFORM "aarch64-android")
else ()
    message(FATAL_ERROR "only support MSVC and ANDROID platform now")
endif ()

#  Define EXTERNAL_BIN and PATH
set(LIBAPPBUILDER_ROOT ${G_EXTERNAL_DIR}/../../../..)
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

# Define EXTERNAL_LIB and PATH
set(EXTERNAL_LIBS Genie${DLL_EXT})
set(EXTERNAL_LIB_PATH ${QNN_LIB_PATH})

#  Define EXTERNAL_HEADER
set(EXTERNAL_HEADER_PATH
        ${G_EXTERNAL_DIR}/libsamplerate/include
        ${G_EXTERNAL_DIR}/dr_libs
        ${G_EXTERNAL_DIR}/stb
        ${G_EXTERNAL_DIR}/cpp-httplib
        ${G_EXTERNAL_DIR}/json/single_include
        ${G_EXTERNAL_DIR}/CLI11/include
        ${G_EXTERNAL_INCLUDE_PATH}/Genie
        $ENV{QNN_SDK_ROOT}/include/Genie
        ${LIBAPPBUILDER_ROOT}/src
)

ExternalProject_Add(Libappbuilder
        SOURCE_DIR ${LIBAPPBUILDER_ROOT}
        INSTALL_COMMAND ""
        BUILD_IN_SOURCE ON
)

list(APPEND EXTERNAL_LIB_PATH ${LIBAPPBUILDER_ROOT}/lib/Release)
list(APPEND EXTERNAL_LIBS libappbuilder)
list(APPEND EXTERNAL_BIN ${LIBAPPBUILDER_ROOT}/lib/Release/libappbuilder.dll)
list(APPEND EXTERNAL_LIBS libappbuilder)

# list(APPEND EXTERNAL_LIBS samplerate)

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