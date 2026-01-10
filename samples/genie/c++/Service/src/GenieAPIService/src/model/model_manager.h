#ifndef MODEL_MANAGER_H
#define MODEL_MANAGER_H

#include "model_config.h"

class ModelManager : public IModelConfig
{
public:
    explicit ModelManager(IModelConfig &&config);

    bool LoadModelByName(const std::string &model_name, bool &first_load);

    bool InitializeConfig(bool load);

    void UnloadModel();

    bool IsLoaded()
    { return loaded_; }

private:
    bool LoadModel();

    PromptType LoadPromptTemplates(std::string &&prompt_path);

    QueryType CheckModelQueryStyle();

    std::string ResolveKnownModelPath(const std::string& model_feature, bool contain);

    void Clean();

    bool ModelComparer(const std::string &source, const std::string &target, bool contain);

    struct ModeVerifier;

    class EmbeddingVerifier;
    
    std::atomic<bool> loaded_{false};
};

#endif //MODEL_MANAGER_H
