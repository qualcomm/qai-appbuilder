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

class ContentSecurityInspector
{
public:
    explicit ContentSecurityInspector(
        const RoutingConfig::SensitivityDetectionConfig &config,
        const RoutingConfig::CacheConfig &cache_config,
        const std::string &policy_id,
        IModelConfig *model_config = nullptr);

    InspectionResult Inspect(const json &request,
                             const InspectionContext &ctx);

    int GetKeywordsRulesCount() const;

private:

    struct Rule {
        std::string rule_id;
        std::string category;
        SensitivityLevel level;
        std::regex pattern;
        bool is_keyword = false;
        std::string keyword;

        SensitiveEntityType entity_type = SensitiveEntityType::UNKNOWN;

        std::string fast_reject_str;
    };

    struct KeywordRule {
        std::string rule_id;
        std::string category;
        SensitivityLevel level;
        std::string keyword;
        std::string keyword_lower;
        bool is_chinese;
    };

    void InitRules();

    bool LoadKeywordsDict(const std::string &path);

    void CheckAndReloadKeywordsDict(InspectionResult &result);

    SensitivityLevel ScanText(const std::string &text,
                               std::vector<std::string> &categories,
                               std::vector<std::string> &rule_ids);

    void ScanAndCollect(const std::string &text,
                        bool from_tool,
                        const std::string &field_path,
                        std::vector<SensitiveSpan> &spans,
                        std::vector<std::string> &cats,
                        std::vector<std::string> &ids);

    SensitivityLevel ScanToolCalls(const json &tool_calls,
                                    std::vector<std::string> &categories,
                                    std::vector<std::string> &rule_ids);

    SensitivityLevel RunRuleEngine(const std::string &text,
                                   std::vector<std::string> &hit_categories,
                                   std::vector<std::string> &hit_rule_ids);

    SensitivityLevel ScanKeywordsDict(const std::string &text,
                                      std::vector<std::string> &hit_categories,
                                      std::vector<std::string> &hit_rule_ids,
                                      const std::string &field_path = "",
                                      std::vector<SensitiveSpan> *out_spans = nullptr);

    bool LocalModelInspect(const std::string &text_summary,
                            int timeout_ms,
                            SensitivityLevel &result_level,
                            std::string &reason);

    std::string ExtractTextForAuxInspection(const json &request,
                                             std::shared_ptr<ContextBase> handle,
                                             size_t max_input_tokens) const;

    std::string ComputeCacheKey(const std::string &text) const;

    struct CacheEntry {
        SensitivityLevel level;
        std::string reason;
        std::chrono::steady_clock::time_point insert_time;
    };

    bool CacheLookup(const std::string &key, CacheEntry &entry);
    void CacheInsert(const std::string &key, const CacheEntry &entry);

    static SensitivityLevel MaxLevel(SensitivityLevel a, SensitivityLevel b);

    static void AddUnique(std::vector<std::string> &vec, const std::string &val);

    static bool IsToolOutputField(const json &request, const std::string &field_path);

    static bool IsDesensitizedPlaceholder(const std::string &matched,
                                          SensitiveEntityType entity_type);

    RoutingConfig::SensitivityDetectionConfig config_;
    IModelConfig *model_config_;
    std::vector<Rule> rules_;

    size_t cache_max_entries_;
    int cache_ttl_seconds_;
    std::string policy_id_;

    mutable std::mutex cache_mutex_;
    std::list<std::pair<std::string, CacheEntry>> cache_list_;
    std::unordered_map<std::string, std::list<std::pair<std::string, CacheEntry>>::iterator> cache_map_;

    mutable std::atomic<int> consecutive_miss_count_{0};
    mutable std::atomic<int> total_request_count_{0};
    static constexpr int CACHE_MISS_WARNING_THRESHOLD = 10;

    mutable std::mutex keywords_mutex_;
    std::vector<KeywordRule> keywords_rules_;
    std::string keywords_dict_path_;
    int keywords_reload_interval_seconds_;
    mutable std::chrono::steady_clock::time_point last_keywords_check_time_;
    mutable std::chrono::system_clock::time_point last_keywords_mtime_;
    mutable bool keywords_dict_loaded_ = false;
};

#endif
