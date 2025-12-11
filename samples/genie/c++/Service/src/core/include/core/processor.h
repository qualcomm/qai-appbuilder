#ifndef GENIEAPICLIENT_SLN_MODEL_PROCESSING_H
#define GENIEAPICLIENT_SLN_MODEL_PROCESSING_H

#include "core/context_base.h"

class ModelProcessor
{
public:
    virtual ~ModelProcessor() = default;

    std::string extractFinalAnswer(const std::string &output)
    {};

    virtual void Clean() = 0;

    virtual std::tuple<bool, std::string> preprocessStream(std::string &chunkText,
                                                           bool isToolResponse,
                                                           std::string &toolResponse) = 0;

    std::string start_tag_;
};

#endif //GENIEAPICLIENT_SLN_MODEL_PROCESSING_H
