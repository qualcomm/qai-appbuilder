//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "long_text_summarizer.h"
#include "text_splitter.h"
#include "log.h"
#include "../gateway/security/security_utils.h"
#include <utils.h>

#include <sstream>
#include <algorithm>

// ── 构造函数 ──────────────────────────────────────────────────────────────────

LongTextSummarizer::LongTextSummarizer(
    const PromptOptimizationConfig::LongTextSummarizationConfig& config,
    const ModelInstanceConfig& instance_config,
    SummaryCache* cache,
    InferFn infer_fn,
    size_t context_size,
    IsAliveFn is_alive_fn
)
    : config_(config)
    , instance_config_(instance_config)
    , cache_(cache)
    , infer_fn_(std::move(infer_fn))
    , is_alive_fn_(std::move(is_alive_fn))
    , context_size_(context_size)
{
    // 计算触发阈值和分块大小
    trigger_threshold_ = static_cast<size_t>(
        static_cast<double>(context_size_) * config_.trigger_ratio);
    chunk_token_limit_ = static_cast<size_t>(
        static_cast<double>(context_size_) * config_.chunk_ratio);

    // 防止分块大小为 0
    if (chunk_token_limit_ == 0) chunk_token_limit_ = 512;
    if (trigger_threshold_ == 0) trigger_threshold_ = 256;
}

// ── ProcessMessages ───────────────────────────────────────────────────────────

void LongTextSummarizer::ProcessMessages(json& messages)
{
    if (!messages.is_array() || messages.empty()) return;

    // ── 规则 A：处理最后一条 user 文本消息 ──────────────────────
    if (config_.summarize_user_messages)
    {
        int user_idx = FindLastUserTextMessage(messages);
        if (user_idx >= 0)
        {
            std::string content = messages[user_idx]["content"].get<std::string>();
            size_t content_len = content.size();  // TokenLength 近似为字符数

            if (content_len > trigger_threshold_)
            {
                if (config_.verbose_logging)
                {
                    My_Log{My_Log::Level::kInfo}
                        << "[LongTextSummarizer] User message [" << user_idx
                        << "] len=" << content_len
                        << " > threshold=" << trigger_threshold_
                        << ", triggering summarization" << std::endl;
                }

                // ── 提取指令前缀 ──────────────────────────────────────────
                // 用户消息通常结构为：
                //   <指令><分隔符><长文档内容>
                // 例如：
                //   "帮我总结如下内容：\n\n# 文档标题\n..."
                //   "分析这段代码：\nfunction foo() {...}"
                //   "总结：# 标题\n内容"
                //
                // 策略：在前 kPrefixSearchWindow 个字符内寻找换行符，
                // 以最后一个换行符为分割点提取指令前缀。
                //
                // 设计原则：
                //   1. 用户指令通常很短（< 200 chars），文档内容通常很长
                //   2. 不依赖特定分隔符（\n\n 或 \n），只依赖长度比例
                //   3. 若前缀搜索窗口内没有换行符，则取整个窗口作为前缀
                //   4. 若 content 长度 <= kPrefixSearchWindow，说明没有长文档，
                //      不做前缀提取（整个 content 都是指令，不应触发摘要）
                //
                // 注意：tool 消息（role=tool）不走此分支，直接摘要整个 content。
                static constexpr size_t kPrefixSearchWindow = 200;

                std::string instruction_prefix;
                std::string content_to_summarize = content;

                if (content_len > kPrefixSearchWindow)
                {
                    // 在前 kPrefixSearchWindow 个字符内找最后一个换行符
                    std::string search_window = content.substr(0, kPrefixSearchWindow);
                    size_t last_lf = search_window.rfind('\n');

                    if (last_lf != std::string::npos && last_lf > 0)
                    {
                        // 以最后一个换行符后面的位置为分割点
                        size_t split_pos = last_lf + 1;
                        instruction_prefix = content.substr(0, split_pos);
                        content_to_summarize = content.substr(split_pos);
                    }
                    else
                    {
                        // 前 kPrefixSearchWindow 内没有换行符：
                        // 取整个搜索窗口作为前缀（保守策略，保留用户指令）
                        instruction_prefix = content.substr(0, kPrefixSearchWindow);
                        content_to_summarize = content.substr(kPrefixSearchWindow);
                    }

                    if (config_.verbose_logging && !instruction_prefix.empty())
                    {
                        My_Log{My_Log::Level::kInfo}
                            << "[LongTextSummarizer] Extracted instruction prefix ("
                            << instruction_prefix.size() << " chars): "
                            << instruction_prefix.substr(0, std::min(instruction_prefix.size(), size_t(80)))
                            << std::endl;
                    }
                }

                std::string summary = Summarize(content_to_summarize, "user message");
                if (!summary.empty() && summary.size() < content_to_summarize.size())
                {
                    // 拼接：指令前缀 + 摘要
                    std::string final_content = instruction_prefix + summary;
                    messages[user_idx]["content"] = final_content;
                    My_Log{My_Log::Level::kInfo}
                        << "[LongTextSummarizer] User message summarized: "
                        << content_len << " -> " << final_content.size() << " chars"
                        << " (prefix=" << instruction_prefix.size()
                        << ", summary=" << summary.size() << ")" << std::endl;
                }
            }
        }
    }

    // ── 规则 B：处理末尾连续 tool 消息链 ────────────────────────
    if (config_.summarize_tool_responses)
    {
        int tool_chain_start = FindTrailingToolChainStart(messages);
        if (tool_chain_start >= 0)
        {
            size_t n = messages.size();
            for (size_t i = static_cast<size_t>(tool_chain_start); i < n; ++i)
            {
                // 只处理 content 为字符串的 tool 消息
                if (!messages[i].contains("content") ||
                    !messages[i]["content"].is_string())
                    continue;

                std::string content = messages[i]["content"].get<std::string>();
                size_t content_len = content.size();

                if (content_len > trigger_threshold_)
                {
                    // 尝试获取工具名称用于日志
                    std::string tool_name = "tool";
                    if (messages[i].contains("name") && messages[i]["name"].is_string())
                        tool_name = messages[i]["name"].get<std::string>();

                    if (config_.verbose_logging)
                    {
                        My_Log{My_Log::Level::kInfo}
                            << "[LongTextSummarizer] Tool message [" << i
                            << "] name=" << tool_name
                            << " len=" << content_len
                            << " > threshold=" << trigger_threshold_
                            << ", triggering summarization" << std::endl;
                    }

                    std::string source_hint = "tool response from " + tool_name;
                    std::string summary = Summarize(content, source_hint);
                    if (!summary.empty() && summary.size() < content.size())
                    {
                        messages[i]["content"] = summary;
                        My_Log{My_Log::Level::kInfo}
                            << "[LongTextSummarizer] Tool message [" << i
                            << "] summarized: "
                            << content_len << " -> " << summary.size() << " chars" << std::endl;
                    }
                }
            }
        }
    }
}

// ── FindLastUserTextMessage ───────────────────────────────────────────────────

int LongTextSummarizer::FindLastUserTextMessage(const json& messages) const
{
    for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i)
    {
        const auto& msg = messages[i];
        if (!msg.is_object()) continue;
        if (!msg.contains("role") || !msg["role"].is_string()) continue;
        if (msg["role"].get<std::string>() != "user") continue;
        if (!msg.contains("content") || !msg["content"].is_string()) continue;
        return i;
    }
    return -1;
}

// ── FindTrailingToolChainStart ────────────────────────────────────────────────

int LongTextSummarizer::FindTrailingToolChainStart(const json& messages) const
{
    int n = static_cast<int>(messages.size());
    if (n == 0) return -1;

    // 从末尾向前扫描，找到连续 tool 消息的起始位置
    int i = n - 1;
    while (i >= 0)
    {
        const auto& msg = messages[i];
        if (!msg.is_object()) break;
        if (!msg.contains("role") || !msg["role"].is_string()) break;
        if (msg["role"].get<std::string>() != "tool") break;
        --i;
    }

    // i 现在指向第一个非 tool 消息（或 -1）
    int chain_start = i + 1;
    if (chain_start >= n) return -1;  // 末尾没有 tool 消息
    return chain_start;
}

// ── Summarize ─────────────────────────────────────────────────────────────────

std::string LongTextSummarizer::Summarize(const std::string& content,
                                           const std::string& source_hint)
{
    // 1. 先查缓存
    if (cache_)
    {
        std::string cached_summary;
        if (cache_->Lookup(content, cached_summary))
        {
            if (config_.verbose_logging)
            {
                My_Log{My_Log::Level::kInfo}
                    << "[LongTextSummarizer] Cache hit for " << source_hint
                    << " (len=" << content.size() << ")" << std::endl;
            }
            return cached_summary;
        }
    }

    // 2. 分块
    // 使用与 /textsplitter 接口相近的分隔符（支持中英文标点）
    std::vector<std::string> separators = {
        "\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", " ", ""
    };

    RecursiveCharacterTextSplitter splitter(
        separators,
        /*keep_separator=*/true,
        static_cast<int>(chunk_token_limit_),
        [](const std::string& s) { return s.size(); }
    );

    std::vector<std::string> chunks = splitter.split_text(content);

    if (chunks.empty())
    {
        My_Log{My_Log::Level::kWarning}
            << "[LongTextSummarizer] split_text returned empty chunks for "
            << source_hint << std::endl;
        return "";
    }

    // 3. 检查是否超过 max_chunks，截断尾部
    if (static_cast<int>(chunks.size()) > config_.max_chunks)
    {
        My_Log{My_Log::Level::kWarning}
            << "[LongTextSummarizer] " << source_hint
            << " has " << chunks.size() << " chunks, truncating to max_chunks="
            << config_.max_chunks << std::endl;
        chunks.resize(static_cast<size_t>(config_.max_chunks));
    }

    int total_chunks = static_cast<int>(chunks.size());

    // 4. Map 阶段：逐块摘要
    std::vector<std::string> chunk_summaries;
    chunk_summaries.reserve(static_cast<size_t>(total_chunks));

    for (int idx = 0; idx < total_chunks; ++idx)
    {
        // 每次 Map 推理前检测客户端连接是否仍然存活
        // 若连接已断开（客户端超时），提前终止摘要推理，避免浪费 NPU 资源
        if (is_alive_fn_ && !is_alive_fn_())
        {
            My_Log{My_Log::Level::kWarning}
                << "[LongTextSummarizer] Client disconnected before chunk " << (idx + 1)
                << "/" << total_chunks << " of " << source_hint
                << ", aborting summarization" << std::endl;
            return "";
        }

        std::string chunk_summary = MapChunk(chunks[static_cast<size_t>(idx)],
                                             idx + 1, total_chunks, source_hint);
        if (chunk_summary.empty())
        {
            // Map 失败：降级，返回空串，调用方保留原文
            My_Log{My_Log::Level::kWarning}
                << "[LongTextSummarizer] Map failed for chunk " << (idx + 1)
                << "/" << total_chunks << " of " << source_hint
                << ", falling back to original" << std::endl;
            return "";
        }
        chunk_summaries.push_back(std::move(chunk_summary));
    }

    // 5. Reduce 阶段（仅当块数 > 1 时）
    std::string final_summary;
    if (total_chunks == 1)
    {
        final_summary = chunk_summaries[0];
    }
    else
    {
        final_summary = ReduceSummaries(chunk_summaries, source_hint);
        if (final_summary.empty())
        {
            My_Log{My_Log::Level::kWarning}
                << "[LongTextSummarizer] Reduce failed for " << source_hint
                << ", falling back to original" << std::endl;
            return "";
        }
    }

    // 6. 保护规则：摘要不比原文短则不使用
    if (final_summary.size() >= content.size())
    {
        My_Log{My_Log::Level::kInfo}
            << "[LongTextSummarizer] Summary not shorter than original for "
            << source_hint << " (" << final_summary.size()
            << " >= " << content.size() << "), discarding" << std::endl;
        return "";
    }

    // 7. 写入缓存
    if (cache_)
    {
        cache_->Put(content, final_summary);
    }

    My_Log{My_Log::Level::kInfo}
        << "[LongTextSummarizer] Final summary for " << source_hint
        << ": " << content.size() << " -> " << final_summary.size() << " chars"
        << " (ratio=" << (100.0 * final_summary.size() / content.size()) << "%)"
        << "\n--- summary begin ---\n"
        << final_summary
        << "\n--- summary end ---" << std::endl;

    return final_summary;
}

// ── MapChunk ──────────────────────────────────────────────────────────────────

std::string LongTextSummarizer::MapChunk(const std::string& chunk,
                                          int chunk_idx, int total_chunks,
                                          const std::string& source_hint)
{
    std::string prompt = BuildMapPrompt(chunk, chunk_idx, total_chunks, source_hint);
    std::string result = infer_fn_(prompt);

    std::string trimmed = SecurityUtils::TrimWhitespace(result);

    My_Log{My_Log::Level::kInfo}
        << "[LongTextSummarizer][Map] chunk " << chunk_idx << "/" << total_chunks
        << " of " << source_hint
        << " -> summary (" << trimmed.size() << " chars):\n"
        << trimmed << std::endl;

    return trimmed;
}

// ── ReduceSummaries ───────────────────────────────────────────────────────────

std::string LongTextSummarizer::ReduceSummaries(const std::vector<std::string>& summaries,
                                                  const std::string& source_hint)
{
    // 将所有分块摘要拼接为一个文本
    std::ostringstream combined;
    for (size_t i = 0; i < summaries.size(); ++i)
    {
        combined << "--- Part " << (i + 1) << " of " << summaries.size() << " ---\n";
        combined << summaries[i] << "\n\n";
    }

    std::string prompt = BuildReducePrompt(combined.str(), source_hint);
    std::string result = infer_fn_(prompt);

    std::string trimmed = SecurityUtils::TrimWhitespace(result);

    My_Log{My_Log::Level::kInfo}
        << "[LongTextSummarizer][Reduce] " << source_hint
        << " -> merged summary (" << trimmed.size() << " chars):\n"
        << trimmed << std::endl;

    return trimmed;
}

// ── BuildMapPrompt ────────────────────────────────────────────────────────────

std::string LongTextSummarizer::BuildMapPrompt(const std::string& chunk,
                                                int chunk_idx, int total_chunks,
                                                const std::string& source_hint) const
{
    // 构造摘要任务说明
    std::ostringstream task;
    task << config_.map_instruction << "\n\n";
    task << "Source: " << source_hint << "\n";
    if (total_chunks > 1)
    {
        task << "Part " << chunk_idx << " of " << total_chunks << "\n";
    }
    task << "\n" << chunk;

    std::string task_str = task.str();

    bool is_harmony = (instance_config_.get_prompt_type() == PromptType::Harmony);

    if (is_harmony)
    {
        // Harmony 格式：使用固定协议字符串（与 BuildHarmonyPrompt 风格一致）
        std::string prompt;
        prompt += "<|start|>system<|message|>";
        prompt += "You are a compression assistant. Your task is to summarize content accurately.";
        prompt += "<|end|>";
        prompt += "<|start|>user<|message|>";
        prompt += task_str;
        prompt += "<|end|>";
        prompt += "<|start|>assistant<|channel|>final<|message|>";
        return prompt;
    }
    else
    {
        // General 格式：完全对齐 BuildPrompt() 的模板替换约定
        // 占位符约定：codebase 中 system / user / assistant / tool 模板均以
        // 字面量 "string" 作为内容槽位（见 chat_history.cpp / model_input_builder.h）
        //
        // 最小推理 prompt 结构（无历史消息）：
        //   str_replace(system, "string", instruction + "/no_think")  ← 禁止思考
        //   + str_replace(user,   "string", task_str)
        //   + start + FILL_THINK  ← 注入空 think 块，与主推理保持一致
        const auto& j = instance_config_.get_prompt_template();

        if (j.is_object()
                && j.contains("system") && j["system"].is_string()
                && j.contains("user")   && j["user"].is_string()
                && j.contains("start")  && j["start"].is_string())
        {
            // 若为 thinking 模型，追加 /no_think 并注入空 think 块，避免模型进入思考模式
            // 思考模式会大幅增加摘要推理耗时，且摘要任务不需要深度推理
            std::string system_instruction =
                "You are a compression assistant. Your task is to summarize content accurately.";
            std::string start_str = j["start"].get<std::string>();

            if (instance_config_.is_thinking_model())
            {
                system_instruction += "/no_think";
                start_str += "<think>\n\n</think>\n\n";
            }

            return str_replace(j["system"].get<std::string>(), "string", system_instruction)
                 + str_replace(j["user"].get<std::string>(), "string", task_str)
                 + start_str;
        }
        else
        {
            // fallback：模板不可用时直接返回任务文本
            return task_str;
        }
    }
}

// ── BuildReducePrompt ─────────────────────────────────────────────────────────

std::string LongTextSummarizer::BuildReducePrompt(const std::string& combined_summaries,
                                                    const std::string& source_hint) const
{
    std::ostringstream task;
    task << config_.reduce_instruction << "\n\n";
    task << "Source: " << source_hint << "\n\n";
    task << combined_summaries;

    std::string task_str = task.str();

    bool is_harmony = (instance_config_.get_prompt_type() == PromptType::Harmony);

    if (is_harmony)
    {
        std::string prompt;
        prompt += "<|start|>system<|message|>";
        prompt += "You are a compression assistant. Your task is to merge partial summaries into one coherent summary.";
        prompt += "<|end|>";
        prompt += "<|start|>user<|message|>";
        prompt += task_str;
        prompt += "<|end|>";
        prompt += "<|start|>assistant<|channel|>final<|message|>";
        return prompt;
    }
    else
    {
        // General 格式：完全对齐 BuildPrompt() 的模板替换约定（同 BuildMapPrompt）
        const auto& j = instance_config_.get_prompt_template();

        if (j.is_object()
                && j.contains("system") && j["system"].is_string()
                && j.contains("user")   && j["user"].is_string()
                && j.contains("start")  && j["start"].is_string())
        {
            std::string system_instruction =
                "You are a compression assistant. Your task is to merge partial summaries into one coherent summary.";
            std::string start_str = j["start"].get<std::string>();

            if (instance_config_.is_thinking_model())
            {
                system_instruction += "/no_think";
                start_str += "<think>\n\n</think>\n\n";
            }

            return str_replace(j["system"].get<std::string>(), "string", system_instruction)
                 + str_replace(j["user"].get<std::string>(), "string", task_str)
                 + start_str;
        }
        else
        {
            // fallback：模板不可用时直接返回任务文本
            return task_str;
        }
    }
}
