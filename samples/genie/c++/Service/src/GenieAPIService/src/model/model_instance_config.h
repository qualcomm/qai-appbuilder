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

#include "../context/llama_runtime_planner.h"

using json = nlohmann::ordered_json;

class ModelInstanceConfig
{
public:
    ModelInstanceConfig() = default;
    ModelInstanceConfig(const ModelInstanceConfig &) = delete;
    ModelInstanceConfig &operator=(const ModelInstanceConfig &) = delete;

    ~ModelInstanceConfig()
    {
        i_model_config_.genieModelHandle.reset();
        i_model_config_.qnn_embedding_.Clean();
    }

    const std::string &get_model_path() const { return model_path_; }
    void set_model_path(const std::string &path) { model_path_ = path; }

    const std::string &get_config_file() const { return config_file_; }
    void set_config_file(const std::string &path) { config_file_ = path; }

    const std::string &get_known_model_path() const { return known_model_path_; }
    void set_known_model_path(const std::string &path) { known_model_path_ = path; }

    const std::string &get_model_name() const { return  model_name_; }
    void set_model_name(const std::string &name) { model_name_ = name; }

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

    bool is_speculative_draft_configured() const { return speculative_draft_configured_; }
    void set_speculative_draft_configured(bool configured) { speculative_draft_configured_ = configured; }

    bool is_speculative_draft_forced_cpu() const { return speculative_draft_forced_cpu_; }
    void set_speculative_draft_forced_cpu(bool forced) { speculative_draft_forced_cpu_ = forced; }

    bool is_speculative_reduced_ubatch() const { return speculative_reduced_ubatch_; }
    void set_speculative_reduced_ubatch(bool reduced) { speculative_reduced_ubatch_ = reduced; }

    bool is_speculative_full_gpu() const { return speculative_full_gpu_; }
    void set_speculative_full_gpu(bool full_gpu) { speculative_full_gpu_ = full_gpu; }

    bool has_runtime_plan() const { return has_runtime_plan_; }
    const llama_runtime::RuntimePlan &runtime_plan() const { return runtime_plan_; }
    void set_runtime_plan(const llama_runtime::RuntimePlan &plan)
    {
        runtime_plan_ = plan;
        has_runtime_plan_ = true;
    }

    const std::string &getloraAdapter() const { return loraAdapter_; }
    void set_lora_adapter(const std::string &adapter) { loraAdapter_ = adapter; }

    float getloraAlpha() const { return loraAlpha_; }
    void set_lora_alpha(float alpha) { loraAlpha_ = alpha; }

    bool getisOutputAllText() const { return outputAllText_; }
    void set_output_all_text(bool output_all) { outputAllText_ = output_all; }

    bool getenableThinking() const { return enableThinking_; }
    void set_enable_thinking(bool enable) { enableThinking_ = enable; }

    int getenablePromptDebug() const { return enablePromptDebug_; }
    void set_enable_prompt_debug(int level) { enablePromptDebug_ = level; }

    int getnumResponse() const { return numResponse_; }
    void set_num_response(int num) { numResponse_ = num; }

    bool IsStatelessMode() const { return numResponse_ == -1; }

    int getminOutputNum() const { return minOutputNum_; }
    void set_min_output_num(int num) { minOutputNum_ = num; }

    const std::string &get_backend() const { return backend_; }
    void set_backend(const std::string &backend) { backend_ = backend; }

    const std::string &get_device() const { return device_; }
    void set_device(const std::string &device) { device_ = device; }

    const std::string &get_tool_prompt_template() const { return tool_prompt_template_; }
    void set_tool_prompt_template(const std::string &template_str) { tool_prompt_template_ = template_str; }

    IModelConfig i_model_config_;

    json &sampler() const { return sampler_; }

private:
    std::string model_path_;
    std::string config_file_;
    std::string known_model_path_;
    std::string model_name_;
    mutable json sampler_;
    int context_size_{DEFAULT_CONTEXT_SIZE};
    json prompt_{json::object()};
    bool thinking_model_{false};
    bool speculative_draft_configured_{false};
    bool speculative_draft_forced_cpu_{false};
    bool speculative_reduced_ubatch_{false};
    bool speculative_full_gpu_{false};
    bool has_runtime_plan_{false};
    llama_runtime::RuntimePlan runtime_plan_{};
    PromptType prompt_type_{};
    ModelFormat model_format_{};

    std::string loraAdapter_ = "default_adapter";
    float loraAlpha_ = 0.5;

    bool outputAllText_ = false;
    bool enableThinking_ = false;
    int enablePromptDebug_ = 0;
    int numResponse_ = 30;
    int minOutputNum_ = 512;

    std::string backend_ = "GGUF";
    std::string device_ = "gpu";

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

#endif
