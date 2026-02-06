# Deployment GenieAPIService and Client on your device

You should choose and download models first by following this [Download Link](https://www.aidevhome.com/?id=51).

The path `models/[MODEL_NAME]/config.json` is recommended. For VLM, Please make the model follow
the [VLM model layout](#Deployment)

- Windows: move the models to `ai-engine-direct-helper\samples\genie\python\models` path.

- Android: Please push the model files into your device. The model files should be pushed to `/sdcard/GenieModels`.

## Deployment

Please keep VLM models following the layout

- [qwen2.5vl3b](#qwen2.5vl3b)
- [phi4mm](#phi4mm)

### qwen2.5vl3b

```
./models/qwen2.5vl3b
│   config.json
│   embedding_weights.raw
│   htp_backend_ext_config.json
│   llm_model-0.bin
│   llm_model-1.bin 
│   prompt.json
│   tokenizer.json
│   veg.serialized.bin
│   
└───raw
        full_attention_mask.raw
        position_ids_cos.raw
        position_ids_sin.raw
        window_attention_mask.raw
```

### phi4mm

```
./models/phi4mm
│   config.json
│   embedding_weights_200064x3072.raw
│   prompt.json
│   tokenizer.json
│   veg.serialized.bin
│   weights_sharing_model_1_of_2.serialized.bin
│   weights_sharing_model_2_of_2.serialized.bin
│
└───raw
        attention_mask.bin
        position_ids.bin
```
