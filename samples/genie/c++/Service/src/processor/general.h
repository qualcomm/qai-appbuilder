#ifndef GENIEAPICLIENT_SLN_GENERAL_H
#define GENIEAPICLIENT_SLN_GENERAL_H

#include "core/processor.h"

class GeneralProcessor : public ModelProcessor
{
public:
    std::tuple<bool, std::string>
    preprocessStream(std::string &chunkText, bool isToolResponse, std::string &toolResponse) override;

    void Clean() final
    {};
private:
    struct Utils;
};

#endif //GENIEAPICLIENT_SLN_GENERAL_H
