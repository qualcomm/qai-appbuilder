//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <cctype>
#include <memory>
#include <string>
#include <tuple>
#include <vector>

class ICommandLineManager {
 public:
  enum class Error { SUCCESS, PARSE_FAILURE, UNUSED_ARGUMENTS, OVER_SUBSCRIBED_ARGUMENTS };

  using ValueList_t = std::vector<std::shared_ptr<const std::string>>;

  /**
   * @brief Parses provided command line arguments into key value pairs
   *
   * @param[in] argc   Number of char* arguments in argv
   *
   * @param[in] argv   Pointer to first element of null terminated character arrays
   *
   * @return Error code:
   *         - SUCCESS: provided command line arguments match expected format: --key=value, --key
   *         - PARSE_FAILURE: The provided command line arguments do not match expected format
   *
   */
  virtual Error parseClArgs(size_t argc, char** argv) = 0;

  /**
   * @brief Provides passed values for requested key if available
   *
   * @param[in] key   Key string of option
   *
   * @return (False, empty) if key is not an available argument
   *
   */
  virtual std::tuple<bool, ValueList_t> serveArg(const std::string& key) = 0;

  /**
   * @brief Checks whether any provided commandline arguments remain unserved
   *
   * @return True if unconsumed arguments remain, False otherwise
   */
  virtual bool allArgumentsServed() const = 0;

  /**
   * @brief Validates command line arguments were correctly utilized
   *
   * @return Error code:
   *         - SUCCESS: provided command line arguments were utilized following implementations
   * policy
   *         - UNUSED_ARGUMENTS: Some arguments passed were not consumed
   *         - OVER_SUBSCRIBED_ARGUMENTS: Some arguments were requested by multiple times
   *
   */
  virtual Error validateUsage() = 0;

  virtual ~ICommandLineManager() = default;

  static bool isKey(const std::string& arg) {
    return (arg.length() > keyPrefix().length()) && (arg.find(keyPrefix()) == 0) &&
           std::isalpha(arg.at(keyPrefix().length()));
  }

  static Error parseKey(const std::string& arg, std::string& keyOut) {
    if (!isKey(arg)) {
      return Error::PARSE_FAILURE;
    }

    auto valueSplit = arg.find(keyValueSplit());
    keyOut          = valueSplit != arg.npos ? arg.substr(0, valueSplit) : arg;
    return Error::SUCCESS;
  }

  static Error parseValue(const std::string& arg, std::string& valueOut) {
    auto valueSplit = arg.find(keyValueSplit());
    if (valueSplit == arg.npos || valueSplit == arg.length() - 1) {
      return Error::PARSE_FAILURE;
    }
    valueOut = arg.substr(valueSplit + 1);
    return Error::SUCCESS;
  }

 private:
  static const std::string keyPrefix() { return "--"; };
  static char keyValueSplit() { return '='; };
};
