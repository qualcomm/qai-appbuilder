//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "Utils.h"
#include <iostream>
#include <string>
#include <sstream>
#include <curl/curl.h>

const std::string SVR_URL = "http://localhost:8910/v1/chat/completions";

std::string build_request_body(const std::string& model, const std::string& prompt, bool stream) {
    json body;
    body["model"] = model;
    body["stream"] = stream;
    body["messages"] = {
        {{"role", "system"}, {"content", "You are a math teacher who teaches algebra."}},
        {{"role", "user"}, {"content", prompt}}
    };
    body["size"] = 4096;
    body["seed"] = 146;
    body["temp"] = 1.5;
    body["top_k"] = 13;
    body["top_p"] = 0.6;

    return body.dump();
}

size_t write_callback_stream(char* ptr, size_t size, size_t nmemb, void* userdata) {
    size_t total_size = size * nmemb;
    std::string chunk(ptr, total_size);
    std::istringstream stream(chunk);
    std::string line;

    while (std::getline(stream, line)) {
        if (line.rfind("data: ", 0) == 0) {
            std::string jsonStr = line.substr(6);
            if (jsonStr == "[DONE]") {
                return total_size;
            }

            if (jsonStr.length() > 10) {
              json j = json::parse(jsonStr);
              if (j.contains("choices") && !j["choices"].empty() &&
                  j["choices"][0].contains("delta") &&
                  j["choices"][0]["delta"].contains("content")) {
                  std::string content = j["choices"][0]["delta"]["content"].get<std::string>();
                  std::cout << content << std::flush;
              }
            }
        }
    }
    return total_size;
}

size_t write_callback(void* contents, size_t size, size_t nmemb, void* userp) {
    ((std::string*)userp)->append((char*)contents, size * nmemb);
    return size * nmemb;
}

int main(int argc, char* argv[]) {
    std::string prompt = "hello";
    std::string model = "IBM-Granite-v3.1-8B";
    bool stream = false;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--stream") stream = true;
        else if (arg == "--prompt" && i + 1 < argc) prompt = argv[++i];
        else if (arg == "--model" && i + 1 < argc) model = argv[++i];
    }

    curl_global_init(CURL_GLOBAL_ALL);

    CURL* curl = curl_easy_init();
    if (!curl) return 1;

    std::string body = build_request_body(model, prompt, stream);

    curl_easy_setopt(curl, CURLOPT_URL, SVR_URL.c_str());

    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    std::string response_string;

    if(stream) {
      headers = curl_slist_append(headers, "Accept: text/event-stream");
      curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback_stream);
    }
    else {
      curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
      curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_string);
    }
    
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());

    CURLcode res = curl_easy_perform(curl);

    if (res != CURLE_OK) {
        std::cerr << "curl_easy_perform() failed: " << curl_easy_strerror(res) << std::endl;
    }else if (!stream){
        auto j = nlohmann::json::parse(response_string);
        std::string content = j["choices"][0]["message"]["content"];
        std::cout << content << std::endl;
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    curl_global_cleanup();

  return 0;
}
