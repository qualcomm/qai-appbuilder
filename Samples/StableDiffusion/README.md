# StableDiffusion Sample Code

## Introduction
This is sample code for using QNNHelper to load Stable Diffusion QNN models, run the inference and free the resource. 

## Stable Diffusion QNN models
You need to generate Stable Diffusion QNN models according to the guide below before you running it with this sample code:
https://docs.qualcomm.com/bundle/publicresource/topics/80-64748-1/introduction.html

## time-embedding
In this sample code, it needs to load 'time-embedding' data which is preprocessed. 
```
time_emb_path = cache_dir + "\\time-embedding\\" + str(user_step) + "\\"
```

The code of generate the 'time-embedding' data:
```
import torch
import numpy as np
from diffusers.models.embeddings import get_timestep_embedding

user_step = 20
time_embeddings = UNet2DConditionModel.from_pretrained('runwayml/stable-diffusion-v1-5', subfolder='unet', cache_dir='./cache/diffusers').time_embedding

def get_time_embedding(timestep):
    timestep = torch.tensor([timestep])
    t_emb = get_timestep_embedding(timestep, 320, True, 0)
    emb = time_embeddings(t_emb).detach().numpy()
    return emb

def gen_time_embedding():
    time_emb_path = "\\models\\cache\\time-embedding\\" + str(user_step) + "\\"
    for step in range(user_step):
        file_path = time_emb_path + str(step) + ".raw"
        timestep = get_timestep(step)
        time_embedding = get_time_embedding(timestep)
        time_embedding.tofile(file_path)

# Only needs to executed once for generating time enbedding data to app folder.
# Modify 'user_step' to '20', '30', '50' to generate 'time_embedding' for steps - '20', '30', '50'.
gen_time_embedding()
```

## clip-vit-base-patch32
In this sample code, it needs *clip-vit-base-patch32* data. 
```
tokenizer = CLIPTokenizer.from_pretrained(cache_dir + '\\clip-vit-base-patch32\\', local_files_only=True)
```

You can download the file below from 'https://huggingface.co/openai/clip-vit-base-patch32/tree/main' and save them to foldet 'clip-vit-base-patch32':
```
config.json
merges.txt
special_tokens_map.json
tokenizer_config.json
vocab.json
```
