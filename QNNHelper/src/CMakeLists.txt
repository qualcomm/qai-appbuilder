#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(QNNHelper LANGUAGES CXX)

add_subdirectory(pybind11)
pybind11_add_module(qnnhelper QNNHelper.cpp)
