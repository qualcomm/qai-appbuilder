//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#pragma warning(push)
#pragma warning(disable:4267)
#include <cmath>
#include <fstream>
#include <iostream>
#include <numeric>
#include <queue>
#ifdef _WIN32
#include <intrin.h>
#endif
#include "DataUtil.hpp"
#include "Logger.hpp"
#ifndef __hexagon__
#include "PAL/Directory.hpp"
#include "PAL/FileOp.hpp"
#include "PAL/Path.hpp"
#endif

using namespace qnn;
using namespace qnn::tools;

std::tuple<datautil::StatusCode, size_t> datautil::getDataTypeSizeInBytes(Qnn_DataType_t dataType) {
  if (g_dataTypeToSize.find(dataType) == g_dataTypeToSize.end()) {
    QNN_ERROR("Invalid qnn data type provided");
    return std::make_tuple(StatusCode::INVALID_DATA_TYPE, 0);
  }
  return std::make_tuple(StatusCode::SUCCESS, g_dataTypeToSize.find(dataType)->second);
}

size_t datautil::calculateElementCount(std::vector<size_t> dims) {
  if (dims.size() == 0) {
    return 0;
  }
  return std::accumulate(dims.begin(), dims.end(), 1, std::multiplies<size_t>());
}

std::tuple<datautil::StatusCode, size_t> datautil::calculateLength(std::vector<size_t> dims,
                                                                   Qnn_DataType_t dataType) {
  if (dims.size() == 0) {
    QNN_ERROR("dims.size() is zero");
    return std::make_tuple(StatusCode::INVALID_DIMENSIONS, 0);
  }
  StatusCode returnStatus{StatusCode::SUCCESS};
  size_t length{0};
  std::tie(returnStatus, length) = getDataTypeSizeInBytes(dataType);
  if (StatusCode::SUCCESS != returnStatus) {
    return std::make_tuple(returnStatus, 0);
  }
  length *= calculateElementCount(dims);
  return std::make_tuple(StatusCode::SUCCESS, length);
}

datautil::StatusCode datautil::readDataFromFile(std::string filePath,
                                                std::vector<size_t> dims,
                                                Qnn_DataType_t dataType,
                                                uint8_t* buffer) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  std::ifstream in(filePath, std::ifstream::binary);
  if (!in) {
    QNN_ERROR("Failed to open input file: %s", filePath.c_str());
    return StatusCode::FILE_OPEN_FAIL;
  }
  in.seekg(0, in.end);
  const size_t length = in.tellg();
  in.seekg(0, in.beg);
  StatusCode err{StatusCode::SUCCESS};
  size_t l{0};
  std::tie(err, l) = datautil::calculateLength(dims, dataType);
  if (StatusCode::SUCCESS != err) {
    return err;
  }
  if (length != l) {
    QNN_ERROR("Input file %s: file size in bytes (%d), should be equal to: %d",
              filePath.c_str(),
              length,
              l);
    return StatusCode::DATA_SIZE_MISMATCH;
  }

  if (!in.read(reinterpret_cast<char*>(buffer), length)) {
    QNN_ERROR("Failed to read the contents of: %s", filePath.c_str());
    return StatusCode::DATA_READ_FAIL;
  }
  return StatusCode::SUCCESS;
}

datautil::ReadBatchDataRetType_t datautil::readBatchData(const std::vector<std::string>& filePaths,
                                                         const size_t filePathsIndexOffset,
                                                         const bool loopBackToStart,
                                                         const std::vector<size_t>& dims,
                                                         const Qnn_DataType_t dataType,
                                                         uint8_t* buffer) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return std::make_tuple(StatusCode::INVALID_BUFFER, 0, 0);
  }
  StatusCode err{StatusCode::SUCCESS};
  size_t tensorLength{0};
  std::tie(err, tensorLength) = datautil::calculateLength(dims, dataType);
  if (StatusCode::SUCCESS != err) {
    return std::make_tuple(err, 0, 0);
  }
  size_t numInputsCopied = 0;
  size_t numBatchSize    = 0;
  size_t totalLength     = 0;
  size_t fileIndex       = filePathsIndexOffset;
  while (true) {
    if (fileIndex >= filePaths.size()) {
      if (loopBackToStart) {
        fileIndex = fileIndex % filePaths.size();
      } else {
        numBatchSize += (tensorLength - totalLength) / (totalLength / numBatchSize);
        // pad the vector with zeros
        memset(buffer + totalLength, 0, (tensorLength - totalLength) * sizeof(char));
        break;
      }
    }
    std::ifstream in(filePaths[fileIndex], std::ifstream::binary);
    if (!in) {
      QNN_ERROR("Failed to open input file: %s", (filePaths[fileIndex]).c_str());
      return std::make_tuple(StatusCode::FILE_OPEN_FAIL, numInputsCopied, numBatchSize);
    }
    in.seekg(0, in.end);
    const size_t fileSize = in.tellg();
    in.seekg(0, in.beg);
    if ((tensorLength % fileSize) != 0 || fileSize > tensorLength || fileSize == 0) {
      QNN_ERROR(
          "Given input file %s with file size in bytes %d. If the model expects a batch size of "
          "one, the file size should match the tensor extent: %d bytes. If the model expects a "
          "batch size > 1, the file size should evenly divide the tensor extent: %d bytes.",
          filePaths[fileIndex].c_str(),
          fileSize,
          tensorLength,
          tensorLength);
      return std::make_tuple(StatusCode::DATA_SIZE_MISMATCH, numInputsCopied, numBatchSize);
    }
    if (!in.read(reinterpret_cast<char*>(buffer + (numInputsCopied * fileSize)), fileSize)) {
      QNN_ERROR("Failed to read the contents of: %s", filePaths.front().c_str());
      return std::make_tuple(StatusCode::DATA_READ_FAIL, numInputsCopied, numBatchSize);
    }
    totalLength += fileSize;
    numInputsCopied += 1;
    numBatchSize += 1;
    fileIndex += 1;
    if (totalLength >= tensorLength) {
      break;
    }
  }
  return std::make_tuple(StatusCode::SUCCESS, numInputsCopied, numBatchSize);
}

std::tuple<datautil::StatusCode, size_t> datautil::getFileSize(std::string filePath) {
  std::ifstream in(filePath, std::ifstream::binary);
  if (!in) {
    QNN_ERROR("Failed to open input file: %s", filePath.c_str());
    return std::make_tuple(StatusCode::FILE_OPEN_FAIL, 0);
  }
  in.seekg(0, in.end);
  const size_t length = in.tellg();
  in.seekg(0, in.beg);
  return std::make_tuple(StatusCode::SUCCESS, length);
}

datautil::StatusCode datautil::readBinaryFromFile(std::string filePath,
                                                  uint8_t* buffer,
                                                  size_t bufferSize) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  std::ifstream in(filePath, std::ifstream::binary);
  if (!in) {
    QNN_ERROR("Failed to open input file: %s", filePath.c_str());
    return StatusCode::FILE_OPEN_FAIL;
  }
  if (!in.read(reinterpret_cast<char*>(buffer), bufferSize)) {
    QNN_ERROR("Failed to read the contents of: %s", filePath.c_str());
    return StatusCode::DATA_READ_FAIL;
  }
  return StatusCode::SUCCESS;
}

#ifndef __hexagon__
datautil::StatusCode datautil::writeDataToFile(std::string fileDir,
                                               std::string fileName,
                                               std::vector<size_t> dims,
                                               Qnn_DataType_t dataType,
                                               uint8_t* buffer) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  if (!pal::Directory::makePath(fileDir)) {
    QNN_ERROR("Failed to create output directory: %s", fileDir.c_str());
    return StatusCode::DIRECTORY_CREATE_FAIL;
  }
  const std::string outputPath(fileDir + pal::Path::getSeparator() + fileName);
  std::ofstream os(outputPath, std::ofstream::binary);
  if (!os) {
    QNN_ERROR("Failed to open output file for writing: %s", outputPath.c_str());
    return StatusCode::FILE_OPEN_FAIL;
  }
  StatusCode err{StatusCode::SUCCESS};
  size_t length{0};
  std::tie(err, length) = datautil::calculateLength(dims, dataType);
  if (StatusCode::SUCCESS != err) {
    return err;
  }
  for (size_t l = 0; l < length; l++) {
    os.write(reinterpret_cast<char*>(&(*(buffer + l))), 1);
  }
  return StatusCode::SUCCESS;
}

datautil::StatusCode datautil::writeBatchDataToFile(std::vector<std::string> fileDirs,
                                                    std::string fileName,
                                                    std::vector<size_t> dims,
                                                    Qnn_DataType_t dataType,
                                                    uint8_t* buffer,
                                                    const size_t batchSize) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  StatusCode err{StatusCode::SUCCESS};
  size_t length{0};
  std::tie(err, length) = datautil::calculateLength(dims, dataType);
  if (StatusCode::SUCCESS != err) {
    return err;
  }
  auto outputSize = (length / batchSize);
  for (size_t batchIndex = 0; batchIndex < fileDirs.size(); batchIndex++) {
    std::string fileDir = fileDirs[batchIndex];
    if (!pal::Directory::makePath(fileDir)) {
      QNN_ERROR("Failed to create output directory: %s", fileDir.c_str());
      return StatusCode::DIRECTORY_CREATE_FAIL;
    }
    const std::string outputPath(fileDir + pal::Path::getSeparator() + fileName);
    std::ofstream os(outputPath, std::ofstream::binary);
    if (!os) {
      QNN_ERROR("Failed to open output file for writing: %s", outputPath.c_str());
      return StatusCode::FILE_OPEN_FAIL;
    }
    for (size_t l = 0; l < outputSize; l++) {
      size_t bufferIndex = l + (batchIndex * outputSize);
      os.write(reinterpret_cast<char*>(&(*(buffer + bufferIndex))), 1);
    }
  }
  return StatusCode::SUCCESS;
}

datautil::StatusCode datautil::writeBinaryToFile(std::string fileDir,
                                                 std::string fileName,
                                                 uint8_t* buffer,
                                                 size_t bufferSize) {
  if (nullptr == buffer) {
    QNN_ERROR("buffer is nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  if (!pal::Directory::makePath(fileDir)) {
    QNN_ERROR("Failed to create output directory: %s", fileDir.c_str());
    return StatusCode::DIRECTORY_CREATE_FAIL;
  }
  const std::string outputPath(fileDir + pal::Path::getSeparator() + fileName);
  std::ofstream os(outputPath, std::ofstream::binary);
  if (!os) {
    QNN_ERROR("Failed to open output file for writing: %s", outputPath.c_str());
    return StatusCode::FILE_OPEN_FAIL;
  }
  os.write(reinterpret_cast<char*>(buffer), bufferSize);
  return StatusCode::SUCCESS;
}
#endif

// Enabling fp16 execution
static inline float datautil::fp16_ieee_to_fp32_value(uint16_t h) {
    const uint32_t w = (uint32_t) h << 16;
    const uint32_t sign = w & UINT32_C(0x80000000);
    const uint32_t two_w = w + w;
    const uint32_t exp_offset = UINT32_C(0xE0) << 23;
#if defined(__STDC_VERSION__) && (__STDC_VERSION__ >= 199901L) || defined(__GNUC__) && !defined(__STRICT_ANSI__)
    const float exp_scale = 0x1.0p-112f;
#else
    const float exp_scale = fp32_from_bits(UINT32_C(0x7800000));
#endif
    const float normalized_value = fp32_from_bits((two_w >> 4) + exp_offset) * exp_scale;
    const uint32_t magic_mask = UINT32_C(126) << 23;
    const float magic_bias = 0.5f;
    const float denormalized_value = fp32_from_bits((two_w >> 17) | magic_mask) - magic_bias;
    const uint32_t denormalized_cutoff = UINT32_C(1) << 27;
    const uint32_t result = sign |
        (two_w < denormalized_cutoff ? fp32_to_bits(denormalized_value) : fp32_to_bits(normalized_value));
    return fp32_from_bits(result);
}

// Enabling fp16 execution
/*
 * Convert a 32-bit floating-point number in IEEE single-precision format to a 16-bit floating-point number in
 * IEEE half-precision format, in bit representation.
 *
 * @note The implementation relies on IEEE-like (no assumption about rounding mode and no operations on denormals)
 * floating-point operations and bitcasts between integer and floating-point variables.
 */
bool datautil::floatNToFloat32(float* out,
                     uint8_t* in,
                     size_t numElements,
                     uint8_t bitWidth)
{
    if(numElements == 0) {
        return false;
    }

    if(bitWidth == 16){
#ifndef __hexagon__
        uint16_t *temp = (uint16_t *)in;
        for(size_t i = 0; i < numElements; i++){
            out[i] = fp16_ieee_to_fp32_value(temp[i]);
        }
#else
        return false;
#endif //__hexagon__
    }
    else if(bitWidth == 32) {
        float* inFloat = reinterpret_cast<float*>(in);
        for (size_t i = 0; i < numElements; i++) {
            out[i] = inFloat[i];
        }
    }
    else {
        return false;
    }

    return true;
}

// Enabling fp16 execution
static inline float datautil::fp32_from_bits(uint32_t w) {
#if defined(__OPENCL_VERSION__)
    return as_float(w);
#elif defined(__CUDA_ARCH__)
    return __uint_as_float((unsigned int) w);
#elif defined(__INTEL_COMPILER)
    return _castu32_f32(w);
#elif defined(_MSC_VER) && (defined(_M_ARM) || defined(_M_ARM64))
    return _CopyFloatFromInt32((__int32) w);
#else
    union {
        uint32_t as_bits;
        float as_value;
    } fp32 = { w };
    return fp32.as_value;
#endif
}

// Enabling fp16 execution
static inline uint32_t datautil::fp32_to_bits(float f) {
#if defined(__OPENCL_VERSION__)
    return as_uint(f);
#elif defined(__CUDA_ARCH__)
    return (uint32_t) __float_as_uint(f);
#elif defined(__INTEL_COMPILER)
    return _castf32_u32(f);
#elif defined(_MSC_VER) && (defined(_M_ARM) || defined(_M_ARM64))
    return (uint32_t) _CopyInt32FromFloat(f);
#else
    union {
        float as_value;
        uint32_t as_bits;
    } fp32 = { f };
    return fp32.as_bits;
#endif
}

// Enabling fp16 execution
static inline uint16_t datautil::fp16_ieee_from_fp32_value(float f) {
 #if defined(__STDC_VERSION__) && (__STDC_VERSION__ >= 199901L) || defined(__GNUC__) && !defined(__STRICT_ANSI__)
     const float scale_to_inf = 0x1.0p+112f;
     const float scale_to_zero = 0x1.0p-110f;
 #else
     const float scale_to_inf = fp32_from_bits(UINT32_C(0x77800000));
     const float scale_to_zero = fp32_from_bits(UINT32_C(0x08800000));
 #endif
     float base = (fabsf(f) * scale_to_inf) * scale_to_zero;
 
     const uint32_t w = fp32_to_bits(f);
     const uint32_t shl1_w = w + w;
     const uint32_t sign = w & UINT32_C(0x80000000);
     uint32_t bias = shl1_w & UINT32_C(0xFF000000);
     if (bias < UINT32_C(0x71000000)) {
         bias = UINT32_C(0x71000000);
     }
 
     base = fp32_from_bits((bias >> 1) + UINT32_C(0x07800000)) + base;
     const uint32_t bits = fp32_to_bits(base);
     const uint32_t exp_bits = (bits >> 13) & UINT32_C(0x00007C00);
     const uint32_t mantissa_bits = bits & UINT32_C(0x00000FFF);
     const uint32_t nonsign = exp_bits + mantissa_bits;
     return (sign >> 16) | (shl1_w > UINT32_C(0xFF000000) ? UINT16_C(0x7E00) : nonsign);
 }

// Enabling fp16 execution
bool datautil::float32ToFloatN(uint8_t* out,
                       float* in,
                       size_t numElements,
                       uint8_t bitWidth)
  {
      if(numElements == 0) {
          return false;
      }
  
      if(bitWidth == 16){
  #ifndef __hexagon__
          uint16_t *temp = (uint16_t *)out;
          for(size_t i = 0; i < numElements; i++){
              temp[i] = fp16_ieee_from_fp32_value(in[i]);
          }
  #else
          return false;
  #endif //__hexagon__
      }
      else if(bitWidth == 32) {
          float* outFloat = reinterpret_cast<float*>(out);
          for (size_t i = 0; i < numElements; i++) {
              outFloat[i] = in[i];
          }
      }
      else {
          return false;
      }
  
      return true;
  }

template <typename T_QuantType>
datautil::StatusCode datautil::floatToTfN(
    T_QuantType* out, float* in, int32_t offset, float scale, size_t numElements) {
  static_assert(std::is_unsigned<T_QuantType>::value, "floatToTfN supports unsigned only!");

  if (nullptr == out || nullptr == in) {
    QNN_ERROR("Received a nullptr");
    return StatusCode::INVALID_BUFFER;
  }

  size_t dataTypeSizeInBytes = sizeof(T_QuantType);
  size_t bitWidth            = dataTypeSizeInBytes * g_bitsPerByte;
  double trueBitWidthMax     = pow(2, bitWidth) - 1;
  double encodingMin         = offset * scale;
  double encodingMax         = (trueBitWidthMax + offset) * scale;
  double encodingRange       = encodingMax - encodingMin;
  double avg = trueBitWidthMax / encodingRange;    // zw: optimize.

  for (size_t i = 0; i < numElements; ++i) {
    int quantizedValue = (int)(avg * (in[i] - encodingMin) + 0.5);  // zw: optimze, replace 'round()' with '+ 0.5'.
    if (quantizedValue < 0)
      quantizedValue = 0;
    else if (quantizedValue > (int)trueBitWidthMax)
      quantizedValue = (int)trueBitWidthMax;
    out[i] = static_cast<T_QuantType>(quantizedValue);
  }
  return StatusCode::SUCCESS;
}

template datautil::StatusCode datautil::floatToTfN<uint8_t>(
    uint8_t* out, float* in, int32_t offset, float scale, size_t numElements);

template datautil::StatusCode datautil::floatToTfN<uint16_t>(
    uint16_t* out, float* in, int32_t offset, float scale, size_t numElements);

template <typename T_QuantType>
datautil::StatusCode datautil::tfNToFloat(
    float* out, T_QuantType* in, int32_t offset, float scale, size_t numElements) {
  static_assert(std::is_unsigned<T_QuantType>::value, "tfNToFloat supports unsigned only!");

  if (nullptr == out || nullptr == in) {
    QNN_ERROR("Received a nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  for (size_t i = 0; i < numElements; i++) {
    double quantizedValue = static_cast<double>(in[i]);
    double offsetDouble   = static_cast<double>(offset);
    out[i]                = static_cast<double>((quantizedValue + offsetDouble) * scale);
  }
  return StatusCode::SUCCESS;
}

template datautil::StatusCode datautil::tfNToFloat<uint8_t>(
    float* out, uint8_t* in, int32_t offset, float scale, size_t numElements);

template datautil::StatusCode datautil::tfNToFloat<uint16_t>(
    float* out, uint16_t* in, int32_t offset, float scale, size_t numElements);

template <typename T_QuantType>
datautil::StatusCode datautil::castToFloat(float* out, T_QuantType* in, size_t numElements) {
  if (nullptr == out || nullptr == in) {
    QNN_ERROR("Received a nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  for (size_t i = 0; i < numElements; i++) {
    out[i] = static_cast<float>(in[i]);
  }
  return StatusCode::SUCCESS;
}

template datautil::StatusCode datautil::castToFloat<uint8_t>(float* out,
                                                             uint8_t* in,
                                                             size_t numElements);

template datautil::StatusCode datautil::castToFloat<uint16_t>(float* out,
                                                              uint16_t* in,
                                                              size_t numElements);

template datautil::StatusCode datautil::castToFloat<uint32_t>(float* out,
                                                              uint32_t* in,
                                                              size_t numElements);

template datautil::StatusCode datautil::castToFloat<uint64_t>(float* out,
                                                              uint64_t* in,
                                                              size_t numElements);

template datautil::StatusCode datautil::castToFloat<int8_t>(float* out,
                                                            int8_t* in,
                                                            size_t numElements);

template datautil::StatusCode datautil::castToFloat<int16_t>(float* out,
                                                             int16_t* in,
                                                             size_t numElements);

template datautil::StatusCode datautil::castToFloat<int32_t>(float* out,
                                                             int32_t* in,
                                                             size_t numElements);

template datautil::StatusCode datautil::castToFloat<int64_t>(float* out,
                                                             int64_t* in,
                                                             size_t numElements);

template <typename T_QuantType>
datautil::StatusCode datautil::castFromFloat(T_QuantType* out, float* in, size_t numElements) {
  if (nullptr == out || nullptr == in) {
    QNN_ERROR("Received a nullptr");
    return StatusCode::INVALID_BUFFER;
  }
  for (size_t i = 0; i < numElements; i++) {
    out[i] = static_cast<T_QuantType>(in[i]);
  }
  return StatusCode::SUCCESS;
}

template datautil::StatusCode datautil::castFromFloat<uint8_t>(uint8_t* out,
                                                               float* in,
                                                               size_t numElements);

template datautil::StatusCode datautil::castFromFloat<uint16_t>(uint16_t* out,
                                                                float* in,
                                                                size_t numElements);

template datautil::StatusCode datautil::castFromFloat<uint32_t>(uint32_t* out,
                                                                float* in,
                                                                size_t numElements);

template datautil::StatusCode datautil::castFromFloat<uint64_t>(uint64_t* out,
                                                                float* in,
                                                                size_t numElements);

template datautil::StatusCode datautil::castFromFloat<int8_t>(int8_t* out,
                                                              float* in,
                                                              size_t numElements);

template datautil::StatusCode datautil::castFromFloat<int16_t>(int16_t* out,
                                                               float* in,
                                                               size_t numElements);

template datautil::StatusCode datautil::castFromFloat<int32_t>(int32_t* out,
                                                               float* in,
                                                               size_t numElements);

template datautil::StatusCode datautil::castFromFloat<int64_t>(int64_t* out,
                                                               float* in,
                                                               size_t numElements);