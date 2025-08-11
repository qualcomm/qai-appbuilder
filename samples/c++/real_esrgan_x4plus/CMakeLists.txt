# ======================================================================
# WoA ARM64 Project Configuration
# ======================================================================
# CMakeLists.txt for WoA ARM64
cmake_minimum_required(VERSION 3.20)  # Minimum version supporting ARM64 targets [9,11](@ref)

# Configure Vcpkg toolchain for ARM64 Windows
set(CMAKE_TOOLCHAIN_FILE "C:/vcpkg/scripts/buildsystems/vcpkg.cmake" CACHE STRING "Vcpkg toolchain file")
set(VCPKG_TARGET_TRIPLET "arm64-windows")
# Basic project configuration
project(WoA_Real_Esrgan_x4plus LANGUAGES CXX)        # Explicitly specify C++ language
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")  # Statically link runtime libraries[11](@ref)

# ARM64 compiler configuration
set(CMAKE_SYSTEM_NAME Windows)        # Target OS is Windows
set(CMAKE_SYSTEM_PROCESSOR ARM64)     # Target architecture is ARM64[11](@ref)
if(NOT DEFINED CMAKE_C_COMPILER)
    set(CMAKE_C_COMPILER "cl.exe")    # Use MSVC ARM64 C compiler
endif()
if(NOT DEFINED CMAKE_CXX_COMPILER)
    set(CMAKE_CXX_COMPILER "cl.exe")  # Use MSVC ARM64 C++ compiler[7](@ref)
endif()

# C++ standard configuration
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Source files configuration (Modify according to actual project structure)
set(SOURCE_FILES
    real_esrgan_x4plus.cpp            # Main source file
    # utils.cpp                       # Uncomment to add more files
)

# Define imported precompiled library target (match exact filenames)
add_library(QAI_AppBuilder_Arm SHARED IMPORTED GLOBAL)
# Configure library properties
set_target_properties(QAI_AppBuilder_Arm PROPERTIES
    # Import library path (for linking)
    IMPORTED_IMPLIB "${CMAKE_CURRENT_SOURCE_DIR}/QAI_AppBuilder-win_arm64-QNN2.34.0-Release/libappbuilder.lib"
    
    # Shared library path (runtime dependency)
    IMPORTED_LOCATION "${CMAKE_CURRENT_SOURCE_DIR}/QAI_AppBuilder-win_arm64-QNN2.34.0-Release/libappbuilder.dll"
    
    # Header file paths (matching include folder structure)
    INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_SOURCE_DIR}/QAI_AppBuilder-win_arm64-QNN2.34.0-Release/include"
)

# Set root paths for dependencies
set(QAI_LIBS_ROOT ${CMAKE_SOURCE_DIR}/qai_libs)
set(MODELS_ROOT ${CMAKE_SOURCE_DIR}/models)

# Prefer the -pthread compiler/linker flag over -lpthread for portability [2,6,8](@ref)
set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

find_package(OpenCV REQUIRED COMPONENTS core imgproc highgui imgcodecs videoio)
if(OpenCV_FOUND)
    message(STATUS "OpenCV library status:")
    message(STATUS "    version: ${OpenCV_VERSION}")
    message(STATUS "    libraries: ${OpenCV_LIBS}")
    message(STATUS "    include path: ${OpenCV_INCLUDE_DIRS}")
    message(STATUS "    lib path: ${OpenCV_LIBRARY_DIRS}")
else()
    message(FATAL_ERROR "Could not find OpenCV")
endif()
foreach(list ${OpenCV_LIBS})
    message(${list})
endforeach(list)

find_package(xtensor CONFIG REQUIRED)
if(xtensor_FOUND)
    message(STATUS "xtensor library status:")
    message(STATUS "    version: ${xtensor_VERSION}")
    message(STATUS "    libraries: ${xtensor_LIBRARY}")
    message(STATUS "    include path: ${xtensor_INCLUDE_DIRS}")
    message(STATUS "    lib path: ${xtensor_LIBRARY_DIRS}")
else()
    message(FATAL_ERROR "Could not find xtensor")
endif()

# Find and configure xtensor
add_executable(${PROJECT_NAME} ${SOURCE_FILES})

# Generate executable
target_compile_options(${PROJECT_NAME} PRIVATE
    /O2               # Enable O2 optimizations (Release)
    /arch:armv8.0     
    /DWIN32           # Define Windows macro
    /D_WINDOWS        # Define Windows environment macro
    /Zc:__cplusplus   # Enable standard __cplusplus macro
)

# Linker configuration
target_link_options(${PROJECT_NAME} PRIVATE
    /SUBSYSTEM:CONSOLE     # Console application type
    /ENTRY:mainCRTStartup  # Explicit entry point specification[11](@ref)
)

# Header include paths
target_include_directories(${PROJECT_NAME} PRIVATE
#	"C:/vcpkg/installed/arm64-windows/include"
)
target_link_directories(${PROJECT_NAME} PRIVATE "C:/vcpkg/installed/arm64-windows/lib")

target_link_libraries(${PROJECT_NAME} PRIVATE
    Threads::Threads
    QAI_AppBuilder_Arm
	${OpenCV_LIBS}
	xtensor
)  # Note: Headers automatically included post-linking

add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/QAI_AppBuilder-win_arm64-QNN2.34.0-Release/libappbuilder.dll"
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
	COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${MODELS_ROOT}/real_esrgan_x4plus.bin"
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
	COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/input.jpg"
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
	COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${QAI_LIBS_ROOT}"
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
    COMMENT "Copying required DLLs to output directory"
)