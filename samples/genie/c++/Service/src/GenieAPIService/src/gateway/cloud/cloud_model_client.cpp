//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "cloud_model_client.h"
#include "log.h"
#include <httplib.h>
#include <thread>
#include <chrono>
#include <regex>
#include <sstream>
#if defined(_WIN32) && defined(USE_WINHTTP)
#include <winhttp.h>
// 前向声明（定义在 DoStreamRequestWinHTTP 之后）
static bool DoRequestWinHTTP(
    const std::string &host, int port,
    const std::string &path, const std::string &body,
    std::string &response_body, int &http_status,
    std::string &error_msg, int connect_timeout_sec,
    const std::string &api_key);
#endif

CloudModelClient::CloudModelClient(const CloudModelConfig &config)
        : config_(config)
{
    // 初始化端点状态
    // 如果配置了 endpoints 列表，使用多端点模式；否则使用 base_url 作为单端点
    if (config_.endpoints.empty() && !config_.base_url.empty()) {
        // 兼容旧版单端点配置：将 base_url 转换为单端点，加入 endpoints 列表
        // 原代码创建了 ep 但未调用 push_back，导致死代码，多端点逻辑永远不触发
        CloudModelConfig::Endpoint ep;
        ep.name = "default";
        ep.base_url = config_.base_url;
        ep.model = config_.model;
        config_.endpoints.push_back(ep);
        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] Constructor: converted base_url to single endpoint"
                      << ", base_url=" << config_.base_url
                      << ", model=" << config_.model
                      << std::endl;
        }
    }
    else
    {
        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] Constructor: endpoints_count=" << config_.endpoints.size()
                      << ", base_url=" << config_.base_url
                      << std::endl;
        }
    }
}

bool CloudModelClient::IsAvailable()
{
    if (!config_.enabled)
        return false;

    // 多端点模式：检查是否有任意端点不在冷却期
    if (!config_.endpoints.empty()) {
        std::lock_guard<std::mutex> lock(endpoints_mutex_);
        auto now = std::chrono::steady_clock::now();
        for (const auto &ep : config_.endpoints) {
            auto it = endpoint_states_.find(ep.name);
            if (it == endpoint_states_.end()) return true; // 未记录状态，视为可用
            const auto &st = it->second;
            if (st.cooldown_until.time_since_epoch().count() == 0 || now >= st.cooldown_until) {
                return true; // 至少有一个端点可用
            }
        }
        return false; // 所有端点都在冷却期
    }

    // 兼容旧版单端点配置：单端点模式已通过构造函数转换为 endpoints 列表，
    // 该分支只在 endpoints 为空时才会到达（此时 base_url 也必然为空），
    // 对应 ChatCompletion/ChatCompletionStream 中的不可用配置场景，始终视为可用。
    return true;
}

bool CloudModelClient::ParseUrl(const std::string &url,
                                 std::string &scheme,
                                 std::string &host,
                                 int &port,
                                 std::string &path)
{
    // 解析 URL：scheme://host[:port]/path
    static const std::regex url_re(
        R"(^(https?)://([^/:]+)(?::(\d+))?(/.*)?)");

    std::smatch match;
    if (!std::regex_match(url, match, url_re))
    {
        return false;
    }

    scheme = match[1].str();
    host = match[2].str();
    port = match[3].matched ? std::stoi(match[3].str()) :
           (scheme == "https" ? 443 : 80);
    path = match[4].matched ? match[4].str() : "/";

    return true;
}

std::string CloudModelClient::BuildEndpointUrl(const std::string &base_url)
{
    std::string url = base_url;
    if (url.empty())
        return url;

    // 去除末尾斜杠
    if (url.back() == '/')
        url.pop_back();

    // 避免重复追加 /chat/completions
    static const std::string suffix = "/chat/completions";
    if (url.length() < suffix.length() ||
        url.compare(url.length() - suffix.length(), suffix.length(), suffix) != 0)
    {
        url += suffix;
    }

    return url;
}

#if defined(_WIN32) && defined(USE_WINHTTP)
// ============================================================
// DoStreamRequestWinHTTP：使用 Windows WinHTTP API 发送 HTTPS SSE 流式请求
//
// 使用 WinHTTP（SChannel）替代 mbedTLS，原因：
//   - WinHTTP 使用 Windows 系统证书存储和 SChannel TLS 实现
//   - 与 Python httpx 使用相同的底层 TLS，兼容性更好
//   - 支持禁用 SSL 证书验证（SECURITY_FLAG_IGNORE_ALL_CERT_ERRORS）
//
// 编译开关：-DUSE_WINHTTP（CMake: -DUSE_WINHTTP=ON，Windows 默认）
// ============================================================
static bool DoStreamRequestWinHTTP(
    const std::string &host,
    int port,
    const std::string &path,
    const std::string &body,
    std::function<bool(const std::string &chunk)> on_chunk,
    std::string &error_msg,
    int connect_timeout_sec,
    int read_timeout_sec,
    const std::string &api_key)
{
    // 将 std::string 转换为 wstring
    auto to_wstring = [](const std::string &s) -> std::wstring {
        if (s.empty()) return L"";
        int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
        std::wstring ws(len - 1, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &ws[0], len);
        return ws;
    };

    HINTERNET hSession = WinHttpOpen(
        L"GenieAPIService/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0);
    if (!hSession) {
        error_msg = "WinHttpOpen failed: " + std::to_string(GetLastError());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        return false;
    }

    // 设置连接超时
    DWORD connect_ms = connect_timeout_sec * 1000;
    DWORD read_ms = read_timeout_sec * 1000;
    WinHttpSetOption(hSession, WINHTTP_OPTION_CONNECT_TIMEOUT, &connect_ms, sizeof(connect_ms));
    WinHttpSetOption(hSession, WINHTTP_OPTION_RECEIVE_TIMEOUT, &read_ms, sizeof(read_ms));
    WinHttpSetOption(hSession, WINHTTP_OPTION_SEND_TIMEOUT, &connect_ms, sizeof(connect_ms));

    HINTERNET hConnect = WinHttpConnect(
        hSession,
        to_wstring(host).c_str(),
        static_cast<INTERNET_PORT>(port),
        0);
    if (!hConnect) {
        error_msg = "WinHttpConnect failed: " + std::to_string(GetLastError());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        WinHttpCloseHandle(hSession);
        return false;
    }

    HINTERNET hRequest = WinHttpOpenRequest(
        hConnect,
        L"POST",
        to_wstring(path).c_str(),
        nullptr,
        WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,
        WINHTTP_FLAG_SECURE);
    if (!hRequest) {
        error_msg = "WinHttpOpenRequest failed: " + std::to_string(GetLastError());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    // 禁用 SSL 证书验证
    DWORD ssl_flags = SECURITY_FLAG_IGNORE_UNKNOWN_CA
                    | SECURITY_FLAG_IGNORE_CERT_WRONG_USAGE
                    | SECURITY_FLAG_IGNORE_CERT_CN_INVALID
                    | SECURITY_FLAG_IGNORE_CERT_DATE_INVALID;
    WinHttpSetOption(hRequest, WINHTTP_OPTION_SECURITY_FLAGS, &ssl_flags, sizeof(ssl_flags));

    // 构建请求头
    std::wstring headers = L"Content-Type: application/json\r\n"
                           L"Accept: text/event-stream\r\n"
                           L"Cache-Control: no-cache\r\n";
    if (!api_key.empty()) {
        headers += L"Authorization: Bearer " + to_wstring(api_key) + L"\r\n";
    }

    // 发送请求
    BOOL ok = WinHttpSendRequest(
        hRequest,
        headers.c_str(),
        static_cast<DWORD>(-1L),
        const_cast<char*>(body.c_str()),
        static_cast<DWORD>(body.size()),
        static_cast<DWORD>(body.size()),
        0);
    if (!ok) {
        error_msg = "WinHttpSendRequest failed: " + std::to_string(GetLastError());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    ok = WinHttpReceiveResponse(hRequest, nullptr);
    if (!ok) {
        error_msg = "WinHttpReceiveResponse failed: " + std::to_string(GetLastError());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    // 检查 HTTP 状态码
    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    WinHttpQueryHeaders(hRequest,
        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX,
        &status_code, &status_size, WINHTTP_NO_HEADER_INDEX);

    My_Log{} << "[CloudModelClient] [WinHTTP] HTTP status: " << status_code << std::endl;

    if (status_code != 200) {
        // 读取错误响应体
        std::string err_body;
        DWORD bytes_available = 0;
        while (WinHttpQueryDataAvailable(hRequest, &bytes_available) && bytes_available > 0) {
            std::vector<char> buf(bytes_available + 1, 0);
            DWORD bytes_read = 0;
            WinHttpReadData(hRequest, buf.data(), bytes_available, &bytes_read);
            err_body.append(buf.data(), bytes_read);
            if (err_body.size() > 500) break;
        }
        error_msg = "SSE stream HTTP error: " + std::to_string(status_code);
        if (!err_body.empty()) error_msg += ", body: " + err_body.substr(0, 200);
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    // 流式读取 SSE 响应
    // 使用持续读取循环：WinHttpQueryDataAvailable 在 SSE 流中可能返回 0，
    // 需要继续等待直到有数据或连接关闭
    std::string line_buffer;
    bool received_done = false;
    bool connection_closed = false;

    while (!received_done && !connection_closed) {
        DWORD bytes_available = 0;
        if (!WinHttpQueryDataAvailable(hRequest, &bytes_available)) {
            // 查询失败，连接可能已关闭
            break;
        }

        if (bytes_available == 0) {
            // 暂时没有数据，短暂等待后继续（SSE 流可能有间隔）
            // 检查是否已收到 [DONE]
            if (received_done) break;
            Sleep(10);  // 等待 10ms
            continue;
        }

        std::vector<char> buf(bytes_available + 1, 0);
        DWORD bytes_read = 0;
        if (!WinHttpReadData(hRequest, buf.data(), bytes_available, &bytes_read)) {
            connection_closed = true;
            break;
        }
        if (bytes_read == 0) {
            connection_closed = true;
            break;
        }

        line_buffer.append(buf.data(), bytes_read);

        // 按行处理 SSE 数据
        // on_chunk 期望接收 data: 字段的纯 JSON 值（去掉 "data: " 前缀）
        // 与 DoStreamRequest() 中的 on_chunk(value) 行为一致
        size_t pos = 0;
        while (pos < line_buffer.size()) {
            size_t newline_pos = line_buffer.find('\n', pos);
            if (newline_pos == std::string::npos) break;

            std::string line = line_buffer.substr(pos, newline_pos - pos);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            pos = newline_pos + 1;

            if (line.empty() || line[0] == ':') continue;  // 跳过空行和注释

            // 解析 SSE 字段
            size_t colon = line.find(':');
            if (colon == std::string::npos) continue;

            std::string field = line.substr(0, colon);
            std::string value = line.substr(colon + 1);
            if (!value.empty() && value[0] == ' ') value = value.substr(1);

            if (field != "data") continue;

            // 检查 [DONE]
            if (value == "[DONE]") {
                received_done = true;
                break;
            }

            // 传递纯 JSON 值给 on_chunk（与 DoStreamRequest 一致）
            if (!on_chunk(value)) {
                received_done = true;  // 调用方中止
                break;
            }
        }
        line_buffer = line_buffer.substr(pos);
    }

    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);

    if (!received_done) {
        My_Log{My_Log::Level::kWarning} << "[CloudModelClient] [WinHTTP] SSE stream ended without [DONE]" << std::endl;
    }
    return true;
}

// ============================================================
// DoRequestWinHTTP：使用 WinHTTP 发送非流式 HTTPS POST 请求
// ============================================================
static bool DoRequestWinHTTP(
    const std::string &host,
    int port,
    const std::string &path,
    const std::string &body,
    std::string &response_body,
    int &http_status,
    std::string &error_msg,
    int connect_timeout_sec,
    const std::string &api_key)
{
    auto to_wstring = [](const std::string &s) -> std::wstring {
        if (s.empty()) return L"";
        int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
        std::wstring ws(len - 1, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &ws[0], len);
        return ws;
    };

    HINTERNET hSession = WinHttpOpen(
        L"GenieAPIService/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0);
    if (!hSession) {
        error_msg = "WinHttpOpen failed: " + std::to_string(GetLastError());
        return false;
    }

    DWORD timeout_ms = connect_timeout_sec * 1000;
    WinHttpSetOption(hSession, WINHTTP_OPTION_CONNECT_TIMEOUT, &timeout_ms, sizeof(timeout_ms));
    WinHttpSetOption(hSession, WINHTTP_OPTION_RECEIVE_TIMEOUT, &timeout_ms, sizeof(timeout_ms));
    WinHttpSetOption(hSession, WINHTTP_OPTION_SEND_TIMEOUT, &timeout_ms, sizeof(timeout_ms));

    HINTERNET hConnect = WinHttpConnect(
        hSession,
        to_wstring(host).c_str(),
        static_cast<INTERNET_PORT>(port),
        0);
    if (!hConnect) {
        error_msg = "WinHttpConnect failed: " + std::to_string(GetLastError());
        WinHttpCloseHandle(hSession);
        return false;
    }

    HINTERNET hRequest = WinHttpOpenRequest(
        hConnect,
        L"POST",
        to_wstring(path).c_str(),
        nullptr,
        WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,
        WINHTTP_FLAG_SECURE);
    if (!hRequest) {
        error_msg = "WinHttpOpenRequest failed: " + std::to_string(GetLastError());
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    // 禁用 SSL 证书验证
    DWORD ssl_flags = SECURITY_FLAG_IGNORE_UNKNOWN_CA
                    | SECURITY_FLAG_IGNORE_CERT_WRONG_USAGE
                    | SECURITY_FLAG_IGNORE_CERT_CN_INVALID
                    | SECURITY_FLAG_IGNORE_CERT_DATE_INVALID;
    WinHttpSetOption(hRequest, WINHTTP_OPTION_SECURITY_FLAGS, &ssl_flags, sizeof(ssl_flags));

    std::wstring headers = L"Content-Type: application/json\r\n"
                           L"Accept: application/json\r\n";
    if (!api_key.empty()) {
        headers += L"Authorization: Bearer " + to_wstring(api_key) + L"\r\n";
    }

    BOOL ok = WinHttpSendRequest(
        hRequest,
        headers.c_str(),
        static_cast<DWORD>(-1L),
        const_cast<char*>(body.c_str()),
        static_cast<DWORD>(body.size()),
        static_cast<DWORD>(body.size()),
        0);
    if (!ok) {
        error_msg = "WinHttpSendRequest failed: " + std::to_string(GetLastError());
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    ok = WinHttpReceiveResponse(hRequest, nullptr);
    if (!ok) {
        error_msg = "WinHttpReceiveResponse failed: " + std::to_string(GetLastError());
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    // 获取 HTTP 状态码
    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    WinHttpQueryHeaders(hRequest,
        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX,
        &status_code, &status_size, WINHTTP_NO_HEADER_INDEX);
    http_status = static_cast<int>(status_code);

    // 读取响应体
    DWORD bytes_available = 0;
    while (WinHttpQueryDataAvailable(hRequest, &bytes_available) && bytes_available > 0) {
        std::vector<char> buf(bytes_available + 1, 0);
        DWORD bytes_read = 0;
        if (!WinHttpReadData(hRequest, buf.data(), bytes_available, &bytes_read)) break;
        response_body.append(buf.data(), bytes_read);
        if (response_body.size() > 1024 * 1024) break;  // 防止响应体过大
    }

    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);
    return true;
}
#endif // defined(_WIN32) && defined(USE_WINHTTP)

bool CloudModelClient::DoStreamRequest(
        const std::string &url,
        const json &request,
        std::function<bool(const std::string &chunk)> on_chunk,
        std::string &error_msg)
{
    std::string scheme, host, path;
    int port;

    if (!ParseUrl(url, scheme, host, port, path))
    {
        error_msg = "Invalid URL: " + url;
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] [DBG] DoStreamRequest: ParseUrl failed for url=" << url << std::endl;
        }
        return false;
    }

    std::string body = request.dump();

    // 计算实际流式读超时：优先使用 stream_timeout_seconds，0 表示自动（timeout_seconds×5）
    int effective_stream_timeout = (config_.stream_timeout_seconds > 0)
        ? config_.stream_timeout_seconds
        : (config_.timeout_seconds * 5);

    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DoStreamRequest] Actual request body: " << body << std::endl;
    }

    My_Log{} << "[CloudModelClient] SSE stream connecting to: "
              << scheme << "://" << host << ":" << port << path
              << " (connection_timeout=" << config_.timeout_seconds << "s"
              << ", read_timeout=" << effective_stream_timeout << "s"
              << ", body_size=" << body.size() << " bytes)" << std::endl;

#if defined(_WIN32) && defined(USE_WINHTTP)
    // ── Windows HTTPS：使用 WinHTTP（SChannel），禁用 SSL 验证 ──────────────
    // WinHTTP 使用 Windows 系统的 SChannel TLS 实现，与 Python httpx 兼容，
    // 避免 mbedTLS 与某些服务器的 TLS 握手不兼容问题。
    // 编译开关：-DUSE_WINHTTP（CMake: -DUSE_WINHTTP=ON，Windows 默认）
    if (scheme == "https")
    {
        return DoStreamRequestWinHTTP(host, port, path, body, on_chunk, error_msg,
                                      config_.timeout_seconds, effective_stream_timeout,
                                      config_.api_key);
    }
#endif // defined(_WIN32) && defined(USE_WINHTTP)

    // SSE streaming 不重试（流式传输中断后重试会导致重复输出）
    try
    {
#if defined(CPPHTTPLIB_SSL_ENABLED) || defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
        auto cli = std::make_unique<httplib::Client>(
            scheme + "://" + host + ":" + std::to_string(port));
#else
        if (scheme == "https")
        {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] HTTPS requires TLS support. "
                                          << "Falling back to plain TCP for: " << host << std::endl;
        }
        auto cli = std::make_unique<httplib::Client>(host, port);
#endif

        // 禁用 SSL 证书验证（支持自签名证书链，如企业内网服务）
        // 仅在 TLS 支持启用时有效（OpenSSL 或 mbedTLS）
#if defined(CPPHTTPLIB_SSL_ENABLED) || defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
        cli->enable_server_certificate_verification(false);
#endif

        // SSE streaming 使用更长的读超时（流式响应可能持续较长时间）
        // 优先使用 stream_timeout_seconds；若为 0，则自动取 timeout_seconds×5
        cli->set_connection_timeout(config_.timeout_seconds);
        cli->set_read_timeout(effective_stream_timeout);
        cli->set_write_timeout(config_.timeout_seconds);

        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] httplib::Client created for "
                      << host << ":" << port << ", path=" << path << std::endl;
        }

        // 构建请求头（SSE 需要 Accept: text/event-stream）
        httplib::Headers headers = {
            {"Content-Type", "application/json"},
            {"Accept", "text/event-stream"},
            {"Cache-Control", "no-cache"}
        };

        if (!config_.api_key.empty())
        {
            headers.emplace("Authorization", "Bearer " + config_.api_key);
        }
        else
        {
            My_Log{My_Log::Level::kWarning}
                << "[CloudModelClient] WARNING: No api_key configured, request will be sent without Authorization header" << std::endl;
        }

        // SSE 解析状态
        bool received_done = false;
        bool aborted = false;
        int chunk_count = 0;
        std::string line_buffer;  // 用于跨 chunk 的行缓冲

        // 用于诊断：记录 content_receiver 最后一次被调用的时间（用于区分超时 vs 正常关闭）
        auto last_data_time = std::chrono::steady_clock::now();

        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] Calling cli->Post() with content_receiver..." << std::endl;
        }

        // 使用 httplib 的 content_receiver 接口接收流式响应
        // content_receiver 在每次收到数据时被调用（可能是部分行）
        //
        // ⚠️ 重要说明：当 content_receiver 返回 false 时（例如收到 [DONE] 或调用方中止），
        // httplib 会将内部 error 设置为 Error::Canceled，导致 result 为失败状态（!result == true）。
        // 因此，在检查 !result 之前，必须先检查 received_done 和 aborted 标志，
        // 以区分"正常完成后主动停止"和"真正的连接错误"。
        auto result = cli->Post(
            path,
            headers,
            body,
            "application/json",
            [&](const char *data, size_t data_length) -> bool {
                last_data_time = std::chrono::steady_clock::now();

                // 将收到的数据追加到行缓冲
                line_buffer.append(data, data_length);

                // 按行处理 SSE 数据
                // SSE 格式：每个事件由一个或多个 "field: value\n" 行组成，
                // 事件之间用空行（\n\n）分隔
                size_t pos = 0;
                while (pos < line_buffer.size())
                {
                    // 查找行结束符（\n 或 \r\n）
                    size_t newline_pos = line_buffer.find('\n', pos);
                    if (newline_pos == std::string::npos)
                    {
                        // 未找到完整行，等待更多数据
                        break;
                    }

                    // 提取一行（去除 \r\n 或 \n）
                    std::string line = line_buffer.substr(pos, newline_pos - pos);
                    if (!line.empty() && line.back() == '\r')
                    {
                        line.pop_back();
                    }
                    pos = newline_pos + 1;

                    // 跳过空行（SSE 事件分隔符）
                    if (line.empty())
                        continue;

                    // 跳过注释行（以 ':' 开头）
                    if (line[0] == ':')
                        continue;

                    // 解析 SSE 字段
                    // 格式：field: value 或 field:value
                    size_t colon_pos = line.find(':');
                    if (colon_pos == std::string::npos)
                        continue;

                    std::string field = line.substr(0, colon_pos);
                    std::string value = line.substr(colon_pos + 1);

                    // 去除 value 开头的空格
                    if (!value.empty() && value[0] == ' ')
                        value = value.substr(1);

                    // 只处理 "data" 字段
                    if (field != "data")
                        continue;

                    // 检查是否为结束标记
                    if (value == "[DONE]")
                    {
                        // ---------------------------------------------------------------
                        // 在收到 [DONE] 之前，先 flush line_buffer 中的剩余内容。
                        //
                        // 问题根因：
                        // 当收到 [DONE] 时，content_receiver 立即返回 false 停止接收。
                        // 但此时 line_buffer 中可能还有未处理的数据（前一个 SSE chunk
                        // 没有以 \n 结尾，被保留在 line_buffer 中等待下一行）。
                        //
                        // 如果在 Post() 返回后再 flush，此时 state->done 已被设置为 true，
                        // 消费者可能已退出等待循环，导致 flush 的 chunk 无法被消费者处理。
                        //
                        // 正确做法：在 content_receiver 内部（Post() 返回前）flush，
                        // 确保所有 chunk 在 state->done 被设置之前都已进入队列。
                        // ---------------------------------------------------------------
                        if (!line_buffer.empty())
                        {
                            // 尝试解析 line_buffer 中的内容作为 SSE 数据
                            size_t flush_pos = 0;
                            while (flush_pos < line_buffer.size())
                            {
                                // 查找行结束符
                                size_t flush_newline = line_buffer.find('\n', flush_pos);
                                std::string flush_line;

                                if (flush_newline == std::string::npos)
                                {
                                    // 没有换行符：这是最后一个不完整的行，直接取到末尾
                                    flush_line = line_buffer.substr(flush_pos);
                                    flush_pos = line_buffer.size();
                                }
                                else
                                {
                                    // 有换行符：正常提取一行
                                    flush_line = line_buffer.substr(flush_pos, flush_newline - flush_pos);
                                    if (!flush_line.empty() && flush_line.back() == '\r')
                                    {
                                        flush_line.pop_back();
                                    }
                                    flush_pos = flush_newline + 1;
                                }

                                // 跳过空行和注释行
                                if (flush_line.empty() || flush_line[0] == ':')
                                    continue;

                                // 解析 SSE 字段
                                size_t flush_colon = flush_line.find(':');
                                if (flush_colon == std::string::npos)
                                    continue;

                                std::string flush_field = flush_line.substr(0, flush_colon);
                                std::string flush_value = flush_line.substr(flush_colon + 1);

                                // 去除 value 开头的空格
                                if (!flush_value.empty() && flush_value[0] == ' ')
                                    flush_value = flush_value.substr(1);

                                // 只处理 "data" 字段
                                if (flush_field != "data")
                                    continue;

                                // 跳过 [DONE]（不应出现在这里，但防御性处理）
                                if (flush_value == "[DONE]")
                                    break;

                                // 调用回调函数处理 chunk
                                ++chunk_count;
                                if (!on_chunk(flush_value))
                                {
                                    // 回调返回 false 表示中止
                                    aborted = true;
                                    line_buffer.clear();
                                    return false;
                                }
                            }

                            line_buffer.clear();
                        }

                        received_done = true;
                        My_Log{} << "[CloudModelClient] SSE stream completed ([DONE] received, chunks="
                                  << chunk_count << ")" << std::endl;
                        return false;  // 通知 httplib 停止接收（会触发 Error::Canceled，属正常行为）
                    }

                    // 调用回调函数处理 chunk
                    ++chunk_count;
                    if (!on_chunk(value))
                    {
                        // 回调返回 false 表示中止（消费者已停止，例如客户端断开连接）
                        aborted = true;
                        My_Log{} << "[CloudModelClient] SSE stream aborted by callback (chunks="
                                  << chunk_count << ")" << std::endl;
                        return false;
                    }
                }

                // 保留未处理的部分（不完整的行）
                if (pos < line_buffer.size())
                {
                    line_buffer = line_buffer.substr(pos);
                }
                else
                {
                    line_buffer.clear();
                }

                return true;  // 继续接收
            });

        // ---------------------------------------------------------------
        // ⚠️ 结果检查顺序非常重要：
        //
        // 正确顺序：
        // 1. 优先检查 received_done（正常完成：收到 [DONE]）
        // 2. 检查 aborted（消费者主动中止：客户端断开等）
        //    以上两种情况下 content_receiver 返回 false，httplib 设置 Error::Canceled，
        //    !result 为 true，但这是预期行为，不应视为错误。
        // 3. 只有在 received_done=false 且 aborted=false 时，
        //    才将 !result 视为真正的连接/传输错误。
        // ---------------------------------------------------------------

        if (received_done)
        {
            // 正常完成：收到 [DONE] 标记后主动停止接收
            if (config_.log_debug) {
                My_Log{} << "[CloudModelClient] [DBG] SSE stream: received [DONE], completed successfully (chunks="
                          << chunk_count << ")" << std::endl;
            }
            return true;
        }

        if (aborted)
        {
            // 消费者主动中止（例如客户端断开连接）：
            // content_receiver 返回 false → httplib 设置 Error::Canceled。
            // 这是预期行为，不应视为云端服务故障，也不应触发熔断或重试。
            if (config_.log_debug) {
                My_Log{} << "[CloudModelClient] [DBG] SSE stream: aborted by consumer (client disconnected?), chunks="
                          << chunk_count << std::endl;
            }
            return true;
        }

        // ---------------------------------------------------------------
        // 到这里说明 content_receiver 从未返回 false（即没有收到 [DONE] 也没有中止）。
        // 这意味着连接被关闭了，但不是由我们主动关闭的。
        //
        // 诊断：根据 httplib error code 和 chunk_count 判断原因：
        //
        // 情况 A：!result（httplib 报告连接错误）
        //   → 真正的网络/连接错误，应触发熔断
        //
        // 情况 B：result 有效（HTTP 200），但未收到 [DONE]
        //   → 服务端提前关闭连接（可能是代理层截断、网络抖动等）
        //   → 若 chunk_count > 0：已收到部分数据，连接中途断开
        //   → 若 chunk_count == 0：连接建立后立即关闭，可能是服务端拒绝
        // ---------------------------------------------------------------

        if (!result)
        {
            // httplib 连接错误（非 Canceled，因为 received_done=false 且 aborted=false）
            error_msg = "SSE stream request failed: " + httplib::to_string(result.error());
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg
                                          << " [target=" << scheme << "://" << host << ":" << port << path << "]"
                                          << " [httplib_error=" << static_cast<int>(result.error()) << "=" << httplib::to_string(result.error()) << "]"
                                          << " [chunks_received=" << chunk_count << "]"
                                          << std::endl;
            return false;
        }

        // HTTP 状态码检查（result 有效，但未收到 [DONE]）
        if (result->status != 200)
        {
            error_msg = "SSE stream HTTP error: " + std::to_string(result->status);
            if (!result->body.empty())
            {
                error_msg += ", body: " + result->body.substr(0, 200);
            }
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg
                                          << " [chunks_received=" << chunk_count << "]" << std::endl;
            return false;
        }

        // ---------------------------------------------------------------
        // 根因分析：
        // - 若 chunk_count == 0：连接建立后立即关闭，可能是服务端或代理层问题
        // - 若 chunk_count > 0：已收到部分数据后连接中断，可能原因：
        //   (a) 代理层（ReverseTunnel）提前关闭了连接（已修复 FIRST_COMPLETED 问题）
        //   (b) 消费者（genie_routing_gateway）关闭了连接但 aborted 标志未被设置
        //       （竞态条件：consumer_stopped 设置后，on_chunk 尚未被调用就连接关闭了）
        //   (c) 网络抖动导致连接中断
        //   (d) 云端服务提前关闭连接（不发送 [DONE]）
        //   (e) [已修复] line_buffer 中有未处理的数据（没有换行符结尾的最后一个 chunk）
        //
        // 处理策略：
        // - chunk_count > 0 时降级为 WARNING（功能上已收到数据，不影响已发送的内容）
        // - chunk_count == 0 时保持 ERROR（完全没有收到数据，可能是严重问题）
        // ---------------------------------------------------------------

        // 计算从最后一次收到数据到连接关闭的时间间隔（用于诊断）
        auto now = std::chrono::steady_clock::now();
        auto ms_since_last_data = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - last_data_time).count();

        error_msg = "SSE stream ended without [DONE] marker";

        if (chunk_count > 0)
        {
            // 已收到部分数据后连接中断。
            // 这通常是由消费者断开连接（客户端关闭）或代理层截断导致的，
            // 不一定是云端服务的问题。降级为 WARNING，不触发熔断。
            //
            // 注意：若消费者断开时 aborted 标志未被及时设置（竞态条件），
            // 也会走到这里。这是一个已知的边缘情况，属于非功能性问题。
            My_Log{My_Log::Level::kWarning}
                << "[CloudModelClient] SSE stream ended without [DONE] after receiving "
                << chunk_count << " chunks (ms_since_last_data=" << ms_since_last_data << "ms)"
                << " -- likely consumer disconnected or proxy truncated the stream"
                << " (non-fatal, data already delivered to consumer)"
                << std::endl;
            return true;
        }
        else
        {
            // 完全没有收到数据就连接关闭，这是真正的错误
            My_Log{My_Log::Level::kError}
                << "[CloudModelClient] SSE stream ended without [DONE] and no chunks received"
                << " (ms_since_last_data=" << ms_since_last_data << "ms)"
                << " -- possible cloud/proxy connection issue"
                << std::endl;
            return false;
        }
    }
    catch (const std::exception &e)
    {
        error_msg = "Exception during SSE stream request: " + std::string(e.what());
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg << std::endl;
        return false;
    }
}

bool CloudModelClient::ChatCompletion(const json &request,
                                       json &response,
                                       std::string &error_msg)
{
    if (!IsAvailable())
    {
        error_msg = "Cloud model client is not available (all endpoints in cooldown)";
        return false;
    }

    // 使用配置中的 model 覆盖请求中的 model 字段：
    // 优先级：config_.model（非空时）> 请求中的 model
    // 原因：客户端（如 QAIAgentForge）发来的 model 字段是本地模型名（如 qwen3-8b-8380），
    // 云端服务不认识本地模型名，必须替换为云端配置的模型名（如 meta-llama/Llama-3.3-70B-Instruct）
    json req = request;
    if (!config_.model.empty())
    {
        req["model"] = config_.model;
    }
    else if (!req.contains("model") || req["model"].get<std::string>().empty())
    {
        // config_.model 为空时才保留请求中的 model（兜底）
        req["model"] = "";
    }

    // 非 streaming 调用
    req["stream"] = false;

    // 使用多端点 Fallback 逻辑（构造函数已保证 endpoints 非空）
    return DoRequestWithFallback(req, response, error_msg);
}

bool CloudModelClient::ChatCompletionStream(
        const json &request,
        std::function<bool(const std::string &chunk)> on_chunk,
        std::string &error_msg)
{
    if (!IsAvailable())
    {
        error_msg = "Cloud model client is not available (all endpoints in cooldown)";
        return false;
    }

    if (!on_chunk)
    {
        error_msg = "on_chunk callback is null";
        return false;
    }

    // 使用配置中的 model 覆盖请求中的 model 字段（与非 streaming 路径保持一致）
    json req = request;
    if (!config_.model.empty())
    {
        req["model"] = config_.model;
    }
    else if (!req.contains("model") || req["model"].get<std::string>().empty())
    {
        req["model"] = "";
    }

    // 强制启用 streaming
    req["stream"] = true;

    // 添加 stream_options 以请求 usage 信息（OpenAI API 兼容）
    // 这样云端会在最后一个 chunk 中返回 token 使用统计
    if (!req.contains("stream_options")) {
        req["stream_options"] = {
            {"include_usage", true}
        };
    }

    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] ChatCompletionStream: endpoints_count="
                  << config_.endpoints.size()
                  << ", model=" << req.value("model", "(not set)")
                  << ", stream_options.include_usage=true"
                  << std::endl;
    }

    // 使用多端点 Fallback 逻辑（构造函数已保证 endpoints 非空）
    return DoStreamRequestWithFallback(req, on_chunk, error_msg);
}

std::string CloudModelClient::GetLastUsedEndpoint() const
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    return last_used_endpoint_;
}

std::vector<int> CloudModelClient::BuildEndpointOrder()
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    auto now = std::chrono::steady_clock::now();
    std::vector<int> order;
    for (int i = 0; i < (int)config_.endpoints.size(); ++i) {
        const auto &ep = config_.endpoints[i];
        auto it = endpoint_states_.find(ep.name);
        if (it != endpoint_states_.end()) {
            const auto &st = it->second;
            if (st.cooldown_until.time_since_epoch().count() > 0 && now < st.cooldown_until) {
                continue; // 跳过冷却期内的端点
            }
        }
        order.push_back(i);
    }
    return order; // 按配置顺序
}

int CloudModelClient::CalcMaxTotalAttempts() const
{
    if (config_.retry.max_total_attempts > 0) {
        return config_.retry.max_total_attempts;
    }
    return (int)config_.endpoints.size() * (config_.retry.max + 1);
}

void CloudModelClient::MarkFailure(const std::string &endpoint_name)
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    endpoint_states_[endpoint_name].consecutive_failures++;
}

void CloudModelClient::ResetFailure(const std::string &endpoint_name)
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    endpoint_states_[endpoint_name].consecutive_failures = 0;
    endpoint_states_[endpoint_name].cooldown_until = {};
}

bool CloudModelClient::ShouldCircuitBreak(const std::string &endpoint_name) const
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    auto it = endpoint_states_.find(endpoint_name);
    if (it == endpoint_states_.end()) return false;
    return it->second.consecutive_failures >= config_.circuit_breaker.failure_threshold;
}

void CloudModelClient::EnterCooldown(const std::string &endpoint_name)
{
    std::lock_guard<std::mutex> lock(endpoints_mutex_);
    auto &st = endpoint_states_[endpoint_name];
    st.cooldown_until = std::chrono::steady_clock::now() +
                        std::chrono::seconds(config_.circuit_breaker.cooldown_seconds);
    My_Log{My_Log::Level::kError} << "[CloudModelClient] Endpoint '" << endpoint_name
                                  << "' entered cooldown for " << config_.circuit_breaker.cooldown_seconds
                                  << "s (consecutive_failures=" << st.consecutive_failures << ")" << std::endl;
}

void CloudModelClient::Backoff(int attempt)
{
    int wait_ms = config_.retry.backoff_ms * attempt;
    if (wait_ms > 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(wait_ms));
    }
}

CloudModelClient::HttpResult CloudModelClient::DoSingleHttp(
        const CloudModelConfig::Endpoint &endpoint,
        const json &request,
        json &response,
        std::string &err)
{
    HttpResult result;
    std::string url = BuildEndpointUrl(endpoint.base_url);
    if (url.empty()) {
        err = "Endpoint '" + endpoint.name + "' has empty base_url";
        return result;
    }

    // 如果端点有自己的 model，覆盖请求中的 model
    json req = request;
    if (!endpoint.model.empty()) {
        req["model"] = endpoint.model;
    }

    std::string scheme, host, path;
    int port;
    if (!ParseUrl(url, scheme, host, port, path)) {
        err = "Invalid URL for endpoint '" + endpoint.name + "': " + url;
        return result;
    }

    std::string body = req.dump();

    try {
#if defined(_WIN32) && defined(USE_WINHTTP)
        // Windows HTTPS：使用 WinHTTP（SChannel）
        if (scheme == "https")
        {
            std::string resp_body;
            int http_status = 0;
            if (!DoRequestWinHTTP(host, port, path, body, resp_body, http_status, err,
                                  config_.timeout_seconds, config_.api_key))
            {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
                return result;
            }
            result.http_status = http_status;
            if (http_status != 200) {
                err = "HTTP error " + std::to_string(http_status) +
                      " from endpoint '" + endpoint.name + "'";
                My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
                return result;
            }
            try {
                response = json::parse(resp_body);
                result.ok = true;
            } catch (const json::exception &e) {
                err = "Failed to parse response JSON from endpoint '" + endpoint.name + "': " + e.what();
                My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
            }
            return result;
        }
#endif // defined(_WIN32) && defined(USE_WINHTTP)

        // cpp-httplib 路径（非 Windows，或 HTTP，或 USE_WINHTTP=OFF）
#if defined(CPPHTTPLIB_SSL_ENABLED) || defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
        auto cli = std::make_unique<httplib::Client>(
            scheme + "://" + host + ":" + std::to_string(port));
#else
        if (scheme == "https")
        {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] HTTPS requires TLS support. "
                                          << "Falling back to plain TCP for: " << host << std::endl;
        }
        auto cli = std::make_unique<httplib::Client>(host, port);
#endif
        // 禁用 SSL 证书验证（支持自签名证书链，如企业内网服务）
        // 仅在 TLS 支持启用时有效（OpenSSL 或 mbedTLS）
#if defined(CPPHTTPLIB_SSL_ENABLED) || defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
        cli->enable_server_certificate_verification(false);
#endif

        cli->set_connection_timeout(config_.timeout_seconds);
        cli->set_read_timeout(config_.timeout_seconds);
        cli->set_write_timeout(config_.timeout_seconds);

        httplib::Headers headers = {
            {"Content-Type", "application/json"},
            {"Accept", "application/json"}
        };
        if (!config_.api_key.empty()) {
            headers.emplace("Authorization", "Bearer " + config_.api_key);
        }

        auto http_result = cli->Post(path, headers, body, "application/json");
        if (!http_result) {
            err = "HTTP request failed for endpoint '" + endpoint.name + "': " +
                  httplib::to_string(http_result.error());
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
            return result;
        }

        result.http_status = http_result->status;

        if (http_result->status != 200) {
            err = "HTTP error " + std::to_string(http_result->status) +
                  " from endpoint '" + endpoint.name + "'";
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
            return result;
        }

        try {
            response = json::parse(http_result->body);
            result.ok = true;
        } catch (const json::exception &e) {
            err = "Failed to parse response JSON from endpoint '" + endpoint.name + "': " + e.what();
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
        }
    } catch (const std::exception &e) {
        err = "Exception for endpoint '" + endpoint.name + "': " + e.what();
        My_Log{My_Log::Level::kError} << "[CloudModelClient] " << err << std::endl;
    }

    return result;
}

CloudModelClient::HttpResult CloudModelClient::DoSingleHttpStream(
        const CloudModelConfig::Endpoint &endpoint,
        const json &request,
        std::function<bool(const std::string &chunk)> on_chunk,
        std::string &err)
{
    HttpResult result;
    std::string url = BuildEndpointUrl(endpoint.base_url);
    if (url.empty()) {
        err = "Endpoint '" + endpoint.name + "' has empty base_url";
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] [DBG] DoSingleHttpStream: " << err << std::endl;
        }
        return result;
    }

    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] DoSingleHttpStream: endpoint='" << endpoint.name
                  << "', url=" << url
                  << ", model=" << (endpoint.model.empty() ? "(from request)" : endpoint.model)
                  << std::endl;
    }

    json req = request;
    if (!endpoint.model.empty()) {
        req["model"] = endpoint.model;
        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] DoSingleHttpStream: overriding model with endpoint model='"
                      << endpoint.model << "'" << std::endl;
        }
    }

    // 打印请求中实际使用的 model
    std::string actual_model = req.value("model", "(not set)");
    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] DoSingleHttpStream: actual model in request='"
                  << actual_model << "'" << std::endl;
    }

    std::string scheme, host, path;
    int port;
    if (!ParseUrl(url, scheme, host, port, path)) {
        err = "Invalid URL for endpoint '" + endpoint.name + "': " + url;
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] [DBG] DoSingleHttpStream: ParseUrl failed for url=" << url << std::endl;
        }
        return result;
    }

    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] DoSingleHttpStream: parsed url: scheme=" << scheme
                  << ", host=" << host << ", port=" << port << ", path=" << path << std::endl;
    }

    // 复用 DoStreamRequest 的逻辑
    bool ok = DoStreamRequest(url, req, on_chunk, err);
    result.ok = ok;
    result.http_status = ok ? 200 : 500;

    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] DoSingleHttpStream: endpoint='" << endpoint.name
                  << "' result=" << (ok ? "OK" : "FAILED")
                  << (ok ? "" : (", err=" + err))
                  << std::endl;
    }

    return result;
}

bool CloudModelClient::DoRequestWithFallback(const json &request, json &response, std::string &err)
{
    auto order = BuildEndpointOrder();
    if (order.empty()) {
        err = "all endpoints in cooldown";
        My_Log{My_Log::Level::kError} << "[CloudModelClient] All endpoints in cooldown" << std::endl;
        return false;
    }

    int total_attempts = 0;
    int effective_max_total = CalcMaxTotalAttempts();

    for (int idx : order) {
        const auto &ep = config_.endpoints[idx];
        for (int r = 0; r <= config_.retry.max; ++r) {
            if (total_attempts++ >= effective_max_total) {
                err = "max_total_attempts reached";
                My_Log{My_Log::Level::kError} << "[CloudModelClient] max_total_attempts=" << effective_max_total << " reached" << std::endl;
                return false;
            }

            if (r > 0) {
                Backoff(r);
            }

            auto http_result = DoSingleHttp(ep, request, response, err);

            if (http_result.ok) {
                ResetFailure(ep.name);
                {
                    std::lock_guard<std::mutex> lock(endpoints_mutex_);
                    last_used_endpoint_ = ep.name;
                }
                My_Log{} << "[CloudModelClient] Request succeeded on endpoint '" << ep.name << "'" << std::endl;
                return true;
            }

            // 400/401/403：不重试，立即失败
            if (http_result.http_status == 400 || http_result.http_status == 401 ||
                http_result.http_status == 403) {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] Endpoint '" << ep.name
                                              << "' returned " << http_result.http_status << ", not retrying" << std::endl;
                return false;
            }

            // 429：退避重试，根据配置决定是否切换端点
            if (http_result.http_status == 429) {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] Endpoint '" << ep.name << "' returned 429 (rate limited)" << std::endl;
                Backoff(r + 1);
                if (!config_.retry.retry_on_429_switch_endpoint) {
                    continue; // 同端点重试
                } else {
                    break; // 切换下一个端点
                }
            }

            // 超时/网络/5xx：标记失败，检查是否触发熔断
            MarkFailure(ep.name);
            if (ShouldCircuitBreak(ep.name)) {
                EnterCooldown(ep.name);
            }
            break; // 切换下一端点
        }
    }

    err = "all endpoints failed";
    return false;
}

bool CloudModelClient::DoStreamRequestWithFallback(
        const json &request,
        std::function<bool(const std::string &chunk)> on_chunk,
        std::string &err)
{
    auto order = BuildEndpointOrder();
    if (order.empty()) {
        err = "all endpoints in cooldown";
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError} << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: all endpoints in cooldown" << std::endl;
        }
        return false;
    }

    int effective_max_total = CalcMaxTotalAttempts();
    if (config_.log_debug) {
        My_Log{} << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: "
                  << "endpoints_count=" << config_.endpoints.size()
                  << ", available_endpoints=" << order.size()
                  << ", effective_max_total=" << effective_max_total
                  << std::endl;
    }

    int total_attempts = 0;
    // 用于最终日志级别判断：若所有失败都是非致命的，降级为 WARNING
    bool all_nonfatal = false;  // 初始为 false，只有在至少有一次非致命失败且无致命失败时才为 true

    for (int idx : order) {
        const auto &ep = config_.endpoints[idx];
        if (total_attempts++ >= effective_max_total) {
            err = "max_total_attempts reached";
            if (config_.log_debug) {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: max_total_attempts="
                                               << effective_max_total << " reached" << std::endl;
            }
            return false;
        }

        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: trying endpoint[" << idx << "]='"
                      << ep.name << "', base_url=" << ep.base_url << std::endl;
        }

        auto http_result = DoSingleHttpStream(ep, request, on_chunk, err);

        if (http_result.ok) {
            ResetFailure(ep.name);
            {
                std::lock_guard<std::mutex> lock(endpoints_mutex_);
                last_used_endpoint_ = ep.name;
            }
            if (config_.log_debug) {
                My_Log{} << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: succeeded on endpoint='"
                          << ep.name << "'" << std::endl;
            }
            return true;
        }

        // ---------------------------------------------------------------
        // 失败处理：区分"非致命失败"和"真正的端点故障"
        //
        // "SSE stream ended without [DONE] marker" 有两种情况：
        //   (a) chunk_count > 0：已收到部分数据，消费者断开或代理截断（非致命）
        //       → 不触发熔断，不计入 consecutive_failures
        //       → 但仍然返回 false（让上层决定是否重试）
        //   (b) chunk_count == 0：完全没有收到数据（真正的端点故障）
        //       → 触发熔断计数
        //
        // 其他错误（连接失败、HTTP 错误等）：触发熔断计数
        // ---------------------------------------------------------------
        bool is_nonfatal_stream_end = (err == "SSE stream ended without [DONE] marker");

        if (is_nonfatal_stream_end)
        {
            // 非致命：消费者断开或代理截断，不触发熔断
            if (config_.log_debug) {
                My_Log{My_Log::Level::kWarning}
                    << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: endpoint='"
                    << ep.name << "' stream ended without [DONE] (non-fatal, not triggering circuit breaker)"
                    << std::endl;
            }
            // 记录本次失败为非致命，用于最终日志级别判断
            all_nonfatal = true;
        }
        else
        {
            // 真正的端点故障：触发熔断计数
            if (config_.log_debug) {
                My_Log{My_Log::Level::kError}
                    << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: endpoint='"
                    << ep.name << "' failed with fatal error, err=" << err << std::endl;
            }
            MarkFailure(ep.name);
            if (ShouldCircuitBreak(ep.name)) {
                EnterCooldown(ep.name);
            }
            all_nonfatal = false;  // 至少有一个真正的端点故障
        }
    }

    err = "all endpoints failed for streaming";
    if (all_nonfatal)
    {
        // 所有失败都是非致命的（消费者断开或代理截断），降级为 WARNING
        if (config_.log_debug) {
            My_Log{My_Log::Level::kWarning}
                << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: all endpoints returned without [DONE]"
                << " (non-fatal: consumer disconnected or proxy truncated, data already delivered)"
                << std::endl;
        }
    }
    else
    {
        // 至少有一个真正的端点故障，保持 ERROR
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError}
                << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: all endpoints failed"
                << std::endl;
        }
    }
    return false;
}
