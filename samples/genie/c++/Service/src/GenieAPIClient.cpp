//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <string>
#include <sstream>
#include <curl/curl.h>
#include <core/log.h>
#include <nlohmann/json.hpp>
#include <thread>
#include <random>

using json = nlohmann::ordered_json;
std::string SVR_URL = "http://10.92.143.176:8910/v1/chat/completions";

static class CLI
{
public:
    std::string prompt{"hello"};
    std::string system{"You are a helpful assistant."};
    std::string model{"IBM-Granite-v3.1-8B"};
    bool loop{false};
    bool stream{false};
    std::string ip{"127.0.0.1"};

    template<typename CharT>
    static void Init(int argc, CharT **argv);

private:
    static std::string ArgToString(const char *s)
    {
        return std::string(s);             // plain char* → std::string
    }

#ifdef WIN32

    static std::string ArgToString(const wchar_t *s)
    {
        int len = WideCharToMultiByte(
                CP_UTF8, 0,
                s,
                wcslen(s),
                nullptr, 0,
                nullptr,
                nullptr);
        std::string out(len, '\0');
        WideCharToMultiByte(
                CP_UTF8, 0,
                s,
                wcslen(s),
                out.data(),
                len,
                nullptr,
                nullptr);
        return out;
    }

#endif
} cli_info;

template<typename CharT>
void CLI::Init(int argc, CharT **argv)
{
    for (int i = 1; i < argc; ++i)
    {
        CharT *arg = argv[i];
        if (ArgToString(arg) == "--stream")
            cli_info.stream = true;
        else if (ArgToString(arg) == "--prompt" && i + 1 < argc)
            cli_info.prompt = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--system" && i + 1 < argc)
            cli_info.system = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--model" && i + 1 < argc)
            cli_info.model = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--loop")
            cli_info.loop = true;
        else if (ArgToString(arg) == "--ip")
            cli_info.ip = ArgToString(argv[++i]);
    }
    SVR_URL = std::string{"http://"} + cli_info.ip + ":8910/v1/chat/completions";
    My_Log{} << SVR_URL << "\n";
}

static class Question
{
public:
    const char *ChoseOne()
    {
        static thread_local std::mt19937 rng{std::random_device{}()};
        std::uniform_int_distribution<std::size_t> dist{0, question.size() - 1};
        return question[dist(rng)];
    }

private:
    const std::vector<const char *> question{
            "今天天气如何？",
            "高通公司是怎么样的企业",
            "3000 字爱国作文",
            "你是哪个大模型？",
            "哥特巴赫猜想是什么"
    };
} question;

std::string message;

size_t write_callback_stream(char *ptr, size_t size, size_t nmemb, void *userdata)
{
    size_t total_size = size * nmemb;
    std::string chunk(ptr, total_size);
    message += chunk;
    std::istringstream stream(chunk);
    std::string line;

    try
    {
        while (std::getline(stream, line))
        {
            if (line.rfind("data: ", 0) == 0)
            {
                std::string jsonStr = line.substr(6);
                if (jsonStr == "[DONE]")
                {
                    return total_size;
                }

                json j = json::parse(jsonStr);
                if (j.contains("choices") && !j["choices"].empty() &&
                    j["choices"][0].contains("delta") &&
                    j["choices"][0]["delta"].contains("content"))
                {
                    My_Log{}.original(true) << j["choices"][0]["delta"]["content"].get<std::string>() ;
                }

            }
        }
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what() << "\n";
        My_Log{} << chunk << "\n";
    }

    return total_size;
}

size_t write_callback(void *contents, size_t size, size_t nmemb, void *userp)
{
    ((std::string *) userp)->append((char *) contents, size * nmemb);
    return size * nmemb;
}

std::string
build_request_body(const std::string &model, const std::string &prompt, const std::string &system, bool stream)
{
    json body;
    body["model"] = model;
    body["stream"] = stream;
    body["messages"] = {
            {{"role", "system"}, {"content", system}},
            {{"role", "user"},   {"content", prompt}}
    };
    body["size"] = 4096;
    body["seed"] = 146;
    body["temp"] = 1.5;
    body["top_k"] = 13;
    body["top_p"] = 0.6;

    return body.dump();
}

void Do(const std::string &body)
{
    CURL *curl = curl_easy_init();
    if (!curl)
    {
        return;
    }
    curl_easy_setopt(curl, CURLOPT_URL, SVR_URL.c_str());
    struct curl_slist *headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    std::string response_string;

    if (cli_info.stream)
    {
        headers = curl_slist_append(headers, "Accept: text/event-stream");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback_stream);
    }
    else
    {
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_string);
    }

    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());

    CURLcode res = curl_easy_perform(curl);

    long response_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response_code);
    if (response_code != 200)
    {
        My_Log{} << "get failed response code: " << response_code << std::endl;
        My_Log{} << "the error message: " << message << std::endl;
        goto clean;
    }

    if (!response_string.empty())
        My_Log{} << nlohmann::json::parse(response_string)["choices"][0]["message"]["content"].get<std::string>()
                 << std::endl;

    if (res != CURLE_OK)
    {
        My_Log{} << "curl_easy_perform() failed: " << curl_easy_strerror(res) << std::endl;
        goto clean;
    }

    clean:
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
}

// TODO: Stop
void DoLoop()
{
    while (true)
    {
        My_Log{} << question.ChoseOne() << std::endl;
        Do(build_request_body(cli_info.model, question.ChoseOne(), cli_info.system, cli_info.stream));
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}

#ifdef WIN32
#define MAIN int wmain(int argc, wchar_t *argv[])
#else
#define MAIN int main(int argc, char *argv[])
#endif

MAIN
{
    My_Log::Init(static_cast<My_Log::Level>(My_Log::Level::kInfo), "");
    CLI::Init(argc, argv);

#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)
    SetConsoleOutputCP(CP_UTF8);
#endif

    curl_global_init(CURL_GLOBAL_ALL);
    try
    {
        cli_info.loop ?
        DoLoop() : Do(build_request_body(cli_info.model, cli_info.prompt, cli_info.system, cli_info.stream));
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what();
    }
    curl_global_cleanup();
    return 0;
}

