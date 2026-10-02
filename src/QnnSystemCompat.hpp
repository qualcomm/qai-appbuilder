//=============================================================================
//
// Compatibility definitions for older QAIRT QNN SDKs.
//
//=============================================================================
#pragma once

#include "QnnCommon.h"

// QAIRT 2.21 exposes the DLC API but omits its handle typedef.  The handle is
// opaque, just like QnnSystemContext_Handle_t, so provide the missing typedef
// for SDKs before the API version that declares it.
#if QNN_API_VERSION_MINOR < 22
typedef void* QnnSystemDlc_Handle_t;
#endif
