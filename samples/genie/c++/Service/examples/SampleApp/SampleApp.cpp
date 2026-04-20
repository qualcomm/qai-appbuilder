//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
#include <iostream>

#include "SampleApp.hpp"

#include <fstream>
#include <string>
#include <random>
#include <windows.h>

#include <fcntl.h>
#include <io.h>
#include <direct.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

using namespace std;

#define BUFF_SIZE 1024 * 1024 * 2
static double sg_prompt_evaluation_speed = 0.0f;
static double sg_average_token_per_second = 0.0f;

// The base directory of qualla_sdk. It will load the 'conf\\env.json' under this directory. 
// The model should be in 'models' folder under this directory.
// The QNN SDK libraries & Qualla libraries should be in 'lib' folder under this directory.
#define QUALLC_DIR     "C:\\Source\\OpenSrc\\LLM\\qc-llama\\QuallaHelper\\qualla_sdk"
// #define ASYNC_MODEL_LOAD  // Disabled - use synchronous loading instead

#define PHI4MMV81

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

int main(int argc, char **argv)
{
    const char *qualla_dir = nullptr;
    const char *question = nullptr;
    if (argc >= 2)
    {
        qualla_dir = argv[1];
    }
    else
    {
        qualla_dir = QUALLC_DIR;
    }
    // now question is path of json
    if (argc >= 3)
    {
        question = argv[2];
    }
    int model_cnt = 2;
    //params llm_params;
    std::string llm_hardware_info{"NPU"};


#ifdef PHI4MMV81
    std::string config_phi4mm = R"(
{
    "dialog" : {
      "version" : 1,
      "type" : "basic",
      "max-num-tokens" : 2048,
      "embedding" : {
        "version" : 1,
        "size" : 3072,
        "datatype" : "float32"
      },
      "context" : {
        "version" : 1,
        "size" : 8192,
        "n-vocab": 200064,
        "bos-token": 200021,
        "eos-token": 199999
      },
      "sampler" : {
        "version" : 1,
        "seed" : 42,
        "temp" : 5.0,
        "top-k" : 5,
        "top-p" : 0.8,
        "greedy" : true
      },
      "tokenizer" : {
        "version" : 1,
        "path" : "models/phi4mm-v81/tokenizer.json"
      },
      "engine" : {
        "version" : 1,
        "n-threads" : 3,
        "backend" : {
          "version" : 1,
          "type" : "QnnHtp",
          "QnnHtp" : {
            "version" : 1,
            "spill-fill-bufsize" : 0,
            "use-mmap" : true,
            "mmap-budget" : 0,
            "poll" : false,
            "allow-async-init" : false,
            "cpu-mask" : "0xe0",
            "kv-dim" : 128,
            "enable-graph-switching": false
          },
          "extensions" : "htp_backend_ext_config.json"
        },
        "model" : {
          "version" : 1,
          "type" : "binary",
          "binary" : {
            "version" : 1,
            "ctx-bins" : [
              "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_1_of_4.serialized.bin",
              "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_2_of_4.serialized.bin",
              "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_3_of_4.serialized.bin",
              "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_4_of_4.serialized.bin"
            ]
          },
          "positional-encoding": {
            "type": "rope",
            "rope-dim": 48,
            "rope-theta": 10000,
            "rope-scaling": {
                "rope-type": "longrope",
                "factor": 32,
                "original-max-position-embeddings": 4096,
                "long-factor" : [1, 1.118320672, 1.250641126, 1.398617824, 1.564103225, 1.74916897, 1.956131817, 2.187582649, 2.446418898, 2.735880826, 3.059592084, 3.421605075, 3.826451687, 4.279200023, 4.785517845, 5.351743533, 5.984965424, 6.693110555, 7.485043894, 8.370679318, 9.36110372, 10.4687158, 11.70738129, 13.09260651, 14.64173252, 16.37415215, 18.31155283, 20.47818807, 22.90118105, 25.61086418, 28.64115884, 32.03, 32.1, 32.13, 32.23, 32.6, 32.61, 32.64, 32.66, 32.7, 32.71, 32.93, 32.97, 33.28, 33.49, 33.5, 44.16, 47.77],
                "short-factor" : [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
            }
        }
        }
      }
    }
  }
)";
#endif

#ifdef PHI4MMV73
    std::string config_phi4mm = R"(
{
    "dialog" : {
      "version" : 1,
      "type" : "basic",
      "max-num-tokens" : 128,
      "embedding" : {
        "version" : 1,
        "size" : 3072,
        "datatype" : "float32"
      },
      "context" : {
        "version" : 1,
        "size" : 3072,
        "n-vocab": 200064,
        "bos-token": 200021,
        "eos-token": 199999
      },
      "sampler" : {
        "version" : 1,
        "seed" : 42,
        "temp" : 5.0,
        "top-k" : 5,
        "top-p" : 0.8,
        "greedy" : true
      },
      "tokenizer" : {
        "version" : 1,
        "path" : "models/phi4mm-v73/tokenizer.json"
      },
      "engine" : {
        "version" : 1,
        "n-threads" : 3,
        "backend" : {
          "version" : 1,
          "type" : "QnnHtp",
          "QnnHtp" : {
            "version" : 1,
            "spill-fill-bufsize" : 0,
            "use-mmap" : true,
            "mmap-budget" : 0,
            "poll" : true,
            "allow-async-init" : false,
            "cpu-mask" : "0xe0",
            "kv-dim" : 128,
            "enable-graph-switching": false
          },
          "extensions" : "htp_backend_ext_config.json"
        },
        "model" : {
          "version" : 1,
          "type" : "binary",
          "binary" : {
            "version" : 1,
            "ctx-bins" : [
              "models/phi4mm-v73/weights_sharing_model_1_of_2.serialized.bin",
              "models/phi4mm-v73/weights_sharing_model_2_of_2.serialized.bin"
            ]
          },
          "positional-encoding": {
            "type": "rope",
            "rope-dim": 48,
            "rope-theta": 10000,
            "rope-scaling": {
                "rope-type": "longrope",
                "factor": 32,
                "original-max-position-embeddings": 4096,
                "long-factor" : [1, 1.118320672, 1.250641126, 1.398617824, 1.564103225, 1.74916897, 1.956131817, 2.187582649, 2.446418898, 2.735880826, 3.059592084, 3.421605075, 3.826451687, 4.279200023, 4.785517845, 5.351743533, 5.984965424, 6.693110555, 7.485043894, 8.370679318, 9.36110372, 10.4687158, 11.70738129, 13.09260651, 14.64173252, 16.37415215, 18.31155283, 20.47818807, 22.90118105, 25.61086418, 28.64115884, 32.03, 32.1, 32.13, 32.23, 32.6, 32.61, 32.64, 32.66, 32.7, 32.71, 32.93, 32.97, 33.28, 33.49, 33.5, 44.16, 47.77],
                "short-factor" : [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
            }
        }
        }
      }
    }
  }
)";
#endif

#ifdef QWEN25_OMINI_3B
    std::string config_qwen = R"(
{
  "dialog" : {
    "version" : 1,
    "type" : "basic",
    "max-num-tokens" : 2048,
    "embedding" : {
      "version" : 1,
      "size" : 2048,
      "datatype" : "float32"
    },
    "context" : {
      "version" : 1,
      "size" : 2048,
      "n-vocab": 151936,
      "bos-token": 151643,
      "eos-token": 151645
    },
    "sampler" : {
      "version" : 1,
      "seed" : 42,
      "temp" : 0.0,
      "top-k" : 1,
      "top-p" : 1.0,
      "greedy" : true
    },
    "tokenizer" : {
      "version" : 1,
      "path" : "models/qwen2.5_omini_3b_8480/tokenizer.json"
    },
    "engine" : {
      "version" : 1,
      "n-threads" : 3,
      "backend" : {
        "version" : 1,
        "type" : "QnnHtp",
        "QnnHtp" : {
          "version" : 1,
          "spill-fill-bufsize" : 0,
          "use-mmap" : true,
          "mmap-budget" : 0,
          "poll" : true,
          "allow-async-init" : false,
          "pos-id-dim" : 64,
          "cpu-mask" : "0xe0",
          "kv-dim" : 128,
          "rope-theta": 1000000,
          "enable-graph-switching": false
        },
        "extensions" : "htp_backend_ext_config.json"
      },
      "model" : {
        "version" : 1,
        "type" : "binary",
        "binary" : {
          "version" : 1,
          "ctx-bins" : [
            "models/qwen2.5_omini_3b_8480/model-1.bin",
			"models/qwen2.5_omini_3b_8480/model-2.bin",
			"models/qwen2.5_omini_3b_8480/model-3.bin"
          ]
        }
      }
    }
  }
}

)";
#endif

    int load_cnt = 0;

#ifdef PHI4MMV81
    api_interface llm = api_interface(config_phi4mm);
    std::string model_path = "models/phi4mm-v81";
    std::vector<std::string> model_name = {
            "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_1_of_4.serialized.bin",
            "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_2_of_4.serialized.bin",
            "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_3_of_4.serialized.bin",
            "models/phi4mm-v81/weight_sharing_model_ar128_ar1_cl8192_4_of_4.serialized.bin"
    };
#endif

#ifdef PHI4MMV73
    api_interface llm = api_interface(config_phi4mm);
    std::string model_path = "models/phi4mm-v73";
    std::vector<std::string> model_name = { 
         "models/phi4mm-v73/weights_sharing_model_1_of_2.serialized.bin",
         "models/phi4mm-v73/weights_sharing_model_2_of_2.serialized.bin"
        };
#endif

#ifdef QWEN25_OMINI_3B
    api_interface llm = api_interface(config_qwen);
    std::string model_path = "models/qwen2.5_omini_3b_8480";
    std::vector<std::string> model_name = { 
        "models/qwen2.5_omini_3b_8480/model-1.bin",
        "models/qwen2.5_omini_3b_8480/model-2.bin",
        "models/qwen2.5_omini_3b_8480/model-3.bin"
        };
#endif

    while (load_cnt == 0)
    {
        // Read all the model files to buffers.
        TimerHelper timeModelToMemoryHelper;

        printf("\n\n============== [model load cnt = %d] ============\n", load_cnt++);

        timeModelToMemoryHelper.PrintDiff("Model to memory");

        TimerHelper timeModelToHTPHelper;
#ifdef ASYNC_MODEL_LOAD
        // Start async model loading (returns immediately)
        llm.api_loadmodel_async(model_path, model_name, llm_hardware_info);
        printf("Model loading started asynchronously, waiting for completion...\n");
        fflush(stdout);
        // Wait indefinitely until model is fully loaded, printing progress every 5s
        bool load_ok = llm.api_wait_loaded(-1);
        timeModelToHTPHelper.PrintDiff("Model to HTP (async)");
        if (!load_ok || llm.api_status() == error) {
            printf("exit due to error 1 (async load failed)\n");
            exit(1);
        }
        printf("Model loaded successfully (async). status: %d\n", llm.api_status());
#else
        llm.api_loadmodel(model_path, model_name, llm_hardware_info);
        timeModelToHTPHelper.PrintDiff("Model to HTP");
        printf("status: %d\n", llm.api_status());
        if (llm.api_status() == error)
        {
            printf("exit due to error 1\n");
            exit(1);
        }
#endif


        std::string q = "";
        std::string a = "";
        //int start_index_int = stoi(std::string(start_index));
        //int end_index_int = stoi(std::string(end_index));

        int start_index_int = 0;
        int end_index_int = 0;

        int cnt = start_index_int;
        while (cnt <= end_index_int)
        {
            a = "";

            if (question)
            {
                q = question;
            }
            std::ifstream in(q.c_str());
            if (!in.good())
            {
                printf("Error: cannot open question file: %s\n", q.c_str());
                exit(3);
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
                auto &message = j["messages"][0];
                if (message.contains("content") && message["content"].contains("question"))
                {
                    question_text = message["content"]["question"].get<std::string>();
                    // Pass the entire JSON to the model (includes image data)
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

            printf("\n============== Question [%d] ==============\n", cnt++);
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
                exit(2);
            }

            printf("==========================================\n");
            fflush(stdout);
        }

        printf("unload model\n");
        llm.api_unloadmodel();
        printf("status: %d\n", llm.api_status());
        if (llm.api_status() == error)
        {
            printf("exit due to error 6\n");
            exit(6);
        }
    }

    printf("\n......exit......\n");

    return 0;
}
