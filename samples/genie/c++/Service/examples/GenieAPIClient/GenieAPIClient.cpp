//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <string>
#include <sstream>
#include <log.h>
#include <thread>
#include <random>
#include <fstream>
#include <base64.h>
#include <curl/curl.h>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

std::string SVR_URL;

static class CLI
{
public:
    std::string prompt;
    std::string picture_path_;
    std::string audio_path_;
    std::string system{"You are a helpful assistant."};
    std::string model{"IBM-Granite-v3.1-8B"};
    std::string raw_file_;
    bool loop{false};
    bool stream{false};
    std::string ip{"127.0.0.1"};

    template<typename CharT>
    static void Init(int argc, CharT **argv);

private:
    static std::string ArgToString(const char *s)
    {
        return std::string(s); // plain char* → std::string
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
        else if (ArgToString(arg) == "--img" && i + 1 < argc)
            cli_info.picture_path_ = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--audio" && i + 1 < argc)
            cli_info.audio_path_ = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--loop")
            cli_info.loop = true;
        else if (ArgToString(arg) == "--ip")
            cli_info.ip = ArgToString(argv[++i]);
        else if (ArgToString(arg) == "--raw_file")
            cli_info.raw_file_ = ArgToString(argv[++i]);
    }

    SVR_URL = std::string{"http://"} + cli_info.ip + ":8910/v1/chat/completions";
    My_Log{} << "url path: " << SVR_URL << "\n";
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
    bool process;
    try
    {
        process = false;
        while (std::getline(stream, line))
        {
            process = true;
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
                    My_Log{}.original(true) << j["choices"][0]["delta"]["content"].get<std::string>();
                }
            }
        }
        if (!process)
        {
            My_Log{} << chunk << "\n";
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

template<typename T1, typename T2>
std::string build_request_body(const std::string &model, const T1 &prompt, const T2 &system, bool stream)
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

    return body.dump(4);
}

void Do(const std::string &body)
{
    CURLcode res;
    curl_global_init(CURL_GLOBAL_ALL);
    CURL *curl = curl_easy_init();
    if (!curl)
    {
        curl_global_cleanup();
        return;
    }

    curl_easy_setopt(curl, CURLOPT_URL, SVR_URL.c_str());
    struct curl_slist *headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");

    if (cli_info.stream)
    {
        My_Log{} << "reponse mode: stream mode\n";
        headers = curl_slist_append(headers, "Accept: text/event-stream");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback_stream);
    }
    else
    {
        My_Log{} << "reponse mode: non-stream mode\n";
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &message);
    }

    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());

    long response_code = 0;
    std::string full_response;
    My_Log{} << "Response: \n";
    if ((res = curl_easy_perform(curl)) != CURLE_OK)
    {
        My_Log{} << "curl_easy_perform() failed: " << curl_easy_strerror(res) << std::endl;
        goto clean;
    }

    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response_code);
    if (response_code != 200)
    {
        My_Log{} << "get failed response code: " << response_code << std::endl;
        My_Log{} << "the error message: " << message << std::endl;
        goto clean;
    }

    if (!cli_info.stream && !message.empty())
    {
        try
        {
            full_response = json::parse(message).dump(4);
            My_Log{} << json::parse(message)["choices"][0]["message"]["content"].get<std::string>()
                     << std::endl;
        }
        catch (std::exception &e)
        {
            My_Log{My_Log::Level::kError} << e.what() << "\n";
            My_Log{} << (full_response.empty() ? message : full_response) << "\n";
        }
    }

    clean:
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    curl_global_cleanup();
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

std::string EncodeBinary(std::string &path)
{
    if (path.empty())
    {
        return "";
    }

    std::ifstream in(path, std::ios::binary);
    if (!in.good())
    {
        throw std::runtime_error("open file path:" + path + " failed\n");
    }

    in.seekg(0, std::ios::end);
    std::vector<char> buf(in.tellg());
    in.seekg(0, std::ios::beg);
    if (!in.read(reinterpret_cast<char *>(buf.data()), buf.size()))
    {
        My_Log{My_Log::Level::kError} << "read form file failed\n";
        return "";
    }

    char *out_buf = new char[BASE64_ENCODE_OUT_SIZE(buf.size())];
    int size = Base64Encode(buf.data(), buf.size(), out_buf);
    if (size == 0)
    {
        My_Log{My_Log::Level::kError} << "encode to binrary failed\n";
        delete[] out_buf;
        return "";
    }

    std::string out(const_cast<const char *>(out_buf), size);
    delete[] out_buf;
    return out;
}

json BuildUserContentV1()
{
    json j_user;
    j_user["question"] = cli_info.prompt;
    j_user["image"] = EncodeBinary(cli_info.picture_path_);
    j_user["audio"] = EncodeBinary(cli_info.audio_path_);
    return j_user;
}

void BuildUserContentV2(json &j_system, json &j_user)
{
    j_user = json::array();
    json text_item;
    text_item["type"] = "text";
    text_item["text"] = cli_info.prompt;
    j_user.push_back(text_item);

    if (!cli_info.picture_path_.empty())
    {
        json img_item;
        img_item["type"] = "image_url";
        img_item["image_url"] = json::object();
        img_item["image_url"]["url"] = "data:image/png;base64," + EncodeBinary(cli_info.picture_path_);
        j_user.push_back(img_item);
    }

    j_system = json::array();
    text_item["type"] = "text";
    text_item["text"] = cli_info.system;
    j_system.push_back(text_item);
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

    try
    {
        std::string req_body;
        if (!cli_info.raw_file_.empty())
        {
            std::ifstream in(cli_info.raw_file_);
            if (!in.good())
            {
                My_Log{} << "open file path:" << cli_info.raw_file_ << " failed\n";
                return 1;
            }

            json j;
            in >> j;
            req_body = j.dump(4);
            cli_info.stream = j["stream"];
            goto ahead;
        }

        if (cli_info.prompt.empty())
        {
            if (cli_info.picture_path_.empty() && cli_info.audio_path_.empty())
            {
                cli_info.prompt = "Hi!";
            }
            else
            {
                cli_info.prompt = "What is it descript?";
            }
        }


//        json j_system, j_user;
//        BuildUserContentV2(j_system, j_user);
//        std::string req_body = build_request_body(cli_info.model, j_user, j_system, cli_info.stream);
        req_body = build_request_body(cli_info.model, BuildUserContentV1(), cli_info.system, cli_info.stream);
//        std::string req_body = build_request_body(cli_info.model, cli_info.prompt, cli_info.system, cli_info.stream);

        ahead:
        cli_info.loop ? DoLoop() : Do(req_body);
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << e.what();
    }
    return 0;
}
