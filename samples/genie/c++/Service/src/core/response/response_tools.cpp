#include "response_tools.h"
#include "core/log.h"
#include "core/utils.h"

std::string ResponseTools::generate_uuid4()
{
    static std::random_device rd;
    static std::mt19937 gen(rd());
    static std::uniform_int_distribution<> dis(0, 15);
    static std::uniform_int_distribution<> dis2(8, 11);

    auto generate_hex = [&](int count)
    {
        std::stringstream ss;
        for (int i = 0; i < count; ++i)
        {
            ss << std::hex << dis(gen);
        }
        return ss.str();
    };

    std::stringstream ss;
    ss << generate_hex(8) << "-"      // 8 hex digits
       << generate_hex(4) << "-"      // 4 hex digits
       << "4" << generate_hex(3) << "-" // 4 + 3 hex digits (UUID version 4)
       << dis2(gen) << generate_hex(3) << "-" // 1 special + 3 hex digits
       << generate_hex(12);           // 12 hex digits

    return "chatcmpl-" + ss.str();
}

bool ResponseTools::post_stream_data(httplib::DataSink &sink, const char *event, const json &data)
{
    std::string str;
    try
    {
        str = std::string(event) + ": " +
              json_to_str(data) +
              "\n\n";
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "data stream pause failed: " << e.what() << std::endl;
    }

    return sink.write(str.c_str(), str.size());
}

json ResponseTools::responseDataJson(const std::string &content,
                                     const std::string &finish_reason,
                                     bool stream,
                                     const std::string &tool_calls_str)
{
    std::string id = generate_uuid4();
    std::string object = stream ? "chat.completion.chunk" : "chat.completion";
    std::string content_name = stream ? "delta" : "message";
    int created = timer.GetSystemTime();
    json tool_calls = json(nullptr);

    if (!tool_calls_str.empty())
    {
        tool_calls = format_tool_calls(tool_calls_str);
    }

    /* @formatter:off */
    json data = {
            {"id", id},
            {"object", object},
            {"model", ""},
            {"created", created},
            {"choices", {{
                                 {"index", 0},
                                 {"finish_reason", finish_reason},
                                 {content_name, {
                                                        {"content", content},
                                                        {"role", "assistant"},
                                                        {"tool_calls", tool_calls}
                                                }
                                 }}}},
            {"usage", {
                         {"prompt_tokens", 0},
                         {"completion_tokens", 0},
                         {"total_tokens", 0}
            }}
    };
    /* @formatter:on */
    return data;
}

std::string ResponseTools::convertToolCallJson(const std::string &input)
{
    std::string jsonStr = extractJsonFromToolCall(input);
    json root;
    try
    {
        root = json::parse(jsonStr);
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "parse tool calls's message as json failed:" << e.what() << std::endl;
        root["name"] = "unknow";
        root["arguments"] = jsonStr;
        goto done;
    }

    if (!root.contains("name") || !root["name"].is_string())
    {
        My_Log{My_Log::Level::kError} << "the tool calls's name as string failed\n";
        root["name"] = "unknow";
    }

    // Always be string, whatever input is object or string.
    // after dump, the object will be escaping string
    if (!root.contains("arguments") || (!root["arguments"].is_object() && !root["arguments"].is_string()))
    {
        My_Log{My_Log::Level::kError} << "parse tool calls's args as json failed:" << std::endl;
        root["arguments"] = jsonStr;
        goto done;
    }

    done:
    return wrapJsonInToolCall(root.dump());
}

std::string ResponseTools::remove_tool_call_content(const std::string &input)
{
    static std::regex tool_call_block(R"(<tool_call>[\s\S]*?<\/tool_call>\s*)");
    static std::regex name_line(R"((\s*\{ *"name": [^\n]*\n?))");
    std::string result = std::regex_replace(input, tool_call_block, "");
    result = std::regex_replace(result, name_line, "");
    result = remove_empty_lines(result);
    return result;
}

std::string ResponseTools::remove_empty_lines(const std::string &input)
{
    return std::regex_replace(input, std::regex(R"((^\s*\n)+)"), "");
}

std::string ResponseTools::json_to_str(const json &data)
{
    return data.dump(-1, ' ', false, json::error_handler_t::replace);
}

json ResponseTools::format_tool_calls(const std::string &tool_calls_str)
{
    std::istringstream iss(tool_calls_str);
    std::string line, name, arguments;

    json call;
    json tool_calls = json::array();

    while (std::getline(iss, line))
    {
        if (line.empty() || line.find("{\"name\":") != 0) continue;
        try
        {
            call = json::parse(line);
        }
        catch (const std::exception &e)
        {
            My_Log{} << "parse handled tool calls message failed:" << e.what()
                     << "  message: " << line << std::endl;
            continue;
        }

        json tool_call = {
                {"id",       generate_uuid4()},
                {"type",     "function"},
                {"function", {
                                     {"name", call["name"]},
                                     {"arguments", call["arguments"].dump()}   // dump at final make json
                             }}
        };
        tool_calls.push_back(tool_call);
    }

    return tool_calls;
}

std::string ResponseTools::extractJsonFromToolCall(const std::string &input)
{
    std::string output = str_replace(input, "<tool_call>", "");
    output = str_replace(output, "</tool_call>", "");
    return output;
}

