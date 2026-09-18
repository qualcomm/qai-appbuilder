cmake_minimum_required(VERSION 3.16)
project(GenieWatermarkProvider CXX)

# alg/synthid_watermark is a fully independent CMake project, packaged and
# built separately from the GenieAPIService main build. It is NOT referenced
# by the root CMakeLists.txt via add_subdirectory, and building it never
# requires touching the main build.
#
# Output: GenieWatermarkProvider.dll, to be manually placed next to
# GenieAPIService.exe / GenieAPILibrary.dll for the watermark plugin to load.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if (NOT CMAKE_RUNTIME_OUTPUT_DIRECTORY)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
endif()

add_library(GenieWatermarkProvider SHARED
    synthid_watermark.cpp
)

target_include_directories(GenieWatermarkProvider PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

target_compile_definitions(GenieWatermarkProvider PRIVATE
    GENIE_WATERMARK_PLUGIN_IMPL
)

set_target_properties(GenieWatermarkProvider PROPERTIES
    OUTPUT_NAME "GenieWatermarkProvider"
)

enable_testing()
add_subdirectory(test)
