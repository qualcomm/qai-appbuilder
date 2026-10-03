//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_MANAGER_H
#define MODEL_MANAGER_H

#include "model_config.h"
#include "model_instance_config.h"
#include <memory>
#include <mutex>
#include <atomic>

class ContextBase;

// ============================================================
// LoadedModel: 已加载的模型实例（配置 + 上下文）
// ============================================================
struct LoadedModel
{
    std::shared_ptr<ModelInstanceConfig> config;
    std::shared_ptr<ContextBase> context;
    std::string backend;  // "GGUF", "mnn", "qnn"
    std::string device;   // "cpu", "gpu", "npu"
    bool is_loaded{false};
};

// ============================================================
// ModelManager: 单模型管理器
// 管理当前唯一活跃的模型实例；支持按名字顺序切换（LoadModelByName，先卸载旧模型
// 再加载新模型），不支持并发驻留多个模型（该设计已删除，原因见 model.md）。
// ============================================================
class ModelManager : public IModelConfig
{
public:
    explicit ModelManager(IModelConfig &&config);

    // 修复：显式析构函数，确保进程退出（含非优雅关闭路径，如测试框架直接终止进程后
    // 触发的 atexit 析构链）时，仍会在真正释放 QNN/NPU 模型最后一份引用之前完成等待。
    // 若仅依赖编译器为 current_model_/genieModelHandle 生成的隐式成员析构，等待逻辑
    // （原本只存在于 Clean()/UnloadModel() 等显式调用路径中）完全不会被触发，
    // 已通过 minidump 复现确认这会在 atexit 析构链中导致竞态型崩溃。
    ~ModelManager() override;

    // 向后兼容的单模型接口
    bool LoadModelByName(const std::string &new_model, bool &first_load);
    bool InitializeConfig();
    void UnloadModel();

    bool IsLoaded()
    {
        return loaded_.load();
    }

    // 重写 IModelConfig::IsLocalModelAvailable()：检查当前是否有已加载的模型
    bool IsLocalModelAvailable() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return current_model_ != nullptr;
    }

    // 重写 IModelConfig::GetDefaultModelHandle()：返回当前模型的上下文句柄，
    // 用于安全检查/复杂度评估/脱敏。
    // 回退策略：当前模型不可用时，回退到 IModelConfig::genieModelHandle（向后兼容）。
    std::weak_ptr<ContextBase> GetDefaultModelHandle() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (current_model_ && current_model_->context) {
            return current_model_->context;
        }
        // 向后兼容：回退到全局 genieModelHandle
        return genieModelHandle;
    }

    // 重写 IModelConfig::GetDefaultInstanceConfig()：返回当前模型的 ModelInstanceConfig*
    // 用于 BuildLocalModelPrompt 等安全相关函数，确保读取的是当前模型的实际配置
    // （is_thinking_model / get_prompt_type / get_prompt_template 等），
    // 而非全局 IModelConfig 的成员。
    // 回退策略：当前模型不可用时，返回 nullptr（调用方回退到全局 IModelConfig）。
    ModelInstanceConfig* GetDefaultInstanceConfig() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (current_model_ && current_model_->config) {
            return current_model_->config.get();
        }
        return nullptr;
    }

    // 根据模型名称获取当前已加载的模型实例（线程安全）；
    // model_name 为空，或与当前模型名称匹配时返回，否则返回 nullptr。
    std::shared_ptr<LoadedModel> GetModel(const std::string &model_name);

    // 获取当前已加载的模型（向后兼容既有调用点）
    std::shared_ptr<LoadedModel> GetDefaultModel();

    // 列出当前已加载的模型（至多一个；保留 vector 形态以兼容既有调用点）
    std::vector<std::string> ListLoadedModels() const;

    // 扫描 model_root_ 目录，返回所有含 config.json 的子目录信息
    // 每项：{"id": name, "context_length": N, "backend": "qnn/GGUF/mnn", "device": "npu/gpu/cpu"}
    // model_root_ 为空时返回空列表
    std::vector<json> ScanModelDirectory() const;

    // 卸载当前活跃模型（若其 device 与参数匹配），切换模型前调用以释放硬件资源
    // device: "npu" / "gpu" / "cpu"
    void UnloadModelsByDevice(const std::string &device);

    bool LoadSingleModel();  // 单模型加载逻辑：-c 主模型与 LoadModelByName() 切换共用

    // 最近一次模型加载失败原因：供 HTTP 层（ChatRequestHandler）在返回加载失败的错误响应时
    // 附加可被程序化识别的失败原因字段，与其它加载失败原因区分（如内存不足 vs 其它）。
    enum class LoadFailureReason
    {
        kNone,               // 未记录到特定失败原因（默认值/最近一次加载成功）
        kInsufficientMemory, // 预检查判定内存不足而拒绝加载（MnnVerifier/GGUFVerify 均会设置）
        kOther               // 其它已知失败原因（预留，当前未细分）
    };

    // 设置最近一次加载失败原因（线程安全，复用 models_mutex_）
    void SetLastLoadFailureReason(LoadFailureReason reason, const std::string &detail)
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        last_load_failure_reason_ = reason;
        last_load_failure_detail_ = detail;
    }

    // 获取最近一次加载失败原因（线程安全）
    LoadFailureReason GetLastLoadFailureReason() const
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return last_load_failure_reason_;
    }

    // 获取最近一次加载失败的详细说明文本（线程安全）
    std::string GetLastLoadFailureDetail() const
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return last_load_failure_detail_;
    }

private:

    // 内部加载逻辑
    std::shared_ptr<ContextBase> CreateContext(
        const std::shared_ptr<ModelInstanceConfig> &config,
        const std::string &backend);

    PromptType LoadPromptTemplates(std::string &&prompt_path);
    std::string ResolveKnownModelPath(const std::string& model_feature, bool only_prefix);
    void Clean();
    static bool ModelComparer(const std::string &source, const std::string &target, bool only_prefix);

    struct ModeVerifier;
    class QNNImpl;

    // 估算即将被替换的旧模型（切换模型时尚未完全释放）的内存占用，供 MnnVerifier 内存预检查
    // 从"可用物理内存"中扣除，降低模型切换瞬间预检查失效的概率。
    // 已知局限：目前只有 MNN 后端有基于权重文件大小的精确估算公式（MNNContext::EstimateMnnMemoryRequirement），
    // 对其它后端（QNN/GGUF）复用同一函数——若其模型目录下没有 .mnn 文件，
    // 至少会计入固定安全余量 kMnnMemoryEstimateMarginBytes，作为对其驻留内存的保守预留。
    uint64_t EstimateOtherLoadedModelsMemoryBytes() const;

    // 当前唯一活跃模型（线程安全）。本仓库已删除并发多模型驻留设计（原 loaded_models_
    // 注册表），模型切换（LoadModelByName）通过替换此指针实现，不支持同时驻留多个模型。
    std::shared_ptr<LoadedModel> current_model_;
    mutable std::mutex models_mutex_;

    std::atomic<bool> loaded_{false};

    // 最近一次模型加载失败原因（受 models_mutex_ 保护）
    LoadFailureReason last_load_failure_reason_{LoadFailureReason::kNone};
    std::string last_load_failure_detail_;
};

#endif //MODEL_MANAGER_H
