
set(QAI_APP_BUILDER_MAJOR_VERSION 2)
set(QAI_APP_BUILDER_MINOR_VERSION 3)
set(QAI_APP_BUILDER_PATCH_VERSION 7)
set(QAI_APP_BUILDER_VERSION ${QAI_APP_BUILDER_MAJOR_VERSION}.${QAI_APP_BUILDER_MINOR_VERSION}.${QAI_APP_BUILDER_PATCH_VERSION})

# Hexagon DSP architectures whose Stub / Skel / .cat runtime files are packaged
# with the service. This is a ';'-separated list, e.g. "v73;v81"; a single
# build with more than one arch listed can run on multiple NPU generations.
# Override from the command line with -DQNN_STUB_VERSION=v73;v81.
if (NOT DEFINED QNN_STUB_VERSION)
    set(QNN_STUB_VERSION "v73;v81")
endif ()

set(BUILD_VERSION_FILE ${CMAKE_BINARY_DIR}/version)
file(WRITE ${BUILD_VERSION_FILE} "")
file(APPEND ${BUILD_VERSION_FILE} "QAI_APP_BUILDER_VERSION: ${QAI_APP_BUILDER_VERSION}\n")
file(APPEND ${BUILD_VERSION_FILE} "QNN_SDK_ROOT: $ENV{QNN_SDK_ROOT}\n")
file(APPEND ${BUILD_VERSION_FILE} "QNN_STUB_VERSION: ${QNN_STUB_VERSION}\n")
