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
#include <unordered_map>
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
// ModelManager: 多模型管理器
// 管理多个模型实例，支持并发推理
// ============================================================
class ModelManager : public IModelConfig
{
public:
    explicit ModelManager(IModelConfig &&config);

    // 修复：显式析构函数，确保进程退出（含非优雅关闭路径，如测试框架直接终止进程后
    // 触发的 atexit 析构链）时，仍会在真正释放 QNN/NPU 模型最后一份引用之前完成等待。
    // 若仅依赖编译器为 loaded_models_/genieModelHandle 生成的隐式成员析构，等待逻辑
    // （原本只存在于 Clean()/UnloadModel() 等显式调用路径中）完全不会被触发，
    // 已通过 minidump 复现确认这会在 atexit 析构链中导致竞态型崩溃。
    ~ModelManager() override;

    // 向后兼容的单模型接口
    bool LoadModelByName(const std::string &new_model, bool &first_load);
    bool InitializeConfig(bool load);
    void UnloadModel();
    // 修复：多模型模式下 loaded_ 只在 LoadSingleModel() 中设置，
    // 通过 LoadModel(name,path,...) 加载的模型不会设置 loaded_，
    // 导致 IsLoaded() 在多模型模式下永远返回 false（FetchModelStatus 端点报告模型未加载）。
    // 修复方案：同时检查 loaded_ 标志和 loaded_models_ 是否非空，
    // 任一为 true 则认为有模型已加载。
    bool IsLoaded()
    {
        if (loaded_.load()) return true;
        std::lock_guard<std::mutex> lock(models_mutex_);
        return !loaded_models_.empty();
    }

    // 重写 IModelConfig::IsLocalModelAvailable()：检查是否有任何已加载的模型（多模型模式）
    bool IsLocalModelAvailable() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        return !loaded_models_.empty();
    }

    // 重写 IModelConfig::GetDefaultModelHandle()：返回 default 模型的上下文句柄
    // 用于安全检查/复杂度评估/脱敏，始终使用 default 模型（不跟随客户端指定的模型）
    // 语义明确：无论客户端指定哪个模型，安全相关操作始终使用 default 模型，
    // 避免安全检查跟随客户端模型动态切换（例如切换到 QNN 模型后安全检查也切换到 QNN）。
    // 回退策略：若 default 模型不可用，回退到 IModelConfig::genieModelHandle（向后兼容）。
    std::weak_ptr<ContextBase> GetDefaultModelHandle() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (!default_model_name_.empty()) {
            auto it = loaded_models_.find(default_model_name_);
            if (it != loaded_models_.end() && it->second && it->second->context) {
                return it->second->context;
            }
        }
        // 向后兼容：回退到全局 genieModelHandle（单模型模式）
        return genieModelHandle;
    }

    // 重写 IModelConfig::GetDefaultInstanceConfig()：返回 default 模型的 ModelInstanceConfig*
    // 用于 BuildLocalModelPrompt 等安全相关函数，确保读取的是 default 模型的实际配置
    // （is_thinking_model / get_prompt_type / get_prompt_template 等），
    // 而非全局 IModelConfig 的成员（后者在多模型场景下可能被 -c 参数模型污染）。
    // 回退策略：若 default 模型不可用，返回 nullptr（调用方回退到全局 IModelConfig）。
    ModelInstanceConfig* GetDefaultInstanceConfig() const override
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        if (!default_model_name_.empty()) {
            auto it = loaded_models_.find(default_model_name_);
            if (it != loaded_models_.end() && it->second && it->second->config) {
                return it->second->config.get();
            }
        }
        return nullptr;
    }

    // 新增：多模型管理接口
    // 根据模型名称获取已加载的模型实例（线程安全）
    std::shared_ptr<LoadedModel> GetModel(const std::string &model_name);
    
    // 加载指定路径的模型（支持指定后端和设备）
    bool LoadModel(const std::string &model_name,
                   const std::string &backend = "GGUF",
                   const std::string &device = "gpu",
                   int context_size = 0,
                   const std::string &model_path = "");
    
    // 从配置文件加载所有模型。backend_filter 非空时，只加载 backend 字段（大小写无关）与其匹配的条目，
    // 其它条目被跳过并记录日志（供库模式限定仅加载 qnn/NPU 模型使用）。
    bool LoadAllModelsFromConfig(const std::string &backend_filter = "");
    
    // 获取默认模型（向后兼容）
    std::shared_ptr<LoadedModel> GetDefaultModel();
    
    // 列出所有已加载的模型
    std::vector<std::string> ListLoadedModels() const;

    // 扫描 model_root_ 目录，返回所有含 config.json 的子目录信息
    // 每项：{"id": name, "context_length": N, "backend": "qnn/GGUF/mnn", "device": "npu/gpu/cpu"}
    // model_root_ 为空时返回空列表
    std::vector<json> ScanModelDirectory() const;

    // 卸载指定设备上的所有已加载模型（切换模型前调用，释放硬件资源）
    // device: "npu" / "gpu" / "cpu"
    void UnloadModelsByDevice(const std::string &device);

    // 设置默认模型名称（动态切换后更新）
    void SetDefaultModel(const std::string &name)
    {
        std::lock_guard<std::mutex> lock(models_mutex_);
        default_model_name_ = name;
    }

    bool LoadSingleModel();  // 原有的单模型加载逻辑（向后兼容），重命名以消除与公有 LoadModel(name,path,backend,device) 的同名重载歧义

    // 最近一次模型加载失败原因：供 HTTP 层（ChatRequestHandler）在返回加载失败的错误响应时
    // 附加可被程序化识别的失败原因字段，与其它加载失败原因区分（如内存不足 vs 其它）。
    enum class LoadFailureReason
    {
        kNone,               // 未记录到特定失败原因（默认值/最近一次加载成功）
        kInsufficientMemory, // 预检查判定内存不足而拒绝加载（目前仅 MnnVerifier 会设置）
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

    // 估算同进程内已加载的其它模型（任意后端）的内存占用总和，供 MnnVerifier 内存预检查
    // 从"可用物理内存"中扣除，降低多模型并发场景下预检查失效的概率。
    // 已知局限：目前只有 MNN 后端有基于权重文件大小的精确估算公式（MNNContext::EstimateMnnMemoryRequirement），
    // 对其它后端（QNN/GGUF）复用同一函数——若其模型目录下没有 .mnn 文件，
    // 至少会计入固定安全余量 kMnnMemoryEstimateMarginBytes，作为对其驻留内存的保守预留。
    uint64_t EstimateOtherLoadedModelsMemoryBytes() const;

    // 多模型存储（线程安全）
    std::unordered_map<std::string, std::shared_ptr<LoadedModel>> loaded_models_;
    mutable std::mutex models_mutex_;
    
    // 默认模型名称（向后兼容）
    std::string default_model_name_;

    // service_config.json 中与 -c config.json 对应模型匹配的启动加载覆盖项
    std::string startup_backend_override_;
    std::string startup_device_override_;
    int startup_context_size_override_{0};
    
    std::atomic<bool> loaded_{false};

    // 最近一次模型加载失败原因（受 models_mutex_ 保护）
    LoadFailureReason last_load_failure_reason_{LoadFailureReason::kNone};
    std::string last_load_failure_detail_;
};

#endif //MODEL_MANAGER_H
