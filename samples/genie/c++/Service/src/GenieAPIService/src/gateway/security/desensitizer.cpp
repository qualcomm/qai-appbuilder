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
    // 手机号：1[3-9]\d{9}（11位）→ 1891***9517（前4位 + *** + 后4位）
    // 与检测规则 R_PHONE_CN（\b1[3-9]\d{9}\b，11位）对齐：
    //   (1[3-9]\d{2}) = 前4位，\d{3} = 中间3位，(\d{4}) = 后4位，共 4+3+4=11位
    // Bug Fix: 原正则 \b(1[3-9]\d{2})\d{4}(\d{4})\b 为 4+4+4=12位，
    //   永远无法匹配11位手机号，导致脱敏空操作、检测规则反复命中、max_rounds 超限报错。
    static const std::regex phone_re(R"(\b(1[3-9]\d{2})\d{3}(\d{4})\b)");
    return std::regex_replace(text, phone_re, "$1***$2");
}

std::string Desensitizer::MaskEmail(const std::string &text)
{
    // 邮箱：a***@xx.com（保留首字母和域名）
    static const std::regex email_re(R"(([a-zA-Z0-9])[a-zA-Z0-9._%+\-]*(@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}))");
    return std::regex_replace(text, email_re, "$1***$2");
}

std::string Desensitizer::MaskBankCard(const std::string &text)
{
    // 银行卡号：保留后4位，其余用 * 替换
    static const std::regex bank_re(R"(\b(\d{4})\d{8,11}(\d{4})\b)");
    return std::regex_replace(text, bank_re, "$1********$2");
}

std::string Desensitizer::MaskIdCard(const std::string &text)
{
    // 身份证：保留前3位和后4位（末位可为X/x）
    // 与检测规则 R_ID_CARD（\b\d{17}[\dXx]\b）对齐：
    //   - 前3位数字 + 中间11位数字 + 后3位数字 + 末1位[\dXx]
    //   - 共18位，末位可为数字或X
    static const std::regex id_re(R"(\b(\d{3})\d{11}(\d{3}[\dXx])\b)");
    return std::regex_replace(text, id_re, "$1***********$2");
}

std::string Desensitizer::MaskApiKey(const std::string &text)
{
    // API Key：sk-xxx → <API_KEY>
    static const std::regex api_key_re(R"(sk-[a-zA-Z0-9]{20,})");
    return std::regex_replace(text, api_key_re, "<API_KEY>");
}

std::string Desensitizer::MaskToken(const std::string &text)
{
    // Bearer Token → Bearer <TOKEN>
    static const std::regex token_re(R"(Bearer\s+[a-zA-Z0-9._\-]{20,})");
    return std::regex_replace(text, token_re, "Bearer <TOKEN>");
}

std::string Desensitizer::DeletePrivateKey(const std::string &text)
{
    // 删除 PEM 私钥块（-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----）
    // 注意：MSVC 不支持 std::regex::multiline，使用默认 ECMAScript 模式
    // [\s\S]*? 可以跨行匹配
    static const std::regex private_key_re(
        R"(-----BEGIN\s+\S*\s*PRIVATE\s+KEY-----[\s\S]*?-----END\s+\S*\s*PRIVATE\s+KEY-----)"
    );
    return std::regex_replace(text, private_key_re, "<PRIVATE_KEY_REMOVED>");
}

std::string Desensitizer::MaskPassword(const std::string &text)
{
    // password=xxx → password=<PASSWORD>（保留关键词，替换值）
    static const std::regex pwd_val_re(
        R"(((?:password|passwd|secret)\s*=\s*)\S+)",
        std::regex::icase
    );
    return std::regex_replace(text, pwd_val_re, "$1<PASSWORD>");
}

std::string Desensitizer::MaskInternalUrl(const std::string &text)
{
    // RFC 1918 私有 IP 地址段 → <INTERNAL_IP>
    // 与检测规则 R_INTERNAL_IP 使用相同的正则，确保脱敏后验证通过
    static const std::regex ip_re(
        R"(\b(?:192\.168\.\d{1,3}\.\d{1,3}|)"
        R"(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|)"
        R"(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b)"
    );
    std::string result = std::regex_replace(text, ip_re, "<INTERNAL_IP>");

    // 内网域名后缀（.internal/.local/.corp/.intranet/.lan）→ <INTERNAL_DOMAIN>
    // 与检测规则 R_INTERNAL_DOMAIN 使用相同的正则（保持同步）
    // 修复：用 [^\s/\\.]+ 替代 \w[\w\-]*，消除路径文本中的灾难性回溯
    static const std::regex domain_re(
        R"(\b[^\s/\\.]+\.(?:internal|local|corp|intranet|lan)\b)",
        std::regex::icase
    );
    return std::regex_replace(result, domain_re, "<INTERNAL_DOMAIN>");
}

std::string Desensitizer::MaskLocalPath(const std::string &text)
{
    // 本地文件路径脱敏：与检测规则 R_PATH_WIN / R_PATH_UNIX 使用相同的正则
    // 确保脱敏后复检不再命中，避免 max_rounds 超限
    
    // Windows 用户目录路径：C:\Users\xxx\... 或 C:\Documents and Settings\xxx\...
    // 与 R_PATH_WIN 正则对齐，保持同步
    // 修复回溯：[^\s<]{3,} 改为 [^\s<]+，消除 {3,} 下界导致的回溯
    static const std::regex win_path_re(
        R"([A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][^\s<]+)"
    );
    std::string result = std::regex_replace(text, win_path_re, "<LOCAL_PATH>");

    // Unix/Linux/macOS 用户目录路径
    // 与 R_PATH_UNIX 正则对齐，保持同步
    static const std::regex unix_path_re(
        R"(/(?:home|Users)/[^/<\s]{1,}(?:/[^\s<]*)?)"
    );
    return std::regex_replace(result, unix_path_re, "<LOCAL_PATH>");
}

std::string Desensitizer::MaskDeviceId(const std::string &text)
{
    // 设备标识脱敏：与检测规则 R_MAC_ADDR / R_IMEI 使用相同的正则
    // 确保脱敏后复检不再命中，避免 max_rounds 超限
    
    // MAC 地址：XX:XX:XX:XX:XX:XX 或 XX-XX-XX-XX-XX-XX 格式
    // 与 R_MAC_ADDR 正则对齐：\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b
    static const std::regex mac_re(
        R"(\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b)"
    );
    std::string result = std::regex_replace(text, mac_re, "<MAC_ADDR>");

    // IMEI：需要上下文关键词（IMEI/imei/设备号）+ 15位数字
    // 与 R_IMEI 正则对齐：(?:IMEI|imei|设备号)\s*[：:=]?\s*\d{15}
    // 策略：保留关键词和分隔符，仅替换15位数字部分
    static const std::regex imei_re(
        R"(((?:IMEI|imei|设备号)\s*[：:=]?\s*)\d{15})",
        std::regex::icase
    );
    return std::regex_replace(result, imei_re, "$1<IMEI>");
}

std::string Desensitizer::MaskImageData(const std::string &text)
{
    // 图片数据脱敏：data URI 格式的内嵌图片
    // 与检测规则 R_BASE64_IMAGE 使用相同的正则前缀
    // 策略：删除整个 data URI（包括 base64 payload），替换为占位符
    
    // data:image/...;base64,<base64_payload>
    // 匹配从 data:image/ 开始到下一个空白字符或引号或尖括号为止
    // 与 R_BASE64_IMAGE 正则对齐（但需要匹配完整 payload）
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

    // 检查启用的策略
    bool has_mask = false;
    bool has_placeholder = false;
    bool has_delete = false;

    for (const auto &strategy : config_.strategies)
    {
        if (strategy == "mask") has_mask = true;
        else if (strategy == "placeholder") has_placeholder = true;
        else if (strategy == "delete") has_delete = true;
    }

    // 按策略顺序处理（各操作受 entity_switches 独立开关控制）
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

    // 递归处理 JSON 值中的字符串
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
        // 处理 content 字段
        if (msg.contains("content"))
        {
            msg["content"] = ProcessMessageContent(msg["content"]);
        }

        // 处理 tool_calls 的 function.arguments
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
                            // 递归处理结构化 arguments
                            func["arguments"] = process_recursive(func["arguments"]);
                        }
                    }
                }
            }
        }
    }
    return result;
}

// ============================================================
// EntityTypeToString：敏感实体类型转字符串
// ============================================================
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

// ============================================================
// WalkJsonStringFieldsWithPath：带路径的可写遍历
// ============================================================
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

// ============================================================
// FormatPlaceholder：格式化结构化占位符
// ============================================================
std::string Desensitizer::FormatPlaceholder(SensitiveEntityType t, int index) const
{
    std::string type_str = EntityTypeToString(t);
    std::string result = config_.placeholder_style; // e.g. "<{type}_{index}>"
    auto pos = result.find("{type}");
    if (pos != std::string::npos) result.replace(pos, 6, type_str);
    pos = result.find("{index}");
    if (pos != std::string::npos) result.replace(pos, 7, std::to_string(index));
    return result; // e.g. "<PHONE_1>"
}

// ============================================================
// SummarizeText：调用本地模型对文本进行脱敏重写
// ============================================================
std::string Desensitizer::SummarizeText(const std::string &text, int timeout_ms)
{
    if (!model_config_) return "";

    // 修复：使用 GetDefaultModelHandle() 确保摘要化脱敏始终使用 default 模型
    auto handle = model_config_->GetDefaultModelHandle().lock();
    if (!handle) return "";

    // 根据模型类型选择不同的提示词格式，与主推理过程保持一致
    // 系统提示词从 service_config.json 的 routing.desensitization.system_prompt 读取
    std::string prompt = BuildLocalModelPrompt(model_config_, config_.system_prompt, "Text:\n" + text);
    My_Log{} << "[Desensitizer] prompt format: "
             << (model_config_->get_prompt_type() == PromptType::Harmony ? "Harmony" : "General")
             << " for summarize" << std::endl;

    // 使用 SecurityUtils::LocalModelQuery 统一处理超时控制（与安全检查/复杂度评估共用）
    // summarize 任务不需要 JSON 早停，token 回调始终返回 true（生成完整响应）
    auto query_result = SecurityUtils::LocalModelQuery(
        handle, prompt,
        /*json_prefill=*/"",
        timeout_ms,
        /*max_gen_tokens=*/0,  // 0 表示不限制（由 timeout_ms 控制）
        [](const std::string& /*output_buf*/, const std::string& /*token*/, bool& /*json_complete*/) -> bool {
            return true;  // summarize 任务：不做早停，生成完整响应
        },
        "[Desensitizer]"
    );

    if (!query_result.success || query_result.output.empty()) return "";
    return query_result.output;
}

// ============================================================
// GenerateRandomAlnum：生成指定长度的随机字母数字字符串
// ============================================================
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

// ============================================================
// FormatMockData：格式保留脱敏
// ============================================================
std::string Desensitizer::FormatMockData(SensitiveEntityType t, int index, const std::string &matched) const
{
    switch (t) {
        case SensitiveEntityType::EMAIL:
            // 生成合法邮箱格式：mock_user_<index>@example.com
            return "mock_user_" + std::to_string(index) + "@example.com";
        
        case SensitiveEntityType::PHONE:
            // 生成11位手机号：1389 + 7位数字（index 补齐到7位）
            // 使用 1389 前缀（而非 138），使 mock 手机号可被白名单识别
            // 白名单规则：^1389\d{7}$（与 IsDesensitizedPlaceholder 中的规则对应）
            {
                std::ostringstream oss;
                oss << "1389" << std::setfill('0') << std::setw(7) << (index % 10000000);
                return oss.str();
            }
        
        case SensitiveEntityType::IDCARD:
            // 生成18位身份证号：固定前17位 + 最后1位（index % 10）
            return "11010519900101123" + std::to_string(index % 10);
        
        case SensitiveEntityType::BANKCARD:
            // 生成16位银行卡号：6222 + 12位数字（index 补齐到12位）
            {
                std::ostringstream oss;
                oss << "6222" << std::setfill('0') << std::setw(12) << (index % 1000000000000LL);
                return oss.str();
            }
        
        case SensitiveEntityType::API_KEY:
            // 生成合法 API Key 格式：sk-mock_<index>_<32位随机字符>
            return "sk-mock_" + std::to_string(index) + "_" + GenerateRandomAlnum(32);
        
        case SensitiveEntityType::TOKEN:
            // 生成合法 Bearer Token 格式：Bearer mock_token_<index>_<32位随机字符>
            return "Bearer mock_token_" + std::to_string(index) + "_" + GenerateRandomAlnum(32);
        
        case SensitiveEntityType::PRIVATE_KEY:
            // 生成合法 PEM 私钥格式（简化版，仅保留结构）
            {
                std::ostringstream oss;
                oss << "-----BEGIN RSA PRIVATE KEY-----\n";
                oss << "MockPrivateKeyData" << index << "\n";
                oss << "-----END RSA PRIVATE KEY-----";
                return oss.str();
            }
        
        case SensitiveEntityType::PASSWORD_VALUE:
        case SensitiveEntityType::PASSWORD_KW:
            // 生成合法密码格式：mock_password_<index>
            return "mock_password_" + std::to_string(index);
        
        case SensitiveEntityType::INTERNAL_URL:
            // 生成内网 IP 格式：192.168.<index/256>.<index%256>
            return "192.168." + std::to_string((index / 256) % 256) + "." + std::to_string(index % 256);
        
        case SensitiveEntityType::PATH:
            // 格式保留脱敏：只替换路径中的用户名部分，保留路径的其余结构。
            // 例如：C:\Users\zhanweiw\Downloads\case\...\get_weather.py
            //   → C:\Users\mock_user_1\Downloads\case\...\get_weather.py
            //
            // 算法：
            //   1. 找到 \Users\ 或 /Users/ 或 \Documents and Settings\ 后的第一个路径分隔符
            //   2. 提取用户名（从 \Users\ 到下一个分隔符之间的部分）
            //   3. 将用户名替换为 mock_user_<index>，保留路径其余部分
            {
                std::string mock_username = "mock_user_" + std::to_string(index);
                // 尝试匹配路径中的用户名（支持 Windows 和 Unix 两种格式）
                // 支持 / 和 \ 两种分隔符，以及 /home/、/Users/ 等前缀
                // 注意：不包含 /root/，因为 /root/ 下没有用户名层级（/root/ 本身就是 root 用户的家目录），
                // 无法做格式保留脱敏（username_end 会为 npos，导致文件名被误当用户名存入 mapping_table）。
                static const std::vector<std::string> user_prefixes = {
                    "\\Users\\", "/Users\\", "\\Users/", "/Users/",
                    "\\Documents and Settings\\", "/Documents and Settings/",
                    "/home/",   // Linux: /home/username/...
                };
                for (const auto &prefix : user_prefixes) {
                    size_t prefix_pos = matched.find(prefix);
                    if (prefix_pos != std::string::npos) {
                        size_t username_start = prefix_pos + prefix.size();
                        // 找到用户名结束位置（下一个 / 或 \）
                        size_t username_end = matched.find_first_of("/\\", username_start);
                        if (username_end != std::string::npos) {
                            // 保留路径结构，只替换用户名
                            std::string result_path = matched.substr(0, username_start) + mock_username + matched.substr(username_end);
                            // 对 Windows 路径统一使用反斜杠。
                            // 原始路径可能含混合斜杠（如 weather-cn/scripts/get_weather.py），
                            // 若 mock key 保留混合斜杠，云端 LLM 会将其标准化为全反斜杠再输出，
                            // 导致 RestoreStringFully 的 normalized_mock 才能匹配（而非直接匹配），
                            // 但滑动窗口 max_mock_len 基于 raw 长度，可能 < json_mock 长度，造成截断失败。
                            // 统一标准化后：mock key = 全反斜杠 → json_mock 长度可精确预计算 → 窗口足够大。
                            if (result_path.size() >= 2 &&
                                std::isalpha(static_cast<unsigned char>(result_path[0])) &&
                                result_path[1] == ':') {
                                for (char &c : result_path) if (c == '/') c = '\\';
                            }
                            return result_path;
                        } else {
                            // 路径只到用户名为止（无子路径）
                            return matched.substr(0, username_start) + mock_username;
                        }
                    }
                }
                // 未能解析用户名，回退到固定格式
                #ifdef _WIN32
                    return "C:\\Users\\mock_user_" + std::to_string(index) + "\\Documents";
                #else
                    return "/home/mock_user_" + std::to_string(index) + "/Documents";
                #endif
            }
        
        case SensitiveEntityType::DEVICE_ID:
            // 生成合法 MAC 地址格式：00:11:22:33:44:<index的16进制>
            {
                std::ostringstream oss;
                oss << "00:11:22:33:44:" << std::hex << std::setfill('0') << std::setw(2) << (index % 256);
                return oss.str();
            }
        
        case SensitiveEntityType::UNKNOWN:
        default:
            // 其他类型暂时使用占位符格式（后续可扩展）
            return FormatPlaceholder(t, index);
    }
}

// ============================================================
// ShouldUseFormatPreserving：判断是否使用格式保留脱敏
// ============================================================
bool Desensitizer::ShouldUseFormatPreserving(const json &request) const
{
    // 条件1：配置启用格式保留脱敏
    if (!config_.format_preserving_enabled) {
        return false;
    }
    
    // 条件2：请求包含 tools 字段（工具调用场景）
    if (!request.contains("tools") || !request["tools"].is_array() || request["tools"].empty()) {
        return false;
    }
    
    return true;
}

// ============================================================
// ApplyStructuredPlaceholders：基于 spans 的结构化占位符替换
// ============================================================
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

    // per-type 计数器 + 已见映射（同一 matched 值 → 同一占位符）
    std::unordered_map<std::string, std::string> value2ph;
    std::unordered_map<int, int> typeIndex; // SensitiveEntityType int → count

    // 若传入了已有映射表，扫描其中已使用的 mock 值，
    // 初始化 typeIndex 计数器，避免新轮次分配与已有 mock 值冲突的占位符。
    // 例如：第1轮已用 mock_user_1@example.com，第2轮应从 mock_user_2 开始。
    if (existing_mapping && !existing_mapping->empty()) {
        static const std::regex email_re("^mock_user_(\\d+)@example\\.com$");
        static const std::regex phone_re("^1389(\\d{7})$");
        static const std::regex path_re_win("^[A-Za-z]:\\\\Users\\\\mock_user_(\\d+)\\\\Documents$");
        static const std::regex path_re_unix("^/home/mock_user_(\\d+)/Documents$");
        static const std::regex path_re_username("^mock_user_(\\d+)$");  // 新格式：只存用户名
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
                // phone mock: 1389XXXXXXX，index 是后7位数字
                int idx = std::stoi(m[1].str());
                int key = static_cast<int>(SensitiveEntityType::PHONE);
                typeIndex[key] = std::max(typeIndex[key], idx);
            } else if (std::regex_match(mock, m, path_re_win) ||
                       std::regex_match(mock, m, path_re_unix) ||
                       std::regex_match(mock, m, path_re_username)) {  // 兼容新的用户名格式
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
                // MAC 地址 mock：00:11:22:33:44:XX，index 是最后一个字节
                // 解析最后两位十六进制
                std::string hex = mock.substr(mock.size() - 2);
                int idx = std::stoi(hex, nullptr, 16);
                int key = static_cast<int>(SensitiveEntityType::DEVICE_ID);
                typeIndex[key] = std::max(typeIndex[key], idx);
            }
            // 注意：IDCARD/BANKCARD 的 mock 格式无法可靠区分 index（末位取模），不做处理
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

    // 记录脱敏映射关系（用于日志输出）
    std::vector<std::tuple<std::string, std::string, std::string>> desensitization_log; // (type, original, mock)

    auto make_placeholder = [&](SensitiveEntityType t, const std::string &val) -> std::string {
        auto it = value2ph.find(val);
        if (it != value2ph.end()) return it->second;
        int idx = ++typeIndex[static_cast<int>(t)];
        
        // 根据配置选择格式保留脱敏或占位符脱敏
        std::string ph;
        if (out.use_format_preserving) {
            ph = FormatMockData(t, idx, val);  // e.g. mock_user_1@example.com
        } else {
            ph = FormatPlaceholder(t, idx);  // e.g. <PHONE_1>
        }
        
        value2ph[val] = ph;

        // PATH 类型只存用户名对（mock_user_N → 真实用户名），
        // 而非全路径对。还原时只需 find("mock_user_N")，完全不受路径分隔符格式（/ vs \）、
        // JSON 转义（\ → \\）、SSE 流式分片截断的影响——mock_user_N 仅 11 字节且不含
        // 特殊字符，任何 chunk 只要包含它就能匹配，根本不需要滑动窗口和 JSON 转义处理。
        //
        // log_original_val：用于日志输出的"真实原始值"。
        // 在迭代脱敏场景下，val 可能已经是上一轮生成的 mock 值（如 C:\Users\mock_user_1\...），
        // 此时日志应打印最终真实路径（C:\Users\zhanweiw\...）。
        // PATH 新分支在提取 real_username 时同步构建此值；其他类型通过 mapping_table 反向查找。
        std::string log_original_val;

        if (t == SensitiveEntityType::PATH && out.use_format_preserving) {
            // ph = 完整 mock 路径（如 C:\Users\mock_user_1\...\get_weather.py），
            // 从中定位 mock_user_N 的起止位置
            const std::string mu_prefix = "mock_user_";
            size_t mu_pos = ph.find(mu_prefix);
            if (mu_pos != std::string::npos) {
                size_t mu_end = ph.find_first_of("/\\", mu_pos);
                std::string mock_username = (mu_end != std::string::npos)
                    ? ph.substr(mu_pos, mu_end - mu_pos)   // "mock_user_1"
                    : ph.substr(mu_pos);
                // FormatMockData/PATH 只替换用户名，路径前缀（如 C:\Users\）长度不变，
                // 因此 val（真实路径）在同一偏移 mu_pos 处即为真实用户名
                if (!mock_username.empty() && val.size() > mu_pos) {
                    size_t ru_end = val.find_first_of("/\\", mu_pos);
                    std::string real_username = (ru_end != std::string::npos)
                        ? val.substr(mu_pos, ru_end - mu_pos)  // "zhanweiw"
                        : val.substr(mu_pos);
                    if (!real_username.empty()) {
                        out.mapping_table[mock_username] = real_username;  // 仅内存，不序列化，严禁写入日志
                        // 构建日志用的真实路径：将 val 中的 mock_user_N（若存在）替换为 real_username，
                        // 处理迭代脱敏场景下 val 本身已是 mock 路径的情况。
                        // 检查 val 中是否含有 mock_user_ 前缀（迭代脱敏场景）
                        size_t val_mu_pos = val.find(mu_prefix);
                        if (val_mu_pos != std::string::npos) {
                            // val 是上一轮的 mock 路径，从 existing_mapping 中查找真实用户名
                            size_t val_mu_end = val.find_first_of("/\\", val_mu_pos);
                            std::string val_mock_username = (val_mu_end != std::string::npos)
                                ? val.substr(val_mu_pos, val_mu_end - val_mu_pos)
                                : val.substr(val_mu_pos);
                            // 在 existing_mapping 中查找 val_mock_username 对应的真实用户名
                            if (existing_mapping) {
                                auto eit = existing_mapping->find(val_mock_username);
                                if (eit != existing_mapping->end()) {
                                    // 用真实用户名替换 val 中的 mock 用户名，得到真实路径
                                    log_original_val = val.substr(0, val_mu_pos) + eit->second
                                        + (val_mu_end != std::string::npos ? val.substr(val_mu_end) : "");
                                }
                            }
                        }
                        // 若未能从 existing_mapping 反向查找（非迭代场景），直接用 val
                        if (log_original_val.empty()) {
                            log_original_val = val;
                        }
                    }
                }
            }
            // 若未能提取用户名（极端情况），回退到 val
            if (log_original_val.empty()) {
                log_original_val = val;
            }
        } else {
            out.mapping_table[ph] = val;  // 仅内存，不序列化，严禁写入日志
            // 非 PATH 类型：通过 mapping_table（ph → original）反向查找真实原始值。
            // 在迭代脱敏场景下，val 可能已经是上一轮生成的 mock 值（如 mock_user_1@example.com），
            // 此时 mapping_table[val] 存有上一轮的真实原始值，确保日志中 original= 打印的是真实数据。
            auto mit = out.mapping_table.find(val);
            log_original_val = (mit != out.mapping_table.end()) ? mit->second : val;
        }

        // 记录脱敏映射（用于日志）
        desensitization_log.push_back(std::make_tuple(EntityTypeToString(t), log_original_val, ph));
        
        if (config_.log_desensitization_details) {
            My_Log{} << "[Desensitizer] [DIAG] Generated mapping: type=" << EntityTypeToString(t)
                     << ", original=" << log_original_val << ", mock=" << ph << std::endl;
        }
        
        return ph;
    };

    // 按 field_path 分组：将 spans 按所在字段路径分组，每个字段内按 start 逆序排序
    std::unordered_map<std::string, std::vector<const SensitiveSpan*>> spans_by_field;
    for (const auto &sp : inspection.spans) {
        spans_by_field[sp.field_path].push_back(&sp);
    }
    for (auto &[path, field_spans] : spans_by_field) {
        // 按 start 逆序排序（从后往前替换，避免位置偏移）
        std::sort(field_spans.begin(), field_spans.end(),
                  [](const SensitiveSpan *a, const SensitiveSpan *b) {
                      return a->start > b->start;
                  });
    }

    // 遍历 request 的每个文本字段，按 field_path 查找对应 spans 并做替换
    WalkJsonStringFieldsWithPath(out.desensitized_request, "",
        [&](std::string &s, const std::string &path) {
            auto it = spans_by_field.find(path);
            if (it == spans_by_field.end()) return;

            size_t last_replaced_start = std::string::npos;
            for (const auto *sp : it->second) {
                // 重叠检测：若当前 span 与已替换区间重叠，跳过
                if (last_replaced_start != std::string::npos && sp->end > last_replaced_start) {
                    continue;
                }
                if (sp->end <= s.size()) {
                    std::string ph = make_placeholder(sp->type, sp->matched);
                    s.replace(sp->start, sp->end - sp->start, ph);
                    last_replaced_start = sp->start;
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

    // 输出脱敏映射日志（受配置控制）
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

// ============================================================
// Apply：主脱敏入口
// ============================================================
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

    // 如果没有敏感内容，直接返回原始请求
    if (inspection.sensitivity_level == SensitivityLevel::S0)
    {
        result.success = true;
        result.desensitized_request = original_request;
        return result;
    }

    try
    {
        // 格式保留脱敏优先级：
        // 当 format_preserving_enabled=true 时，强制使用 structured_placeholder 策略，
        // 跳过传统的 mask/placeholder/delete 正则替换（legacy 策略）。
        // 这确保了工具调用场景下生成的是符合语义格式的 Mock Data，而非占位符。
        
        bool use_structured = false;
        bool has_summarize = false;
        
        // 优先级1：format_preserving_enabled=true 时，强制使用 structured_placeholder
        if (result.use_format_preserving) {
            use_structured = true;
            My_Log{} << "[Desensitizer] Format-preserving mode enabled, forcing structured_placeholder strategy" << std::endl;
        }
        
        // 优先级2：检查配置的 strategies 列表（同时判断是否启用 summarize，可与 structured_placeholder 共存）
        for (const auto &strategy : config_.strategies) {
            if (!result.use_format_preserving && strategy == "structured_placeholder") use_structured = true;
            if (strategy == "summarize") has_summarize = true;
        }

        if (use_structured && !inspection.spans.empty()) {
            // 记录脱敏前的请求内容（仅记录 messages 字段的摘要，受配置控制）
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
                            // 限制预览长度
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

            // 优先使用结构化占位符替换（基于 spans）
            // 传入已有映射表，避免新轮次分配与已有 mock 值冲突的占位符
            bool ok = ApplyStructuredPlaceholders(original_request, inspection, result, existing_mapping);
            if (!ok) {
                result.success = false;
                result.failure_reason = "structured_placeholder_failed";
                return result;
            }

            // 记录脱敏后的请求内容（仅记录 messages 字段的摘要，受配置控制）
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
                            // 限制预览长度
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

            // 如果还启用了 summarize，对长段落进行摘要化脱敏
            if (has_summarize && model_config_) {
                int timeout_ms = config_.summarize_timeout_ms;
                WalkJsonStringFieldsWithPath(result.desensitized_request, "",
                    [&](std::string &s, const std::string & /*path*/) {
                        // 仅对较长的文本段落进行摘要化（超过 500 字符）
                        if (s.size() > 500) {
                            std::string summarized = SummarizeText(s, timeout_ms);
                            if (!summarized.empty()) {
                                s = summarized;
                            }
                            // 超时/失败时保持 structured_placeholder 的结果（降级）
                        }
                    });
                result.applied_strategies.push_back("summarize");
            }
            else
            {
                // summarize 本地模型被跳过，记录原因
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
            // 兼容模式：使用传统正则替换策略
            json desensitized = original_request;

            // 处理 messages
            if (desensitized.contains("messages"))
            {
                desensitized["messages"] = ProcessMessages(desensitized["messages"]);
            }

            // 记录应用的策略
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
        result.desensitized_request = original_request;  // 失败时返回原始请求
        My_Log{My_Log::Level::kError} << "[Desensitizer] " << result.failure_reason << std::endl;
    }

    return result;
}
