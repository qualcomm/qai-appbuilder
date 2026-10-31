//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "desensitizer.h"
#include "../../context/context_base.h"
#include "security_prompt_builder.h"
#include "security_utils.h"
#include "log.h"
#include <algorithm>
#include <sstream>
#include <functional>
#include <thread>
#include <future>
#include <chrono>
#include <atomic>
#include <random>
#include <iomanip>

Desensitizer::Desensitizer(const RoutingConfig::DesensitizationConfig &config,
                            IModelConfig *model_config)
        : config_(config), model_config_(model_config)
{
}

std::string Desensitizer::MaskPhone(const std::string &text)
{

    static const std::regex phone_re(R"(\b(1[3-9]\d{2})\d{3}(\d{4})\b)");
    return std::regex_replace(text, phone_re, "$1***$2");
}

std::string Desensitizer::MaskEmail(const std::string &text)
{

    static const std::regex email_re(R"(([a-zA-Z0-9])[a-zA-Z0-9._%+\-]*(@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}))");
    return std::regex_replace(text, email_re, "$1***$2");
}

std::string Desensitizer::MaskBankCard(const std::string &text)
{

    static const std::regex bank_re(R"(\b(\d{4})\d{8,11}(\d{4})\b)");
    return std::regex_replace(text, bank_re, "$1********$2");
}

std::string Desensitizer::MaskIdCard(const std::string &text)
{

    static const std::regex id_re(R"(\b(\d{3})\d{11}(\d{3}[\dXx])\b)");
    return std::regex_replace(text, id_re, "$1***********$2");
}

std::string Desensitizer::MaskApiKey(const std::string &text)
{

    static const std::regex api_key_re(R"(sk-[a-zA-Z0-9]{20,})");
    return std::regex_replace(text, api_key_re, "<API_KEY>");
}

std::string Desensitizer::MaskToken(const std::string &text)
{

    static const std::regex token_re(R"(Bearer\s+[a-zA-Z0-9._\-]{20,})");
    return std::regex_replace(text, token_re, "Bearer <TOKEN>");
}

std::string Desensitizer::DeletePrivateKey(const std::string &text)
{

    static const std::regex private_key_re(
        R"(-----BEGIN\s+\S*\s*PRIVATE\s+KEY-----[\s\S]*?-----END\s+\S*\s*PRIVATE\s+KEY-----)"
    );
    return std::regex_replace(text, private_key_re, "<PRIVATE_KEY_REMOVED>");
}

std::string Desensitizer::MaskPassword(const std::string &text)
{

    static const std::regex pwd_val_re(
        R"(((?:password|passwd|secret)\s*=\s*)\S+)",
        std::regex::icase
    );
    return std::regex_replace(text, pwd_val_re, "$1<PASSWORD>");
}

std::string Desensitizer::MaskInternalUrl(const std::string &text)
{

    static const std::regex ip_re(
        R"(\b(?:192\.168\.\d{1,3}\.\d{1,3}|)"
        R"(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|)"
        R"(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b)"
    );
    std::string result = std::regex_replace(text, ip_re, "<INTERNAL_IP>");

    static const std::regex domain_re(
        R"(\b[^\s/\\.]+\.(?:internal|local|corp|intranet|lan)\b)",
        std::regex::icase
    );
    return std::regex_replace(result, domain_re, "<INTERNAL_DOMAIN>");
}

std::string Desensitizer::MaskLocalPath(const std::string &text)
{

    static const std::regex win_path_re(
        R"([A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][^\s<]+)"
    );
    std::string result = std::regex_replace(text, win_path_re, "<LOCAL_PATH>");

    static const std::regex unix_path_re(
        R"(/(?:home|Users)/[^/<\s]{1,}(?:/[^\s<]*)?)"
    );
    return std::regex_replace(result, unix_path_re, "<LOCAL_PATH>");
}

std::string Desensitizer::MaskDeviceId(const std::string &text)
{

    static const std::regex mac_re(
        R"(\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b)"
    );
    std::string result = std::regex_replace(text, mac_re, "<MAC_ADDR>");

    static const std::regex imei_re(
        R"(((?:IMEI|imei|设备号)\s*[：:=]?\s*)\d{15})",
        std::regex::icase
    );
    return std::regex_replace(result, imei_re, "$1<IMEI>");
}

std::string Desensitizer::MaskImageData(const std::string &text)
{

    static const std::regex image_data_re(
        R"(data:image/(?:jpeg|jpg|png|gif|webp|bmp|svg\+xml);base64,[A-Za-z0-9+/=]+)",
        std::regex::icase
    );
    return std::regex_replace(text, image_data_re, "<IMAGE_DATA_REMOVED>");
}

std::string Desensitizer::ProcessText(const std::string &text)
{
    if (text.empty())
        return text;

    std::string result = text;

    bool has_mask = false;
    bool has_placeholder = false;
    bool has_delete = false;

    for (const auto &strategy : config_.strategies)
    {
        if (strategy == "mask") has_mask = true;
        else if (strategy == "placeholder") has_placeholder = true;
        else if (strategy == "delete") has_delete = true;
    }

    if (has_delete)
    {
        if (config_.entity_switches.enable_private_key)
            result = DeletePrivateKey(result);
    }

    if (has_placeholder)
    {
        if (config_.entity_switches.enable_api_key)
            result = MaskApiKey(result);
        if (config_.entity_switches.enable_token)
            result = MaskToken(result);
    }

    if (has_mask)
    {
        if (config_.entity_switches.enable_phone)
            result = MaskPhone(result);
        if (config_.entity_switches.enable_email)
            result = MaskEmail(result);
        if (config_.entity_switches.enable_bank_card)
            result = MaskBankCard(result);
        if (config_.entity_switches.enable_id_card)
            result = MaskIdCard(result);
        if (config_.entity_switches.enable_password)
            result = MaskPassword(result);
        if (config_.entity_switches.enable_internal_url)
            result = MaskInternalUrl(result);
        if (config_.entity_switches.enable_local_path)
            result = MaskLocalPath(result);
        if (config_.entity_switches.enable_device_id)
            result = MaskDeviceId(result);
        if (config_.entity_switches.enable_image_data)
            result = MaskImageData(result);
    }

    return result;
}

json Desensitizer::ProcessMessageContent(const json &content)
{
    if (content.is_string())
    {
        return ProcessText(content.get<std::string>());
    }
    else if (content.is_array())
    {
        json result = content;
        for (auto &part : result)
        {
            if (part.contains("type") && part["type"] == "text" &&
                part.contains("text") && part["text"].is_string())
            {
                part["text"] = ProcessText(part["text"].get<std::string>());
            }
        }
        return result;
    }
    return content;
}

json Desensitizer::ProcessMessages(const json &messages)
{
    if (!messages.is_array())
        return messages;

    json result = messages;

    std::function<json(const json&)> process_recursive = [&](const json &j) -> json {
        if (j.is_string())
        {
            return ProcessText(j.get<std::string>());
        }
        if (j.is_array())
        {
            json arr = json::array();
            for (const auto &item : j)
            {
                arr.push_back(process_recursive(item));
            }
            return arr;
        }
        if (j.is_object())
        {
            json obj = json::object();
            for (const auto &el : j.items())
            {
                obj[el.key()] = process_recursive(el.value());
            }
            return obj;
        }
        return j;
    };

    for (auto &msg : result)
    {

        if (msg.contains("content"))
        {
            msg["content"] = ProcessMessageContent(msg["content"]);
        }

        if (msg.contains("tool_calls") && msg["tool_calls"].is_array())
        {
            for (auto &tc : msg["tool_calls"])
            {
                if (tc.contains("function"))
                {
                    auto &func = tc["function"];
                    if (func.contains("arguments"))
                    {
                        if (func["arguments"].is_string())
                        {
                            func["arguments"] = ProcessText(func["arguments"].get<std::string>());
                        }
                        else if (func["arguments"].is_object() || func["arguments"].is_array())
                        {

                            func["arguments"] = process_recursive(func["arguments"]);
                        }
                    }
                }
            }
        }
    }
    return result;
}

static std::string EntityTypeToString(SensitiveEntityType t)
{
    switch (t) {
        case SensitiveEntityType::PHONE:          return "PHONE";
        case SensitiveEntityType::EMAIL:          return "EMAIL";
        case SensitiveEntityType::IDCARD:         return "IDCARD";
        case SensitiveEntityType::BANKCARD:       return "BANKCARD";
        case SensitiveEntityType::API_KEY:        return "API_KEY";
        case SensitiveEntityType::TOKEN:          return "TOKEN";
        case SensitiveEntityType::PRIVATE_KEY:    return "PRIVATE_KEY";
        case SensitiveEntityType::PASSWORD_VALUE: return "PASSWORD_VALUE";
        case SensitiveEntityType::PASSWORD_KW:    return "PASSWORD_KW";
        case SensitiveEntityType::INTERNAL_URL:   return "INTERNAL_URL";
        case SensitiveEntityType::PATH:           return "PATH";
        case SensitiveEntityType::DEVICE_ID:      return "DEVICE_ID";
        case SensitiveEntityType::KEYWORD:        return "KEYWORD";
        case SensitiveEntityType::UNKNOWN:
        default:                                  return "SENSITIVE";
    }
}

static void WalkJsonStringFieldsWithPath(
        json &node,
        const std::string &current_path,
        const std::function<void(std::string &, const std::string &)> &visitor)
{
    if (node.is_string()) {
        std::string val = node.get<std::string>();
        visitor(val, current_path);
        node = val;
    } else if (node.is_object()) {
        for (auto &[key, child] : node.items()) {
            WalkJsonStringFieldsWithPath(child, current_path + "/" + key, visitor);
        }
    } else if (node.is_array()) {
        for (size_t i = 0; i < node.size(); ++i) {
            WalkJsonStringFieldsWithPath(node[i], current_path + "/" + std::to_string(i), visitor);
        }
    }
}

std::string Desensitizer::FormatPlaceholder(SensitiveEntityType t, int index) const
{
    std::string type_str = EntityTypeToString(t);
    std::string result = config_.placeholder_style;
    auto pos = result.find("{type}");
    if (pos != std::string::npos) result.replace(pos, 6, type_str);
    pos = result.find("{index}");
    if (pos != std::string::npos) result.replace(pos, 7, std::to_string(index));
    return result;
}

std::string Desensitizer::SummarizeText(const std::string &text, int timeout_ms)
{
    if (!model_config_) return "";

    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle) return "";

    const auto *default_instance_config = model_config_->GetDefaultInstanceConfig();
    const PromptType prompt_type = default_instance_config
            ? default_instance_config->get_prompt_type()
            : model_config_->get_prompt_type();
    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text:\n" + text);
    My_Log{} << "[Desensitizer] prompt format: "
             << (prompt_type == PromptType::Harmony ? "Harmony" : "General")
             << " for summarize" << std::endl;

    auto query_result = SecurityUtils::LocalModelQuery(
        handle, prompt,
                         "",
        timeout_ms,
                           0,
        [](const std::string& /*output_buf*/, const std::string& /*token*/, bool& /*json_complete*/) -> bool {
            return true;
        },
        "[Desensitizer]"
    );

    if (!query_result.success || query_result.output.empty()) return "";
    return query_result.output;
}

std::string Desensitizer::GenerateRandomAlnum(int len) const
{
    std::ostringstream oss;
    static const char charset[] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    static thread_local std::mt19937 rng(std::random_device{}());
    std::uniform_int_distribution<int> dist(0, sizeof(charset) - 2);
    for (int i = 0; i < len; ++i) {
        oss << charset[dist(rng)];
    }
    return oss.str();
}

std::string Desensitizer::FormatMockData(SensitiveEntityType t, int index, const std::string &matched) const
{
    switch (t) {
        case SensitiveEntityType::EMAIL:

            return "mock_user_" + std::to_string(index) + "@example.com";

        case SensitiveEntityType::PHONE:

            {
                std::ostringstream oss;
                oss << "1389" << std::setfill('0') << std::setw(7) << (index % 10000000);
                return oss.str();
            }

        case SensitiveEntityType::IDCARD:

            return "11010519900101123" + std::to_string(index % 10);

        case SensitiveEntityType::BANKCARD:

            {
                std::ostringstream oss;
                oss << "6222" << std::setfill('0') << std::setw(12) << (index % 1000000000000LL);
                return oss.str();
            }

        case SensitiveEntityType::API_KEY:

            return "sk-mock_" + std::to_string(index) + "_" + GenerateRandomAlnum(32);

        case SensitiveEntityType::TOKEN:

            return "Bearer mock_token_" + std::to_string(index) + "_" + GenerateRandomAlnum(32);

        case SensitiveEntityType::PRIVATE_KEY:

            {
                std::ostringstream oss;
                oss << "-----BEGIN RSA PRIVATE KEY-----\n";
                oss << "MockPrivateKeyData" << index << "\n";
                oss << "-----END RSA PRIVATE KEY-----";
                return oss.str();
            }

        case SensitiveEntityType::PASSWORD_VALUE:
        case SensitiveEntityType::PASSWORD_KW:

            return "mock_password_" + std::to_string(index);

        case SensitiveEntityType::INTERNAL_URL:

            return "192.168." + std::to_string((index / 256) % 256) + "." + std::to_string(index % 256);

        case SensitiveEntityType::PATH:

            {
                std::string mock_username = "mock_user_" + std::to_string(index);

                static const std::vector<std::string> user_prefixes = {
                    "\\Users\\", "/Users\\", "\\Users/", "/Users/",
                    "\\Documents and Settings\\", "/Documents and Settings/",
                    "/home/",
                };
                for (const auto &prefix : user_prefixes) {
                    size_t prefix_pos = matched.find(prefix);
                    if (prefix_pos != std::string::npos) {
                        size_t username_start = prefix_pos + prefix.size();

                        size_t username_end = matched.find_first_of("/\\", username_start);
                        if (username_end != std::string::npos) {

                            std::string result_path = matched.substr(0, username_start) + mock_username + matched.substr(username_end);

                            if (result_path.size() >= 2 &&
                                std::isalpha(static_cast<unsigned char>(result_path[0])) &&
                                result_path[1] == ':') {
                                for (char &c : result_path) if (c == '/') c = '\\';
                            }
                            return result_path;
                        } else {

                            return matched.substr(0, username_start) + mock_username;
                        }
                    }
                }

                #ifdef _WIN32
                    return "C:\\Users\\mock_user_" + std::to_string(index) + "\\Documents";
                #else
                    return "/home/mock_user_" + std::to_string(index) + "/Documents";
                #endif
            }

        case SensitiveEntityType::DEVICE_ID:

            {
                std::ostringstream oss;
                oss << "00:11:22:33:44:" << std::hex << std::setfill('0') << std::setw(2) << (index % 256);
                return oss.str();
            }

        case SensitiveEntityType::UNKNOWN:
        default:

            return FormatPlaceholder(t, index);
    }
}

bool Desensitizer::ShouldUseFormatPreserving(const json &request) const
{

    if (!config_.format_preserving_enabled) {
        return false;
    }

    if (!request.contains("tools") || !request["tools"].is_array() || request["tools"].empty()) {
        return false;
    }

    return true;
}

bool Desensitizer::ApplyStructuredPlaceholders(
        const json &original_request,
        const InspectionResult &inspection,
        DesensitizationResult &out,
        const std::unordered_map<std::string, std::string> *existing_mapping)
{
    out.desensitized_request = original_request;

    if (inspection.spans.empty()) {
        out.success = true;
        return true;
    }

    std::unordered_map<std::string, std::string> value2ph;
    std::unordered_map<int, int> typeIndex;

    if (existing_mapping && !existing_mapping->empty()) {
        static const std::regex email_re("^mock_user_(\\d+)@example\\.com$");
        static const std::regex phone_re("^1389(\\d{7})$");
        static const std::regex path_re_win("^[A-Za-z]:\\\\Users\\\\mock_user_(\\d+)\\\\Documents$");
        static const std::regex path_re_unix("^/home/mock_user_(\\d+)/Documents$");
        static const std::regex path_re_username("^mock_user_(\\d+)$");
        static const std::regex apikey_re("^sk-mock_(\\d+)_[a-zA-Z0-9]{32}$");
        static const std::regex token_re("^Bearer mock_token_(\\d+)_[a-zA-Z0-9]{32}$");
        static const std::regex mac_re("^00:11:22:33:44:[0-9a-f]{2}$");

        for (const auto &[mock, real] : *existing_mapping) {
            std::smatch m;
            if (std::regex_match(mock, m, email_re)) {
                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::EMAIL);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, m, phone_re)) {

                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::PHONE);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, m, path_re_win) ||
                       std::regex_match(mock, m, path_re_unix) ||
                       std::regex_match(mock, m, path_re_username)) {
                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::PATH);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, m, apikey_re)) {
                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::API_KEY);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, m, token_re)) {
                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::TOKEN);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, mac_re)) {

                std::string hex = mock.substr(mock.size() - 2);
                int idx = std::stoi(hex, nullptr, 16);
                int key = static_cast<int>(SensitiveEntityType::DEVICE_ID);
                typeIndex[key] = std::max(typeIndex[key], idx);
            }

        }

        if (!typeIndex.empty() && config_.log_desensitization_details) {
            My_Log{} << "[Desensitizer] [IterFix] Initialized typeIndex from existing_mapping ("
                     << existing_mapping->size() << " entries): ";
            for (const auto &[k, v] : typeIndex) {
                My_Log{} << "type=" << k << " maxIdx=" << v << " ";
            }
            My_Log{} << std::endl;
        }
    }

    std::vector<std::tuple<std::string, std::string, std::string>> desensitization_log;

    auto make_placeholder = [&](SensitiveEntityType t, const std::string &val) -> std::string {
        auto it = value2ph.find(val);
        if (it != value2ph.end()) return it->second;
        int idx = ++typeIndex[static_cast<int>(t)];

        std::string ph;
        if (out.use_format_preserving) {
            ph = FormatMockData(t, idx, val);
        } else {
            ph = FormatPlaceholder(t, idx);
        }

        value2ph[val] = ph;

        std::string log_original_val;

        if (t == SensitiveEntityType::PATH && out.use_format_preserving) {

            const std::string mu_prefix = "mock_user_";
            size_t mu_pos = ph.find(mu_prefix);
            if (mu_pos != std::string::npos) {
                size_t mu_end = ph.find_first_of("/\\", mu_pos);
                std::string mock_username = (mu_end != std::string::npos)
                    ? ph.substr(mu_pos, mu_end - mu_pos)
                    : ph.substr(mu_pos);

                if (!mock_username.empty() && val.size() > mu_pos) {
                    size_t ru_end = val.find_first_of("/\\", mu_pos);
                    std::string real_username = (ru_end != std::string::npos)
                        ? val.substr(mu_pos, ru_end - mu_pos)
                        : val.substr(mu_pos);
                    if (!real_username.empty()) {
                        out.mapping_table[mock_username] = real_username;

                        size_t val_mu_pos = val.find(mu_prefix);
                        if (val_mu_pos != std::string::npos) {

                            size_t val_mu_end = val.find_first_of("/\\", val_mu_pos);
                            std::string val_mock_username = (val_mu_end != std::string::npos)
                                ? val.substr(val_mu_pos, val_mu_end - val_mu_pos)
                                : val.substr(val_mu_pos);

                            if (existing_mapping) {
                                auto eit = existing_mapping->find(val_mock_username);
                                if (eit != existing_mapping->end()) {

                                    log_original_val = val.substr(0, val_mu_pos) + eit->second
                                        + (val_mu_end != std::string::npos ? val.substr(val_mu_end) : "");
                                }
                            }
                        }

                        if (log_original_val.empty()) {
                            log_original_val = val;
                        }
                    }
                }
            }

            if (log_original_val.empty()) {
                log_original_val = val;
            }
        } else {
            out.mapping_table[ph] = val;

            auto mit = out.mapping_table.find(val);
            log_original_val = (mit != out.mapping_table.end()) ? mit->second : val;
        }

        desensitization_log.push_back(std::make_tuple(EntityTypeToString(t), log_original_val, ph));

        if (config_.log_desensitization_details) {
            My_Log{} << "[Desensitizer] [DIAG] Generated mapping: type=" << EntityTypeToString(t)
                     << ", original=" << log_original_val << ", mock=" << ph << std::endl;
        }

        return ph;
    };

    std::unordered_map<std::string, std::vector<const SensitiveSpan*>> spans_by_field;
    for (const auto &sp : inspection.spans) {
        spans_by_field[sp.field_path].push_back(&sp);
    }
    for (auto &[path, field_spans] : spans_by_field) {

        std::sort(field_spans.begin(), field_spans.end(),
                  [](const SensitiveSpan *a, const SensitiveSpan *b) {
                      return a->start > b->start;
                  });
    }

    WalkJsonStringFieldsWithPath(out.desensitized_request, "",
        [&](std::string &s, const std::string &path) {
            auto it = spans_by_field.find(path);
            if (it != spans_by_field.end()) {
                size_t last_replaced_start = std::string::npos;
                for (const auto *sp : it->second) {

                    if (last_replaced_start != std::string::npos && sp->end > last_replaced_start) {
                        continue;
                    }
                    if (sp->end <= s.size()) {
                        std::string ph = make_placeholder(sp->type, sp->matched);
                        s.replace(sp->start, sp->end - sp->start, ph);
                        last_replaced_start = sp->start;
                    }
                }
            }
        });

    if (config_.log_desensitization_details) {
        My_Log{} << "[Desensitizer] [DIAG] Final mapping_table size=" << out.mapping_table.size() << std::endl;
        if (!out.mapping_table.empty()) {
            My_Log{} << "[Desensitizer] [DIAG] Sample mapping (first entry): "
                     << out.mapping_table.begin()->first << " -> " << out.mapping_table.begin()->second << std::endl;
        }
    }

    if (!desensitization_log.empty() && config_.log_desensitization_details) {
        My_Log{} << "[Desensitizer] Desensitization mapping ("
                 << (out.use_format_preserving ? "format-preserving" : "placeholder")
                 << " mode):" << std::endl;
        for (const auto &[type, original, mock] : desensitization_log) {
            My_Log{} << "  [" << type << "] " << original << " → " << mock << std::endl;
        }
    }

    out.applied_strategies.push_back("structured_placeholder");
    out.success = true;
    return true;
}

DesensitizationResult Desensitizer::Apply(
        const json &original_request,
        const InspectionResult &inspection,
        const std::unordered_map<std::string, std::string> *existing_mapping)
{
    DesensitizationResult result;
    result.success = false;

    result.use_format_preserving = ShouldUseFormatPreserving(original_request);

    if (!config_.enabled)
    {
        result.success = true;
        result.desensitized_request = original_request;
        result.failure_reason = "desensitization disabled";
        return result;
    }

    if (inspection.sensitivity_level == SensitivityLevel::S0)
    {
        result.success = true;
        result.desensitized_request = original_request;
        return result;
    }

    try
    {

        bool use_structured = false;
        bool has_summarize = false;

        if (result.use_format_preserving) {
            use_structured = true;
            My_Log{} << "[Desensitizer] Format-preserving mode enabled, forcing structured_placeholder strategy" << std::endl;
        }

        for (const auto &strategy : config_.strategies) {
            if (!result.use_format_preserving && strategy == "structured_placeholder") use_structured = true;
            if (strategy == "summarize") has_summarize = true;
        }

        if (use_structured && !inspection.spans.empty()) {

            if (config_.log_desensitization_details) {
                My_Log{} << "[Desensitizer] ===== BEFORE DESENSITIZATION =====" << std::endl;
                if (original_request.contains("messages") && original_request["messages"].is_array()) {
                    int msg_count = 0;
                    for (const auto &msg : original_request["messages"]) {
                        if (msg.contains("content")) {
                            std::string content_preview;
                            if (msg["content"].is_string()) {
                                content_preview = msg["content"].get<std::string>();
                            } else if (msg["content"].is_array()) {
                                content_preview = msg["content"].dump();
                            }

                            if (content_preview.length() > 200) {
                                content_preview = content_preview.substr(0, 200) + "...";
                            }
                            My_Log{} << "  Message[" << msg_count << "]: " << content_preview << std::endl;
                            msg_count++;
                        }
                    }
                }
                My_Log{} << "[Desensitizer] ========================================" << std::endl;
            }

            bool ok = ApplyStructuredPlaceholders(original_request, inspection, result, existing_mapping);
            if (!ok) {
                result.success = false;
                result.failure_reason = "structured_placeholder_failed";
                return result;
            }

            if (config_.log_desensitization_details) {
                My_Log{} << "[Desensitizer] ===== AFTER DESENSITIZATION =====" << std::endl;
                if (result.desensitized_request.contains("messages") && result.desensitized_request["messages"].is_array()) {
                    int msg_count = 0;
                    for (const auto &msg : result.desensitized_request["messages"]) {
                        if (msg.contains("content")) {
                            std::string content_preview;
                            if (msg["content"].is_string()) {
                                content_preview = msg["content"].get<std::string>();
                            } else if (msg["content"].is_array()) {
                                content_preview = msg["content"].dump();
                            }

                            if (content_preview.length() > 200) {
                                content_preview = content_preview.substr(0, 200) + "...";
                            }
                            My_Log{} << "  Message[" << msg_count << "]: " << content_preview << std::endl;
                            msg_count++;
                        }
                    }
                }
                My_Log{} << "[Desensitizer] ========================================" << std::endl;
            }

            if (has_summarize && model_config_) {
                int timeout_ms = config_.summarize_timeout_ms;
                WalkJsonStringFieldsWithPath(result.desensitized_request, "",
                    [&](std::string &s, const std::string & /*path*/) {

                        if (s.size() > 500) {
                            std::string summarized = SummarizeText(s, timeout_ms);
                            if (!summarized.empty()) {
                                s = summarized;
                            }

                        }
                    });
                result.applied_strategies.push_back("summarize");
            }
            else
            {

                std::string skip_reason;
                if (!has_summarize)
                    skip_reason = "summarize not in strategies list";
                else if (!model_config_)
                    skip_reason = "model_config=null";
                else
                    skip_reason = "unknown";
                My_Log{} << "[Desensitizer] LocalModel (summarize) skipped: " << skip_reason << std::endl;
            }

            My_Log{} << "[Desensitizer] Applied structured_placeholder strategy, "
                     << "mapping_table_size=" << result.mapping_table.size() << std::endl;
        } else {

            json desensitized = original_request;

            if (desensitized.contains("messages"))
            {
                desensitized["messages"] = ProcessMessages(desensitized["messages"]);
            }

            for (const auto &strategy : config_.strategies) {
                if (strategy == "mask" || strategy == "placeholder" || strategy == "delete") {
                    result.applied_strategies.push_back(strategy);
                }
            }
            result.success = true;
            result.desensitized_request = desensitized;

            My_Log{} << "[Desensitizer] Applied legacy strategies: ";
            for (const auto &s : result.applied_strategies) {
                My_Log{}.original(true) << s << " ";
            }
            My_Log{}.original(true) << std::endl;
        }
    }
    catch (const std::exception &e)
    {
        result.success = false;
        result.failure_reason = std::string("Desensitization failed: ") + e.what();
        result.desensitized_request = original_request;
        My_Log{My_Log::Level::kError} << "[Desensitizer] " << result.failure_reason << std::endl;
    }

    return result;
}
