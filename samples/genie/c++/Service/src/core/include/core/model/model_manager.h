#ifndef MODEL_MANAGER_H
#define MODEL_MANAGER_H

#include "model_config.h"

class ChatHistory;

class ModelManager : public IModelConfig
{
public:
    ModelManager(IModelConfig &&config);

    bool LoadModelByName(const std::string &model_name, bool &new_model);

    bool InitializeConfig(bool load);

    void UnloadModel();

private:
    bool loadModel();

    bool LoadPromptTemplates(const std::string &promptPath);

    ModelStyle check_model_style();

    struct ModeInfo;

    bool loaded_{false};
};

#endif //MODEL_MANAGER_H
