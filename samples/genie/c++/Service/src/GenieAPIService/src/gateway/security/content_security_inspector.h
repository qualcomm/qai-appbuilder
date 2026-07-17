//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef CONTENT_SECURITY_INSPECTOR_H
#define CONTENT_SECURITY_INSPECTOR_H

#include "content_security_types.h"
#include "../../model/model_config.h"
#include <regex>
#include <vector>
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include <functional>
#include <chrono>
#include <atomic>

// ============================================================
// ContentSecurityInspector：基于规则引擎的内容安全检测器
// 纯正则规则检测；可选本地模型辅助判断（use_local_model_fallback=true 时启用）；
// ScanAndCollect 产出 SensitiveSpan 列表；关键词词典热更新
// ============================================================
class ContentSecurityInspector
{
public:
    explicit ContentSecurityInspector(
        const RoutingConfig::SensitivityDetectionConfig &config,
        const RoutingConfig::CacheConfig &cache_config,
        const std::string &policy_id,
        IModelConfig *model_config = nullptr);

    // 主检测入口
    InspectionResult Inspect(const json &request,
                             const InspectionContext &ctx);

    // 获取当前关键词词典规则数量（供审计日志使用）
    int GetKeywordsRulesCount() const;

private:
    // 规则定义
    struct Rule {
        std::string rule_id;
        std::string category;       // "PII" | "FINANCIAL" | "SECRET"
        SensitivityLevel level;     // 命中后的敏感等级
        std::regex pattern;
        bool is_keyword = false;    // true 表示关键词匹配（不区分大小写）
        std::string keyword;        // 关键词（当 is_keyword=true 时使用）
        // 敏感实体类型（用于 ScanAndCollect 产出 SensitiveSpan）
        SensitiveEntityType entity_type = SensitiveEntityType::UNKNOWN;
        // [PERF] 快速预检字符串：若文本中不包含此字符串，直接跳过正则匹配。
        // 选取正则中必然出现的固定子串（如 "@" 对应邮箱，":\\Users\\" 对应 Windows 路径）。
        // 空字符串表示不做预检（始终执行正则）。
        std::string fast_reject_str;
    };

    // 关键词词典条目
    struct KeywordRule {
        std::string rule_id;
        std::string category;
        SensitivityLevel level;
        std::string keyword;        // 原始关键词
        std::string keyword_lower;  // 小写版本（英文关键词用于大小写不敏感匹配）
        bool is_chinese;            // true 表示中文关键词（直接 find）
    };

    // 初始化规则集
    void InitRules();

    // 从文件加载关键词词典
    // 返回 true 表示成功加载（或文件不存在时返回 false 但不报错）
    bool LoadKeywordsDict(const std::string &path);

    // 检查是否需要热更新关键词词典
    void CheckAndReloadKeywordsDict(InspectionResult &result);

    // 扫描单个文本片段，返回最高敏感等级
    SensitivityLevel ScanText(const std::string &text,
                               std::vector<std::string> &categories,
                               std::vector<std::string> &rule_ids);

    // 扫描并收集命中片段（产出 SensitiveSpan 列表）
    // text: 待扫描文本
    // from_tool: 是否来自 role==tool 的消息
    // field_path: JSON 字段路径（如 "/messages/0/content"）
    // spans: 输出的命中片段列表（追加模式）
    // cats: 命中类别列表（追加，去重）
    // ids: 命中规则 ID 列表（追加，去重）
    void ScanAndCollect(const std::string &text,
                        bool from_tool,
                        const std::string &field_path,
                        std::vector<SensitiveSpan> &spans,
                        std::vector<std::string> &cats,
                        std::vector<std::string> &ids);

    // 扫描 tool_calls 的 function.arguments
    SensitivityLevel ScanToolCalls(const json &tool_calls,
                                    std::vector<std::string> &categories,
                                    std::vector<std::string> &rule_ids);

    // 运行规则引擎（对文本执行所有规则）
    SensitivityLevel RunRuleEngine(const std::string &text,
                                   std::vector<std::string> &hit_categories,
                                   std::vector<std::string> &hit_rule_ids);

    // 扫描关键词词典（对文本执行所有词典规则）
    // field_path: 可选，用于日志输出（如 "/messages/2/content"），为空时不输出字段路径
    // out_spans:  可选，collect_spans=true 时传入，命中时追加 SensitiveSpan（KEYWORD 类型）
    SensitivityLevel ScanKeywordsDict(const std::string &text,
                                      std::vector<std::string> &hit_categories,
                                      std::vector<std::string> &hit_rule_ids,
                                      const std::string &field_path = "",
                                      std::vector<SensitiveSpan> *out_spans = nullptr);

    // ---- 本地模型辅助判断 ----

    // 调用本地模型进行辅助敏感性判断
    // text_summary: 待判断的文本摘要（不含原始敏感内容）
    // timeout_ms: 超时时间（毫秒）
    // 返回 true 表示成功，result 填充模型判断结果
    bool LocalModelInspect(const std::string &text_summary,
                            int timeout_ms,
                            SensitivityLevel &result_level,
                            std::string &reason);

    // 提取文本摘要（截断至合理长度，避免过长输入）
    // 使用 token 数限制而非字符数，避免多字节字符导致截断长度失真
    std::string ExtractTextForAuxInspection(const json &request,
                                             std::shared_ptr<ContextBase> handle,
                                             size_t max_input_tokens) const;

    // 计算文本内容哈希（用于 LRU 缓存 key）
    std::string ComputeCacheKey(const std::string &text) const;

    // LRU 缓存：内容哈希 → 模型判断结果
    struct CacheEntry {
        SensitivityLevel level;
        std::string reason;
        std::chrono::steady_clock::time_point insert_time; // 用于 TTL 过期判断
    };

    // LRU 缓存操作（线程安全）
    bool CacheLookup(const std::string &key, CacheEntry &entry);
    void CacheInsert(const std::string &key, const CacheEntry &entry);

    // 辅助：合并两个敏感等级，取较高者
    static SensitivityLevel MaxLevel(SensitivityLevel a, SensitivityLevel b);

    // 辅助：向 vector 中添加不重复元素
    static void AddUnique(std::vector<std::string> &vec, const std::string &val);

    // 辅助：判断字段路径对应的消息是否为 tool 角色
    static bool IsToolOutputField(const json &request, const std::string &field_path);

    // 白名单机制：检查匹配到的值是否是脱敏占位符（FormatMockData 生成的 mock 数据）
    // 若是，则跳过该匹配，避免脱敏循环（max_rounds 超限）
    // matched: 规则匹配到的原始字符串
    // entity_type: 规则对应的敏感实体类型
    // 返回 true 表示该匹配是脱敏占位符，应跳过
    static bool IsDesensitizedPlaceholder(const std::string &matched,
                                          SensitiveEntityType entity_type);

    RoutingConfig::SensitivityDetectionConfig config_;
    IModelConfig *model_config_;  // 可为 nullptr（未启用本地模型辅助判断时）
    std::vector<Rule> rules_;

    // LRU 缓存
    // 从 CacheConfig 读取容量和 TTL
    size_t cache_max_entries_;
    int cache_ttl_seconds_;
    std::string policy_id_;

    mutable std::mutex cache_mutex_;
    std::list<std::pair<std::string, CacheEntry>> cache_list_;
    std::unordered_map<std::string, std::list<std::pair<std::string, CacheEntry>>::iterator> cache_map_;

    // 缓存 Miss 告警
    mutable std::atomic<int> consecutive_miss_count_{0};
    mutable std::atomic<int> total_request_count_{0};
    static constexpr int CACHE_MISS_WARNING_THRESHOLD = 10;

    // 关键词词典（热更新）
    mutable std::mutex keywords_mutex_;
    std::vector<KeywordRule> keywords_rules_;
    std::string keywords_dict_path_;
    int keywords_reload_interval_seconds_;
    mutable std::chrono::steady_clock::time_point last_keywords_check_time_;
    mutable std::chrono::system_clock::time_point last_keywords_mtime_;
    mutable bool keywords_dict_loaded_ = false;
};

#endif // CONTENT_SECURITY_INSPECTOR_H
