include(ExternalProject)

#  Define EXTERNAL_BIN and PATH
set(EXTERNAL_BIN_PATH ${CMAKE_BINARY_DIR}/bin)
set(QNN_BIN_PATH $ENV{QNN_SDK_ROOT}bin\\aarch64-windows-msvc)
set(QNN_LIB_PATH $ENV{QNN_SDK_ROOT}lib\\aarch64-windows-msvc)
set(QNN_STUB_PATH $ENV{QNN_SDK_ROOT}lib\\hexagon-${QNN_STUB_VERSION}\\unsigned)
set(EXTERNAL_BIN
        ${QNN_BIN_PATH}\\genie-t2t-run.exe
        ${QNN_LIB_PATH}\\Genie.dll
        ${QNN_LIB_PATH}\\QnnHTP.dll
        ${QNN_LIB_PATH}\\QnnHtpNetRunExtensions.dll
        ${QNN_LIB_PATH}\\QnnHtpPrepare.dll
        ${QNN_LIB_PATH}\\QnnSystem.dll
        ${QNN_STUB_PATH}\\libQnnHtp${QNN_STUB_VERSION}Skel.so
        ${QNN_STUB_PATH}\\libqnnhtp${QNN_STUB_VERSION}.cat
        $ENV{QNN_SDK_ROOT}examples\\Genie\\configs\\htp_backend_ext_config.json
)

# Define EXTERNAL_LIB and PATH
set(EXTERNAL_LIBS Genie.lib)
set(EXTERNAL_LIB_PATH
        ${QNN_LIB_PATH}
        ${CMAKE_BINARY_DIR}/lib
)

#  Define EXTERNAL_HEADER
set(External_Dir ${CMAKE_SOURCE_DIR}/../External)
set(External_Include_Path ${CMAKE_BINARY_DIR}/include)
set(EXTERNAL_HEADER_PATH
        ${External_Dir}/cpp-httplib
        ${External_Dir}/json/single_include
        ${External_Dir}/CLI11/include
        ${External_Include_Path}
        ${External_Include_Path}/Genie
        $ENV{QNN_SDK_ROOT}/include/Genie
)

ExternalProject_Add(Libcurl
        SOURCE_DIR ${External_Dir}/curl
        CMAKE_ARGS
        -DCMAKE_INSTALL_PREFIX=${CMAKE_BINARY_DIR}
        -DBUILD_SHARED_LIBS=OFF
        -DBUILD_CURL_EXE=OFF
        -DBUILD_TESTING=OFF
        -DCURL_USE_LIBPSL=OFF
        -DENABLE_UNICODE=ON
        BUILD_IN_SOURCE ON
)

set(VS_VC_PATH "C:/Program Files/Microsoft Visual Studio/2022/Community/VC")
set(VS_VC_BUILD_TOOL_PATH "C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC")

if (USE_MNN)
    set(MSVC_CLANG_COMPILER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/clang.exe)
    set(MSVC_CLANG_LINKER ${VS_VC_PATH}/Tools/Llvm/ARM64/bin/lld.exe)
    ExternalProject_Add(Libmnn
            SOURCE_DIR ${External_Dir}/mnn
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
    list(APPEND EXTERNAL_BIN ${EXTERNAL_BIN_PATH}/mnn.dll)
endif ()

if (USE_GGUF)
    ExternalProject_Add(Libllama.cpp
            SOURCE_DIR ${External_Dir}/llama.cpp
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
    list(APPEND EXTERNAL_HEADER_PATH ${External_Dir}/llama.cpp/common)
    list(APPEND EXTERNAL_LIB_PATH ${External_Dir}/llama.cpp/common/Release)

    if (EXISTS ${VS_VC_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt)
        file(STRINGS ${VS_VC_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt VC_VERSION LIMIT_COUNT 1)
    else ()
        file(STRINGS ${VS_VC_BUILD_TOOL_PATH}/Auxiliary/Build/Microsoft.VCRedistVersion.default.txt VC_VERSION LIMIT_COUNT 1)
    endif ()

    list(APPEND EXTERNAL_LIBS common llama ggml ggml-cpu ggml-base)
    list(APPEND EXTERNAL_BIN
            ${EXTERNAL_BIN_PATH}/llama.dll
            ${EXTERNAL_BIN_PATH}/ggml.dll
            ${EXTERNAL_BIN_PATH}/ggml-cpu.dll
            ${EXTERNAL_BIN_PATH}/ggml-base.dll
            ${VS_VC_PATH}/Redist/MSVC/${VC_VERSION}/debug_nonredist/arm64/Microsoft.VC143.OpenMP.LLVM/libomp140.aarch64.dll
    )
endif ()

link_directories(${EXTERNAL_LIB_PATH})