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

/*
#include <algorithm>
#include <execution>
#include <numeric>
#include <vector>
#include <cstddef>
#include <cstring>

size_t pal::StringOp::memscpy(void* __restrict dst, size_t dstSize,
      const void* __restrict src, size_t copySize,
      unsigned blocks)
{
    if (!dst || !src || dstSize == 0 || copySize == 0) return 0;

    const size_t n = (dstSize < copySize) ? dstSize : copySize;

    if (blocks == 0) blocks = 1;

    // ?????????(????? 1 ??,????“??”)
    if (blocks > n) blocks = static_cast<unsigned>(n);

    // ??:????????????(??????/????)
    unsigned hw = std::thread::hardware_concurrency();
    if (hw >= 8) hw = hw -2;
    if (hw == 0) hw = 4;
    // ???????? 2~8 ????????,???????
    blocks = std::min(blocks, std::min(hw, 8u));
    printf("blocks = %d\n", blocks);

    auto* d = static_cast<unsigned char*>(dst);
    auto* s = static_cast<const unsigned char*>(src);

    // ??:?“??”???chunk = ceil(n / blocks)
    size_t chunk = (n + blocks - 1) / blocks;

    // ??:??? 64B,????????? cache line ??
    chunk = (chunk + 63) & ~size_t(63);

    std::vector<unsigned> ids(blocks);
    std::iota(ids.begin(), ids.end(), 0u);

    std::for_each(std::execution::par, ids.begin(), ids.end(),
                  [=](unsigned i) noexcept {
                    const size_t begin = static_cast<size_t>(i) * chunk;
                    if (begin >= n) return;
                    const size_t end = std::min(begin + chunk, n);
                    memcpy(d + begin, s + begin, end - begin);
                  });

    return n;
}
*/

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
