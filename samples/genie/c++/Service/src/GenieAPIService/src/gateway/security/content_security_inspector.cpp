//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "content_security_inspector.h"
#include "../../context/context_base.h"
#include "security_prompt_builder.h"
#include "security_utils.h"
#include "log.h"
#include "../../../../common/utils.h"
#include <algorithm>
#include <sstream>
#include <future>
#include <chrono>
#include <functional>
#include <thread>
#include <atomic>
#include <fstream>
#include <sys/stat.h>

static bool LuhnCheck(const std::string &digits)
{
    if (digits.empty())
        return false;

    int sum = 0;
    bool double_it = false;

    for (int i = static_cast<int>(digits.size()) - 1; i >= 0; --i)
    {
        if (!std::isdigit(static_cast<unsigned char>(digits[i])))
            return false;

        int digit = digits[i] - '0';

        if (double_it)
        {
            digit *= 2;
            if (digit > 9)
                digit -= 9;
        }

        sum += digit;
        double_it = !double_it;
    }

    return (sum % 10) == 0;
}

static SensitivityLevel ParseSensitivityLevel(const std::string &level_str, SensitivityLevel default_level)
{
    if (level_str == "S0") return SensitivityLevel::S0;
    if (level_str == "S1") return SensitivityLevel::S1;
    if (level_str == "S2") return SensitivityLevel::S2;
    My_Log{My_Log::Level::kError}
        << "[ContentSecurityInspector] Invalid sensitivity level string: \"" << level_str
        << "\", using default: " << to_string(default_level) << std::endl;
    return default_level;
}

static bool MatchPassesLuhn(const std::string &matched_str)
{
    std::string digits;
    digits.reserve(matched_str.size());
    for (char c : matched_str)
    {
        if (std::isdigit(static_cast<unsigned char>(c)))
            digits += c;
    }

    if (digits.size() < 13 || digits.size() > 19)
        return false;
    return LuhnCheck(digits);
}

ContentSecurityInspector::ContentSecurityInspector(
        const RoutingConfig::SensitivityDetectionConfig &config,
        const RoutingConfig::CacheConfig &cache_config,
        const std::string &policy_id,
        IModelConfig *model_config)
        : config_(config), model_config_(model_config),
          cache_max_entries_(cache_config.max_entries),
          cache_ttl_seconds_(cache_config.ttl_seconds),
          policy_id_(policy_id),
          keywords_dict_path_(config.keywords_dict_path),
          keywords_reload_interval_seconds_(config.keywords_reload_interval_seconds),
          last_keywords_check_time_(std::chrono::steady_clock::now()),
          last_keywords_mtime_{}
{
    InitRules();

    if (!keywords_dict_path_.empty()) {
        LoadKeywordsDict(keywords_dict_path_);
    }
}

void ContentSecurityInspector::InitRules()
{
    rules_.clear();

    if (config_.detection_rules.enable_phone) {
        rules_.push_back({
            "R_PHONE_CN", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_phone, SensitivityLevel::S1),
            std::regex(R"(\b1[3-9]\d{9}\b)"), false, "",
            SensitiveEntityType::PHONE, ""
        });
    }

    if (config_.detection_rules.enable_id_card) {
        rules_.push_back({
            "R_ID_CARD", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_id_card, SensitivityLevel::S2),
            std::regex(R"(\b\d{17}[\dXx]\b)"), false, "",
            SensitiveEntityType::IDCARD, ""
        });
    }

    if (config_.detection_rules.enable_email) {
        rules_.push_back({
            "R_EMAIL", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_email, SensitivityLevel::S1),
            std::regex(R"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"), false, "",
            SensitiveEntityType::EMAIL, "@"
        });
    }

    if (config_.detection_rules.enable_bank_card) {
        const SensitivityLevel bank_card_level =
            ParseSensitivityLevel(config_.detection_rules.level_bank_card, SensitivityLevel::S2);
        rules_.push_back({
            "R_BANK_CARD_KW", "FINANCIAL", bank_card_level,
            std::regex(
                R"((?:(?:卡号|银行卡|信用卡|借记卡|card\s*(?:number|no)|bank\s*account|account\s*(?:number|no))\s*[：:＝=]?\s*)"
                R"(\d[\d\s\-]{14,21}\d))",
                std::regex::icase
            ), false, "",
            SensitiveEntityType::BANKCARD, ""
        });
        rules_.push_back({
            "R_BANK_CARD_FMT", "FINANCIAL", bank_card_level,
            std::regex(R"(\b\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}(?:[\s\-]\d{1,3})?\b)"), false, "",
            SensitiveEntityType::BANKCARD, ""
        });
    }

    if (config_.detection_rules.enable_api_key) {
        rules_.push_back({
            "R_API_KEY", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_api_key, SensitivityLevel::S2),
            std::regex(R"(sk-[a-zA-Z0-9]{20,})"), false, "",
            SensitiveEntityType::API_KEY, "sk-"
        });
    }

    if (config_.detection_rules.enable_private_key) {
        rules_.push_back({
            "R_PRIVATE_KEY", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_private_key, SensitivityLevel::S2),
            std::regex(R"(-----BEGIN\s+\S*\s*PRIVATE\s+KEY-----)"), false, "",
            SensitiveEntityType::PRIVATE_KEY, "-----BEGIN"
        });
    }

    if (config_.detection_rules.enable_token) {
        rules_.push_back({
            "R_TOKEN", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_token, SensitivityLevel::S2),
            std::regex(R"(Bearer\s+[a-zA-Z0-9._\-]{20,})"), false, "",
            SensitiveEntityType::TOKEN, "Bearer "
        });
    }

    if (config_.detection_rules.enable_password) {
        rules_.push_back({
            "R_PASSWORD_KW", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_password, SensitivityLevel::S1),
            std::regex(R"((?:password|passwd|secret)\s*=\s*\S+)", std::regex::icase), false, "",
            SensitiveEntityType::PASSWORD_KW, "="
        });
    }

    if (config_.extended_rules.enable_local_path) {
        rules_.push_back({
            "R_PATH_UNIX", "PII", SensitivityLevel::S1,
            std::regex(R"((?:/home/|/root/|/Users/|/var/private/)[^\s<]+)"),
            false, "", SensitiveEntityType::PATH, "/home/"
        });
        rules_.push_back({
            "R_PATH_WIN", "PII", SensitivityLevel::S1,
            std::regex(R"([A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][^\s<]+)"),
            false, "", SensitiveEntityType::PATH, "Users"
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: local_path (R_PATH_UNIX, R_PATH_WIN)" << std::endl;
    }

    if (config_.extended_rules.enable_internal_url) {
        rules_.push_back({
            "R_INTERNAL_IP", "PII", SensitivityLevel::S1,
            std::regex(
                R"(\b(?:192\.168\.\d{1,3}\.\d{1,3}|)"
                R"(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|)"
                R"(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b)"
            ), false, "", SensitiveEntityType::INTERNAL_URL, "."
        });
        rules_.push_back({
            "R_INTERNAL_DOMAIN", "PII", SensitivityLevel::S1,
            std::regex(R"(\b[^\s/\\.]+\.(?:internal|local|corp|intranet|lan)\b)",
                       std::regex::icase),
            false, "", SensitiveEntityType::INTERNAL_URL, ".local"
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: internal_url (R_INTERNAL_IP, R_INTERNAL_DOMAIN)" << std::endl;
    }

    if (config_.extended_rules.enable_device_id) {
        rules_.push_back({
            "R_MAC_ADDR", "PII", SensitivityLevel::S1,
            std::regex(R"(\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b)"),
            false, "", SensitiveEntityType::DEVICE_ID, ":"
        });
        rules_.push_back({
            "R_IMEI", "PII", SensitivityLevel::S1,
            std::regex(R"((?:IMEI|imei|设备号)\s*[：:=]?\s*\d{15})",
                       std::regex::icase),
            false, "", SensitiveEntityType::DEVICE_ID, "IMEI"
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: device_id (R_MAC_ADDR, R_IMEI)" << std::endl;
    }

    if (config_.extended_rules.enable_image_data) {
        rules_.push_back({
            "R_BASE64_IMAGE", "PII", SensitivityLevel::S1,
            std::regex(R"(data:image/(?:jpeg|jpg|png|gif|webp|bmp|svg\+xml);base64,)",
                       std::regex::icase),
            false, "", SensitiveEntityType::UNKNOWN, "data:image/"
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: image_data (R_BASE64_IMAGE)" << std::endl;
    }

    for (auto& rule : rules_)
    {
        auto it = config_.rule_level_overrides.find(rule.rule_id);
        if (it != config_.rule_level_overrides.end())
        {
            SensitivityLevel original_level = rule.level;
            if (it->second == "S1") rule.level = SensitivityLevel::S1;
            else if (it->second == "S2") rule.level = SensitivityLevel::S2;
            else {
                My_Log{My_Log::Level::kError}
                    << "[ContentSecurityInspector] Invalid rule_level_override value (only S1/S2 allowed) for "
                    << rule.rule_id << ": " << it->second << " (ignored)" << std::endl;
                continue;
            }
            My_Log{} << "[ContentSecurityInspector] rule_level_override applied (auditable): "
                     << rule.rule_id << " "
                     << to_string(original_level) << " -> " << to_string(rule.level) << std::endl;
        }
    }
}

SensitivityLevel ContentSecurityInspector::MaxLevel(SensitivityLevel a, SensitivityLevel b)
{
    return static_cast<int>(a) > static_cast<int>(b) ? a : b;
}

void ContentSecurityInspector::AddUnique(std::vector<std::string> &vec, const std::string &val)
{
    if (std::find(vec.begin(), vec.end(), val) == vec.end())
    {
        vec.push_back(val);
    }
}

bool ContentSecurityInspector::IsDesensitizedPlaceholder(
        const std::string &matched,
        SensitiveEntityType entity_type)
{
    switch (entity_type) {
        case SensitiveEntityType::EMAIL:

            {
                static const std::regex mock_email_re(R"(^mock_user_\d+@example\.com$)");
                return std::regex_match(matched, mock_email_re);
            }

        case SensitiveEntityType::PHONE:

            {
                static const std::regex mock_phone_re(R"(^1389\d{7}$)");
                return std::regex_match(matched, mock_phone_re);
            }

        case SensitiveEntityType::IDCARD:

            return false;

        case SensitiveEntityType::BANKCARD:

            return false;

        case SensitiveEntityType::API_KEY:

            {
                static const std::regex mock_api_key_re(R"(^sk-mock_\d+_[a-zA-Z0-9]{32}$)");
                return std::regex_match(matched, mock_api_key_re);
            }

        case SensitiveEntityType::TOKEN:

            {
                static const std::regex mock_token_re(R"(^Bearer\s+mock_token_\d+_[a-zA-Z0-9]{32}$)");
                return std::regex_match(matched, mock_token_re);
            }

        case SensitiveEntityType::PRIVATE_KEY:

            return false;

        case SensitiveEntityType::INTERNAL_URL:

            return false;

        case SensitiveEntityType::PATH:

            {
                static const std::regex mock_win_path_re(
                    R"(^[A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\]mock_user_\d+(?:[/\\].*)?)");
                static const std::regex mock_unix_path_re(
                    R"(^/(?:home|Users)/mock_user_\d+(?:/.*)?)");
                return std::regex_match(matched, mock_win_path_re) ||
                       std::regex_match(matched, mock_unix_path_re);
            }

        case SensitiveEntityType::DEVICE_ID:

            {
                static const std::regex mock_mac_re(R"(^00:11:22:33:44:[0-9a-f]{2}$)");
                return std::regex_match(matched, mock_mac_re);
            }

        default:
            return false;
    }
}

SensitivityLevel ContentSecurityInspector::RunRuleEngine(
        const std::string &text,
        std::vector<std::string> &hit_categories,
        std::vector<std::string> &hit_rule_ids)
{
    SensitivityLevel max_level = SensitivityLevel::S0;

    for (const auto &rule : rules_)
    {
        try
        {

            if (!rule.fast_reject_str.empty() &&
                text.find(rule.fast_reject_str) == std::string::npos) {
                continue;
            }

            bool is_bank_card_rule = (rule.rule_id == "R_BANK_CARD_KW" ||
                                      rule.rule_id == "R_BANK_CARD_FMT");

            if (is_bank_card_rule)
            {

                std::sregex_iterator it(text.begin(), text.end(), rule.pattern);
                std::sregex_iterator end;
                bool luhn_passed = false;
                for (; it != end; ++it)
                {
                    const std::string &matched = (*it)[0].str();
                    if (MatchPassesLuhn(matched))
                    {
                        luhn_passed = true;
                        break;
                    }
                }
                if (luhn_passed)
                {
                    max_level = MaxLevel(max_level, rule.level);
                    AddUnique(hit_categories, rule.category);
                    AddUnique(hit_rule_ids, rule.rule_id);
                }
            }
            else
            {

                if (!config_.debug_log_matches) {

                    std::smatch m;
                    std::string::const_iterator search_start = text.cbegin();
                    bool any_real_match = false;
                    while (std::regex_search(search_start, text.cend(), m, rule.pattern)) {
                        const std::string &raw = m.str();
                        if (!IsDesensitizedPlaceholder(raw, rule.entity_type)) {
                            any_real_match = true;
                            break;
                        }

                        search_start = m.suffix().first;
                        if (search_start == text.cend()) break;
                    }
                    if (any_real_match) {
                        max_level = MaxLevel(max_level, rule.level);
                        AddUnique(hit_categories, rule.category);
                        AddUnique(hit_rule_ids, rule.rule_id);
                    }
                } else {

                    bool any_real_match = false;
                    std::sregex_iterator it(text.begin(), text.end(), rule.pattern);
                    std::sregex_iterator end_it;
                    for (; it != end_it; ++it) {
                        const auto &m = *it;
                        const std::string &raw = m.str();
                        if (IsDesensitizedPlaceholder(raw, rule.entity_type)) continue;
                        any_real_match = true;
                        size_t pos = static_cast<size_t>(m.position());
                        size_t ctx_before_start = utf8_align_start(text, (pos >= 30) ? pos - 30 : 0);
                        size_t ctx_after_end = std::min(pos + raw.size() + 30, text.size());
                        std::string ctx_before = text.substr(ctx_before_start, pos - ctx_before_start);
                        std::string ctx_after = safe_utf8_truncate(
                            text.substr(pos + raw.size(), ctx_after_end - (pos + raw.size())),
                            ctx_after_end - (pos + raw.size()), "");
                        for (auto &c : ctx_before) if (c == '\n' || c == '\r') c = ' ';
                        for (auto &c : ctx_after) if (c == '\n' || c == '\r') c = ' ';
                        My_Log{My_Log::Level::kInfo} << "[ContentSecurityInspector] Rule=" << rule.rule_id
                                 << " matched=" << raw
                                 << " context=..." << ctx_before << "[" << raw << "]" << ctx_after << "..."
                                 << std::endl;
                    }
                    if (any_real_match) {
                        max_level = MaxLevel(max_level, rule.level);
                        AddUnique(hit_categories, rule.category);
                        AddUnique(hit_rule_ids, rule.rule_id);
                    }
                }
            }
        }
        catch (const std::regex_error &e)
        {
            My_Log{My_Log::Level::kError} << "Regex error in rule " << rule.rule_id
                                          << ": " << e.what() << std::endl;
        }
    }

    return max_level;
}

SensitivityLevel ContentSecurityInspector::ScanText(
        const std::string &text,
        std::vector<std::string> &categories,
        std::vector<std::string> &rule_ids)
{
    if (text.empty())
        return SensitivityLevel::S0;

    if (text.find('\n') != std::string::npos) {
        SensitivityLevel max_level = SensitivityLevel::S0;
        std::string::size_type start = 0;
        while (start <= text.size()) {
            std::string::size_type end = text.find('\n', start);
            if (end == std::string::npos) end = text.size();
            if (end > start) {

                std::string::size_type line_end = end;
                if (line_end > start && text[line_end - 1] == '\r') --line_end;
                if (line_end > start) {
                    std::string line = text.substr(start, line_end - start);
                    std::vector<std::string> line_cats, line_ids;
                    SensitivityLevel line_level = RunRuleEngine(line, line_cats, line_ids);
                    max_level = MaxLevel(max_level, line_level);
                    for (const auto& c : line_cats) AddUnique(categories, c);
                    for (const auto& id : line_ids) AddUnique(rule_ids, id);

                    if (max_level == SensitivityLevel::S2) break;
                }
            }
            if (end == text.size()) break;
            start = end + 1;
        }
        return max_level;
    }

    return RunRuleEngine(text, categories, rule_ids);
}

SensitivityLevel ContentSecurityInspector::ScanToolCalls(
        const json &tool_calls,
        std::vector<std::string> &categories,
        std::vector<std::string> &rule_ids)
{
    SensitivityLevel max_level = SensitivityLevel::S0;

    if (!tool_calls.is_array())
        return max_level;

    for (const auto &tc : tool_calls)
    {

        if (tc.contains("function"))
        {
            const auto &func = tc["function"];
            if (func.contains("arguments"))
            {
                std::string args;
                if (func["arguments"].is_string())
                    args = func["arguments"].get<std::string>();
                else
                    args = func["arguments"].dump();

                max_level = MaxLevel(max_level, ScanText(args, categories, rule_ids));
            }

            if (func.contains("name") && func["name"].is_string())
            {
                max_level = MaxLevel(max_level,
                    ScanText(func["name"].get<std::string>(), categories, rule_ids));
            }
        }
    }

    return max_level;
}

std::string ContentSecurityInspector::ExtractTextForAuxInspection(
        const json &request,
        std::shared_ptr<ContextBase> handle,
        size_t max_input_tokens) const
{
    const json& messages = request.contains("messages") ? request["messages"] : json::array();
    if (!messages.is_array() || messages.empty()) return "";

    auto extract_msg_text = [](const json& msg) -> std::string {
        if (!msg.contains("content")) return "";
        const auto& content = msg["content"];
        std::string text;
        if (content.is_string()) {
            text = content.get<std::string>();
        } else if (content.is_array()) {
            for (const auto& part : content) {
                if (part.contains("type") && part["type"] == "text" &&
                    part.contains("text") && part["text"].is_string()) {
                    text += part["text"].get<std::string>() + " ";
                }
            }
        } else {
            return "";
        }

        return SecurityUtils::CleanMessageText(text);
    };

    if (!handle || max_input_tokens == 0) {
        size_t max_chars = static_cast<size_t>(config_.model_input_max_chars);
        std::vector<std::string> segments;
        size_t total = 0;

        for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i) {
            const auto& msg = messages[i];
            std::string role = msg.value("role", "");
            if (role == "system") continue;

            std::string text = extract_msg_text(msg);
            if (text.empty()) continue;

            size_t remaining = max_chars - total;
            if (text.size() > remaining) {
                text = safe_utf8_truncate(text, remaining, "");
            }
            segments.push_back(text);
            total += text.size();
            if (total >= max_chars) break;
        }

        if (segments.empty()) return "";
        std::reverse(segments.begin(), segments.end());

        std::string full_text;
        full_text.reserve(total + segments.size());
        for (const auto& s : segments) { full_text += s; full_text += ' '; }

        My_Log{} << "[ContentSecurityInspector] ExtractTextForAuxInspection (char-based fallback): "
                 << "chars=" << full_text.size() << ", max_chars=" << max_chars << std::endl;
        return full_text;
    }

    auto truncate_to_tokens = [&handle](const std::string& text, size_t max_tokens)
            -> std::pair<std::string, size_t> {
        size_t tokens = handle->TokenLength(text);
        if (tokens <= max_tokens) return {text, tokens};

        size_t estimated_chars = text.size() * max_tokens / tokens;
        std::string truncated = safe_utf8_truncate(text, estimated_chars, "");

        for (int iter = 0; iter < 3; ++iter) {
            tokens = handle->TokenLength(truncated);
            if (tokens <= max_tokens) break;

            size_t remove = std::max(size_t(1), truncated.size() / 10);
            truncated = safe_utf8_truncate(truncated, truncated.size() - remove, "");
        }

        tokens = handle->TokenLength(truncated);
        return {truncated, tokens};
    };

    std::vector<std::string> segments;
    size_t total_tokens = 0;

    for (int i = static_cast<int>(messages.size()) - 1; i >= 0; --i) {
        const auto& msg = messages[i];

        std::string role = msg.value("role", "");
        if (role == "system") continue;

        std::string text = extract_msg_text(msg);
        if (text.empty()) continue;

        size_t remaining_tokens = max_input_tokens - total_tokens;
        if (remaining_tokens == 0) break;

        size_t text_tokens = handle->TokenLength(text);

        if (text_tokens > remaining_tokens) {

            auto [truncated, actual_tokens] = truncate_to_tokens(text, remaining_tokens);
            if (!truncated.empty()) {
                segments.push_back(truncated);
                total_tokens += actual_tokens;
            }
            break;
        }

        segments.push_back(text);
        total_tokens += text_tokens;

        if (total_tokens >= max_input_tokens) break;
    }

    if (segments.empty()) return "";

    std::reverse(segments.begin(), segments.end());

    std::string full_text;
    for (const auto& s : segments) {
        full_text += s;
        full_text += ' ';
    }

    My_Log{} << "[ContentSecurityInspector] ExtractTextForAuxInspection (token-based): "
             << "segments=" << segments.size()
             << ", chars=" << full_text.size()
             << ", tokens=" << total_tokens
             << ", max_tokens=" << max_input_tokens
             << std::endl;

    return full_text;
}

std::string ContentSecurityInspector::ComputeCacheKey(const std::string &text) const
{
#ifdef HAVE_OPENSSL
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256(reinterpret_cast<const unsigned char*>(text.data()), text.size(), digest);
    char hex[SHA256_DIGEST_LENGTH * 2 + 1];
    for (int i = 0; i < SHA256_DIGEST_LENGTH; ++i)
        snprintf(hex + i * 2, 3, "%02x", digest[i]);
    return std::string(hex) + ":" + policy_id_;
#else

    uint64_t h = SecurityUtils::FNV1aHash(text);
    char buf[128];
    snprintf(buf, sizeof(buf), "%016llx:%s", static_cast<unsigned long long>(h), policy_id_.c_str());
    return std::string(buf);
#endif
}

bool ContentSecurityInspector::CacheLookup(const std::string &key, CacheEntry &entry)
{
    std::lock_guard<std::mutex> lock(cache_mutex_);
    auto it = cache_map_.find(key);
    if (it == cache_map_.end()) return false;

    auto now = std::chrono::steady_clock::now();
    auto age_seconds = std::chrono::duration_cast<std::chrono::seconds>(
        now - it->second->second.insert_time).count();
    if (age_seconds > cache_ttl_seconds_) {

        cache_list_.erase(it->second);
        cache_map_.erase(it);
        return false;
    }

    cache_list_.splice(cache_list_.begin(), cache_list_, it->second);
    entry = it->second->second;
    return true;
}

void ContentSecurityInspector::CacheInsert(const std::string &key, const CacheEntry &entry_in)
{
    std::lock_guard<std::mutex> lock(cache_mutex_);

    auto it = cache_map_.find(key);
    if (it != cache_map_.end())
    {
        cache_list_.erase(it->second);
        cache_map_.erase(it);
    }

    CacheEntry entry_with_time = entry_in;
    entry_with_time.insert_time = std::chrono::steady_clock::now();

    cache_list_.push_front({key, entry_with_time});
    cache_map_[key] = cache_list_.begin();

    while (cache_list_.size() > cache_max_entries_) {
        const std::string &lru_key = cache_list_.back().first;
        cache_map_.erase(lru_key);
        cache_list_.pop_back();
    }
}

bool ContentSecurityInspector::LocalModelInspect(
        const std::string &text_summary,
        int timeout_ms,
        SensitivityLevel &result_level,
        std::string &reason)
{
    if (!model_config_)
    {
        reason = "model_config not available";
        return false;
    }

    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle)
    {
        reason = "default model handle not available";
        return false;
    }

    if (handle->is_query_busy()) {
        My_Log{My_Log::Level::kWarning}
            << "[ContentSecurityInspector] WARNING: model query_mutex_ is LOCKED "
            << "(another Query() is in progress). Security check will block until it completes. "
            << "This may cause LocalModelInspect timeout (" << timeout_ms << "ms)." << std::endl;
    } else {
        My_Log{} << "[ContentSecurityInspector] model query_mutex_ is free (no lock contention)" << std::endl;
    }

    const auto *default_instance_config = model_config_->GetDefaultInstanceConfig();
    const PromptType prompt_type = default_instance_config
            ? default_instance_config->get_prompt_type()
            : model_config_->get_prompt_type();
    const bool is_harmony = (prompt_type == PromptType::Harmony);
    const std::string json_prefill = "{\"sensitivity\":\"";
    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text: " + text_summary,
                                               json_prefill);
    My_Log{} << "[ContentSecurityInspector] prompt format: "
             << (is_harmony ? "Harmony" : "General")
             << " for security check" << std::endl;

    size_t prompt_chars = prompt.size();
    size_t prompt_tokens = handle->TokenLength(prompt);
    My_Log{} << "[ContentSecurityInspector] LocalModelInspect: "
             << "prompt_chars=" << prompt_chars
             << ", prompt_tokens=" << prompt_tokens
             << ", timeout_ms=" << timeout_ms
             << std::endl;
    if (prompt_tokens > static_cast<size_t>(timeout_ms / 5)) {

        My_Log{My_Log::Level::kWarning}
            << "[ContentSecurityInspector] WARNING: prompt_tokens=" << prompt_tokens
            << " may exceed timeout_ms=" << timeout_ms
            << " (estimated processing time > " << (prompt_tokens / 5) << "ms). "
            << "Consider reducing max_input_tokens." << std::endl;
    }

    const int kSecurityCheckMaxTokens = config_.max_gen_tokens > 0 ? config_.max_gen_tokens : 2048;

    auto query_result = SecurityUtils::LocalModelQuery(
            handle, prompt, json_prefill, timeout_ms, kSecurityCheckMaxTokens,
            [](std::string &output_buf, const std::string &token, bool &json_complete) -> bool {
                (void)token;

                size_t search_from = 0;
                while (true) {
                    size_t open_brace = output_buf.find('{', search_from);
                    if (open_brace == std::string::npos) {
                        break;
                    }
                    size_t close_brace = output_buf.find('}', open_brace + 1);
                    while (close_brace != std::string::npos) {
                        std::string candidate = output_buf.substr(open_brace, close_brace - open_brace + 1);
                        bool is_valid_json = false;
                        try {
                            auto j = nlohmann::json::parse(candidate);
                            if (j.contains("sensitivity") && j["sensitivity"].is_string()) {
                                const std::string &sv = j["sensitivity"].get<std::string>();
                                is_valid_json = (sv == "S0" || sv == "S1" || sv == "S2");
                            }
                        } catch (...) {
                        }
                        if (is_valid_json) {
                            json_complete = true;
                            My_Log{} << "[ContentSecurityInspector] [STREAM] JSON complete. Early stop. buf=\""
                                     << output_buf << "\"\n";
                            return false;
                        }
                        close_brace = output_buf.find('}', close_brace + 1);
                    }
                    search_from = open_brace + 1;
                }

                std::string completed_json, matched_value;
                if (SecurityUtils::CheckPrefixEarlyStop(output_buf, "{\"sensitivity\":\"", {"S0", "S1", "S2"}, completed_json, matched_value)) {
                    json_complete = true;
                    output_buf = completed_json;
                    My_Log{} << "[ContentSecurityInspector] [STREAM] Prefix match early stop: "
                             << "level=" << matched_value << ", completed_json=" << completed_json << "\n";
                    return false;
                }

                return true;
            },
            "[ContentSecurityInspector]");

    if (!query_result.success || query_result.output.empty())
    {
        reason = "local model query failed or timed out, conservative fallback";
        return false;
    }

    const std::string &model_output = query_result.output;
    try
    {
        std::string json_str;
        bool found_valid_json = false;

        size_t search_from = 0;
        while (!found_valid_json) {
            size_t json_start = model_output.find('{', search_from);
            if (json_start == std::string::npos) {
                break;
            }

            size_t json_end = model_output.find('}', json_start + 1);
            while (json_end != std::string::npos) {
                std::string candidate = model_output.substr(json_start, json_end - json_start + 1);
                try {
                    json parsed_candidate = json::parse(candidate);
                    if (parsed_candidate.contains("sensitivity") && parsed_candidate["sensitivity"].is_string()) {
                        std::string sensitivity_candidate = parsed_candidate["sensitivity"].get<std::string>();
                        if (sensitivity_candidate == "S0" || sensitivity_candidate == "S1" || sensitivity_candidate == "S2") {
                            json_str = candidate;
                            found_valid_json = true;
                            break;
                        }
                    }
                } catch (...) {
                }
                json_end = model_output.find('}', json_end + 1);
            }

            search_from = json_start + 1;
        }

        if (!found_valid_json)
        {
            reason = "model output does not contain valid sensitivity JSON: " + model_output.substr(0, 100);
            return false;
        }

        json parsed = json::parse(json_str);

        if (!parsed.contains("sensitivity") || !parsed["sensitivity"].is_string())
        {
            reason = "model output missing 'sensitivity' field";
            return false;
        }

        std::string sensitivity_str = parsed["sensitivity"].get<std::string>();
        if (sensitivity_str == "S0")
            result_level = SensitivityLevel::S0;
        else if (sensitivity_str == "S1")
            result_level = SensitivityLevel::S1;
        else if (sensitivity_str == "S2")
            result_level = SensitivityLevel::S2;
        else
        {
            reason = "unknown sensitivity value: " + sensitivity_str;
            return false;
        }

        if (parsed.contains("reason") && parsed["reason"].is_string())
            reason = parsed["reason"].get<std::string>();
        else
            reason = "model classified as " + sensitivity_str;

        return true;
    }
    catch (const json::exception &e)
    {
        reason = "failed to parse model output JSON: " + std::string(e.what());
        return false;
    }
}

static void WalkJsonStringFieldsReadOnly(const nlohmann::json& node,
                                          const std::function<void(const std::string&)>& visitor) {
    if (node.is_string()) {
        visitor(node.get<std::string>());
    } else if (node.is_object()) {
        for (auto& [key, child] : node.items()) WalkJsonStringFieldsReadOnly(child, visitor);
    } else if (node.is_array()) {
        for (auto& elem : node) WalkJsonStringFieldsReadOnly(elem, visitor);
    }
}

static void WalkJsonStringFieldsReadOnlyWithPath(
        const nlohmann::json& node,
        const std::string& current_path,
        const std::function<void(const std::string&, const std::string&)>& visitor) {
    if (node.is_string()) {
        visitor(node.get<std::string>(), current_path);
    } else if (node.is_object()) {
        for (auto& [key, child] : node.items())
            WalkJsonStringFieldsReadOnlyWithPath(child, current_path + "/" + key, visitor);
    } else if (node.is_array()) {
        for (size_t i = 0; i < node.size(); ++i)
            WalkJsonStringFieldsReadOnlyWithPath(node[i], current_path + "/" + std::to_string(i), visitor);
    }
}

InspectionResult ContentSecurityInspector::Inspect(
        const json &request,
        const InspectionContext &ctx)
{
    InspectionResult result;
    result.sensitivity_level = SensitivityLevel::S0;
    result.rule_engine_failed = false;

    CheckAndReloadKeywordsDict(result);

    SensitivityLevel max_level = SensitivityLevel::S0;

    try
    {
        if (ctx.collect_spans) {

            WalkJsonStringFieldsReadOnlyWithPath(request, "",
                [&](const std::string& text, const std::string& path) {

                    const std::string scan_text_truncated_ = (text.size() > 32768) ? safe_utf8_truncate(text, 32768, "") : text;
                    const std::string& scan_text_ref = (text.size() > 32768) ? scan_text_truncated_ : text;
                    if (text.size() > 32768) {
                        result.oversized_field_warning = true;
                        result.oversized_fields_count++;
                    }
                    bool from_tool = IsToolOutputField(request, path);
                    ScanAndCollect(scan_text_ref, from_tool, path,
                                   result.spans, result.hit_categories, result.hit_rule_ids);
                });

            max_level = SensitivityLevel::S0;
            for (const auto& sp : result.spans) {
                for (const auto& rule : rules_) {
                    if (rule.rule_id == sp.rule_id) {
                        max_level = MaxLevel(max_level, rule.level);
                        break;
                    }
                }
            }

            {
                std::lock_guard<std::mutex> kw_lock(keywords_mutex_);
                if (!keywords_rules_.empty()) {
                    WalkJsonStringFieldsReadOnlyWithPath(request, "",
                        [&](const std::string& text, const std::string& path) {
                            const std::string scan_text_truncated_kw_ = (text.size() > 32768) ? safe_utf8_truncate(text, 32768, "") : text;
                            const std::string& scan_text_ref = (text.size() > 32768) ? scan_text_truncated_kw_ : text;
                            std::vector<std::string> kw_cats, kw_ids;

                            SensitivityLevel kw_level = ScanKeywordsDict(scan_text_ref, kw_cats, kw_ids, path, &result.spans);
                            max_level = MaxLevel(max_level, kw_level);
                            for (const auto& c : kw_cats) AddUnique(result.hit_categories, c);
                            for (const auto& id : kw_ids) AddUnique(result.hit_rule_ids, id);
                        });
                }
            }

            for (const auto& sp : result.spans) {
                if (sp.from_tool_output) {
                    for (const auto& rule : rules_) {
                        if (rule.rule_id == sp.rule_id && rule.level == SensitivityLevel::S2) {
                            result.tool_output_escalation = true;
                            My_Log{} << "[ContentSecurityInspector] Tool output escalation detected (S2 in tool message)" << std::endl;
                            break;
                        }
                    }
                }
            }
        } else {

            WalkJsonStringFieldsReadOnly(request, [&](const std::string& text) {

                std::string scan_text;
                if (text.size() > 32768) {
                    scan_text = safe_utf8_truncate(text, 32768, "");
                    result.oversized_field_warning = true;
                    result.oversized_fields_count++;
                } else {
                    scan_text = text;
                }

                std::vector<std::string> cats, ids;
                SensitivityLevel level = ScanText(scan_text, cats, ids);
                max_level = MaxLevel(max_level, level);
                for (const auto& c : cats) AddUnique(result.hit_categories, c);
                for (const auto& id : ids) AddUnique(result.hit_rule_ids, id);
            });

            {
                std::lock_guard<std::mutex> kw_lock(keywords_mutex_);
                if (!keywords_rules_.empty()) {
                    WalkJsonStringFieldsReadOnlyWithPath(request, "",
                        [&](const std::string& text, const std::string& path) {
                        const std::string scan_text_truncated_kw2_ = (text.size() > 32768) ? safe_utf8_truncate(text, 32768, "") : text;
                        const std::string& scan_text_ref = (text.size() > 32768) ? scan_text_truncated_kw2_ : text;
                        std::vector<std::string> kw_cats, kw_ids;
                        SensitivityLevel kw_level = ScanKeywordsDict(scan_text_ref, kw_cats, kw_ids, path);
                        max_level = MaxLevel(max_level, kw_level);
                        for (const auto& c : kw_cats) AddUnique(result.hit_categories, c);
                        for (const auto& id : kw_ids) AddUnique(result.hit_rule_ids, id);
                    });
                }
            }

            if (request.contains("messages") && request["messages"].is_array()) {
                for (const auto& msg : request["messages"]) {
                    if (msg.contains("role") && msg["role"].is_string() &&
                        msg["role"].get<std::string>() == "tool") {
                        std::string tool_content = SecurityUtils::ExtractMessageContentText(msg);
                        if (!tool_content.empty()) {
                            std::vector<std::string> tool_cats, tool_ids;
                            SensitivityLevel tool_level = ScanText(tool_content, tool_cats, tool_ids);
                            if (tool_level == SensitivityLevel::S2) {
                                result.tool_output_escalation = true;
                                My_Log{} << "[ContentSecurityInspector] Tool output escalation detected (S2 in tool message)" << std::endl;
                            }
                        }
                    }
                }
            }
        }
    }
    catch (const std::exception &e)
    {

        My_Log{My_Log::Level::kError}
            << "[ContentSecurityInspector] Rule engine exception: " << e.what()
            << ". Forcing S2 (conservative fallback)." << std::endl;
        result.sensitivity_level = SensitivityLevel::S2;
        result.rule_engine_failed = true;
        result.model_confidence = "low";
        result.summary_reason = "rule_engine_failed: " + std::string(e.what());
        return result;
    }
    catch (...)
    {

        My_Log{My_Log::Level::kError}
            << "[ContentSecurityInspector] Rule engine unknown exception. "
            << "Forcing S2 (conservative fallback)." << std::endl;
        result.sensitivity_level = SensitivityLevel::S2;
        result.rule_engine_failed = true;
        result.model_confidence = "low";
        result.summary_reason = "rule_engine_failed: unknown exception";
        return result;
    }

    result.sensitivity_level = max_level;
    result.model_confidence = "high";

    if (!ctx.is_internal_inspection &&
        !ctx.rule_engine_only &&
        config_.use_local_model_fallback &&
        max_level == SensitivityLevel::S0 &&
        model_config_ != nullptr)
    {

        auto handle_for_extract = model_config_->GetDefaultModelHandle().lock();
        const auto *default_instance_config = model_config_->GetDefaultInstanceConfig();
        size_t max_input_tokens = 0;
        if (handle_for_extract) {
            int ctx_size = default_instance_config ? default_instance_config->get_context_size() : 0;
            size_t context_size = (ctx_size > 0) ? static_cast<size_t>(ctx_size) : 8192;
            max_input_tokens = context_size * 4 / 5;
            My_Log{} << "[ContentSecurityInspector] LocalModel input limit: "
                     << "context_size=" << context_size
                     << ", max_input_tokens=" << max_input_tokens
                     << std::endl;
        }
        std::string text_summary = ExtractTextForAuxInspection(request, handle_for_extract, max_input_tokens);

        if (!text_summary.empty())
        {

            std::string cache_key = ComputeCacheKey(text_summary);
            CacheEntry cached_entry;

            total_request_count_.fetch_add(1);

            if (CacheLookup(cache_key, cached_entry))
            {

                consecutive_miss_count_.store(0);
                My_Log{} << "[ContentSecurityInspector] LocalModel cache hit, level="
                         << to_string(cached_entry.level) << std::endl;
                max_level = MaxLevel(max_level, cached_entry.level);
                result.model_confidence = "med";
                if (cached_entry.level != SensitivityLevel::S0)
                {
                    result.summary_reason = "local_model_cached: " + cached_entry.reason;
                }
            }
            else
            {

                int miss_count = consecutive_miss_count_.fetch_add(1) + 1;
                int total = total_request_count_.load();
                if (miss_count >= CACHE_MISS_WARNING_THRESHOLD && total > CACHE_MISS_WARNING_THRESHOLD) {

                    My_Log{My_Log::Level::kWarning}
                        << "[ContentSecurityInspector] cache_miss_warning: consecutive_miss="
                        << miss_count << ", total_requests=" << total << std::endl;

                    consecutive_miss_count_.store(0);
                }

                SensitivityLevel model_level = SensitivityLevel::S0;
                std::string model_reason;

                bool model_ok = LocalModelInspect(
                    text_summary,
                    config_.timeout_ms,
                    model_level,
                    model_reason);

                if (model_ok)
                {

                    CacheInsert(cache_key, {model_level, model_reason});

                    My_Log{} << "[ContentSecurityInspector] LocalModel result: level="
                             << to_string(model_level) << ", reason=" << model_reason << std::endl;

                    if (config_.strict_s2_union || model_level != SensitivityLevel::S0)
                    {
                        max_level = MaxLevel(max_level, model_level);
                        result.model_confidence = "med";
                        if (model_level != SensitivityLevel::S0)
                        {
                            result.summary_reason = "local_model: " + model_reason;
                        }
                    }
                }
                else
                {

                    SensitivityLevel elevated_level;
                    if (max_level == SensitivityLevel::S0)
                        elevated_level = SensitivityLevel::S1;
                    else if (max_level == SensitivityLevel::S1)
                        elevated_level = SensitivityLevel::S2;
                    else
                        elevated_level = SensitivityLevel::S2;

                    My_Log{My_Log::Level::kError}
                        << "[ContentSecurityInspector] LocalModel failed: " << model_reason
                        << ". Elevating sensitivity from " << to_string(max_level)
                        << " to " << to_string(elevated_level)
                        << " (conservative fallback per §七-A)" << std::endl;

                    max_level = elevated_level;
                    result.model_confidence = "low";
                    result.summary_reason = "local_model_failed_elevated: " + model_reason
                        + " (elevated to " + to_string(elevated_level) + ")";

                    CacheInsert(cache_key, {elevated_level, "local_model_timeout_fallback"});
                }
            }
        }
    }
    else
    {

        std::string skip_reason;
        if (ctx.is_internal_inspection)
            skip_reason = "is_internal_inspection=true (prevent recursion)";
        else if (ctx.rule_engine_only)
            skip_reason = "rule_engine_only=true";
        else if (!config_.use_local_model_fallback)
            skip_reason = "use_local_model_fallback=false";
        else if (max_level != SensitivityLevel::S0)
            skip_reason = "rule engine result=" + to_string(max_level) + " (not S0, local model not needed)";
        else if (model_config_ == nullptr)
            skip_reason = "model_config=null";
        else
            skip_reason = "unknown";
        My_Log{} << "[ContentSecurityInspector] LocalModel skipped: " << skip_reason << std::endl;
    }

    result.sensitivity_level = max_level;

    if (result.summary_reason.empty())
    {
        if (max_level == SensitivityLevel::S0)
        {
            result.summary_reason = "No sensitive content detected by rule engine";
        }
        else
        {
            std::ostringstream oss;
            oss << "Rule engine detected sensitivity=" << to_string(max_level)
                << ", categories=[";
            for (size_t i = 0; i < result.hit_categories.size(); ++i)
            {
                if (i > 0) oss << ",";
                oss << result.hit_categories[i];
            }
            oss << "], rules=[";
            for (size_t i = 0; i < result.hit_rule_ids.size(); ++i)
            {
                if (i > 0) oss << ",";
                oss << result.hit_rule_ids[i];
            }
            oss << "]";
            result.summary_reason = oss.str();
        }
    }

    if (max_level == SensitivityLevel::S0)
    {
        My_Log{} << "[ContentSecurityInspector] No sensitive content detected by rule engine" << std::endl;
    }
    else
    {
        My_Log{} << "[ContentSecurityInspector] " << result.summary_reason << std::endl;
    }

    return result;
}

void ContentSecurityInspector::ScanAndCollect(
        const std::string &text,
        bool from_tool,
        const std::string &field_path,
        std::vector<SensitiveSpan> &spans,
        std::vector<std::string> &cats,
        std::vector<std::string> &ids)
{
    if (text.empty()) return;

    const bool is_multiline = (text.find('\n') != std::string::npos);

    for (const auto &rule : rules_) {
        try {

            if (!rule.fast_reject_str.empty() &&
                text.find(rule.fast_reject_str) == std::string::npos) {
                continue;
            }

            bool is_bank_card_rule = (rule.rule_id == "R_BANK_CARD_KW" ||
                                      rule.rule_id == "R_BANK_CARD_FMT");

            if (is_multiline) {

                std::string::size_type line_start = 0;
                while (line_start <= text.size()) {
                    std::string::size_type line_end_pos = text.find('\n', line_start);
                    if (line_end_pos == std::string::npos) line_end_pos = text.size();

                    std::string::size_type line_content_end = line_end_pos;
                    if (line_content_end > line_start && text[line_content_end - 1] == '\r')
                        --line_content_end;

                    if (line_content_end > line_start) {
                        const std::string line = text.substr(line_start, line_content_end - line_start);
                        const size_t line_offset = line_start;

                        for (auto it = std::sregex_iterator(line.begin(), line.end(), rule.pattern);
                             it != std::sregex_iterator(); ++it) {
                            const auto &m = *it;
                            const std::string &raw = m.str();

                            if (is_bank_card_rule && !MatchPassesLuhn(raw)) continue;
                            if (IsDesensitizedPlaceholder(raw, rule.entity_type)) continue;

                            SensitiveSpan sp;
                            sp.type = rule.entity_type;
                            sp.rule_id = rule.rule_id;

                            sp.start = line_offset + static_cast<size_t>(m.position());
                            sp.end = sp.start + static_cast<size_t>(m.length());
                            sp.matched = raw;
                            sp.from_tool_output = from_tool;
                            sp.field_path = field_path;
                            spans.push_back(std::move(sp));

                            if (config_.debug_log_matches) {
                                size_t pos = static_cast<size_t>(m.position());
                                size_t ctx_before_start = utf8_align_start(line, (pos >= 30) ? pos - 30 : 0);
                                size_t ctx_after_end = std::min(pos + raw.size() + 30, line.size());
                                std::string ctx_before = line.substr(ctx_before_start, pos - ctx_before_start);
                                std::string ctx_after = safe_utf8_truncate(
                                    line.substr(pos + raw.size(), ctx_after_end - (pos + raw.size())),
                                    ctx_after_end - (pos + raw.size()), "");
                                for (auto &c : ctx_before) if (c == '\n' || c == '\r') c = ' ';
                                for (auto &c : ctx_after) if (c == '\n' || c == '\r') c = ' ';
                                My_Log{My_Log::Level::kInfo} << "[ContentSecurityInspector] Rule=" << rule.rule_id
                                         << " field=" << field_path
                                         << " matched=" << raw
                                         << " context=..." << ctx_before << "[" << raw << "]" << ctx_after << "..."
                                         << std::endl;
                            }

                            AddUnique(ids, rule.rule_id);
                            AddUnique(cats, rule.category);
                        }
                    }

                    if (line_end_pos == text.size()) break;
                    line_start = line_end_pos + 1;
                }
            } else {

                for (auto it = std::sregex_iterator(text.begin(), text.end(), rule.pattern);
                     it != std::sregex_iterator(); ++it) {
                    const auto &m = *it;
                    const std::string &raw = m.str();

                    if (is_bank_card_rule && !MatchPassesLuhn(raw)) continue;
                    if (IsDesensitizedPlaceholder(raw, rule.entity_type)) continue;

                    SensitiveSpan sp;
                    sp.type = rule.entity_type;
                    sp.rule_id = rule.rule_id;
                    sp.start = static_cast<size_t>(m.position());
                    sp.end = sp.start + static_cast<size_t>(m.length());
                    sp.matched = raw;
                    sp.from_tool_output = from_tool;
                    sp.field_path = field_path;
                    spans.push_back(std::move(sp));

                    if (config_.debug_log_matches) {
                        size_t pos = static_cast<size_t>(m.position());
                        size_t ctx_before_start = utf8_align_start(text, (pos >= 30) ? pos - 30 : 0);
                        size_t ctx_after_end = std::min(pos + raw.size() + 30, text.size());
                        std::string ctx_before = text.substr(ctx_before_start, pos - ctx_before_start);
                        std::string ctx_after = safe_utf8_truncate(
                            text.substr(pos + raw.size(), ctx_after_end - (pos + raw.size())),
                            ctx_after_end - (pos + raw.size()), "");
                        for (auto &c : ctx_before) if (c == '\n' || c == '\r') c = ' ';
                        for (auto &c : ctx_after) if (c == '\n' || c == '\r') c = ' ';
                        My_Log{My_Log::Level::kInfo} << "[ContentSecurityInspector] Rule=" << rule.rule_id
                                 << " field=" << field_path
                                 << " matched=" << raw
                                 << " context=..." << ctx_before << "[" << raw << "]" << ctx_after << "..."
                                 << std::endl;
                    }

                    AddUnique(ids, rule.rule_id);
                    AddUnique(cats, rule.category);
                }
            }
        } catch (const std::regex_error &e) {
            My_Log{My_Log::Level::kError} << "[ContentSecurityInspector] ScanAndCollect regex error in rule "
                                          << rule.rule_id << ": " << e.what() << std::endl;
        }
    }
}

SensitivityLevel ContentSecurityInspector::ScanKeywordsDict(
        const std::string &text,
        std::vector<std::string> &hit_categories,
        std::vector<std::string> &hit_rule_ids,
        const std::string &field_path,
        std::vector<SensitiveSpan> *out_spans)
{

    SensitivityLevel max_level = SensitivityLevel::S0;
    if (text.empty() || keywords_rules_.empty()) return max_level;

    std::string text_lower = text;
    std::transform(text_lower.begin(), text_lower.end(), text_lower.begin(),
                   [](unsigned char c) { return std::tolower(c); });

    for (const auto &kw_rule : keywords_rules_) {
        size_t hit_pos = std::string::npos;
        if (kw_rule.is_chinese) {

            hit_pos = text.find(kw_rule.keyword);
        } else {

            size_t search_from = 0;
            const size_t kw_len = kw_rule.keyword_lower.size();
            while (search_from < text_lower.size()) {
                size_t pos = text_lower.find(kw_rule.keyword_lower, search_from);
                if (pos == std::string::npos) break;

                bool left_ok  = (pos == 0) ||
                                (!std::isalnum(static_cast<unsigned char>(text_lower[pos - 1])) &&
                                 text_lower[pos - 1] != '_');
                bool right_ok = (pos + kw_len >= text_lower.size()) ||
                                (!std::isalnum(static_cast<unsigned char>(text_lower[pos + kw_len])) &&
                                 text_lower[pos + kw_len] != '_');

                if (left_ok && right_ok) {
                    hit_pos = pos;
                    break;
                }
                search_from = pos + 1;
            }
        }

        if (hit_pos != std::string::npos) {
            max_level = MaxLevel(max_level, kw_rule.level);
            AddUnique(hit_categories, kw_rule.category);
            AddUnique(hit_rule_ids, kw_rule.rule_id);

            if (out_spans != nullptr) {
                SensitiveSpan sp;
                sp.type       = SensitiveEntityType::KEYWORD;
                sp.rule_id    = kw_rule.rule_id;
                sp.start      = hit_pos;
                sp.end        = hit_pos + kw_rule.keyword.size();
                sp.matched    = kw_rule.keyword;
                sp.from_tool_output = false;
                sp.field_path = field_path;
                out_spans->push_back(std::move(sp));
            }

            if (config_.debug_log_matches) {
                const std::string &kw_display = kw_rule.keyword;
                size_t ctx_before_start = utf8_align_start(text, (hit_pos >= 30) ? hit_pos - 30 : 0);
                size_t kw_end = hit_pos + kw_rule.keyword.size();
                size_t ctx_after_end = std::min(kw_end + 30, text.size());
                std::string ctx_before = text.substr(ctx_before_start, hit_pos - ctx_before_start);
                std::string ctx_after = safe_utf8_truncate(
                    text.substr(kw_end, ctx_after_end - kw_end),
                    ctx_after_end - kw_end, "");
                for (auto &c : ctx_before) if (c == '\n' || c == '\r') c = ' ';
                for (auto &c : ctx_after) if (c == '\n' || c == '\r') c = ' ';
                My_Log{My_Log::Level::kInfo} << "[ContentSecurityInspector] Keyword=" << kw_rule.rule_id
                         << " level=" << to_string(kw_rule.level)
                         << " matched=" << kw_display
                         << (field_path.empty() ? "" : (" field=" + field_path))
                         << " context=..." << ctx_before << "[" << kw_display << "]" << ctx_after << "..."
                         << std::endl;
            }
        }
    }

    return max_level;
}

bool ContentSecurityInspector::LoadKeywordsDict(const std::string &path)
{
    if (path.empty()) return false;

    try {
        std::ifstream f(path);
        if (!f.is_open()) {

            My_Log{My_Log::Level::kWarning}
                << "[ContentSecurityInspector] Keywords dict file not found or cannot be opened: "
                << path << " (keywords detection disabled)" << std::endl;
            return false;
        }

        json dict_json;
        f >> dict_json;

        std::vector<KeywordRule> new_rules;

        auto build_kw_rule = [](const std::string &keyword,
                                const std::string &rule_id,
                                const std::string &category,
                                const std::string &level_str) -> KeywordRule {
            KeywordRule kw;
            kw.keyword = keyword;
            kw.rule_id = rule_id;
            kw.category = category;
            kw.level = (level_str == "S2") ? SensitivityLevel::S2 : SensitivityLevel::S1;

            kw.is_chinese = false;
            for (unsigned char c : kw.keyword) {
                if (c > 127) { kw.is_chinese = true; break; }
            }

            kw.keyword_lower = kw.keyword;
            std::transform(kw.keyword_lower.begin(), kw.keyword_lower.end(),
                           kw.keyword_lower.begin(),
                           [](unsigned char c) { return std::tolower(c); });
            return kw;
        };

        if (dict_json.is_array()) {

            for (const auto &item : dict_json) {
                if (!item.contains("keyword") || !item["keyword"].is_string()) continue;

                std::string keyword  = item["keyword"].get<std::string>();
                std::string rule_id  = item.value("rule_id", "KW_" + keyword.substr(0, 8));
                std::string category = item.value("category", "KEYWORD");
                std::string level    = item.value("level", "S1");

                new_rules.push_back(build_kw_rule(keyword, rule_id, category, level));
            }
        } else if (dict_json.is_object() && dict_json.contains("rules") && dict_json["rules"].is_array()) {

            for (const auto &rule_item : dict_json["rules"]) {
                if (!rule_item.contains("keywords") || !rule_item["keywords"].is_array()) continue;

                std::string rule_id  = rule_item.value("id", "KW_UNKNOWN");
                std::string category = rule_item.value("category", "KEYWORD");
                std::string level    = rule_item.value("level", "S1");

                for (const auto &kw_val : rule_item["keywords"]) {
                    if (!kw_val.is_string()) continue;
                    std::string keyword = kw_val.get<std::string>();
                    if (keyword.empty()) continue;
                    new_rules.push_back(build_kw_rule(keyword, rule_id, category, level));
                }
            }
        } else {
            My_Log{My_Log::Level::kError}
                << "[ContentSecurityInspector] Keywords dict format unrecognized in " << path
                << ". Expected: flat array or {\"rules\":[...]} object." << std::endl;
            return false;
        }

        std::sort(new_rules.begin(), new_rules.end(),
                  [](const KeywordRule &a, const KeywordRule &b) {
                      return a.keyword.size() > b.keyword.size();
                  });

        {
            std::lock_guard<std::mutex> lock(keywords_mutex_);
            keywords_rules_ = std::move(new_rules);
            keywords_dict_loaded_ = true;
        }

        {
            struct stat st_update;
            if (stat(path.c_str(), &st_update) == 0) {
                last_keywords_mtime_ = std::chrono::system_clock::from_time_t(st_update.st_mtime);
            }
        }

        My_Log{} << "[ContentSecurityInspector] Keywords dict loaded from " << path
                 << ", rules_count=" << keywords_rules_.size() << std::endl;
        return true;

    } catch (const std::exception &e) {
        My_Log{My_Log::Level::kError} << "[ContentSecurityInspector] Failed to load keywords dict from "
                                      << path << ": " << e.what() << std::endl;
        return false;
    }
}

void ContentSecurityInspector::CheckAndReloadKeywordsDict(InspectionResult &result)
{
    if (keywords_dict_path_.empty()) return;

    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
        now - last_keywords_check_time_).count();

    if (elapsed < keywords_reload_interval_seconds_) return;

    last_keywords_check_time_ = now;

    struct stat st;
    if (stat(keywords_dict_path_.c_str(), &st) != 0) {

        return;
    }

    auto file_mtime = std::chrono::system_clock::from_time_t(st.st_mtime);

    if (keywords_dict_loaded_ && file_mtime <= last_keywords_mtime_) {

        return;
    }

    bool loaded = LoadKeywordsDict(keywords_dict_path_);
    if (loaded) {
        last_keywords_mtime_ = file_mtime;
        result.keywords_dict_reloaded = true;
        result.keywords_dict_rules_count = GetKeywordsRulesCount();
        My_Log{} << "[ContentSecurityInspector] Keywords dict hot-reloaded, rules_count="
                 << result.keywords_dict_rules_count << std::endl;
    }
}

int ContentSecurityInspector::GetKeywordsRulesCount() const
{
    std::lock_guard<std::mutex> lock(keywords_mutex_);
    return static_cast<int>(keywords_rules_.size());
}

bool ContentSecurityInspector::IsToolOutputField(const json &request, const std::string &field_path)
{

    if (field_path.empty()) return false;

    static const std::string messages_prefix = "/messages/";
    if (field_path.substr(0, messages_prefix.size()) != messages_prefix) return false;

    size_t idx_start = messages_prefix.size();
    size_t idx_end = field_path.find('/', idx_start);
    if (idx_end == std::string::npos) return false;

    std::string idx_str = field_path.substr(idx_start, idx_end - idx_start);
    int msg_idx = -1;
    try { msg_idx = std::stoi(idx_str); } catch (...) { return false; }

    if (!request.contains("messages") || !request["messages"].is_array()) return false;
    const auto &messages = request["messages"];
    if (msg_idx < 0 || msg_idx >= static_cast<int>(messages.size())) return false;

    const auto &msg = messages[msg_idx];
    return msg.contains("role") && msg["role"].is_string() &&
           msg["role"].get<std::string>() == "tool";
}
