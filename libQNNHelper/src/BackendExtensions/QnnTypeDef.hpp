//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef QNN_TYPE_DEF_H_
#define QNN_TYPE_DEF_H_

#include "QnnInterface.h"
#include "QnnTypes.h"
#include "Log.hpp"
#include "QnnTypeMacros.hpp"


typedef enum ModelError {
  MODEL_NO_ERROR         = 0,
  MODEL_TENSOR_ERROR     = 1,
  MODEL_PARAMS_ERROR     = 2,
  MODEL_NODES_ERROR      = 3,
  MODEL_GRAPH_ERROR      = 4,
  MODEL_CONTEXT_ERROR    = 5,
  MODEL_GENERATION_ERROR = 6,
  MODEL_SETUP_ERROR      = 7,
  MODEL_INVALID_ARGUMENT_ERROR = 8,
  MODEL_FILE_ERROR             = 9,
  MODEL_MEMORY_ALLOCATE_ERROR  = 10,
  // Value selected to ensure 32 bits.
  MODEL_UNKNOWN_ERROR = 0x7FFFFFFF
} ModelError_t;

#ifndef QNN_ENABLE_API_2x
typedef struct QnnTensorWrapper {
  char *name;
  Qnn_Tensor_t tensor;
} Qnn_TensorWrapper_t;
#endif

#ifdef QNN_ENABLE_API_2x
using TensorWrapper = Qnn_Tensor_t;
#define GET_TENSOR_WRAPPER_TENSOR(tensorWrapper) tensorWrapper
#define GET_TENSOR_WRAPPER_NAME(tensorWrapper)   QNN_TENSOR_GET_NAME(tensorWrapper)
#else
using TensorWrapper = Qnn_TensorWrapper_t;
#define GET_TENSOR_WRAPPER_TENSOR(tensorWrapper) tensorWrapper.tensor
#define GET_TENSOR_WRAPPER_NAME(tensorWrapper)   tensorWrapper.name
#endif

typedef struct GraphInfo {
  Qnn_GraphHandle_t graph;
  char *graphName;
  TensorWrapper *inputTensors;
  uint32_t numInputTensors;
  TensorWrapper *outputTensors;
  uint32_t numOutputTensors;
} GraphInfo_t;
typedef GraphInfo_t *GraphInfoPtr_t;

typedef struct GraphConfigInfo {
  char *graphName;
  const QnnGraph_Config_t **graphConfigs;
} GraphConfigInfo_t;

#ifndef QNN_ENABLE_API_2x
typedef struct QnnParamWrapper {
  /// Type is scalar or tensor
  Qnn_ParamType_t paramType;
  /// Name of the parameter
  char *name;
  union {
    Qnn_Scalar_t scalarParam;
    Qnn_TensorWrapper_t tensorParam;
  };
} Qnn_ParamWrapper_t;
#endif

#endif  // QNN_TYPE_DEF_H_
