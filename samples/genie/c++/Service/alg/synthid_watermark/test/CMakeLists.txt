cmake_minimum_required(VERSION 3.16)

add_executable(test_synthid_watermark
    test_synthid.cpp
)

target_include_directories(test_synthid_watermark PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
)

add_test(NAME synthid_watermark_golden_vectors COMMAND test_synthid_watermark)
