# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import json
import math
import os
import sys
from typing import Optional, Union
import torch
import queue
from transformers import AutoImageProcessor,AutoProcessor
import numpy as np
from PIL import Image
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, GenieContext)
from transformers import AutoConfig
from qwen_vl_utils import process_vision_info


class Qwen3VLQnnVeg(QNNContext):
    def __init__(self,
                 veg_model_path: Optional[str] = None, 
                 runtime_path: Optional[str] = None):
        self.veg_model_path = veg_model_path
        self.runtime_path = runtime_path
        super().__init__(
            model_name="Qwen3-VL-Veg",
            backend_lib_path="libQnnHtp.so",
            system_lib_path="libQnnSystem.so",
            model_path=veg_model_path)
        print(f"Initialized Qwen3VLQnnVeg with model path: {veg_model_path} and runtime path: {runtime_path}")
        self.param_path = self.veg_model_path.replace("qwen3_vl_4b_vit.serialized.bin","")
        print(f"Loading VEG parameters from {self.param_path}...")
        self.position_ids_cos=np.fromfile(os.path.join(self.param_path, "position_ids_cos.raw"), dtype=np.float32)
        self.position_ids_sin=np.fromfile(os.path.join(self.param_path, "position_ids_sin.raw"), dtype=np.float32)
        #self.mask=np.fromfile(os.path.join(self.param_path, "mask.raw"), dtype=np.float32)
        #self.pixel_values=np.fromfile(os.path.join(self.param_path, "pixel_values.raw"), dtype=np.float32)  


    def Inference(self, pixel_values):
        input_datas=[pixel_values, self.position_ids_cos, self.position_ids_sin]
        output_data = super().Inference(input_datas)    
        return output_data
    
class Qwen3VLQnnLLM(GenieContext):
    def __init__(self, config_path: str, lookup_table: str, onGenieCallback=None, debug: bool = False):
        self.onGenieCallback = onGenieCallback
        super().__init__(config_path, debug)
        json_file = open(config_path, 'r')
        genie_config = json.load(json_file)
        self.lookup_table_np = np.fromfile(lookup_table, dtype=np.float32)
        # Reshape lookup table to n-vocab x embedding_vector_len
        self.lookup_table_np = self.lookup_table_np.reshape(genie_config["dialog"]["context"]["n-vocab"], genie_config["dialog"]["embedding"]["size"])

        self.stream_chunk = ""
        super().SetEmbeddingTable(lookup_table)

    def get_embeddings(self, token_ids):
        token_embeddings =  []
        # Get embedding for each token:
        for token_id in token_ids:
            token_embeddings.append(self.lookup_table_np[token_id, :])
        # Stack all token embeddings together:
        token_embeddings_np = np.stack(token_embeddings, axis=0)
        return token_embeddings_np

    # def embedding_layer(self, token_ids, image_embeddings):
    #     inputs_embeds = torch.from_numpy(self.get_embeddings(token_ids))
    #     image_embeddings = torch.from_numpy(image_embeddings)
    #     
    #     image_mask = (token_ids == self.video_token_id).unsqueeze(-1).expand_as(inputs_embeds)
    #     inputs_embeds = inputs_embeds.masked_scatter(image_mask, image_embeddings).detach().numpy()
    #     return inputs_embeds
    
    def Inference(self, video_token_id, token_ids, image_embeddings):        
        inputs_embeds =torch.from_numpy(self.get_embeddings(token_ids))
        image_embeddings=torch.from_numpy(image_embeddings)
        
        image_mask = (token_ids == video_token_id).unsqueeze(-1).expand_as(inputs_embeds)
        inputs_embeds = inputs_embeds.masked_scatter(image_mask, image_embeddings).detach().numpy()
        inputs_embeds.tofile("inputs_embeds.raw")       
        input_data = inputs_embeds.astype("float32").ravel().tolist()
        response=super().QueryByEmbedding(input_data, self.on_stream)
        return response
    
    def on_stream(self,text: str,stop: bool = False) -> bool:        
        print(text, end="", flush=True)
        if self.onGenieCallback:
            self.onGenieCallback(text)        
        return True  # return False to stop early if you wish

class Qwen3VLQnn():
    def __init__(self,                  
                 veg_model_path: Optional[str] = None, 
                 llm_model_path: str = None, 
                 look_up_table_path:str= None,
                 runtime_path: Optional[str] = None):
        self.veg_model_path = veg_model_path
        self.llm_model_path = llm_model_path
        self.runtime_path = runtime_path
        self.look_up_table_path=look_up_table_path
    
    def create_message(self, image_path, prompt):    
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "image": image_path,
                        "resized_height": 448,
                        "resized_width": 448,
                    },
                    {
                        "type": "text",
                        "text": prompt
                    },
                ],
            },
        ]
        return messages

       
    def Init(self,onGenieCallback=None):
        QNNConfig.Config(self.runtime_path, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
        
        self.veg = Qwen3VLQnnVeg(self.veg_model_path, self.runtime_path)       
        self.llm = Qwen3VLQnnLLM(self.llm_model_path, lookup_table=self.look_up_table_path,onGenieCallback=onGenieCallback)
        self.processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-4B-Instruct", trust_remote_code=True)
        self.llm_config = AutoConfig.from_pretrained("Qwen/Qwen3-VL-4B-Instruct", trust_remote_code=True)
        self.video_token_id= self.llm_config.video_token_id
    

    def Inference(self, image_path: str, prompt: str) -> str:
        # Debug: print the type and value of image_path
        print(f"DEBUG: image_path type: {type(image_path)}, value: {image_path}")
        
        # Handle different input types
        if isinstance(image_path, list):
            print(f"DEBUG: image_path is a list with {len(image_path)} elements")
            # If it's a list, take the first element
            if len(image_path) > 0:
                image_path = image_path[0]
                print(f"DEBUG: Extracted first element, new type: {type(image_path)}, value: {image_path}")
            else:
                raise ValueError("Empty image list provided")
        
        # If image_path is a numpy array (from Gradio), save it first
        if isinstance(image_path, np.ndarray):
            print(f"DEBUG: image_path is a numpy array with shape: {image_path.shape}")
            import tempfile
            temp_path = tempfile.mktemp(suffix=".jpg")
            Image.fromarray(image_path).save(temp_path)
            image_path = temp_path
            print(f"DEBUG: Saved numpy array to: {image_path}")
        
        print(f"DEBUG: Final image_path before Image.open: type={type(image_path)}, value={image_path}")
        
        message = self.create_message(image_path, prompt)
        
        text = self.processor.apply_chat_template(
            message, 
            tokenize=False, 
            add_generation_prompt=True)
      
        # Load the image directly
        image = Image.open(image_path)

        image_inputs, video_inputs, video_kwargs = process_vision_info(message, return_video_kwargs=True)
        
        # For Qwen3-VL, images are treated as single-frame videos
        # Pass the image as a video (list of frames)
        inputs = self.processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt"
        )
        input_data = inputs['pixel_values'].detach().numpy().astype(np.float32)        
        image_embeddings = self.veg.Inference(input_data)[0]
        # Remove batch dimension from token_ids
        token_ids = inputs['input_ids'].squeeze(0)
        
        llm_outputs = self.llm.Inference(self.video_token_id, token_ids, image_embeddings)
        return llm_outputs