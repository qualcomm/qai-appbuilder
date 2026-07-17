//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <fstream>
#include <filesystem>
#include <vector>
#include <string>
#include <GenieAPILibrary.h>
#include <utils.h>
#ifdef _WIN32
#include <windows.h>
#endif

using namespace std;

// Extract answer text from streaming chunk
bool stream_process(const std::string &chunk)
{
    if (chunk.empty())
    {
        return true;
    }

    // Handle DONE signal - filter out various DONE formats
    if (chunk == "[DONE]" || chunk == "DONE" ||
        chunk.find("[DONE]") == 0 ||
        chunk.find("data: [DONE]") != std::string::npos)
    {
        return true;
    }

    try
    {
        std::string json_str = chunk;

        // Remove "data: " prefix if present
        if (json_str.find("data: ") == 0)
        {
            json_str = json_str.substr(6);
        }

        // Try to parse as JSON
        json j = json::parse(json_str);

        // Try multiple possible JSON structures
        // Structure 1: {"choices": [{"delta": {"content": "..."}}]}
        if (j.contains("choices") && j["choices"].size() > 0)
        {
            auto &choice = j["choices"][0];
            if (choice.contains("delta") && choice["delta"].contains("content"))
            {
                std::string token = choice["delta"]["content"].get<std::string>();
                // Filter out special tokens
                if (!token.empty() && token.find("<|end|>") == std::string::npos)
                {
                    printf("%s", token.c_str());
                    fflush(stdout);
                    return true;
                }
            }
            // Structure 2: {"choices": [{"message": {"content": "..."}}]}
            if (choice.contains("message") && choice["message"].contains("content"))
            {
                std::string token = choice["message"]["content"].get<std::string>();
                // Filter out special tokens
                if (!token.empty() && token.find("<|end|>") == std::string::npos)
                {
                    printf("%s", token.c_str());
                    fflush(stdout);
                    return true;
                }
            }
            // Structure 3: {"choices": [{"text": "..."}]}
            if (choice.contains("text"))
            {
                std::string token = choice["text"].get<std::string>();
                // Filter out special tokens
                if (!token.empty() && token.find("<|end|>") == std::string::npos)
                {
                    printf("%s", token.c_str());
                    fflush(stdout);
                    return true;
                }
            }
        }

        // Structure 4: {"content": "..."}
        if (j.contains("content"))
        {
            std::string token = j["content"].get<std::string>();
            // Filter out special tokens
            if (!token.empty() && token.find("<|end|>") == std::string::npos)
            {
                printf("%s", token.c_str());
                fflush(stdout);
                return true;
            }
        }

        // Structure 5: {"text": "..."}
        if (j.contains("text"))
        {
            std::string token = j["text"].get<std::string>();
            if (!token.empty())
            {
                printf("%s", token.c_str());
                fflush(stdout);
                return true;
            }
        }

        // Structure 6: {"token": "..."}
        if (j.contains("token"))
        {
            std::string token = j["token"].get<std::string>();
            if (!token.empty())
            {
                printf("%s", token.c_str());
                fflush(stdout);
                return true;
            }
        }

        // If no known structure matched, silently ignore
    }
    catch (const std::exception &e)
    {
        // If not JSON, check if it's a special token before printing
        if (chunk.find("data: [DONE]") == std::string::npos &&
            chunk.find("<|end|>") == std::string::npos)
        {
            printf("%s", chunk.c_str());
            fflush(stdout);
        }
    }
    return true;
}

// Extract answer text from non-streaming result JSON
std::string extract_answer(const std::string &result_json)
{
    try
    {
        json outer = json::parse(result_json);
        std::string response_str = outer["response"].get<std::string>();
        json inner = json::parse(response_str);
        if (inner.contains("choices") && inner["choices"].size() > 0)
        {
            auto &msg = inner["choices"][0]["message"];
            if (msg.contains("content"))
            {
                std::string content = msg["content"].get<std::string>();
                // Remove trailing <|end|> token if present
                auto pos = content.rfind("<|end|>");
                if (pos != std::string::npos)
                {
                    content = content.substr(0, pos);
                }
                return content;
            }
        }
    }
    catch (std::exception &)
    {
    }
    return result_json;
}

static void print_usage(const char *prog)
{
    printf("Usage: %s --config <config.json> <input.json>\n", prog);
    printf("  --config <config.json>  Path to a single model's config.json (required).\n");
    printf("                          The model directory is derived from this path.\n");
    printf("  <input.json>            Path to the input JSON (messages / string / question).\n");
    fflush(stdout);
}

int main(int argc, char **argv)
{
    const char *question = nullptr;
    const char *external_config_path = nullptr;

    // 手写扫描 --config <path>,其余按顺序作为位置参数(输入 JSON 路径),
    // 不引入新的 CLI 解析依赖,与本文件现有风格一致。
    std::vector<const char *> positional_args;
    for (int i = 1; i < argc; ++i)
    {
        if (std::string(argv[i]) == "--config" && i + 1 < argc)
        {
            external_config_path = argv[++i];
        }
        else
        {
            positional_args.push_back(argv[i]);
        }
    }

    // --config 现为必填:模型只靠 -c 传入的 config.json 驱动,不再回落到任何编译期宏配置。
    if (!external_config_path)
    {
        printf("Error: --config <config.json> is required.\n");
        print_usage(argv[0]);
        return 1;
    }

    if (!positional_args.empty())
    {
        question = positional_args[0];
    }

    // active_config 直接就是 --config 指定的路径(MNN 需要真实文件路径才能 Llm::createLLM);
    // active_model_path 取其父目录,等价于 GenieAPIService.exe -c 的单模型加载路径。
    std::string active_config = std::string(external_config_path);
    std::string active_model_path =
            std::filesystem::path(external_config_path).parent_path().generic_string();

    // model_name(.bin 列表)与 hwinfo 在 -c 模式下被引擎忽略,传空 vector 与占位值即可,
    // 保持 api_loadmodel 现有函数签名不变。
    std::vector<std::string> model_name{};
    std::string llm_hardware_info{"NPU"};

    api_interface llm = api_interface(active_config, Level::kWarning);

    // Read the model files and load onto the target device.
    Timer timeModelToHTPHelper;
    llm.api_loadmodel(active_model_path, model_name, llm_hardware_info);
#ifdef _WIN32
    SleepEx(3000, TRUE);
#endif
    printf("status: %d\n", llm.api_status());
    if (llm.api_status() == error)
    {
        printf("exit due to error 1\n");
        llm.api_unloadmodel();
        return 1;
    }

    std::string q = "";
    std::string a = "";

    if (question)
    {
        q = question;
    }
    std::ifstream in(q.c_str());
    if (!in.good())
    {
        printf("Error: cannot open question file: %s\n", q.c_str());
        llm.api_unloadmodel();
        return 3;
    }

    json j;
    in >> j;
    in.close();

    // Extract the actual question text from JSON for display
    std::string question_text;
    std::string input_json_str;

    if (j.is_string())
    {
        question_text = j.get<std::string>();
        input_json_str = question_text;
    }
    else if (j.contains("messages") && j["messages"].size() > 0)
    {
        // Find the user-role message; fall back to first message
        const json *user_msg = nullptr;
        for (auto &msg: j["messages"])
        {
            if (msg.contains("role") && msg["role"].get<std::string>() == "user")
            {
                user_msg = &msg;
                break;
            }
        }
        if (!user_msg)
            user_msg = &j["messages"][0];

        auto &content = (*user_msg)["content"];
        if (content.is_string())
        {
            question_text = content.get<std::string>();
        }
        else if (content.is_array())
        {
            // OpenAI-style: [{"type":"text","text":"..."}]
            for (auto &item: content)
            {
                if (item.value("type", "") == "text" && item.contains("text"))
                {
                    question_text = item["text"].get<std::string>();
                    break;
                }
            }
        }
        else if (content.is_object() && content.contains("question"))
        {
            question_text = content["question"].get<std::string>();
        }

        if (!question_text.empty())
        {
            input_json_str = j.dump();
        }
        else
        {
            question_text = "Hello, how are you?";
            input_json_str = question_text;
        }
    }
    else if (j.contains("question"))
    {
        question_text = j["question"].get<std::string>();
        input_json_str = question_text;
    }
    else
    {
        question_text = "Hello, how are you?";
        input_json_str = question_text;
    }

    printf("\n============== Question ==============\n");
    printf("Q: %s\n", question_text.c_str());
    printf("\nA: ");
    fflush(stdout);

    // Streaming inference - pass full JSON string (includes image) to model
    try
    {
        llm.api_Generate(input_json_str, stream_process);
        printf("\n");
        fflush(stdout);
    } catch (const std::exception &e)
    {
        printf("\n[Error in inference: %s]\n", e.what());
    }

    if (llm.api_status() == error)
    {
        printf("exit due to inference error\n");
        llm.api_unloadmodel();
        return 2;
    }

    printf("==========================================\n");
    fflush(stdout);

    printf("unload model\n");
    llm.api_unloadmodel();
    printf("status: %d\n", llm.api_status());
    if (llm.api_status() == error)
    {
        printf("exit due to error 6\n");
        return 6;
    }

    printf("\n......exit......\n");

    return 0;
}
