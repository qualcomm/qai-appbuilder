#pragma once

#include <string>
#include <vector>
#include "Lora.hpp"

LoraAdaptor::LoraAdaptor(const std::string &graph_name, const std::vector<std::string> &bin_paths) {
    m_graph_name = graph_name;
    m_bin_paths = bin_paths;
}

LoraAdaptor::~LoraAdaptor(){
}