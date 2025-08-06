//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifdef _WIN32
#pragma once
#endif

#include <string>
#include <vector>
#include "Lora.hpp"

LoraAdapter::LoraAdapter(const std::string &graph_name, const std::vector<std::string> &bin_paths) {
    m_graph_name = graph_name;
    m_bin_paths = bin_paths;
}

LoraAdapter::~LoraAdapter(){
}