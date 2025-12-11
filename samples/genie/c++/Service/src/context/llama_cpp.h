#ifndef GENIEAPICLIENT_SLN_LLAMA_CPP_H
#define GENIEAPICLIENT_SLN_LLAMA_CPP_H

#include "core/context_base.h"

class LLAMACppBuilder : public ContextBase
{
public:
    explicit LLAMACppBuilder(const IModelConfig &info);

    ~LLAMACppBuilder() override;

    bool Query(const std::string &prompt, Callback callback) override;

    bool Stop() override;

    size_t TokenLength(const std::string &text) override;

    json HandleProfile() override;

private:
    class Impl;

    Impl *impl_;
};

#endif //GENIEAPICLIENT_SLN_LLAMA_CPP_H
