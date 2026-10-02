//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_CIRCUIT_BREAKER_STORE_SELFTEST_H
#define TOOL_CALL_CIRCUIT_BREAKER_STORE_SELFTEST_H

#include <ostream>

// 离线自测：直接调用 ToolCallCircuitBreakerStore 的公开接口验证"会话+模型维度连续
// 触发 Layer3 计数/降级/冷却重置"这条链路的端到端语义（Step3 计划 Testing 一节要求的
// 同一 session 连续多轮、不同 session 交错、模型切换场景计数归属、无竞态）。纯本地、
// 不依赖真机推理，可反复重跑做回归。由隐藏 CLI 分支 `--self-test-circuit-breaker`
// （GenieAPIService.cpp main() 顶部拦截）触发。设计取舍见
// chat_request_handler/tool_call_circuit_breaker_store.md。
//
// out：逐条打印用例结果与最终统计。
// 返回值：全部用例都符合预期时返回 true（供 CLI 退出码使用：0=全部通过，1=存在不符合项）。
bool RunToolCallCircuitBreakerSelfTest(std::ostream &out);

#endif //TOOL_CALL_CIRCUIT_BREAKER_STORE_SELFTEST_H
