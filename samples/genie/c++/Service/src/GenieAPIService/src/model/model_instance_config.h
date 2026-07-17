//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_INSTANCE_CONFIG_H
#define MODEL_INSTANCE_CONFIG_H

#include "model_config.h"

using json = nlohmann::ordered_json;

// ============================================================
// ModelInstanceConfig: 单个模型实例的配置状态
// 从 IModelConfig 中分离出来，使每个模型实例拥有独立的配置
// ============================================================
class ModelInstanceConfig
{
public:
    // 基础配置访问器
    const std::string &get_model_path() const { return model_path_; }
    void set_model_path(const std::string &path) { model_path_ = path; }

    const std::string &get_model_name() const { return  model_name_; }
    void set_model_name(const std::string &name) { model_name_ = name; }

    // 修复：返回 int 值而非 int& 引用，避免外部直接修改私有成员破坏封装性
    // 同时移除 const 方法中的 mutable 修改语义，使接口语义更清晰
    int get_context_size() const { return context_size_; }
    void set_context_size(int size) { context_size_ = size; }

    const json &get_prompt_template() const { return prompt_; }
    void set_prompt_template(const json &prompt) { prompt_ = prompt; }

    PromptType get_prompt_type() const { return prompt_type_; }
    void set_prompt_type(PromptType type) { prompt_type_ = type; }

    ModelFormat get_model_format() const { return model_format_; }
    void set_model_format(ModelFormat format) { model_format_ = format; }

    bool is_thinking_model() const { return thinking_model_; }
    void set_thinking_model(bool is_thinking) { thinking_model_ = is_thinking; }

    // LoRA 配置
    const std::string &getloraAdapter() const { return loraAdapter_; }
    void set_lora_adapter(const std::string &adapter) { loraAdapter_ = adapter; }

    float getloraAlpha() const { return loraAlpha_; }
    void set_lora_alpha(float alpha) { loraAlpha_ = alpha; }

    // 输出配置
    bool getisOutputAllText() const { return outputAllText_; }
    void set_output_all_text(bool output_all) { outputAllText_ = output_all; }

    bool getenableThinking() const { return enableThinking_; }
    void set_enable_thinking(bool enable) { enableThinking_ = enable; }

    int getenablePromptDebug() const { return enablePromptDebug_; }
    void set_enable_prompt_debug(int level) { enablePromptDebug_ = level; }

    int getnumResponse() const { return numResponse_; }
    void set_num_response(int num) { numResponse_ = num; }

    int getminOutputNum() const { return minOutputNum_; }
    void set_min_output_num(int num) { minOutputNum_ = num; }

    // 后端类型（用于多模型路由）
    const std::string &get_backend() const { return backend_; }
    void set_backend(const std::string &backend) { backend_ = backend; }

    // 设备类型（CPU/GPU/NPU）
    const std::string &get_device() const { return device_; }
    void set_device(const std::string &device) { device_ = device; }

    // Tool Prompt Template
    const std::string &get_tool_prompt_template() const { return tool_prompt_template_; }
    void set_tool_prompt_template(const std::string &template_str) { tool_prompt_template_ = template_str; }

    IModelConfig i_model_config_;

    json &sampler() const { return sampler_; }

private:
    std::string model_path_;
    std::string model_name_;
    mutable json sampler_;
    int context_size_{DEFAULT_CONTEXT_SIZE};
    json prompt_{json::object()};
    bool thinking_model_{false};
    PromptType prompt_type_{};
    ModelFormat model_format_{};

    std::string loraAdapter_ = "default_adapter";
    float loraAlpha_ = 0.5;

    bool outputAllText_ = false;
    bool enableThinking_ = false;
    int enablePromptDebug_ = 0;
    int numResponse_ = 30;
    int minOutputNum_ = 512;

    std::string backend_ = "GGUF";  // GGUF, mnn, qnn
    std::string device_ = "gpu";    // cpu, gpu, npu

    std::string tool_prompt_template_ = "\n# Tools\n\n"
                                        "You may call one or more functions to assist with the user query.\n"
                                        "\n"
                                        "You are provided with function signatures within <tools></tools> XML tags:\n"
                                        "<tools>\n"
                                        "{tool_descs}\n"
                                        "</tools>\n"
                                        "\n"
                                        "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
                                        "<tool_call>\n"
                                        "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n"
                                        "</tool_call>\n";
};

#endif //MODEL_INSTANCE_CONFIG_H
