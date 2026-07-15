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
# Load Qwen3-VL model and configuration
from transformers.models.qwen3_vl import modeling_qwen3_vl

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
        self.lookup_table_np = self.lookup_table_np.reshape(
            genie_config["dialog"]["context"]["n-vocab"], 
            genie_config["dialog"]["embedding"]["size"]
        )

        self.stream_chunk = ""
        
        # Try to load embedding layer from local model if available
        try:
            print("Attempting to load Qwen3-VL embedding layer from local cache...")
            from transformers.models.qwen3_vl import modeling_qwen3_vl
            
            model_id = "Qwen/Qwen3-VL-4B-Instruct"
            vl_config = AutoConfig.from_pretrained(model_id, local_files_only=True)
            model = modeling_qwen3_vl.Qwen3VLForConditionalGeneration.from_pretrained(
                model_id, 
                cache_dir="./cache", 
                config=vl_config,
                local_files_only=True
            )
            self.embedding_layer = model.model.language_model.embed_tokens
            print("✓ Loaded Qwen3-VL embedding layer from local cache")
        except Exception as e:
            print(f"Warning: Could not load Qwen3-VL embedding layer: {e}")
            print("Using lookup table for embeddings instead")
            self.embedding_layer = None
        
        super().SetEmbeddingTable(lookup_table)

    def get_embeddings(self, token_ids, image_embeddings=None):
        """Get embeddings for token IDs."""
        if self.embedding_layer is not None:
            # Use transformer embedding layer if available
            inputs_embeds = self.embedding_layer(token_ids)
            print(f"Token processing:")
            print(f"  Token IDs shape: {token_ids.shape}")
            print(f"  Input embeddings shape: {inputs_embeds.shape}")
            return inputs_embeds
        else:
            # Fallback to lookup table
            print("Using lookup table for embeddings")
            token_embeddings = []
            for token_id in token_ids:
                token_embeddings.append(self.lookup_table_np[token_id, :])
            token_embeddings_np = np.stack(token_embeddings, axis=0)
            return torch.from_numpy(token_embeddings_np)
    
    # def Inference(self, video_token_id, token_ids, image_embeddings):        
    #     inputs_embeds =torch.from_numpy(self.get_embeddings(token_ids))
    #     image_embeddings=torch.from_numpy(image_embeddings)
        
    #     image_mask = (token_ids == video_token_id).unsqueeze(-1).expand_as(inputs_embeds)
    #     inputs_embeds = inputs_embeds.masked_scatter(image_mask, image_embeddings).detach().numpy()
    #     inputs_embeds.tofile("inputs_embeds.raw")       
    #     input_data = inputs_embeds.astype("float32").ravel().tolist()
    #     response=super().QueryByEmbedding(input_data, self.on_stream)
    #     return response

    def Inference(self, video_token_id, token_ids, image_embeddings):        
        # Ensure image_embeddings is a torch tensor
        if isinstance(image_embeddings, np.ndarray):
            image_embeddings = torch.from_numpy(image_embeddings)

        # Get token embeddings (returns torch tensor)
        inputs_embeds = self.get_embeddings(token_ids)

        image_mask = (token_ids == video_token_id)
        num_image_tokens = image_mask.sum().item()

        print(f"\nImage token analysis:")
        print(f"  Image token ID: {video_token_id}")
        print(f"  Number of image tokens found: {num_image_tokens}")
        print(f"  Vision embeddings shape: {image_embeddings.shape}")

        # Flatten image_embeddings to [num_tokens, embed_dim]
        image_embeddings_flat = image_embeddings.reshape(-1, image_embeddings.shape[-1])
        print(f"  Flattened vision embeddings: {image_embeddings_flat.shape}")

        # Validate token count consistency
        if image_embeddings_flat.shape[0] != num_image_tokens:
            print(f"  Warning: vision tokens ({image_embeddings_flat.shape[0]}) "
                  f"!= image placeholder tokens ({num_image_tokens}), proceeding anyway")

        # Replace image placeholder embeddings with vision embeddings
        image_mask_expanded = image_mask.unsqueeze(-1).expand_as(inputs_embeds)
        multimodal_embeddings = inputs_embeds.masked_scatter(
            image_mask_expanded, 
            image_embeddings_flat
        ).detach().numpy()

        multimodal_embeddings.tofile("inputs_embeds.raw")
        input_data = multimodal_embeddings.astype("float32").ravel().tolist()
        response = super().QueryByEmbedding(input_data, self.on_stream)
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

       
    def Init(self, onGenieCallback=None):
        QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
        
        self.veg = Qwen3VLQnnVeg(self.veg_model_path, self.runtime_path)       
        self.llm = Qwen3VLQnnLLM(self.llm_model_path, lookup_table=self.look_up_table_path, onGenieCallback=onGenieCallback)
        
        # Try to load processor from local path first, fallback to Qwen2-VL if not available
        try:
            # Try Qwen3-VL processor
            self.processor = AutoProcessor.from_pretrained(
                "Qwen/Qwen3-VL-4B-Instruct", 
                trust_remote_code=True,
                local_files_only=False
            )
            self.llm_config = AutoConfig.from_pretrained(
                "Qwen/Qwen3-VL-4B-Instruct", 
                trust_remote_code=True,
                local_files_only=False
            )
            print("Loaded Qwen3-VL processor from local cache")
        except Exception as e:
            print(f"Warning: Could not load Qwen3-VL processor locally: {e}")
            print("Falling back to Qwen2-VL processor...")
            # Fallback to Qwen2-VL which is more commonly available
            self.processor = AutoProcessor.from_pretrained(
                "Qwen/Qwen2-VL-2B-Instruct", 
                trust_remote_code=True
            )
            self.llm_config = AutoConfig.from_pretrained(
                "Qwen/Qwen2-VL-2B-Instruct", 
                trust_remote_code=True
            )
            print("Loaded Qwen2-VL processor as fallback")
        
        # Use image_token_id instead of video_token_id
        self.video_token_id = self.llm_config.image_token_id
    

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