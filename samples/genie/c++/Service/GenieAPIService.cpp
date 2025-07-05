//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "Utils.h"
#include "GenieBuilder.h"
#include <iostream>
#include <string>
#include <csignal>

// TODO: support run 'Query' more than 3 times if input question is too long.

#define DEBUG_PROMPT 1
#define DEBUG_OUTPUT 1
#define JSON_RESPONSE 1  // TODO: remove this in the future.


static std::string s_cmd_model_name = "";
static std::string s_cmd_config_file = "";
static bool s_cmd_load_model = false;
static bool s_cmd_all_text = false;
static bool s_cmd_enable_thinking = false;

static std::string s_model_path = "";
static std::string s_model_root = "";
static std::string s_model_name = "";
static std::vector<std::string> s_model_list;

static std::string s_prompt_tag1 = "";
static std::string s_prompt_tag2 = "";
static std::string s_prompt_tag3 = "";
static std::string s_tools_output = "";
static std::string s_tools_output_history = "";
static std::string s_prompt_history = "";

static std::unique_ptr<GenieContext> s_genie_context;
static TimerHelper timer;

static bool s_client_connected = false;

static bool post_stream_data(httplib::DataSink & sink, const char * event, const json & data) {
  const std::string str = std::string(event) + ": " +
                          json_to_str(data) +
                          "\n\n";

  // My_Log("data stream, to_send: " + str);

  return sink.write(str.c_str(), str.size());
}

bool is_connection_alive(const httplib::Request& req) {
  return !req.is_connection_closed();
}

json format_tool_calls(const std::string& tool_calls_str) {
    std::istringstream iss(tool_calls_str);
    std::string line;
    json tool_calls = json::array();
    s_tools_output_history = "";

    while (std::getline(iss, line)) {
        if (line.empty() || line.find("{\"name\":") != 0) continue;
        try {
            json call = json::parse(line);
            if (!call.is_object() || !call.contains("name") || !call.contains("arguments")) continue;
            s_tools_output_history += "<tool_call>\n" + line + "\n</tool_call>\n";
            json tool_call = {
                {"id", generate_uuid4()},
                {"type", "function"},
                {"function", {
                    {"name", call["name"]},
                    {"arguments", call["arguments"].dump()}
                }}
            };
            tool_calls.push_back(tool_call);
        } catch (...) {
        }
    }

    return tool_calls;
}

json response_data(const std::string& content, const std::string& finish_reason, bool stream=true, 
                   const std::string& tool_calls_str = "") {
  std::string id = generate_uuid4();
  std::string object = stream ? "chat.completion.chunk" : "chat.completion";
  std::string content_name = stream ? "delta" : "message";
  int created = timer.GetSystemTime();
  json tool_calls = json(nullptr);
  
  if (!tool_calls_str.empty()) {
    tool_calls = format_tool_calls(tool_calls_str);
  }

#ifdef JSON_RESPONSE
  json data = {
    {"id", id},
    {"object", object},
    {"model", s_model_name},
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

  return data;
#else
  content = escape_string(content);

  std::string jsonStr = fmt::format(R"(
    {{
        "id": "{}",
        "object": "{}",
        "model": "{}",
        "created": {},
        "choices": [
            {{
                "index": 0,
                "finish_reason": "{}",
                "{}": {{
                    "content": "{}",
                    "role": "assistant"
                }}
            }}
        ],
        "usage": {{
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }}
    }}
  )", id, object, s_model_name, created, finish_reason, content_name, content);

  json data = json::parse(jsonStr);
  // std::cout << data << std::endl;

  return data;
#endif
}

// https://www.openaidoc.com.cn/api-reference/models
json get_model_list() {
  json jsonData;

  std::vector<json> models;
  bool list_inited = false;

  if (s_model_list.size() > 0) {
    list_inited = true;
  }

  if (fs::exists(s_model_root) && fs::is_directory(s_model_root)) {
    for (const auto& entry : fs::directory_iterator(s_model_root)) {
        if (entry.is_directory()) {
            bool has_bin = false, has_tokenizer = false, has_prompt = false;
            for (const auto& file : fs::directory_iterator(entry.path())) {
                auto filename = file.path().filename().string();
                if (file.is_regular_file()) {
                    if (filename == "tokenizer.json") has_tokenizer = true;
                    else if (filename == "prompt.conf") has_prompt = true;
                    else if (file.path().extension() == ".bin") has_bin = true;
                }
            }

            if (has_bin && has_tokenizer && has_prompt) {
                std::string model_name = entry.path().filename().string();
                if (false == list_inited) {
                  s_model_list.push_back(model_name);
                }

                json model;
                model["id"] = model_name;
                model["object"] = "model";
                model["created"] = timer.GetSystemTime();
                model["owned_by"] = "owner";
                model["permission"] = json::array();
                models.push_back(model);  
            }
        }
    }
  }

  jsonData["data"] = models;
  jsonData["object"] = "list";

  // std::cout << jsonData.dump(4) << std::endl;

  return jsonData;
}

bool process_arguments(int argc, char* argv[]) {
  cli::Parser parser(argc, argv);

  parser.set_optional<std::string>("c", "config_file", "", "Path to the config file.");
  parser.set_optional<std::string>("m", "model_name", "", "Name of the model to use.");
  parser.set_optional<bool>("l", "load_model", false, "Load the model.");
  parser.set_optional<bool>("a", "all_text", false, "Output all text includes tool calls text.");
  parser.set_optional<bool>("t", "enable_thinking", false, "Enable thinking mode.");

  parser.run_and_exit_if_error();

  s_cmd_config_file = parser.get<std::string>("c");
  s_cmd_model_name = parser.get<std::string>("m");
  s_cmd_load_model = parser.get<bool>("l");
  s_cmd_all_text = parser.get<bool>("a");
  s_cmd_enable_thinking = parser.get<bool>("t");

  My_Log("config_file: " + s_cmd_config_file);
  My_Log("model_name: " + s_cmd_model_name);
  if (s_cmd_load_model) {
    My_Log("load model: Yes");
  } else {
    My_Log("load model: No");
  }
  printf("\n");

  return true;
}

void get_profile() {
  std::string jsonStr = s_genie_context->GetProfile();
  json j = json::parse(jsonStr);

  std::cout << std::endl;
  std::cout << "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl;

  if (!j["components"].empty() && j["components"][0]["type"] == "dialog") {
    const auto& events = j["components"][0]["events"];
    if (!events.empty()) {
        const auto& last_event = events.back();
        if (last_event.at("type") == "GenieDialog_query") {
          auto time_to_first_token = (int)(last_event.at("time-to-first-token")["value"].get<int>() / 1000);
          std::cout << "Time to First Token: " << time_to_first_token << " ms" << std::endl;

          auto token_generation_time = (int)(last_event.at("token-generation-time")["value"].get<int>() / 1000);
          std::cout << "Token Generation Time: " <<  token_generation_time << " ms" << std::endl;

          std::cout << "Num Prompt Tokens: " << last_event.at("num-prompt-tokens")["value"] << " " << std::endl;
          std::cout << "Prompt Processing Rate: " << (int)(last_event.at("prompt-processing-rate")["value"]) << " toks/sec" << std::endl;

          std::cout << "Num Generated Tokens: " << last_event.at("num-generated-tokens")["value"] << " " << std::endl;
          std::cout << "Token Generation Rate: " << (int)(last_event.at("token-generation-rate")["value"]) << " toks/sec" << std::endl;
        }
    }
  }

  std::cout << "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl << std::endl;
}

bool load_model(std::string config_file, std::string model_name) {
  fs::path config_file_path = "";
  fs::path directory_path = "";
  std::string prompt_path = "";

  if (!config_file.empty()) {  // Load model through config file. Make sure we load/reload model if config_file is provided.
    if (!fs::exists(config_file)) {
      std::cerr << "ERR:  Can't open file " + config_file << std::endl;
      return false;
    }

    config_file_path = config_file;
    directory_path = config_file_path.parent_path();
    s_model_name = directory_path.filename().string();
    s_model_path = directory_path.string();
    s_model_root = directory_path.parent_path().string();
  }
  else if (!model_name.empty()){  // Load model through model_name. If model has been loaded, don't reload it.
    if (s_model_root.empty()) { // If model_root is not set, set it to "models" directory.
      s_model_root = "models";
    }

    cout << "model root: " << s_model_root << endl;
    
    if (s_model_list.empty()) {
      get_model_list();
    }

    bool load_model = false;

    if (str_search(s_model_name, model_name)) {
      cout << s_model_name << " is loaded." << endl;
      return true;
    }
    else {
      for (const auto& item : s_model_list) {
          if (str_search(item, model_name)) {
            s_model_name = item;
            s_model_path = s_model_root + "/" + s_model_name;
            config_file_path = s_model_path + "/config.json";
            load_model = true;
            break;
          }
      }
    }

    if (load_model) {   // Continu below code to load the model.
      // cout << "loading new model" << s_model_name << endl;
    }
    else {  // Hasn't found model, return error.
      cout << "Err: model <<< " << model_name << " >>> not found" << endl;
      return false;
    }
  }

  s_genie_context = nullptr;  // Reset the context to release the model.

  // Begin to load the model.
  prompt_path = s_model_path + "/prompt.conf";

  std::cout << "model path: " << s_model_path << std::endl;
  std::cout << "model name: " << s_model_name << std::endl;

  std::ifstream prompt_file(prompt_path);
  if (!prompt_file.is_open()) {
      std::cerr << "ERR:  Can't open file " + prompt_path << std::endl;
      return false;
  }
  std::string line1, line2;
  std::getline(prompt_file, line1);
  std::getline(prompt_file, line2);
  s_prompt_tag1 = extract_tag(line1, "prompt_tags_1: ");
  s_prompt_tag2 = extract_tag(line2, "prompt_tags_2: ");
  s_prompt_tag1 = str_replace(s_prompt_tag1, "\\n", "\n");
  s_prompt_tag2 = str_replace(s_prompt_tag2, "\\n", "\n");
  s_prompt_tag3 = str_replace(s_prompt_tag2, "assistant", "tool");

  // std::cout << "tag1: " << s_prompt_tag1 << std::endl;
  // std::cout << "tag2: " << s_prompt_tag2 << std::endl;

  timer.Reset();
  std::cout << GREEN << "INFO: loading model <<< " + s_model_name + " >>>" << RESET << std::endl;
  s_genie_context = std::make_unique<GenieContext>(config_file_path.string());

  if (str_search(s_model_name, "IBM-Granite")) {
    std::string stop_sequence = "{\n  \"stop-sequence\" : [\"<|end_of_text|>\", \"<|end_of_role|>\", \"<|start_of_role|>\"]\n}";
    std::cout << "SetStopSequence: " << stop_sequence << std::endl;
    s_genie_context->SetStopSequence(stop_sequence);
  }

  std::cout << GREEN << "INFO: model <<< " + s_model_name + " >>> is ready!" << RESET << std::endl;
  timer.Print("model load time >>");

  return true;
}

void print_debug_prompt(std::string& prompt) {
#ifdef DEBUG_PROMPT
        std::cout << "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl;
        print_wstring(prompt + "\n");
#endif

#ifdef DEBUG_OUTPUT
        std::cout << "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl;
#endif
}

std::tuple<bool, std::string> preprocess_tools_tokens(const std::string& chunk_text, bool tools_output) {
    bool output = tools_output;
    std::string keep_chunk = "";
    bool has_flag = str_contains(chunk_text, FN_FLAG);

    if (output) {
        s_tools_output += chunk_text;
    } else if (has_flag) {
        std::string result = "";
        size_t pos = chunk_text.find('<');
        if (pos != std::string::npos) {
            result = chunk_text.substr(pos);
            keep_chunk = chunk_text.substr(0, pos);
        }

        output = true;
        s_tools_output += result;
    }

    if (s_tools_output.length() > FN_NAME.length() && !str_contains(s_tools_output, FN_NAME)) {
        output = false;
        s_tools_output.clear();
    }

    return std::make_tuple(output, keep_chunk);
}

void signal_handler(int signum) {
  std::cout << "Interrupt signal (" << signum << ") received.\n";
  std::cout << "Exiting..." << std::endl;
  s_genie_context = nullptr;  // Free model resource.

  exit(signum);
}

int run_service(int argc, char* argv[]) {

  process_arguments(argc, argv);

  if (s_cmd_load_model) {
    bool ret = load_model(s_cmd_config_file, s_cmd_model_name);  // Load model through config file or model name.
    if (!ret) {
      return 1;
    }
  }

  signal(SIGINT, signal_handler);

  Server svr;

  auto response_error = [](httplib::Response & res, const json & error_data) {
    json final_response {{"error", error_data}};
    res.set_content(json_to_str(final_response), MIMETYPE_JSON);
    res.status = 500;
  };

  auto response_ok = [](httplib::Response & res, const json & data) {
    res.set_content(json_to_str(data), MIMETYPE_JSON);
    res.status = 200;
  };

  const auto handle_chat_completions = [&](const httplib::Request & req, httplib::Response & res) {

    if (s_client_connected) {
      json error_data {{"message", "There is connection in using."}};
      std::cout << "There is connection in using." << std::endl;
      response_error(res, error_data);
      return;
    }

    s_client_connected = true;

    std::string body = req.body;

    json data = json::parse(body);
    json msg = data["messages"];
    json tools = data["tools"];
    std::string tools_prompt = "";

    // std::cout << "data: " << data << std::endl;
    // std::cout << "tools: " << tools << std::endl;
    if (tools != nullptr) {
      size_t tools_length = 0;
      for (const auto& element : tools) {
        std::string string = json_to_str(element);
        size_t length = s_genie_context->TokenLength(string);

        if (tools_length + length < DOCS_MAX_SIZE) {
          tools_prompt += string + "\n";
          tools_length += length;
        }
        else {
          std::cout << "Tool is too long and will be truncated." << std::endl;
        }
      }

      tools_prompt = str_replace(tool_prompt_template, "{tool_descs}", tools_prompt);
      // std::cout << tools_prompt << std::endl;
    }

    std::string user = "";
    std::string system_prompt = "";
    static std::string tool_result = "";
    tool_result = "";

    bool stream = get_json_value(data, "stream", false);
    auto model = get_json_value(data, "model", BLANK_STRING);
    auto size = std::to_string(get_json_value(data, "size", CONTEXT_SIZE));
    auto temp = std::to_string(get_json_value(data, "temp", 0.8));
    auto top_k = std::to_string(get_json_value(data, "top_k", 40));
    auto top_p = std::to_string(get_json_value(data, "top_p", 0.95));

    for (const auto& element : msg) { // TODO: Handle history message.
      // std::cout << element << std::endl;
      auto role = get_json_value(element, "role", BLANK_STRING);
      if (role == "user") {
        user = get_json_value(element, "content", BLANK_STRING);
      }
      else if (role == "system") {
        system_prompt = get_json_value(element, "content", BLANK_STRING);
      }
      else if (role == "tool") {  // TODO: Check tool id.
        tool_result += "<tool_response>\n" + trim_empty_lines(get_json_value(element, "content", BLANK_STRING)) + "\n</tool_response>\n";
      }
    }

    if (!tool_result.empty()) {
      tool_result = s_prompt_history + s_tools_output_history + s_prompt_tag3 + tool_result + s_prompt_tag2;
    }

    bool ret = load_model("", model);  // Load model through model name.
    if (!ret) {
      json error_data {{"message", "model [ " + model + " ] load failed."}};
      response_error(res, error_data);
      s_client_connected = false;
      return;
    }

    s_genie_context->SetParams(size, temp, top_k, top_p);

    /*
    cout << "stream: " << stream << endl;
    cout << "model: " << model << endl;
    cout << "temp: " << temp << endl;
    cout << "top_k: " << top_k << endl;
    cout << "top_p: " << top_p << endl;
    cout << "user: " << user << endl;
    cout << "system: " << system_prompt << endl;
    */

    std::string prompt_1 = s_prompt_tag1;
    std::string prompt_2 = s_prompt_tag2;
    std::string query = user;

    if (system_prompt == "") {
      system_prompt = "You are a helpful assistant.";
    }
    system_prompt = str_replace(system_prompt, "\\n", "\n");

    if (is_thinking_model(s_model_name)) {
      if (s_cmd_enable_thinking) {
        system_prompt = system_prompt + "/think";
      }
      else {
        system_prompt = system_prompt + "/no_think";
      }
    }

    prompt_1 = str_replace(prompt_1, "You are a helpful assistant.", system_prompt + tools_prompt);

    size_t tokens_count = s_genie_context->TokenLength(query);
    std::vector<std::string> query_list;

    auto length_function = [](const std::string& s) {
      return s_genie_context->TokenLength(s);
    };

    if (tokens_count > DOCS_MAX_SIZE) {
      RecursiveCharacterTextSplitter splitter(SEPARATORS, true, DOCS_MAX_SIZE, length_function);
      query_list = splitter.split_text(query);
    }
    else {
      query_list.push_back(query);
    }

    s_tools_output = "";

    cout << "query_list size: " << query_list.size() << endl;
    cout << "tokens_count: " << tokens_count << endl << endl;

    const auto chunked_content_provider = [&req, query_list, prompt_1, prompt_2, tools_prompt](size_t, httplib::DataSink & sink) {
      static bool tools_output = false;

      auto genie_callback = [&sink, &req, tools_prompt](const std::string& message) {
        if(!is_connection_alive(req)) {
          s_genie_context->Stop();
          return false;
        }
        
        std::string chunk_text = message;

#ifdef DEBUG_OUTPUT
        print_wstring(chunk_text);
#endif

        if (!(tools_prompt == "")) { // Tools call.
            auto result = preprocess_tools_tokens(chunk_text, tools_output);
            tools_output = std::get<0>(result);
            std::string keep_chunk = std::get<1>(result);
            if (tools_output && keep_chunk == "" && !s_cmd_all_text) {
                return true;
            }
            /*
            else if (keep_chunk != "") {
                chunk_text = keep_chunk;
            }
            */
        }

        post_stream_data(sink, "data", response_data(chunk_text, "", true));
        return true;
      };

      if (tools_prompt == "") { // Not tools call.
        // Send empty data to start stream. https://github.com/langflow-ai/langflow/issues/5338
        post_stream_data(sink, "data", response_data("", "", true));
      }

      std::string q = "";
      int count = 0;
      std::string finish_reason = "stop";

      for (const auto& item : query_list) {
        q = prompt_1 + item + prompt_2;
        count++;
        if (query_list.size() > 1) {
          std::cout << "********************** [ " << count << " ] **********************" << std::endl;
        }

        tools_output = false;

        if (!tool_result.empty()) {
          q = tool_result;
        }
        else {
          s_prompt_history = q;
        }

        if (is_thinking_model(s_model_name)) {
          if (!s_cmd_enable_thinking) {
            q += FILL_THINK;
          }
        }

        print_debug_prompt(q);

        s_genie_context->Query(q, genie_callback);

        if (!(tools_prompt == "") && tools_output == true) { // Tools call.
          // std::cout << "[s_tools_output]\n" << std::endl;
          // print_wstring(s_tools_output);

          finish_reason = "tool_calls";

          std::string content = "";
          if (!s_cmd_all_text) {
            remove_tool_call_content(s_tools_output);
            // Send the data not start with '<tool_call>', '</tool_call>' and '{"name": ' to client together with 's_tools_output'.
            if (content != "") {
              content += "\n\n";
            }
          }

          post_stream_data(sink, "data", response_data(content, "", true, s_tools_output));

          // std::cout << "\n[content]\n" << std::endl;
          // print_wstring(content);
        }

        get_profile();
      }

      // Send empty data with finish_reason='stop' or 'tool_calls'.
      post_stream_data(sink, "data", response_data("", finish_reason, true));
    
      {
        std::string done = "data: [DONE]\n\n";
        sink.write(done.data(), done.size());
      }

      sink.done();

      return false;
    };  // end chunked_content_provider.

    auto on_complete = [&] (bool) {
        s_client_connected = false;
        // My_Log("on_complete\n");
    };

    if (stream) {
      res.set_chunked_content_provider("text/event-stream", chunked_content_provider, on_complete);
    }
    else {
      std::string response = "";

      auto genie_callback = [&response, &req](const std::string& message) {
        response += message;

#ifdef DEBUG_OUTPUT
        print_wstring(message);
#endif

        if(!is_connection_alive(req)) {
          s_genie_context->Stop();
        }
        return false;
      };

      std::string q = "";
      int count = 0;
      for (const auto& item : query_list) {
        q = prompt_1 + item + prompt_2;
        count++;
        if (query_list.size() > 1) {
          std::cout << "********************** [ " << count << " ] **********************" << std::endl;
        }

        if (!tool_result.empty()) {
          q = tool_result;
        }
        else {
          s_prompt_history = q;
        }

        if (is_thinking_model(s_model_name)) {
          if (!s_cmd_enable_thinking) {
            q += FILL_THINK;
          }
        }

        print_debug_prompt(q);

        s_genie_context->Query(q, genie_callback);

        get_profile();
      }

      std::string finish_reason = "stop";
      bool tools_output = str_contains(response, FN_NAME);
      if (!(tools_prompt == "") && tools_output == true) { // Tools call.
        finish_reason = "tool_calls";
        s_tools_output = response;

        if (!s_cmd_all_text) {
          // Send the data not start with '<tool_call>', '</tool_call>' and '{"name": ' to client together with 's_tools_output'.
          std::string content = remove_tool_call_content(s_tools_output);
          response = content;
          // std::cout << "\n[content]\n" << std::endl;
          // print_wstring(content);
        }

        if (response != "") {
          response += "\n\n";
        }
      }

      auto data = response_data(response, finish_reason, false, s_tools_output);
      response = json_to_str(data);

      res.set_content(response, MIMETYPE_JSON);
    
      s_client_connected = false;
    }
  };

  const auto handle_models = [&response_ok](const httplib::Request &, httplib::Response & res) {
    json models = get_model_list();
    response_ok(res, models);
  };

  // Register server middlewares
  svr.set_pre_routing_handler([](const httplib::Request & req, httplib::Response & res) {
    res.set_header("Access-Control-Allow-Origin", req.get_header_value("Origin"));
    // If this is OPTIONS request, skip validation because browsers don't include Authorization header
    if (req.method == "OPTIONS") {
        res.set_header("Access-Control-Allow-Credentials", "true");
        res.set_header("Access-Control-Allow-Methods",     "GET, POST");
        res.set_header("Access-Control-Allow-Headers",     "*");
        res.set_content("", "text/html");   // blank response, no data
        return httplib::Server::HandlerResponse::Handled; // skip further processing
    }

    return httplib::Server::HandlerResponse::Unhandled;
  });


  // Register APIs
  svr.Get("/", [&](const Request & /*req*/, Response &res) {
    res.set_content(root_html, "text/html");
  });

  svr.Post("/completions",         handle_chat_completions);
  svr.Post("/v1/completions",      handle_chat_completions);
  svr.Post("/chat/completions",    handle_chat_completions);
  svr.Post("/v1/chat/completions", handle_chat_completions);
  svr.Get ("/models",              handle_models);
  svr.Get ("/v1/models",           handle_models);

  std::cout << YELLOW << "INFO: Service Is Ready Now!" << RESET << std::endl << std::endl;

  svr.listen(HOST, 8910);

  return 0;
}

int main(int argc, char* argv[]) {
  run_service(argc, argv);
}

#ifdef __ANDROID__
#include <jni.h>

extern "C" JNIEXPORT void JNICALL
Java_com_example_genieapiservice_MyNativeLib_runService(JNIEnv *env, jobject /* this */, jobjectArray args) {
  int argc = env->GetArrayLength(args);
  std::vector<char*> argv;

  // My_Log("MyNativeLib_runService argc: " + std::to_string(argc));

  for (int i = 0; i < argc; ++i) {
      jstring arg = (jstring)env->GetObjectArrayElement(args, i);
      const char* c_str = env->GetStringUTFChars(arg, nullptr);
      // My_Log("MyNativeLib_runService argv: " + std::string(c_str));
      argv.push_back(const_cast<char*>(c_str));
  }

  run_service(argc, argv.data());

  for (int i = 0; i < argc; ++i) {
      jstring arg = (jstring)env->GetObjectArrayElement(args, i);
      env->ReleaseStringUTFChars(arg, argv[i]);
  }
}
#endif
