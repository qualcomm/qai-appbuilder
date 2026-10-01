//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RESPONSE_TOOLS_SANITIZATION_SELFTEST_H
#define RESPONSE_TOOLS_SANITIZATION_SELFTEST_H

#include <ostream>

// 离线回放自测：验证 ResponseTools::remove_tool_call_content() 对"未闭合/跨多行截断的
// <tool_call>"输入的最终防线是否生效（Layer3 情形B 纯文本兜底场景，对应
// ToolCallFailureReason::kTruncated 等分类）。纯本地、零推理开销，可反复重跑做回归。
// 由隐藏 CLI 分支 `--self-test-sanitization`（GenieAPIService.cpp main() 顶部拦截）触发。
// 样本集设计取舍见同目录 response_tools.md。
//
// out：逐条打印用例结果与最终统计。
// 返回值：全部用例都符合预期时返回 true（供 CLI 退出码使用：0=全部通过，1=存在不符合项）。
bool RunToolCallSanitizationSelfTest(std::ostream &out);

#endif //RESPONSE_TOOLS_SANITIZATION_SELFTEST_H
