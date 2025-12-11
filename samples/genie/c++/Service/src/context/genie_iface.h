//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENIE_IFACE_H
#define GENIE_IFACE_H

#include "../context/genie.h"
#include "core/log.h"
#include "core/utils.h"

#define CONTENT_LENGTH 4096  // TODO. need to calculate.
#define GENIE_BUILDER_DEBUG 1

static int g_CurLength = 0;
static int g_MaxLength = CONTENT_LENGTH;

class GenieContext::Interface
{
public:
    virtual ~Interface() = default;

    static GenieContext::Interface *CreateInterface(GenieContext *context);

    explicit Interface(GenieContext *context) :
            context_{context},
            DialogHandle_{context->m_DialogHandle},
            content_{&context->content}
    {}

    virtual bool set_content(const std::string &info) = 0;

    virtual Genie_Status_t GenieDialogQuery(const Content &content) = 0;

protected:
    GenieContext *context_;
    Content *content_;
    GenieDialog_Handle_t DialogHandle_;

    static void GenieCallBack(const char *response,
                              const GenieDialog_SentenceCode_t sentence_code,
                              const void *user_data);
};

class IGeneral : public GenieContext::Interface
{
public:
    explicit IGeneral(GenieContext *context) : GenieContext::Interface(context)
    {}

    bool set_content(const std::string &prompt) final
    {
        content_->prompt_ = prompt;
#ifdef GENIE_BUILDER_DEBUG
        My_Log{} << "\n[Prompt]:\n"
                 << content_->prompt_ << "\n------------\n\n"
                 << "[Response]:\n";
#endif
        return true;
    }

    Genie_Status_t GenieDialogQuery(const GenieContext::Content &content) final
    {
        return GenieDialog_query(DialogHandle_,
                                 content.prompt_.c_str(),
                                 GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_COMPLETE,
                                 GenieCallBack,
                                 this);
    }
};

class IEmbedding : public GenieContext::Interface
{
public:
    explicit IEmbedding(GenieContext *context, const std::vector<uint8_t> &embedded_buffer) :
            GenieContext::Interface(context),
            embedded_buffer_{embedded_buffer}
    {}

    bool set_content(const std::string &path) final
    {
        if (!File::IsFileExist(path) || File::IsFileEmpty(path))
        {
            My_Log{My_Log::Level::kError} << "embed path: " << path << " is not correct \n";
            return false;
        }

        My_Log{} << "content.embeddings_.11111size(): " << "\n";
        std::ifstream in(path, std::ifstream::binary);
        auto size = File::get_file_size(path);
        content_->embeddings_.resize(size);
        in.read(reinterpret_cast<char *>(content_->embeddings_.data()), size);
        return true;
    }

    Genie_Status_t GenieDialogQuery(const GenieContext::Content &content) final
    {
        My_Log{} << "content.embeddings_.size(): " << content.embeddings_.size() << "\n";

        return GenieDialog_embeddingQuery(DialogHandle_,
                                          content.embeddings_.data(),
                                          content.embeddings_.size(),
                                          GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_COMPLETE,
                                          tokenToEmbedCallback,
                                          GenieCallBack,
                                          this);
    }

private:
    const std::vector<uint8_t> &embedded_buffer_;

    static void tokenToEmbedCallback(const int32_t token,
                                     void *embedding,
                                     const uint32_t embeddingSize,
                                     const void *userData)
    {
        auto *self = static_cast<IEmbedding *>(const_cast<void *>(userData));

        const size_t lutIndex = token * embeddingSize;
        if ((lutIndex + embeddingSize) <= self->embedded_buffer_.size())
        {
            const int8_t *embeddingSrc = reinterpret_cast<const int8_t *>(self->embedded_buffer_.data()) + lutIndex;
            int8_t *embeddingDst = static_cast<int8_t *>(embedding);
            std::copy(embeddingSrc, embeddingSrc + embeddingSize, embeddingDst);
        }
        else
        {
            My_Log{My_Log::Level::kError} << "Error: T2E conversion overflow.\n";
        }
    }
};

void GenieContext::Interface::GenieCallBack(const char *response,
                                            const GenieDialog_SentenceCode_t sentence_code,
                                            const void *user_data)
{
    auto *self = static_cast<Interface *>(const_cast<void *>(user_data))->context_;
    if (sentence_code == GenieDialog_SentenceCode_t::GENIE_DIALOG_SENTENCE_END
        || !response
        || !strlen(response))
    {
        return;
    }

    std::lock_guard<std::mutex> guard(self->m_stream_lock);
    self->m_stream_answer += response;
    g_CurLength += self->TokenLength(response);
    if (g_CurLength >= g_MaxLength)
    {
        My_Log{My_Log::Level::kError} << "g_CurLength: " << g_CurLength << "is over and will stop self"
                                      << std::endl;
        self->Stop();
    }
}

GenieContext::Interface *GenieContext::Interface::CreateInterface(GenieContext *context)
{
    // TODO: QueryType::TokenQuery
    switch (context->model_config_.get_query_type().v_)
    {
        case QueryType::TextQuery:
        case QueryType::TokenQuery:
            return new IGeneral{context};
        case QueryType::EmbeddingQuery:
            return new IEmbedding{context, context->model_config_.get_query_embedded_buffer()};
    }
}

#endif //GENIE_IFACE_H
