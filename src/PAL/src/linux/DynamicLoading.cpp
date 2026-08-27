//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <dlfcn.h>
#include <stdlib.h>
#include <link.h>  // issue#97: Lmid_t / LM_ID_BASE for dlOpenInNamespace (dlmopen)

#include "PAL/Debug.hpp"
#include "PAL/DynamicLoading.hpp"

void *pal::dynamicloading::dlOpen(const char *filename, int flags) {
  int realFlags = 0;

  if (flags & DL_NOW) {
    realFlags |= RTLD_NOW;
  }

  if (flags & DL_LOCAL) {
    realFlags |= RTLD_LOCAL;
  }

  if (flags & DL_GLOBAL) {
    realFlags |= RTLD_GLOBAL;
  }

  return ::dlopen(filename, realFlags);
}

// issue#97: dlmopen wrapper - load a library into a specific link-map
// namespace. Used by the fork()ed-child backend reset to get a fresh copy of
// the QNN/fastrpc client libraries with clean static state (rpcmem pool /
// fastrpc session handles) instead of the parent's inherited ones.
void *pal::dynamicloading::dlOpenInNamespace(long lmid, const char *filename, int flags) {
  int realFlags = 0;

  if (flags & DL_NOW) {
    realFlags |= RTLD_NOW;
  }

  if (flags & DL_LOCAL) {
    realFlags |= RTLD_LOCAL;
  }

  // dlmopen does not accept RTLD_GLOBAL (EINVAL); drop it for non-base
  // namespaces. Local visibility is fine: QNN resolves symbols relative to its
  // own handles.
  if ((flags & DL_GLOBAL) && lmid == LM_ID_BASE) {
    realFlags |= RTLD_GLOBAL;
  }

  if (lmid == LM_ID_BASE) {
    return ::dlopen(filename, realFlags);
  }
  return ::dlmopen((Lmid_t)lmid, filename, realFlags);
}

void *pal::dynamicloading::dlSym(void *handle, const char *symbol) {
  if (handle == DL_DEFAULT) {
    return ::dlsym(RTLD_DEFAULT, symbol);
  }

  return ::dlsym(handle, symbol);
}

int pal::dynamicloading::dlAddrToLibName(void *addr, std::string &name) {
  // Clean the output buffer
  name = std::string();

  // If the address is empty, return zero as treating failure
  if (!addr) {
    DEBUG_MSG("Input address is nullptr.");
    return 0;
  }

  // Dl_info do not maintain the lifetime of its string members,
  // it would be maintained by dlopen() and dlclose(),
  // so we do not need to release it manually
  Dl_info info;
  int result = ::dladdr(addr, &info);

  // If dladdr() successes, set name to the library name
  if (result) {
    name = std::string(info.dli_fname);
  } else {
    DEBUG_MSG("Input address could not be matched to a shared object.");
  }

  return result;
}

int pal::dynamicloading::dlClose(void *handle) {
  if (!handle) {
    return 0;
  }

  return ::dlclose(handle);
}

const char *pal::dynamicloading::dlError(void) { return ::dlerror(); }
