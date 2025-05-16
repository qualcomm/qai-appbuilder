//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "Utils.h"
#include <iostream>
#include <string>


int main(void) {

  httplib::Client("http://localhost:8910")
      .Get("/", [&](const char *data, size_t data_length) {
        std::cout << string(data, data_length);
        return true;
      });

  return 0;
}
