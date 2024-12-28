//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <stdlib.h>
#include <string.h>

#include "PAL/StringOp.hpp"

//---------------------------------------------------------------------------
//    pal::StringOp::memscpy
//---------------------------------------------------------------------------
size_t pal::StringOp::memscpy(void *dst, size_t dstSize, const void *src, size_t copySize) {
  if (!dst || !src || !dstSize || !copySize) return 0;

  size_t minSize = dstSize < copySize ? dstSize : copySize;

  memcpy(dst, src, minSize);

  return minSize;
}

#ifdef __hexagon__
size_t strnlen(const char *s, size_t n) {
  size_t i;
  for (i = 0; i < n && s[i] != '\0'; i++) continue;
  return i;
}
#endif

//---------------------------------------------------------------------------
//    pal::StringOp::strndup
//---------------------------------------------------------------------------
char *pal::StringOp::strndup(const char *source, size_t maxlen) {
#ifdef _WIN32
  size_t length = ::strnlen(source, maxlen);

  char *destination = (char *)malloc((length + 1) * sizeof(char));
  if (destination == nullptr) return nullptr;

  // copy length bytes to destination and leave destination[length] to be
  // null terminator
  strncpy_s(destination, length + 1, source, length);

  return destination;
#elif __hexagon__
  size_t length = strnlen(source, maxlen);

  char *destination = (char *)malloc((length + 1) * sizeof(char));
  if (destination == nullptr) return nullptr;
  // copy length bytes to destination and leave destination[length] to be
  // null terminator
  strncpy(destination, source, length);
  destination[length] = '\0';
  return destination;
#else
  return ::strndup(source, maxlen);
#endif
}
