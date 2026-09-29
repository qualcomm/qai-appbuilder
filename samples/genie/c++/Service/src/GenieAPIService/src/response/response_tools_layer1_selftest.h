//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef RESPONSE_TOOLS_LAYER1_SELFTEST_H
#define RESPONSE_TOOLS_LAYER1_SELFTEST_H

#include <ostream>

// 离线回放自测：驱动一批真实感畸形 tool_call 样本，验证 ResponseTools::convertToolCallJson()
// 的 Layer1 兜底提取（现有正则修复链全部失败之后触发）。纯本地、零推理开销，可反复重跑做回归。
// 由隐藏 CLI 分支 `--self-test-layer1-recovery`（GenieAPIService.cpp main() 顶部拦截）触发。
// 样本集设计取舍、判定口径见同目录 response_tools.md。
//
// out：逐条打印用例结果与最终统计（总数 / 成功恢复语义正确率 / 失败分类准确率）。
// 返回值：全部用例都符合预期时返回 true（供 CLI 退出码使用：0=全部通过，1=存在不符合项）。
bool RunLayer1RecoverySelfTest(std::ostream &out);

#endif //RESPONSE_TOOLS_LAYER1_SELFTEST_H
