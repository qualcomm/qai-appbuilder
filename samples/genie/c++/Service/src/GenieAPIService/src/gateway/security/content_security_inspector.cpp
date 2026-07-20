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

// ============================================================
// 缺口6：Luhn 算法校验（用于银行卡号验证）
// 参考：https://en.wikipedia.org/wiki/Luhn_algorithm
// 输入：仅含数字的字符串（已去除空格/连字符）
// 返回：true 表示通过 Luhn 校验（可能是有效卡号）
// ============================================================
static bool LuhnCheck(const std::string &digits)
{
    if (digits.empty())
        return false;

    int sum = 0;
    bool double_it = false;

    // 从最右侧数字开始向左遍历
    for (int i = static_cast<int>(digits.size()) - 1; i >= 0; --i)
    {
        if (!std::isdigit(static_cast<unsigned char>(digits[i])))
            return false;  // 含非数字字符，直接返回 false

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

// 将配置字符串（"S0"/"S1"/"S2"）转换为 SensitivityLevel，无效值回退到 default_level
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

// 从匹配字符串中提取纯数字（去除空格和连字符），并执行 Luhn 校验
static bool MatchPassesLuhn(const std::string &matched_str)
{
    std::string digits;
    digits.reserve(matched_str.size());
    for (char c : matched_str)
    {
        if (std::isdigit(static_cast<unsigned char>(c)))
            digits += c;
    }
    // 长度检查：银行卡号通常为 13-19 位
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
    // 初始化时尝试加载关键词词典（文件不存在时不报错）
    if (!keywords_dict_path_.empty()) {
        LoadKeywordsDict(keywords_dict_path_);
    }
}

void ContentSecurityInspector::InitRules()
{
    rules_.clear();

    // ---- PII 规则 ----
    // R_PHONE_CN: 中国大陆手机号（11位，1[3-9]开头）
    // 受 detection_rules.enable_phone 开关控制；等级由 detection_rules.level_phone 配置
    if (config_.detection_rules.enable_phone) {
        rules_.push_back({
            "R_PHONE_CN", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_phone, SensitivityLevel::S1),
            std::regex(R"(\b1[3-9]\d{9}\b)"), false, "",
            SensitiveEntityType::PHONE, ""  // 数字在任何文本中都有，无有效预检字符串
        });
    }

    // R_ID_CARD: 中国居民身份证（18位）
    // 受 detection_rules.enable_id_card 开关控制；等级由 detection_rules.level_id_card 配置
    if (config_.detection_rules.enable_id_card) {
        rules_.push_back({
            "R_ID_CARD", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_id_card, SensitivityLevel::S2),
            std::regex(R"(\b\d{17}[\dXx]\b)"), false, "",
            SensitiveEntityType::IDCARD, ""  // 数字无有效预检
        });
    }

    // R_EMAIL: 电子邮件地址
    // 受 detection_rules.enable_email 开关控制；等级由 detection_rules.level_email 配置
    if (config_.detection_rules.enable_email) {
        rules_.push_back({
            "R_EMAIL", "PII",
            ParseSensitivityLevel(config_.detection_rules.level_email, SensitivityLevel::S1),
            std::regex(R"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"), false, "",
            SensitiveEntityType::EMAIL, "@"  // 邮箱必含 @
        });
    }

    // ---- FINANCIAL 规则 ----
    // R_BANK_CARD: 银行卡号（16-19位数字）
    // 改进：在纯数字长度匹配基础上，要求前后有上下文关键词（"卡号"/"card"/"bank"/"account"等），
    // 或者数字中包含空格分隔（如 "1234 5678 9012 3456"），以降低误报率。
    // 同时将敏感等级从 S2 降为 S1，避免因误报导致不必要的 POLICY_ERROR。
    // 注意：此规则仍有一定误报率，后续可进一步引入 Luhn 算法校验。
    //
    // 规则拆分为两条：
    //   R_BANK_CARD_KW：有上下文关键词时匹配（S2，高置信度）
    //   R_BANK_CARD_FMT：标准格式（4位-4位-4位-4位，含空格/连字符分隔）（S2，中置信度）
    // 受 detection_rules.enable_bank_card 开关控制；等级由 detection_rules.level_bank_card 配置
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
            SensitiveEntityType::BANKCARD, ""  // 关键词中英文混合，无单一有效预检
        });
        rules_.push_back({
            "R_BANK_CARD_FMT", "FINANCIAL", bank_card_level,
            std::regex(R"(\b\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}(?:[\s\-]\d{1,3})?\b)"), false, "",
            SensitiveEntityType::BANKCARD, ""  // 数字格式无有效预检
        });
    }

    // ---- SECRET 规则 ----
    // R_API_KEY: OpenAI 风格 API Key（sk- 开头）
    // 受 detection_rules.enable_api_key 开关控制；等级由 detection_rules.level_api_key 配置
    if (config_.detection_rules.enable_api_key) {
        rules_.push_back({
            "R_API_KEY", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_api_key, SensitivityLevel::S2),
            std::regex(R"(sk-[a-zA-Z0-9]{20,})"), false, "",
            SensitiveEntityType::API_KEY, "sk-"  // API Key 必含 sk- 前缀
        });
    }

    // R_PRIVATE_KEY: PEM 私钥块
    // 受 detection_rules.enable_private_key 开关控制；等级由 detection_rules.level_private_key 配置
    if (config_.detection_rules.enable_private_key) {
        rules_.push_back({
            "R_PRIVATE_KEY", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_private_key, SensitivityLevel::S2),
            std::regex(R"(-----BEGIN\s+\S*\s*PRIVATE\s+KEY-----)"), false, "",
            SensitiveEntityType::PRIVATE_KEY, "-----BEGIN"  // 私钥必含此前缀
        });
    }

    // R_TOKEN: Bearer Token
    // 受 detection_rules.enable_token 开关控制；等级由 detection_rules.level_token 配置
    if (config_.detection_rules.enable_token) {
        rules_.push_back({
            "R_TOKEN", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_token, SensitivityLevel::S2),
            std::regex(R"(Bearer\s+[a-zA-Z0-9._\-]{20,})"), false, "",
            SensitiveEntityType::TOKEN, "Bearer "  // Bearer Token 必含此前缀
        });
    }

    // R_PASSWORD_KW: 密码关键词（关键词匹配，不区分大小写）
    // 受 detection_rules.enable_password 开关控制；等级由 detection_rules.level_password 配置
    if (config_.detection_rules.enable_password) {
        rules_.push_back({
            "R_PASSWORD_KW", "SECRET",
            ParseSensitivityLevel(config_.detection_rules.level_password, SensitivityLevel::S1),
            std::regex(R"((?:password|passwd|secret)\s*=\s*\S+)", std::regex::icase), false, "",
            SensitiveEntityType::PASSWORD_KW, "="  // password=xxx 必含 =
        });
    }

    // ---- 扩展规则：本地文件路径（PATH）----
    if (config_.extended_rules.enable_local_path) {
        rules_.push_back({
            "R_PATH_UNIX", "PII", SensitivityLevel::S1,
            std::regex(R"((?:/home/|/root/|/Users/|/var/private/)[^\s<]+)"),
            false, "", SensitiveEntityType::PATH, "/home/"  // Unix 路径必含 /home/ 或 /Users/ 等，取最常见的
        });
        rules_.push_back({
            "R_PATH_WIN", "PII", SensitivityLevel::S1,
            std::regex(R"([A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][^\s<]+)"),
            false, "", SensitiveEntityType::PATH, "Users"  // Windows/Unix 路径必含 Users
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: local_path (R_PATH_UNIX, R_PATH_WIN)" << std::endl;
    }

    // ---- 扩展规则：内网地址（INTERNAL_URL）----
    if (config_.extended_rules.enable_internal_url) {
        rules_.push_back({
            "R_INTERNAL_IP", "PII", SensitivityLevel::S1,
            std::regex(
                R"(\b(?:192\.168\.\d{1,3}\.\d{1,3}|)"
                R"(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|)"
                R"(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b)"
            ), false, "", SensitiveEntityType::INTERNAL_URL, "."  // IP 必含 .，但太宽泛；无更好选择
        });
        rules_.push_back({
            "R_INTERNAL_DOMAIN", "PII", SensitivityLevel::S1,
            std::regex(R"(\b[^\s/\\.]+\.(?:internal|local|corp|intranet|lan)\b)",
                       std::regex::icase),
            false, "", SensitiveEntityType::INTERNAL_URL, ".local"  // 最常见的内网域名后缀
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: internal_url (R_INTERNAL_IP, R_INTERNAL_DOMAIN)" << std::endl;
    }

    // ---- 扩展规则：设备标识（DEVICE_ID）----
    if (config_.extended_rules.enable_device_id) {
        rules_.push_back({
            "R_MAC_ADDR", "PII", SensitivityLevel::S1,
            std::regex(R"(\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b)"),
            false, "", SensitiveEntityType::DEVICE_ID, ":"  // MAC 地址必含 :，但太宽泛；无更好选择
        });
        rules_.push_back({
            "R_IMEI", "PII", SensitivityLevel::S1,
            std::regex(R"((?:IMEI|imei|设备号)\s*[：:=]?\s*\d{15})",
                       std::regex::icase),
            false, "", SensitiveEntityType::DEVICE_ID, "IMEI"  // IMEI 必含关键词
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: device_id (R_MAC_ADDR, R_IMEI)" << std::endl;
    }

    // ---- 扩展规则：图片数据基础保护（IMAGE_DATA）----
    if (config_.extended_rules.enable_image_data) {
        rules_.push_back({
            "R_BASE64_IMAGE", "PII", SensitivityLevel::S1,
            std::regex(R"(data:image/(?:jpeg|jpg|png|gif|webp|bmp|svg\+xml);base64,)",
                       std::regex::icase),
            false, "", SensitiveEntityType::UNKNOWN, "data:image/"  // Base64 图片必含此前缀
        });
        My_Log{} << "[ContentSecurityInspector] Extended rules loaded: image_data (R_BASE64_IMAGE)" << std::endl;
    }

    // 应用 rule_level_overrides 配置
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

// ============================================================
// IsDesensitizedPlaceholder：白名单机制
// 检查匹配到的值是否是 FormatMockData 生成的脱敏占位符
// 若是，则跳过该匹配，避免脱敏循环（max_rounds 超限）
// ============================================================
bool ContentSecurityInspector::IsDesensitizedPlaceholder(
        const std::string &matched,
        SensitiveEntityType entity_type)
{
    switch (entity_type) {
        case SensitiveEntityType::EMAIL:
            // FormatMockData 生成格式：mock_user_<N>@example.com
            // 正则：^mock_user_\d+@example\.com$
            {
                static const std::regex mock_email_re(R"(^mock_user_\d+@example\.com$)");
                return std::regex_match(matched, mock_email_re);
            }

        case SensitiveEntityType::PHONE:
            // FormatMockData 生成格式：1389XXXXXXX（11位，以1389开头，后7位为 index % 10000000 补零）
            // 使用 1389 前缀使 mock 手机号可被白名单识别
            // 正则：^1389\d{7}$
            {
                static const std::regex mock_phone_re(R"(^1389\d{7}$)");
                return std::regex_match(matched, mock_phone_re);
            }

        case SensitiveEntityType::IDCARD:
            // FormatMockData 生成格式：110105199001011230<N%10>（19位，固定前18位）
            // 注意：真实身份证也可能以110105199001011230开头，此处不做白名单处理
            return false;

        case SensitiveEntityType::BANKCARD:
            // FormatMockData 生成格式：6222XXXXXXXXXXXX（16位，以6222开头）
            // 注意：真实银行卡也可能以6222开头，此处不做白名单处理
            return false;

        case SensitiveEntityType::API_KEY:
            // FormatMockData 生成格式：sk-mock_<N>_<32chars>
            // 正则：^sk-mock_\d+_[a-zA-Z0-9]{32}$
            {
                static const std::regex mock_api_key_re(R"(^sk-mock_\d+_[a-zA-Z0-9]{32}$)");
                return std::regex_match(matched, mock_api_key_re);
            }

        case SensitiveEntityType::TOKEN:
            // FormatMockData 生成格式：Bearer mock_token_<N>_<32chars>
            // 注意：R_TOKEN 规则匹配 "Bearer\s+[a-zA-Z0-9._\-]{20,}"
            // mock 数据中 Bearer 后面的部分是 "mock_token_<N>_<32chars>"
            // 正则：^Bearer\s+mock_token_\d+_[a-zA-Z0-9]{32}$
            {
                static const std::regex mock_token_re(R"(^Bearer\s+mock_token_\d+_[a-zA-Z0-9]{32}$)");
                return std::regex_match(matched, mock_token_re);
            }

        case SensitiveEntityType::PRIVATE_KEY:
            // FormatMockData 生成格式：-----BEGIN RSA PRIVATE KEY-----\nMockPrivateKeyData<N>\n-----END RSA PRIVATE KEY-----
            // R_PRIVATE_KEY 规则只匹配 "-----BEGIN ... PRIVATE KEY-----" 前缀
            // mock 数据中包含 "MockPrivateKeyData" 字样，可以通过此特征识别
            // 但由于规则只匹配前缀，matched 只是 "-----BEGIN RSA PRIVATE KEY-----"
            // 无法通过 matched 区分真实私钥和 mock 数据，此处不做白名单处理
            return false;

        case SensitiveEntityType::INTERNAL_URL:
            // FormatMockData 生成格式：192.168.X.X（仍然是内网IP格式）
            // 无法通过格式区分真实内网IP和 mock 数据，此处不做白名单处理
            return false;

        case SensitiveEntityType::PATH:
            // FormatMockData 生成格式（Windows）：C:\Users\mock_user_<N>\<原始路径其余部分>
            // FormatMockData 生成格式（Unix）：/home/mock_user_<N>/<原始路径其余部分>
            // 或回退格式（Windows）：C:\Users\mock_user_<N>\Documents
            // 或回退格式（Unix）：/home/mock_user_<N>/Documents
            // 正则（Windows）：^[A-Za-z]:[/\\]Users[/\\]mock_user_\d+（后跟任意路径或结尾）
            // 正则（Unix）：^/(?:home|Users)/mock_user_\d+（后跟任意路径或结尾）
            {
                static const std::regex mock_win_path_re(
                    R"(^[A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\]mock_user_\d+(?:[/\\].*)?)");
                static const std::regex mock_unix_path_re(
                    R"(^/(?:home|Users)/mock_user_\d+(?:/.*)?)");
                return std::regex_match(matched, mock_win_path_re) ||
                       std::regex_match(matched, mock_unix_path_re);
            }

        case SensitiveEntityType::DEVICE_ID:
            // FormatMockData 生成格式：00:11:22:33:44:XX（MAC地址，前5组固定为00:11:22:33:44）
            // 正则：^00:11:22:33:44:[0-9a-f]{2}$
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
            // [PERF] 快速预检：若规则设置了 fast_reject_str，先用 string::find 检查
            // 文本中是否包含该子串。find 是 O(n) 线性扫描，比正则引擎快数十倍。
            // 不包含则直接跳过此规则，避免正则引擎的 NFA 状态机开销。
            if (!rule.fast_reject_str.empty() &&
                text.find(rule.fast_reject_str) == std::string::npos) {
                continue;
            }

            // 缺口6：对银行卡规则额外执行 Luhn 算法校验，降低误报率
            // R_BANK_CARD_KW 和 R_BANK_CARD_FMT 命中后需通过 Luhn 校验才计入结果
            bool is_bank_card_rule = (rule.rule_id == "R_BANK_CARD_KW" ||
                                      rule.rule_id == "R_BANK_CARD_FMT");

            if (is_bank_card_rule)
            {
                // 使用 regex_iterator 遍历所有匹配项，逐一执行 Luhn 校验
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
                // [PERF] 非 debug 模式：用 regex_search 快速判断是否存在匹配，
                // 找到第一个匹配即返回，不遍历所有匹配项。
                // debug 模式：改用 regex_iterator 遍历所有匹配以输出完整上下文。
                if (!config_.debug_log_matches) {
                    // 快速路径：regex_search 找到第一个非占位符匹配即停止
                    std::smatch m;
                    std::string::const_iterator search_start = text.cbegin();
                    bool any_real_match = false;
                    while (std::regex_search(search_start, text.cend(), m, rule.pattern)) {
                        const std::string &raw = m.str();
                        if (!IsDesensitizedPlaceholder(raw, rule.entity_type)) {
                            any_real_match = true;
                            break;
                        }
                        // 当前匹配是占位符，跳过继续搜索
                        search_start = m.suffix().first;
                        if (search_start == text.cend()) break;
                    }
                    if (any_real_match) {
                        max_level = MaxLevel(max_level, rule.level);
                        AddUnique(hit_categories, rule.category);
                        AddUnique(hit_rule_ids, rule.rule_id);
                    }
                } else {
                    // debug 路径：遍历所有匹配，输出每个命中的上下文
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

    // [PERF] 逐行扫描优化：
    // 对于多行文本（如 dir /s /b 输出的路径列表），正则在整块文本上会产生
    // 灾难性回溯（即使截断到 32KB，NFA 引擎在每行路径上都要回溯）。
    // 按行分割后每行只有几十字节，正则在短字符串上不产生回溯，速度提升数百倍。
    // 单行文本（无换行符）直接走原有路径，无额外开销。
    if (text.find('\n') != std::string::npos) {
        SensitivityLevel max_level = SensitivityLevel::S0;
        std::string::size_type start = 0;
        while (start <= text.size()) {
            std::string::size_type end = text.find('\n', start);
            if (end == std::string::npos) end = text.size();
            if (end > start) {
                // 去掉行尾 \r（Windows 换行）
                std::string::size_type line_end = end;
                if (line_end > start && text[line_end - 1] == '\r') --line_end;
                if (line_end > start) {
                    std::string line = text.substr(start, line_end - start);
                    std::vector<std::string> line_cats, line_ids;
                    SensitivityLevel line_level = RunRuleEngine(line, line_cats, line_ids);
                    max_level = MaxLevel(max_level, line_level);
                    for (const auto& c : line_cats) AddUnique(categories, c);
                    for (const auto& id : line_ids) AddUnique(rule_ids, id);
                    // 已达到最高等级 S2，无需继续扫描
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
        // 扫描 function.arguments
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
            // 扫描 function.name
            if (func.contains("name") && func["name"].is_string())
            {
                max_level = MaxLevel(max_level,
                    ScanText(func["name"].get<std::string>(), categories, rule_ids));
            }
        }
    }

    return max_level;
}

// ============================================================
// 本地模型辅助判断
// ============================================================

std::string ContentSecurityInspector::ExtractTextForAuxInspection(
        const json &request,
        std::shared_ptr<ContextBase> handle,
        size_t max_input_tokens) const
{
    const json& messages = request.contains("messages") ? request["messages"] : json::array();
    if (!messages.is_array() || messages.empty()) return "";

    // 辅助 lambda：提取单条消息的文本内容，并过滤掉 untrusted metadata 块和时间戳前缀
    // 使用 SecurityUtils::CleanMessageText 统一处理（与 task_complexity_evaluator.cpp 共用）
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
        // SecurityUtils::CleanMessageText 依次执行：
        //   步骤1：过滤 "Sender (untrusted metadata):" JSON 块（含 trim）
        //   步骤2：过滤 OpenClaw 时间戳前缀 "[Wed 2026-03-25 20:13 GMT+8] "
        //   步骤3：再次 trim
        return SecurityUtils::CleanMessageText(text);
    };

    // 如果没有 handle 或 max_input_tokens 为 0，回退到字符数限制（兼容旧行为）
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

    // ---- Token-based 实现 ----
    // 辅助 lambda：将文本截断到指定 token 数以内，返回 {截断后文本, 实际 token 数}
    // 策略：先按比例估算截断位置，再迭代调整（最多 3 次），避免多次全量 tokenize
    auto truncate_to_tokens = [&handle](const std::string& text, size_t max_tokens)
            -> std::pair<std::string, size_t> {
        size_t tokens = handle->TokenLength(text);
        if (tokens <= max_tokens) return {text, tokens};

        // 按比例估算截断字符数
        size_t estimated_chars = text.size() * max_tokens / tokens;
        std::string truncated = safe_utf8_truncate(text, estimated_chars, "");

        // 迭代调整（最多 3 次）
        for (int iter = 0; iter < 3; ++iter) {
            tokens = handle->TokenLength(truncated);
            if (tokens <= max_tokens) break;
            // 仍然超出，再去掉约 10%
            size_t remove = std::max(size_t(1), truncated.size() / 10);
            truncated = safe_utf8_truncate(truncated, truncated.size() - remove, "");
        }

        tokens = handle->TokenLength(truncated);
        return {truncated, tokens};
    };

    // [改进 1] 跳过 role=system 的消息（system prompt 不含用户隐私数据，跳过可提升缓存命中率）
    // [改进 2] 从最新消息往前拼接（逆序遍历），优先检查最新的用户输入
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
            // 截断到剩余 token 预算
            auto [truncated, actual_tokens] = truncate_to_tokens(text, remaining_tokens);
            if (!truncated.empty()) {
                segments.push_back(truncated);
                total_tokens += actual_tokens;
            }
            break;  // 已填满预算，停止
        }

        segments.push_back(text);
        total_tokens += text_tokens;

        if (total_tokens >= max_input_tokens) break;
    }

    if (segments.empty()) return "";

    // 反转片段顺序，恢复时间顺序（最旧的在前，最新的在后）
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
    // 如果没有 OpenSSL，可以退而求其次使用简化的方式，或者集成 picosha2
    // 由于环境限制，如果未配置 HAVE_OPENSSL，暂时用原有的 fnv1a + policy_id 模拟
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

    // TTL 检查：超过 ttl_seconds 的缓存条目视为过期
    auto now = std::chrono::steady_clock::now();
    auto age_seconds = std::chrono::duration_cast<std::chrono::seconds>(
        now - it->second->second.insert_time).count();
    if (age_seconds > cache_ttl_seconds_) {
        // 过期：从缓存中删除
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

    // 使用可配置的容量上限 cache_max_entries_
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

    // 获取模型句柄（weak_ptr → shared_ptr）
    // 修复：使用 GetDefaultModelHandle() 而非 get_genie_model_handle()，
    // 确保安全检查始终使用 default 模型（不跟随客户端指定的模型动态切换）。
    // 在多模型场景下，get_genie_model_handle() 返回的是最后通过 LoadSingleModel 加载的模型句柄，
    // 而 GetDefaultModelHandle() 明确返回 service_config.json 中 default_model 对应的模型句柄。
    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle)
    {
        reason = "default model handle not available";
        return false;
    }

    // [诊断] 检查模型是否正在执行推理（query_mutex_ 是否被锁定）
    // 在单任务场景下，安全检查调用时主推理应该已经完成（锁应为空闲状态）
    // 若锁被占用，说明存在并发推理，安全检查将阻塞直到主推理完成，可能导致超时
    if (handle->is_query_busy()) {
        My_Log{My_Log::Level::kWarning}
            << "[ContentSecurityInspector] WARNING: model query_mutex_ is LOCKED "
            << "(another Query() is in progress). Security check will block until it completes. "
            << "This may cause LocalModelInspect timeout (" << timeout_ms << "ms)." << std::endl;
    } else {
        My_Log{} << "[ContentSecurityInspector] model query_mutex_ is free (no lock contention)" << std::endl;
    }

    // 构建 Prompt（不含原始敏感内容，仅传入摘要）
    // 根据模型类型选择不同的提示词格式，与主推理过程保持一致
    // 增强 system prompt，明确禁止推理/思考过程，要求立即输出 JSON。
    // 与 security_prompt_builder.h 中的 reasoning_level="low" 和 <|constrain|>json 协同：
    //   - reasoning_level="low" : 从模型配置层面降低推理级别
    //   - "Do NOT think/reason" : 从指令层面明确禁止 CoT
    //   - <|constrain|>json    : 从 prompt 结构层面强制直接输出 JSON
    // 系统提示词从 service_config.json 的 routing.sensitivity_detection.system_prompt 读取

    // 传入 json_prefill 参数，在 Harmony 格式的 assistant prefill 中预填 JSON 开始部分
    // 这样模型只需要继续生成 S0|S1|S2","reason":"..."} 即可，强制约束输出格式
    // 对所有模型（Harmony 和 General）统一传入 json_prefill，
    // 引导模型直接输出 JSON 内容，避免模型输出无关文本。
    // - Harmony 模型：注入到 assistant prefill（final 通道标记之后）
    // - General 模型：追加到 start_prompt 末尾（<|im_start|>assistant\n 之后）
    const bool is_harmony = (model_config_->get_prompt_type() == PromptType::Harmony);
    (void)is_harmony;  // 保留变量供日志使用，prefill 对两种模型均适用
    const std::string json_prefill = "{\"sensitivity\":\"";
    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text: " + text_summary,
                                               json_prefill);
    My_Log{} << "[ContentSecurityInspector] prompt format: "
             << (model_config_->get_prompt_type() == PromptType::Harmony ? "Harmony" : "General")
             << " for security check" << std::endl;

    // [诊断] 记录 prompt 长度（字符数和 token 数），帮助诊断超时原因
    size_t prompt_chars = prompt.size();
    size_t prompt_tokens = handle->TokenLength(prompt);
    My_Log{} << "[ContentSecurityInspector] LocalModelInspect: "
             << "prompt_chars=" << prompt_chars
             << ", prompt_tokens=" << prompt_tokens
             << ", timeout_ms=" << timeout_ms
             << std::endl;
    if (prompt_tokens > static_cast<size_t>(timeout_ms / 5)) {
        // 粗略估算：若 token 数 > timeout_ms/5，则推理时间可能超过 timeout_ms
        // （假设处理速度约 5 tokens/ms，即 5000 tokens/s）
        My_Log{My_Log::Level::kWarning}
            << "[ContentSecurityInspector] WARNING: prompt_tokens=" << prompt_tokens
            << " may exceed timeout_ms=" << timeout_ms
            << " (estimated processing time > " << (prompt_tokens / 5) << "ms). "
            << "Consider reducing max_input_tokens." << std::endl;
    }

    // [诊断] 安全检查最大生成 token 数：从配置读取（默认 2048）。
    // 思考类模型（如 Harmony 格式）在输出 JSON 前会先生成 analysis channel 内容，
    // 需要更大的 token 预算。正常情况下 JSON 完整性检测会更早触发早停。
    const int kSecurityCheckMaxTokens = config_.max_gen_tokens > 0 ? config_.max_gen_tokens : 2048;

    // 使用共享的本地模型推理框架（SecurityUtils::LocalModelQuery）：
    // 内部已通过 std::thread + std::promise 实现真正的非阻塞超时控制，
    // 并已包含硬上限 kSecurityCheckMaxTokens 强制停止逻辑，此处只需提供早停判定的 token 回调。
    auto query_result = SecurityUtils::LocalModelQuery(
            handle, prompt, json_prefill, timeout_ms, kSecurityCheckMaxTokens,
            [](std::string &output_buf, const std::string &token, bool &json_complete) -> bool {
                (void)token;
                // ─────────────────────────────────────────────────────────────────
                // 早停：General 模型在普通文本中夹带合法 JSON 时也允许提前停止。
                // 这里采用"扫描所有可能的 {...} 子串并验证"的方式，兼容：
                // 1) 纯 JSON 输出
                // 2) 前置 <think> / 思考文本 + JSON
                // 3) JSON 后又跟随多余解释
                //
                // 策略1：完整 JSON 解析（标准路径）
                // 策略2：前缀匹配早停（防御路径，复用 SecurityUtils::CheckPrefixEarlyStop）
                //   当 output_buf 包含 {"sensitivity":"S0/S1/S2 后跟非字母字符时立即停止，
                //   防止模型在输出有效值后继续生成奇怪内容（如 "([{"），导致 JSON 解析失败
                //   并触发保守升级 S0→S1。
                // 策略1：完整 JSON 解析
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

                // 策略2：前缀匹配早停
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

    // 解析模型输出 JSON
    // General 模型可能输出：
    //   <think>...</think>{"sensitivity":"S0",...}
    // 或 纯 JSON / JSON + 尾随解释。
    // 因此不能直接用首个 '{' 到最后一个 '}'，而要扫描并提取第一个合法 JSON 对象。
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

// 添加只读遍历辅助函数（文件内静态函数）
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

// 带路径参数的只读遍历辅助函数
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

    // is_internal_inspection 仅用于防止"辅助判定递归"，不能跳过规则引擎扫描。
    // 在这里不再直接返回。

    CheckAndReloadKeywordsDict(result);

    // =========================================================
    // 缺口1：规则引擎整体 try-catch
    // 若规则引擎发生任何未预期异常（如正则引擎崩溃、内存错误等），
    // 强制返回 S2（最严格），并设置 rule_engine_failed=true 供审计日志记录。
    // =========================================================
    SensitivityLevel max_level = SensitivityLevel::S0;

    try
    {
        if (ctx.collect_spans) {
            // 带路径遍历，产出 SensitiveSpan 列表
            WalkJsonStringFieldsReadOnlyWithPath(request, "",
                [&](const std::string& text, const std::string& path) {
                    // 截断超长字段（单个字段超过 32KB 时仅取前 32KB，使用 UTF-8 安全截断）
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
            // 重新计算 max_level（基于 spans 中的规则等级）
            max_level = SensitivityLevel::S0;
            for (const auto& sp : result.spans) {
                for (const auto& rule : rules_) {
                    if (rule.rule_id == sp.rule_id) {
                        max_level = MaxLevel(max_level, rule.level);
                        break;
                    }
                }
            }
            // 关键词词典扫描（collect_spans=true 时同时产出 KEYWORD 类型的 SensitiveSpan）
            {
                std::lock_guard<std::mutex> kw_lock(keywords_mutex_);
                if (!keywords_rules_.empty()) {
                    WalkJsonStringFieldsReadOnlyWithPath(request, "",
                        [&](const std::string& text, const std::string& path) {
                            const std::string scan_text_truncated_kw_ = (text.size() > 32768) ? safe_utf8_truncate(text, 32768, "") : text;
                            const std::string& scan_text_ref = (text.size() > 32768) ? scan_text_truncated_kw_ : text;
                            std::vector<std::string> kw_cats, kw_ids;
                            // 传入 &result.spans，让关键词命中也产出 SensitiveSpan
                            SensitivityLevel kw_level = ScanKeywordsDict(scan_text_ref, kw_cats, kw_ids, path, &result.spans);
                            max_level = MaxLevel(max_level, kw_level);
                            for (const auto& c : kw_cats) AddUnique(result.hit_categories, c);
                            for (const auto& id : kw_ids) AddUnique(result.hit_rule_ids, id);
                        });
                }
            }
            // tool output 门禁：检查 spans 中是否有来自 tool 的 S2 命中
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
            // 防御性遍历，接受整个 request（不产出 spans）
            WalkJsonStringFieldsReadOnly(request, [&](const std::string& text) {
                // 截断超长字段（单个字段超过 32KB 时仅取前 32KB）
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

            // 关键词词典扫描（使用带路径版本，以便日志中能显示 field_path）
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

            // tool output 门禁（§5.4）：额外对 role=="tool" 的消息单独检查
            // 全量遍历无法区分字段来源，需单独处理 tool 消息以设置 tool_output_escalation 标志
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
        // 缺口1：规则引擎异常 → 强制 S2，记录 rule_engine_failed=true
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
        // 缺口1：未知异常 → 强制 S2，记录 rule_engine_failed=true
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
    result.model_confidence = "high";  // 规则引擎置信度固定为 high

    // ---- 本地模型辅助判断 ----
    // 仅在 is_internal_inspection 为 false 时才调用模型
    // rule_engine_only 为 true 时禁止调用本地模型
    if (!ctx.is_internal_inspection &&
        !ctx.rule_engine_only &&
        config_.use_local_model_fallback &&
        max_level == SensitivityLevel::S0 &&
        model_config_ != nullptr)
    {
        // 提取文本摘要（不含原始敏感内容，仅用于辅助判断）
        // 获取模型句柄和上下文窗口大小，计算最大输入 tokens
        // 保留 1/5 给模型输出，输入最多使用 4/5 的上下文窗口
        // 修复：使用 GetDefaultModelHandle() 确保始终使用 default 模型
        auto handle_for_extract = model_config_->GetDefaultModelHandle().lock();
        size_t max_input_tokens = 0;
        if (handle_for_extract) {
            int ctx_size = model_config_->context_size();
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
            // 查询 LRU 缓存（以内容哈希为 key，不缓存原文）
            std::string cache_key = ComputeCacheKey(text_summary);
            CacheEntry cached_entry;

            total_request_count_.fetch_add(1);

            if (CacheLookup(cache_key, cached_entry))
            {
                // 缓存命中
                consecutive_miss_count_.store(0); // 命中：重置连续 miss 计数
                My_Log{} << "[ContentSecurityInspector] LocalModel cache hit, level="
                         << to_string(cached_entry.level) << std::endl;
                max_level = MaxLevel(max_level, cached_entry.level);
                result.model_confidence = "med";  // 缓存结果置信度为 med
                if (cached_entry.level != SensitivityLevel::S0)
                {
                    result.summary_reason = "local_model_cached: " + cached_entry.reason;
                }
            }
            else
            {
                // 缓存 Miss 告警
                int miss_count = consecutive_miss_count_.fetch_add(1) + 1;
                int total = total_request_count_.load();
                if (miss_count >= CACHE_MISS_WARNING_THRESHOLD && total > CACHE_MISS_WARNING_THRESHOLD) {
                    // 改为 kWarning：cache_miss 是诊断性告警，不是真正的错误
                    My_Log{My_Log::Level::kWarning}
                        << "[ContentSecurityInspector] cache_miss_warning: consecutive_miss="
                        << miss_count << ", total_requests=" << total << std::endl;
                    // 重置计数，避免重复告警
                    consecutive_miss_count_.store(0);
                }

                // 缓存未命中，调用本地模型
                SensitivityLevel model_level = SensitivityLevel::S0;
                std::string model_reason;

                bool model_ok = LocalModelInspect(
                    text_summary,
                    config_.timeout_ms,
                    model_level,
                    model_reason);

                if (model_ok)
                {
                    // 缓存结果（不缓存原文，仅缓存哈希→结果映射）
                    CacheInsert(cache_key, {model_level, model_reason});

                    My_Log{} << "[ContentSecurityInspector] LocalModel result: level="
                             << to_string(model_level) << ", reason=" << model_reason << std::endl;

                    // strict_s2_union=true 时：规则引擎与模型结果取并集（更严格）
                    // strict_s2_union=false 时：仅在规则引擎无命中时采用模型结果
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
                    // =========================================================
                    // 缺口2：本地模型辅助判定失败 → 保守降级策略
                    // 将规则引擎结果提升一级（S0→S1，S1→S2）
                    // 若规则引擎也未命中（max_level==S0），强制升级为 S1
                    // 依据：§七-A 降级策略 - 敏感检测辅助失败时的保守处理
                    // =========================================================
                    SensitivityLevel elevated_level;
                    if (max_level == SensitivityLevel::S0)
                        elevated_level = SensitivityLevel::S1;  // S0 → S1（规则引擎无命中时强制 S1）
                    else if (max_level == SensitivityLevel::S1)
                        elevated_level = SensitivityLevel::S2;  // S1 → S2
                    else
                        elevated_level = SensitivityLevel::S2;  // S2 保持不变

                    My_Log{My_Log::Level::kError}
                        << "[ContentSecurityInspector] LocalModel failed: " << model_reason
                        << ". Elevating sensitivity from " << to_string(max_level)
                        << " to " << to_string(elevated_level)
                        << " (conservative fallback per §七-A)" << std::endl;

                    max_level = elevated_level;
                    result.model_confidence = "low";
                    result.summary_reason = "local_model_failed_elevated: " + model_reason
                        + " (elevated to " + to_string(elevated_level) + ")";

                    // 将保守升级结果写入缓存，避免后续请求重复触发超时形成恶性循环。
                    // 缓存 TTL 内不再重试本地模型，直接使用保守结果（elevated_level）。
                    CacheInsert(cache_key, {elevated_level, "local_model_timeout_fallback"});
                }
            }
        }
    }
    else
    {
        // 本地模型辅助判断被跳过，记录原因
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

    // 生成摘要原因（若尚未由模型辅助判断填充）
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

    // 统一日志输出格式：
    // - S0 且无模型辅助结果：输出 "No sensitive content detected by rule engine"
    // - S0 且有模型辅助结果（local_model: ...）：额外输出 "No sensitive content detected by rule engine"
    // - S1/S2：输出 summary_reason（规则引擎或模型辅助结果）
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

// ============================================================
// ScanAndCollect：扫描并收集命中片段
// ============================================================
void ContentSecurityInspector::ScanAndCollect(
        const std::string &text,
        bool from_tool,
        const std::string &field_path,
        std::vector<SensitiveSpan> &spans,
        std::vector<std::string> &cats,
        std::vector<std::string> &ids)
{
    if (text.empty()) return;

    // [PERF] 多行文本逐行扫描：与 ScanText 相同的优化策略。
    // 关键区别：ScanAndCollect 需要产出 SensitiveSpan，其 start/end 是原始文本的字节偏移。
    // 逐行扫描时，m.position() 是行内偏移，需加上行起始位置 line_offset 才是原始偏移。
    // 单行文本（无换行符）直接走原有路径，无额外开销。
    const bool is_multiline = (text.find('\n') != std::string::npos);

    for (const auto &rule : rules_) {
        try {
            // [PERF] 快速预检：与 RunRuleEngine 相同的策略
            if (!rule.fast_reject_str.empty() &&
                text.find(rule.fast_reject_str) == std::string::npos) {
                continue;
            }

            bool is_bank_card_rule = (rule.rule_id == "R_BANK_CARD_KW" ||
                                      rule.rule_id == "R_BANK_CARD_FMT");

            if (is_multiline) {
                // 逐行扫描：每行单独跑正则，用 line_offset 修正 span 偏移量
                std::string::size_type line_start = 0;
                while (line_start <= text.size()) {
                    std::string::size_type line_end_pos = text.find('\n', line_start);
                    if (line_end_pos == std::string::npos) line_end_pos = text.size();
                    // 去掉行尾 \r
                    std::string::size_type line_content_end = line_end_pos;
                    if (line_content_end > line_start && text[line_content_end - 1] == '\r')
                        --line_content_end;

                    if (line_content_end > line_start) {
                        const std::string line = text.substr(line_start, line_content_end - line_start);
                        const size_t line_offset = line_start;  // 行在原始文本中的起始偏移

                        for (auto it = std::sregex_iterator(line.begin(), line.end(), rule.pattern);
                             it != std::sregex_iterator(); ++it) {
                            const auto &m = *it;
                            const std::string &raw = m.str();

                            if (is_bank_card_rule && !MatchPassesLuhn(raw)) continue;
                            if (IsDesensitizedPlaceholder(raw, rule.entity_type)) continue;

                            SensitiveSpan sp;
                            sp.type = rule.entity_type;
                            sp.rule_id = rule.rule_id;
                            // 关键：start/end 加上行偏移，转换为原始文本偏移
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
                // 单行路径：原有逻辑不变
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

// ============================================================
// ScanKeywordsDict：扫描关键词词典
// ============================================================
SensitivityLevel ContentSecurityInspector::ScanKeywordsDict(
        const std::string &text,
        std::vector<std::string> &hit_categories,
        std::vector<std::string> &hit_rule_ids,
        const std::string &field_path,
        std::vector<SensitiveSpan> *out_spans)
{
    // 注意：调用方需持有 keywords_mutex_ 锁
    SensitivityLevel max_level = SensitivityLevel::S0;
    if (text.empty() || keywords_rules_.empty()) return max_level;

    // 生成小写版本（用于英文关键词大小写不敏感匹配）
    std::string text_lower = text;
    std::transform(text_lower.begin(), text_lower.end(), text_lower.begin(),
                   [](unsigned char c) { return std::tolower(c); });

    for (const auto &kw_rule : keywords_rules_) {
        size_t hit_pos = std::string::npos;
        if (kw_rule.is_chinese) {
            // 中文关键词：直接 find
            hit_pos = text.find(kw_rule.keyword);
        } else {
            // 英文关键词：大小写不敏感匹配，并检查单词边界（避免子串误报，如 "archives" 匹配 "HIV"）
            size_t search_from = 0;
            const size_t kw_len = kw_rule.keyword_lower.size();
            while (search_from < text_lower.size()) {
                size_t pos = text_lower.find(kw_rule.keyword_lower, search_from);
                if (pos == std::string::npos) break;

                // 单词边界检查：命中位置前后的字符不能是字母/数字/下划线
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

            // 若 out_spans != nullptr，追加 SensitiveSpan（KEYWORD 类型）
            if (out_spans != nullptr) {
                SensitiveSpan sp;
                sp.type       = SensitiveEntityType::KEYWORD;
                sp.rule_id    = kw_rule.rule_id;
                sp.start      = hit_pos;
                sp.end        = hit_pos + kw_rule.keyword.size();
                sp.matched    = kw_rule.keyword;  // 仅内存，严禁写入日志
                sp.from_tool_output = false;       // 关键词词典不区分 tool 来源
                sp.field_path = field_path;
                out_spans->push_back(std::move(sp));
            }

            // 若 debug_log_matches=true，输出命中的关键词、等级和上下文（前后各30字符）
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

// ============================================================
// LoadKeywordsDict：从文件加载关键词词典
// ============================================================
bool ContentSecurityInspector::LoadKeywordsDict(const std::string &path)
{
    if (path.empty()) return false;

    try {
        std::ifstream f(path);
        if (!f.is_open()) {
            // 文件不存在或无法打开时输出 Warning（便于排查路径配置问题）
            My_Log{My_Log::Level::kWarning}
                << "[ContentSecurityInspector] Keywords dict file not found or cannot be opened: "
                << path << " (keywords detection disabled)" << std::endl;
            return false;
        }

        json dict_json;
        f >> dict_json;

        std::vector<KeywordRule> new_rules;

        // 辅助 lambda：将单个关键词字符串构建为 KeywordRule
        auto build_kw_rule = [](const std::string &keyword,
                                const std::string &rule_id,
                                const std::string &category,
                                const std::string &level_str) -> KeywordRule {
            KeywordRule kw;
            kw.keyword = keyword;
            kw.rule_id = rule_id;
            kw.category = category;
            kw.level = (level_str == "S2") ? SensitivityLevel::S2 : SensitivityLevel::S1;

            // 判断是否为中文关键词（简单判断：含非 ASCII 字符）
            kw.is_chinese = false;
            for (unsigned char c : kw.keyword) {
                if (c > 127) { kw.is_chinese = true; break; }
            }

            // 生成小写版本（用于英文关键词大小写不敏感匹配）
            kw.keyword_lower = kw.keyword;
            std::transform(kw.keyword_lower.begin(), kw.keyword_lower.end(),
                           kw.keyword_lower.begin(),
                           [](unsigned char c) { return std::tolower(c); });
            return kw;
        };

        // ----------------------------------------------------------------
        // 支持两种格式（向后兼容）：
        //
        // 格式 A（扁平数组，旧格式）：
        //   [
        //     {"rule_id": "KW_001", "category": "MEDICAL", "level": "S1", "keyword": "病历"},
        //     ...
        //   ]
        //
        // 格式 B（分组对象，新格式，与 sensitive_keywords.json 一致）：
        //   {
        //     "rules": [
        //       {"id": "KW_MEDICAL_001", "category": "MEDICAL", "level": "S1",
        //        "keywords": ["病历", "诊断结果", ...]},
        //       ...
        //     ]
        //   }
        // ----------------------------------------------------------------
        if (dict_json.is_array()) {
            // 格式 A：顶层为数组，每条含单个 "keyword" 字段
            for (const auto &item : dict_json) {
                if (!item.contains("keyword") || !item["keyword"].is_string()) continue;

                std::string keyword  = item["keyword"].get<std::string>();
                std::string rule_id  = item.value("rule_id", "KW_" + keyword.substr(0, 8));
                std::string category = item.value("category", "KEYWORD");
                std::string level    = item.value("level", "S1");

                new_rules.push_back(build_kw_rule(keyword, rule_id, category, level));
            }
        } else if (dict_json.is_object() && dict_json.contains("rules") && dict_json["rules"].is_array()) {
            // 格式 B：顶层为对象含 "rules" 数组，每条含 "keywords" 数组
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

        // 按关键词长度降序排序（优先命中更长的词，降低短词误报）
        std::sort(new_rules.begin(), new_rules.end(),
                  [](const KeywordRule &a, const KeywordRule &b) {
                      return a.keyword.size() > b.keyword.size();
                  });

        // 原子替换词典（mutex 保护）
        {
            std::lock_guard<std::mutex> lock(keywords_mutex_);
            keywords_rules_ = std::move(new_rules);
            keywords_dict_loaded_ = true;
        }

        // 同步更新 last_keywords_mtime_，避免构造函数初始加载后
        // 在第一次请求时被 CheckAndReloadKeywordsDict 误判为"文件已变化"而触发不必要的热重载。
        // 原因：LoadKeywordsDict 之前只设置 keywords_dict_loaded_=true，但不更新 last_keywords_mtime_，
        // 导致 CheckAndReloadKeywordsDict 中 file_mtime > last_keywords_mtime_(epoch) 始终成立，
        // 每次到达 reload interval 都会触发一次多余的热重载（即使文件未变化）。
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

// ============================================================
// CheckAndReloadKeywordsDict：检查并热更新关键词词典
// ============================================================
void ContentSecurityInspector::CheckAndReloadKeywordsDict(InspectionResult &result)
{
    if (keywords_dict_path_.empty()) return;

    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
        now - last_keywords_check_time_).count();

    if (elapsed < keywords_reload_interval_seconds_) return;

    last_keywords_check_time_ = now;

    // 检查文件 mtime
    struct stat st;
    if (stat(keywords_dict_path_.c_str(), &st) != 0) {
        // 文件不存在，不报错
        return;
    }

    // 将 mtime 转换为 system_clock::time_point 进行比较
    auto file_mtime = std::chrono::system_clock::from_time_t(st.st_mtime);

    if (keywords_dict_loaded_ && file_mtime <= last_keywords_mtime_) {
        // 文件未变化，无需重载
        return;
    }

    // 文件有变化，重载
    bool loaded = LoadKeywordsDict(keywords_dict_path_);
    if (loaded) {
        last_keywords_mtime_ = file_mtime;
        result.keywords_dict_reloaded = true;
        result.keywords_dict_rules_count = GetKeywordsRulesCount();
        My_Log{} << "[ContentSecurityInspector] Keywords dict hot-reloaded, rules_count="
                 << result.keywords_dict_rules_count << std::endl;
    }
}

// ============================================================
// GetKeywordsRulesCount：获取当前关键词词典规则数量
// ============================================================
int ContentSecurityInspector::GetKeywordsRulesCount() const
{
    std::lock_guard<std::mutex> lock(keywords_mutex_);
    return static_cast<int>(keywords_rules_.size());
}

// ============================================================
// IsToolOutputField：判断字段路径是否来自 tool 角色消息
// ============================================================
bool ContentSecurityInspector::IsToolOutputField(const json &request, const std::string &field_path)
{
    // field_path 格式如 "/messages/2/content"
    // 解析路径：/messages/{index}/...
    if (field_path.empty()) return false;

    // 简单解析：检查路径是否以 /messages/ 开头
    static const std::string messages_prefix = "/messages/";
    if (field_path.substr(0, messages_prefix.size()) != messages_prefix) return false;

    // 提取消息索引
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
