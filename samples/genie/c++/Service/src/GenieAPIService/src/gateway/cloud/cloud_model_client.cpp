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
    if (config_.endpoints.empty() && !config_.base_url.empty()) {
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

    if (!config_.endpoints.empty()) {
        std::lock_guard<std::mutex> lock(endpoints_mutex_);
        auto now = std::chrono::steady_clock::now();
        for (const auto &ep : config_.endpoints) {
            auto it = endpoint_states_.find(ep.name);
            if (it == endpoint_states_.end()) return true;
            const auto &st = it->second;
            if (st.cooldown_until.time_since_epoch().count() == 0 || now >= st.cooldown_until) {
                return true;
            }
        }
        return false;
    }

    return true;
}

bool CloudModelClient::ParseUrl(const std::string &url,
                                 std::string &scheme,
                                 std::string &host,
                                 int &port,
                                 std::string &path)
{
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

    if (url.back() == '/')
        url.pop_back();

    static const std::string suffix = "/chat/completions";
    if (url.length() < suffix.length() ||
        url.compare(url.length() - suffix.length(), suffix.length(), suffix) != 0)
    {
        url += suffix;
    }

    return url;
}

#if defined(_WIN32) && defined(USE_WINHTTP)
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

    DWORD ssl_flags = SECURITY_FLAG_IGNORE_UNKNOWN_CA
                    | SECURITY_FLAG_IGNORE_CERT_WRONG_USAGE
                    | SECURITY_FLAG_IGNORE_CERT_CN_INVALID
                    | SECURITY_FLAG_IGNORE_CERT_DATE_INVALID;
    WinHttpSetOption(hRequest, WINHTTP_OPTION_SECURITY_FLAGS, &ssl_flags, sizeof(ssl_flags));

    std::wstring headers = L"Content-Type: application/json\r\n"
                           L"Accept: text/event-stream\r\n"
                           L"Cache-Control: no-cache\r\n";
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

    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    WinHttpQueryHeaders(hRequest,
        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX,
        &status_code, &status_size, WINHTTP_NO_HEADER_INDEX);

    My_Log{} << "[CloudModelClient] [WinHTTP] HTTP status: " << status_code << std::endl;

    if (status_code != 200) {
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

    std::string line_buffer;
    bool received_done = false;
    bool connection_closed = false;

    while (!received_done && !connection_closed) {
        DWORD bytes_available = 0;
        if (!WinHttpQueryDataAvailable(hRequest, &bytes_available)) {
            break;
        }

        if (bytes_available == 0) {
            if (received_done) break;
            Sleep(10);
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

        size_t pos = 0;
        while (pos < line_buffer.size()) {
            size_t newline_pos = line_buffer.find('\n', pos);
            if (newline_pos == std::string::npos) break;

            std::string line = line_buffer.substr(pos, newline_pos - pos);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            pos = newline_pos + 1;

            if (line.empty() || line[0] == ':') continue;

            size_t colon = line.find(':');
            if (colon == std::string::npos) continue;

            std::string field = line.substr(0, colon);
            std::string value = line.substr(colon + 1);
            if (!value.empty() && value[0] == ' ') value = value.substr(1);

            if (field != "data") continue;

            if (value == "[DONE]") {
                received_done = true;
                break;
            }

            if (!on_chunk(value)) {
                received_done = true;
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

    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    WinHttpQueryHeaders(hRequest,
        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX,
        &status_code, &status_size, WINHTTP_NO_HEADER_INDEX);
    http_status = static_cast<int>(status_code);

    DWORD bytes_available = 0;
    while (WinHttpQueryDataAvailable(hRequest, &bytes_available) && bytes_available > 0) {
        std::vector<char> buf(bytes_available + 1, 0);
        DWORD bytes_read = 0;
        if (!WinHttpReadData(hRequest, buf.data(), bytes_available, &bytes_read)) break;
        response_body.append(buf.data(), bytes_read);
        if (response_body.size() > 1024 * 1024) break;
    }

    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);
    return true;
}
#endif

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
    if (scheme == "https")
    {
        return DoStreamRequestWinHTTP(host, port, path, body, on_chunk, error_msg,
                                      config_.timeout_seconds, effective_stream_timeout,
                                      config_.api_key);
    }
#endif

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

#if defined(CPPHTTPLIB_SSL_ENABLED) || defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
        cli->enable_server_certificate_verification(false);
#endif

        cli->set_connection_timeout(config_.timeout_seconds);
        cli->set_read_timeout(effective_stream_timeout);
        cli->set_write_timeout(config_.timeout_seconds);

        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] httplib::Client created for "
                      << host << ":" << port << ", path=" << path << std::endl;
        }

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

        bool received_done = false;
        bool aborted = false;
        int chunk_count = 0;
        std::string line_buffer;

        auto last_data_time = std::chrono::steady_clock::now();

        if (config_.log_debug) {
            My_Log{} << "[CloudModelClient] [DBG] Calling cli->Post() with content_receiver..." << std::endl;
        }

        auto result = cli->Post(
            path,
            headers,
            body,
            "application/json",
            [&](const char *data, size_t data_length) -> bool {
                last_data_time = std::chrono::steady_clock::now();

                line_buffer.append(data, data_length);

                size_t pos = 0;
                while (pos < line_buffer.size())
                {
                    size_t newline_pos = line_buffer.find('\n', pos);
                    if (newline_pos == std::string::npos)
                    {
                        break;
                    }

                    std::string line = line_buffer.substr(pos, newline_pos - pos);
                    if (!line.empty() && line.back() == '\r')
                    {
                        line.pop_back();
                    }
                    pos = newline_pos + 1;

                    if (line.empty())
                        continue;

                    if (line[0] == ':')
                        continue;

                    size_t colon_pos = line.find(':');
                    if (colon_pos == std::string::npos)
                        continue;

                    std::string field = line.substr(0, colon_pos);
                    std::string value = line.substr(colon_pos + 1);

                    if (!value.empty() && value[0] == ' ')
                        value = value.substr(1);

                    if (field != "data")
                        continue;

                    if (value == "[DONE]")
                    {
                        if (!line_buffer.empty())
                        {
                            size_t flush_pos = 0;
                            while (flush_pos < line_buffer.size())
                            {
                                size_t flush_newline = line_buffer.find('\n', flush_pos);
                                std::string flush_line;

                                if (flush_newline == std::string::npos)
                                {
                                    flush_line = line_buffer.substr(flush_pos);
                                    flush_pos = line_buffer.size();
                                }
                                else
                                {
                                    flush_line = line_buffer.substr(flush_pos, flush_newline - flush_pos);
                                    if (!flush_line.empty() && flush_line.back() == '\r')
                                    {
                                        flush_line.pop_back();
                                    }
                                    flush_pos = flush_newline + 1;
                                }

                                if (flush_line.empty() || flush_line[0] == ':')
                                    continue;

                                size_t flush_colon = flush_line.find(':');
                                if (flush_colon == std::string::npos)
                                    continue;

                                std::string flush_field = flush_line.substr(0, flush_colon);
                                std::string flush_value = flush_line.substr(flush_colon + 1);

                                if (!flush_value.empty() && flush_value[0] == ' ')
                                    flush_value = flush_value.substr(1);

                                if (flush_field != "data")
                                    continue;

                                if (flush_value == "[DONE]")
                                    break;

                                ++chunk_count;
                                if (!on_chunk(flush_value))
                                {
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
                        return false;
                    }

                    ++chunk_count;
                    if (!on_chunk(value))
                    {
                        aborted = true;
                        My_Log{} << "[CloudModelClient] SSE stream aborted by callback (chunks="
                                  << chunk_count << ")" << std::endl;
                        return false;
                    }
                }

                if (pos < line_buffer.size())
                {
                    line_buffer = line_buffer.substr(pos);
                }
                else
                {
                    line_buffer.clear();
                }

                return true;
            });

        if (received_done)
        {
            if (config_.log_debug) {
                My_Log{} << "[CloudModelClient] [DBG] SSE stream: received [DONE], completed successfully (chunks="
                          << chunk_count << ")" << std::endl;
            }
            return true;
        }

        if (aborted)
        {
            if (config_.log_debug) {
                My_Log{} << "[CloudModelClient] [DBG] SSE stream: aborted by consumer (client disconnected?), chunks="
                          << chunk_count << std::endl;
            }
            return true;
        }

        if (!result)
        {
            error_msg = "SSE stream request failed: " + httplib::to_string(result.error());
            My_Log{My_Log::Level::kError} << "[CloudModelClient] " << error_msg
                                          << " [target=" << scheme << "://" << host << ":" << port << path << "]"
                                          << " [httplib_error=" << static_cast<int>(result.error()) << "=" << httplib::to_string(result.error()) << "]"
                                          << " [chunks_received=" << chunk_count << "]"
                                          << std::endl;
            return false;
        }

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

        auto now = std::chrono::steady_clock::now();
        auto ms_since_last_data = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - last_data_time).count();

        error_msg = "SSE stream ended without [DONE] marker";

        if (chunk_count > 0)
        {
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

    json req = request;
    if (!config_.model.empty())
    {
        req["model"] = config_.model;
    }
    else if (!req.contains("model") || req["model"].get<std::string>().empty())
    {
        req["model"] = "";
    }

    req["stream"] = false;

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

    json req = request;
    if (!config_.model.empty())
    {
        req["model"] = config_.model;
    }
    else if (!req.contains("model") || req["model"].get<std::string>().empty())
    {
        req["model"] = "";
    }

    req["stream"] = true;

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
                continue;
            }
        }
        order.push_back(i);
    }
    return order;
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
#endif

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

            if (http_result.http_status == 400 || http_result.http_status == 401 ||
                http_result.http_status == 403) {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] Endpoint '" << ep.name
                                              << "' returned " << http_result.http_status << ", not retrying" << std::endl;
                return false;
            }

            if (http_result.http_status == 429) {
                My_Log{My_Log::Level::kError} << "[CloudModelClient] Endpoint '" << ep.name << "' returned 429 (rate limited)" << std::endl;
                Backoff(r + 1);
                if (!config_.retry.retry_on_429_switch_endpoint) {
                    continue;
                } else {
                    break;
                }
            }

            MarkFailure(ep.name);
            if (ShouldCircuitBreak(ep.name)) {
                EnterCooldown(ep.name);
            }
            break;
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
    bool all_nonfatal = false;

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

        bool is_nonfatal_stream_end = (err == "SSE stream ended without [DONE] marker");

        if (is_nonfatal_stream_end)
        {
            if (config_.log_debug) {
                My_Log{My_Log::Level::kWarning}
                    << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: endpoint='"
                    << ep.name << "' stream ended without [DONE] (non-fatal, not triggering circuit breaker)"
                    << std::endl;
            }
            all_nonfatal = true;
        }
        else
        {
            if (config_.log_debug) {
                My_Log{My_Log::Level::kError}
                    << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: endpoint='"
                    << ep.name << "' failed with fatal error, err=" << err << std::endl;
            }
            MarkFailure(ep.name);
            if (ShouldCircuitBreak(ep.name)) {
                EnterCooldown(ep.name);
            }
            all_nonfatal = false;
        }
    }

    err = "all endpoints failed for streaming";
    if (all_nonfatal)
    {
        if (config_.log_debug) {
            My_Log{My_Log::Level::kWarning}
                << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: all endpoints returned without [DONE]"
                << " (non-fatal: consumer disconnected or proxy truncated, data already delivered)"
                << std::endl;
        }
    }
    else
    {
        if (config_.log_debug) {
            My_Log{My_Log::Level::kError}
                << "[CloudModelClient] [DBG] DoStreamRequestWithFallback: all endpoints failed"
                << std::endl;
        }
    }
    return false;
}
