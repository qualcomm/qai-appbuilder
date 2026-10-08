//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <fstream>
#include <iostream>
#include "GenieBuilder.h"

// #define GENIE_BUILDER_DEBUG 1
#define CONTENT_LENGTH 4096  // TODO. need to calculate.

static int g_CurLength = 0;
static int g_MaxLength = CONTENT_LENGTH;

size_t g_embeddingLutSize{0};
std::shared_ptr<void> g_embeddingLut{};

// Forward declaration for allocator used in tokenizer decode
void MyAllocCallback(const size_t size, const char **allocatedData);

void GenieCallBack(const char* response, const GenieDialog_SentenceCode_t sentence_code, const void* user_data) {
    GenieContext* self = static_cast<GenieContext*>(const_cast<void*>(user_data));
    if (response && strlen(response) > 0) {
        {
            std::lock_guard<std::mutex> guard(self->m_stream_lock);
            self->m_stream_answer += response;
            // std::cout << response << std::flush;
            g_CurLength += self->TokenLength(response);    // TODO: We should calculate the input length together. input + output < CONTENT_LENGTH.
            // printf("g_CurLength = %d, g_MaxLength = %d\n", g_CurLength, g_MaxLength);
        }
        self->m_stream_cond.notify_one();  // Notify waiting thread that new data is available
    }

    if(g_CurLength >= g_MaxLength) { // Stop current generation.
        self->Stop();
    }

#ifdef GENIE_BUILDER_DEBUG
    std::cout << response;

    if (sentence_code == GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_END) {
        printf("\n-----------------------------------------------------\n");
    }
#endif
}

void tokenToEmbedCallback(int32_t token,
                          void* embedding,
                          uint32_t embeddingSize,
                          const void* /*userData*/) {
  size_t lutIndex = static_cast<size_t>(token) * embeddingSize;
  if ((lutIndex + embeddingSize) <= g_embeddingLutSize) {
    int8_t* embeddingSrc = static_cast<int8_t*>(g_embeddingLut.get()) + lutIndex;
    int8_t* embeddingDst = static_cast<int8_t*>(embedding);
    std::copy(embeddingSrc, embeddingSrc + embeddingSize, embeddingDst);
  } else {
    std::cerr << "Error: T2E conversion overflow." << std::endl;
  }
}

void GenieTokenQueryCallback(
    const uint32_t* token_ids,
    const uint32_t numTokens,
    const GenieDialog_SentenceCode_t sentence_code,
    const void* user_data
) {
    GenieContext* self = static_cast<GenieContext*>(const_cast<void*>(user_data));
    // Convert token IDs to text using helper
    if (token_ids && numTokens > 0) {
        std::string decoded = self->DecodeTokens(token_ids, numTokens);
        if (!decoded.empty()) {
            {
                std::lock_guard<std::mutex> guard(self->m_stream_lock);
                self->m_stream_answer += decoded;
            }
            self->m_stream_cond.notify_one();  // Notify waiting thread that new data is available
            g_CurLength += self->TokenLength(decoded);
        }
    }
    

    if (g_CurLength >= g_MaxLength) {
        self->Stop();
    }

#ifdef GENIE_BUILDER_DEBUG
    if (sentence_code == GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_END) {
        printf("\n-----------------------------------------------------\n");
    }
#endif
}

std::string GenieContext::DecodeTokens(const uint32_t* token_ids, uint32_t numTokens) {
    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t tstatus = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (tstatus != GENIE_STATUS_SUCCESS || !tokenizerHandle || !token_ids || numTokens == 0) {
        return std::string();
    }

    const char* decoded = nullptr;
    // GenieTokenizer_decode expects int32_t token IDs
    tstatus = GenieTokenizer_decode(
        tokenizerHandle,
        reinterpret_cast<const int32_t*>(token_ids),
        numTokens,
        MyAllocCallback,
        &decoded
    );
    if (tstatus != GENIE_STATUS_SUCCESS || !decoded) {
        return std::string();
    }

    std::string out(decoded);
    free((void*)decoded);
    return out;
}

void GenieContext::inference_thread() {
    while(true) {
        std::unique_lock<std::mutex> lock(m_request_lock);
        m_request_cond.wait(lock, [this]{return m_request_ready;});     // m_request_ready == true, wakeup thread; m_request_ready == false, sleep continually.
        if(m_thread_exit) {
            return;
        }

        auto status = GenieDialog_query(m_DialogHandle, m_prompt.c_str(), GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_COMPLETE, GenieCallBack, this);
        if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status) {
            std::cerr << "Failed to get response from GenieDialog.\n";
        }

        m_inference_busy = false;
        m_request_ready = false;
    }
}

void GenieContext::embedding_inference_thread() {
    while (true) {
        std::unique_lock<std::mutex> lock(m_embedding_request_lock);
        m_embedding_request_cond.wait(lock, [this]{ return m_embedding_request_ready; });
        if (m_embedding_thread_exit) {
            return;
        }

        if(m_embedding.size() == 0) {
            std::cerr << "Embedding input is empty.\n";
            m_embedding_inference_busy = false;
            m_embedding_request_ready = false;
            continue;
        }

        GenieDialog_TokenToEmbeddingCallback_t t2eCallback=tokenToEmbedCallback;
        auto status = GenieDialog_embeddingTokenQuery(
            m_DialogHandle,
            m_embedding.data(),
            static_cast<uint32_t>(m_embedding.size()*sizeof(float)),
            GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_COMPLETE,
            t2eCallback,
            GenieTokenQueryCallback,
            this
        );
        if (GENIE_STATUS_SUCCESS != status && GENIE_STATUS_WARNING_ABORTED != status) {
            std::cerr << "Failed to get response from GenieDialog (embedding).\n";
        }

        m_embedding_inference_busy = false;
        m_embedding_request_ready = false;
    }
}

bool GenieContext::SetEmbeddingTable(const std::string table_path)
{
    std::ifstream infile(table_path, std::ios::binary | std::ios::ate);
    if (!infile) {
        std::cerr << "Failed to open embedding table file: " << table_path << "\n";
        return false;
    }

    std::streamsize size = infile.tellg();
    infile.seekg(0, std::ios::beg);

    g_embeddingLutSize = static_cast<size_t>(size);
    g_embeddingLut.reset(malloc(g_embeddingLutSize), free);
    if (!g_embeddingLut) {
        std::cerr << "Failed to allocate memory for embedding LUT.\n";
        return false;
    }

    if (!infile.read(static_cast<char*>(g_embeddingLut.get()), size)) {
        std::cerr << "Failed to read embedding LUT data from file.\n";
        return false;
    }

    return true;
}

bool GenieContext::Query(const std::string& prompt, const Callback callback) {
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle)) {    // TODO: add a Python function for this.
        std::cerr << "Failed to reset Genie Dialog.\n";
    }

    g_CurLength = 0;
    m_prompt = prompt;

#ifdef GENIE_BUILDER_DEBUG
    std::cout << "\n[Prompt]:\n";
    std::cout << prompt << "\n\n";
    std::cout << "\n[Response]:\n";
#endif

    m_request_ready = true;
    m_inference_busy = true;
    m_request_cond.notify_one();   // Notify the inference thread to work.

    std::string response = "";
    while(m_inference_busy) {
        std::unique_lock<std::mutex> lock(m_stream_lock);
        // Wait for new data or inference completion (with timeout to check m_inference_busy)
        m_stream_cond.wait_for(lock, std::chrono::milliseconds(100), [this] {
            return !m_stream_answer.empty() || !m_inference_busy;
        });
        
        if (!m_stream_answer.empty()) {
            response = m_stream_answer;
            m_stream_answer = "";
            lock.unlock();  // Unlock before callback
            
            //std::cout << response << std::flush;
            callback(response);
            response = "";
        }
    }

    // Send remainder data after inference completes
    if (m_stream_answer.size() > 0) {
        // std::cout << remainder << std::flush;
        // std::cout << "[more data]" << std::flush;
        callback(m_stream_answer);
        m_stream_answer = "";
    }

    return true;
}

std::string GenieContext::QueryByEmbedding(const std::vector<float>& embedding, const Callback callback) {
    if (GENIE_STATUS_SUCCESS != GenieDialog_reset(m_DialogHandle)) {    // TODO: add a Python function for this.
        std::cerr << "Failed to reset Genie Dialog.\n";
    }

    g_CurLength = 0;
    m_embedding = embedding;


#ifdef GENIE_BUILDER_DEBUG
    std::cout << "\n[Embedding Input]:\n";
    // for (size_t i = 0; i < m_embedding.size(); i++) {
    //     std::cout << m_embedding[i] << " ";
    // }
    std::cout <<"m_embedding size:"<<m_embedding.size()<< "\n\n";
    std::cout << "\n[Response]:\n";
#endif

    m_embedding_request_ready = true;
    m_embedding_inference_busy = true;
    m_embedding_request_cond.notify_one();

    std::string response = "";
    std::string chunk="";
    while (m_embedding_inference_busy) {
        std::unique_lock<std::mutex> lock(m_stream_lock);
        // Wait for new data or inference completion (with timeout to check m_embedding_inference_busy)
        m_stream_cond.wait_for(lock, std::chrono::milliseconds(100), [this] {
            return !m_stream_answer.empty() || !m_embedding_inference_busy;
        });
        
        if (!m_stream_answer.empty()) {
            response = m_stream_answer;
            m_stream_answer = "";
            lock.unlock();  // Unlock before callback
            
            if(callback) {
                callback(response);
            }
            chunk += response;
            response = "";
        }
    }

    // Send remainder data after inference completes
    if (m_stream_answer.size() > 0) {
        if(callback) {
            callback(m_stream_answer);
        }
        chunk += m_stream_answer;
        m_stream_answer = "";
    }

    return chunk;
}

GenieContext::GenieContext(const std::string& config, bool debug) {
    std::string config_str;
    std::string sample_config_str = "{\n  \"sampler\" : {\n      \"version\" : 1,\n      \"temp\" : 1.2,\n      \"top-k\" : 25,\n      \"top-p\" : 0.8\n  }\n}";
    int32_t status = 0;
    m_debug = debug;

    std::ifstream config_file(config);
    if (!config_file) {
        std::cerr << "Failed to open Genie config file: " + config;
    }
    config_str.assign((std::istreambuf_iterator<char>(config_file)), std::istreambuf_iterator<char>());

    // std::cerr << sample_config_str << std::endl;
    // std::cerr << config_str << std::endl;

    // Create Genie config
    if (GENIE_STATUS_SUCCESS != GenieDialogConfig_createFromJson(config_str.c_str(), &m_ConfigHandle)) {
        std::cerr << "Failed to create the Genie Dialog config.\n";
        return;
    }

    if (m_debug) {
        GenieLog_Callback_t callback = nullptr;
        status = GenieLog_create(nullptr, callback, GENIE_LOG_LEVEL_VERBOSE, &m_LogHandle);
        if ((GENIE_STATUS_SUCCESS != status) || (!m_LogHandle)) {
        throw std::runtime_error("Failed to create the Log handle.");
        }

        status = GenieDialogConfig_bindLogger(m_ConfigHandle, m_LogHandle);
        if (GENIE_STATUS_SUCCESS != status) {
        throw std::runtime_error("Failed to bind the log handle with the dialog config.");
        }
    }

    status = GenieProfile_create(nullptr, &m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status) {
        std::cerr <<  "Failed to create the profile handle.\n";
        return;
    }

    status = GenieDialogConfig_bindProfiler(m_ConfigHandle, m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status) {
        std::cerr <<  "Failed to bind the profile handle with the dialog config.\n";
        return;
    }

    // Create Genie dialog handle
    if (GENIE_STATUS_SUCCESS != GenieDialog_create(m_ConfigHandle, &m_DialogHandle)) {
        std::cerr <<  "Failed to create the Genie Dialog.\n";
        return;
    }

    status = GenieSamplerConfig_createFromJson(sample_config_str.c_str(), &m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status) {
        std::cerr <<  "Failed to create sampler config.\n";
        return;
    }

    status = GenieDialog_getSampler(m_DialogHandle, &m_SamplerHandle);
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr <<  "Failed to get sampler.\n";
      return;
    }

    if(!m_stream_thread) {
        m_stream_thread = std::make_unique<std::thread>(&GenieContext::inference_thread, this);
    }
    if (!m_embedding_stream_thread) {
        m_embedding_stream_thread = std::make_unique<std::thread>(&GenieContext::embedding_inference_thread, this);
    }
}

GenieContext::~GenieContext() {
#ifdef GENIE_BUILDER_DEBUG
    std::cout << "INFO: GenieContext::~GenieContext():\n";
#endif

    int32_t status = 0;

    // Notify thread exiting.
    if(m_stream_thread) {
        m_thread_exit = true;
        m_request_ready = true;
        m_request_cond.notify_one();
    }
    if (m_embedding_stream_thread) {
        m_embedding_thread_exit = true;
        m_embedding_request_ready = true;
        m_embedding_request_cond.notify_one();
    }

    if (m_ConfigHandle != nullptr) {
        if (GENIE_STATUS_SUCCESS != GenieDialogConfig_free(m_ConfigHandle)) {
            std::cerr << "Failed to free the Genie Dialog config.\n";
        }
    }

    if (m_DialogHandle != nullptr) {
        if (GENIE_STATUS_SUCCESS != GenieDialog_free(m_DialogHandle)) {
            std::cerr << "Failed to free the Genie Dialog.\n";
        }
    }

    status = GenieSamplerConfig_free(m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to free the sampler config." << std::endl;
    }

    if (m_LogHandle != nullptr) {
        status = GenieLog_free(m_LogHandle);
        if (GENIE_STATUS_SUCCESS != status) {
          std::cerr << "Failed to free the Log handle." << std::endl;
        }
    }

    status = GenieProfile_free(m_ProfileHandle);
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to free the profile handle." << std::endl;
    }

    // Waiting thread clean.
    if(m_stream_thread) {
        m_stream_thread->join();
        m_stream_thread = nullptr;

        // reset the global variable.
        m_request_ready = false;
        m_thread_exit = false;
    }
    if (m_embedding_stream_thread) {
        m_embedding_stream_thread->join();
        m_embedding_stream_thread = nullptr;

        m_embedding_request_ready = false;
        m_embedding_thread_exit = false;
    }
}

bool GenieContext::Stop() {

    if (GENIE_STATUS_SUCCESS != GenieDialog_signal(m_DialogHandle, GENIE_DIALOG_ACTION_ABORT)) {
        std::cerr << "Failed to stop generation.\n";
        return false;
    }

    return true;
}

bool GenieContext::SetParams(const std::string max_length, const std::string temp, const std::string top_k, const std::string top_p) {
    int32_t status = 0;

    g_MaxLength = std::stoi(max_length);

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "temp", temp.c_str());
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to setParam.\n";
      return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "top-k", top_k.c_str());
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to setParam.\n";
      return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "top-p", top_p.c_str());
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to setParam.\n";
      return false;
    }

    status = GenieSamplerConfig_setParam(m_SamplerConfigHandle, "type", "basic");
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to setParam.\n";
      return false;
    }

    status = GenieSampler_applyConfig(m_SamplerHandle, m_SamplerConfigHandle);
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to apply sampler config.\n";
      return false;
    }

    return true;
}

std::string GenieContext::GetProfile() {
    const Genie_AllocCallback_t callback([](size_t size, const char** data) {
        *data = (char*)malloc(size);
        if (*data == nullptr) {
          std::cerr << "Cannot allocate memory for JSON data.\n";
        }
      });

    const char* jsonData = nullptr;
    const int32_t status = GenieProfile_getJsonData(m_ProfileHandle, callback, &jsonData);
    if (GENIE_STATUS_SUCCESS != status) {
      std::cerr << "Failed to get the profile data.\n";
      return "";
    }

    std::string strProfileData(jsonData);
    free((char*)jsonData);

    return strProfileData;
}

bool GenieContext::SetStopSequence(const std::string& stop_sequences) {
    if (GENIE_STATUS_SUCCESS != GenieDialog_setStopSequence(m_DialogHandle, stop_sequences.c_str())) {
        std::cerr << "Failed to set stop sequence.\n";
        return false;
    }

    return true;
}

void MyAllocCallback(const size_t size, const char **allocatedData)
{
    *allocatedData = reinterpret_cast<const char *>(malloc(size));
}

size_t GenieContext::TokenLength(const std::string &text)
{
    GenieTokenizer_Handle_t tokenizerHandle = nullptr;
    Genie_Status_t status = GenieDialog_getTokenizer(m_DialogHandle, &tokenizerHandle);
    if (status != GENIE_STATUS_SUCCESS)
    {
        std::cerr << "get tokenizer failed, error code: " << status << std::endl;
        return 0;
    }

    const int32_t *tokenIds = nullptr;
    uint32_t numTokenIds = 0;

    status = GenieTokenizer_encode(
            tokenizerHandle,
            text.c_str(),
            MyAllocCallback,
            &tokenIds,
            &numTokenIds
    );

    if (status != GENIE_STATUS_SUCCESS)
    {
        std::cerr << "encode failed, erroe code: " << status << " " << "string is: " << text.c_str() << std::endl;
        return text.size();
    }

    free((void *) tokenIds);
    return static_cast<size_t>(numTokenIds);
}

bool GenieContext::SetLora(const std::string AdapterName, 
                           const std::unordered_map<std::string, float> &alphaValue)
{
    static std::string engineRole{"primary"};
    bool AnyInstancePassed{false};

    int32_t status = GenieDialog_applyLora(m_DialogHandle, engineRole.c_str(), AdapterName.c_str());
    if (GENIE_STATUS_SUCCESS != status)
    {
        std::cerr << "Failed to apply the LoRA adapter.\n";
        return false;
    }

    for (auto it = alphaValue.begin(); it != alphaValue.end(); it++)
    {
        int32_t status = GenieDialog_setLoraStrength(m_DialogHandle, engineRole.c_str(), it->first.c_str(), it->second);
        if (GENIE_STATUS_SUCCESS != status)
        {
            std::cerr << "Failed to set the LoRA alpha strength.\n";
        }
        AnyInstancePassed = true;
    }

    return AnyInstancePassed;
}

#include "common.h"

PYBIND11_MODULE(geniebuilder, m) {
    m.doc() = R"pbdoc(
        Pybind11 GenieBuilder Extension.
        -----------------------
        .. currentmodule:: qai_geniebuilder
        .. autosummary::
            :toctree: _generate

            Query
            )pbdoc";

    m.attr("__name__") = "qai_geniebuilder";
    m.attr("__version__") = APPBUILDER_VERSION;
    m.attr("__author__") = "quic-zhanweiw";
    m.attr("__name__") = "qai_geniebuilder";

    py::class_<GenieContext>(m, "GenieContext")
        .def(py::init<const std::string&, bool>())
        .def("Query", &GenieContext::Query)
        .def("QueryByEmbedding", &GenieContext::QueryByEmbedding)
        .def("SetEmbeddingTable", &GenieContext::SetEmbeddingTable)
        .def("SetParams", &GenieContext::SetParams)
        .def("GetProfile", &GenieContext::GetProfile)
        .def("TokenLength", &GenieContext::TokenLength)
        .def("SetStopSequence", &GenieContext::SetStopSequence)
        .def("Stop", &GenieContext::Stop)
		.def("SetLora", &GenieContext::SetLora);
}
