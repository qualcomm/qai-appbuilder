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

// 离线自测：验证 ResponseTools::DetectBareToolCall()（"Layer -1"：无 <tool_call> 标签时的
// 裸 JSON 工具调用检测）。覆盖 think 前缀剥离后场景、代码围栏、短前置说明文字、真机复现的
// 转义引号损坏样本，以及"不应误判"的负向对照（无标签已知工具名、无 arguments 字段、
// 幻觉工具名、JSON 嵌在长说明文字中）。由隐藏 CLI 分支 `--self-test-bare-json-detect`
// （GenieAPIService.cpp main() 顶部拦截）触发。设计取舍见同目录 response_tools.md「Layer -1」节。
//
// out：逐条打印用例结果与最终统计。
// 返回值：全部用例都符合预期时返回 true（供 CLI 退出码使用：0=全部通过，1=存在不符合项）。
bool RunBareToolCallDetectionSelfTest(std::ostream &out);

// 离线自测：验证 ResponseTools::ApplyBareJsonHoldBack()（"Layer -1" 流式 hold-back 状态机）
// 在"逐 chunk 到达"场景下的行为，与 RunBareToolCallDetectionSelfTest 是两条独立代码路径
// （后者只测生成结束后对完整文本的一次性判定）。至少覆盖真机复现的确切边界 bug："</think>"
// 独占一个 chunk、裸 JSON 分散在后续多个 chunk 到达；以及回归防护：</think> 与 post-think
// 内容同在一个 chunk 内（修复前唯一能正常工作的场景）。由隐藏 CLI 分支
// `--self-test-bare-json-holdback`（GenieAPIService.cpp main() 顶部拦截）触发。设计取舍见
// 同目录 response_dispatcher.md「Layer -1 hold-back」一节。
//
// out：逐条打印用例结果与最终统计。
// 返回值：全部用例都符合预期时返回 true（供 CLI 退出码使用：0=全部通过，1=存在不符合项）。
bool RunBareJsonHoldBackStreamingSelfTest(std::ostream &out);

#endif //RESPONSE_TOOLS_LAYER1_SELFTEST_H
