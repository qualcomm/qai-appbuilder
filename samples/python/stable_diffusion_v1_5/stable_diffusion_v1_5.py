# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import time
from PIL import Image
import os
import shutil
import cv2
import numpy as np
import torch
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler

from qai_appbuilder import (QNNContext, QNNContextProc, QNNShareMemory, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, timer)

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qnn"

#Model pathes.
model_dir = execution_ws + "\\models"
sd_dir = model_dir
clip_dir = model_dir + "\\clip-vit-large-patch14\\"
time_embedding_dir = model_dir + "\\time-embedding_v1.5\\"

tokenizer = None
scheduler = None
tokenizer_max_length = 77   # Define Tokenizer output max length (must be 77)

# model objects.
text_encoder = None
unet = None
vae_decoder = None

# Any user defined prompt
user_prompt = ""
uncond_prompt = ""
user_seed = np.int64(0)
user_step = 20              # User defined step value, any integer value in {20, 30, 50}
user_text_guidance = 7.5    # User define text guidance, any float value in [5.0, 15.0]

####################################################################

class TextEncoder(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]

        # Output of Text encoder should be of shape (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data

class Unet(QNNContext):
    def Inference(self, input_data_1, input_data_2, input_data_3):
        # We need to reshape the array to 1 dimensionality before send it to the network. 'input_data_2' already is 1 dimensionality, so doesn't need to reshape.
        input_data_1 = input_data_1.reshape(input_data_1.size)
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas=[input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

class VaeDecoder(QNNContext):
    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        input_datas=[input_data]

        output_data = super().Inference(input_datas)[0]
        
        return output_data

####################################################################


def model_initialize():
    global scheduler
    global tokenizer
    global text_encoder
    global unet
    global vae_decoder

    result = True

    # model names
    model_text_encoder  = "text_encoder"
    model_unet          = "model_unet"
    model_vae_decoder   = "vae_decoder"

    # models' path.
    text_encoder_model = sd_dir + "\\stable_diffusion_v1_5_quantized-textencoder_quantized.bin"
    unet_model = sd_dir + "\\stable_diffusion_v1_5_quantized-unet_quantized.bin"
    vae_decoder_model = sd_dir + "\\stable_diffusion_v1_5_quantized-vaedecoder_quantized.bin"

    # Instance for TextEncoder 
    text_encoder = TextEncoder(model_text_encoder, text_encoder_model)

    # Instance for Unet 
    unet = Unet(model_unet, unet_model)

    # Instance for VaeDecoder 
    vae_decoder = VaeDecoder(model_vae_decoder, vae_decoder_model)

    # Initializing the Tokenizer
    tokenizer = CLIPTokenizer.from_pretrained(clip_dir, local_files_only=True)

    # Scheduler - initializing the Scheduler.
    scheduler = DPMSolverMultistepScheduler(num_train_timesteps=1000, beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear")

    return result

def run_tokenizer(prompt):
    text_input = tokenizer(prompt, padding="max_length", max_length=tokenizer_max_length, truncation=True)
    text_input = np.array(text_input.input_ids, dtype=np.float32)
    return text_input

# These parameters can be configured through GUI 'settings'.
def setup_parameters(prompt, un_prompt, seed, step, text_guidance):

    global user_prompt
    global uncond_prompt
    global user_seed
    global user_step
    global user_text_guidance

    user_prompt = prompt
    uncond_prompt = un_prompt
    user_seed = seed
    user_step = step
    user_text_guidance = text_guidance

    assert isinstance(user_seed, np.int64) == True, "user_seed should be of type int64"
    assert isinstance(user_step, int) == True, "user_step should be of type int"
    assert user_step == 20 or user_step == 30 or user_step == 50, "user_step should be either 20, 30 or 50"
    assert isinstance(user_text_guidance, float) == True, "user_text_guidance should be of type float"
    assert user_text_guidance >= 5.0 and user_text_guidance <= 15.0, "user_text_guidance should be a float from [5.0, 15.0]"

def run_scheduler(noise_pred_uncond, noise_pred_text, latent_in, timestep):
    # Convert all inputs from NHWC to NCHW
    noise_pred_uncond = np.transpose(noise_pred_uncond, (0, 3, 1, 2)).copy()
    noise_pred_text = np.transpose(noise_pred_text, (0, 3, 1, 2)).copy()
    latent_in = np.transpose(latent_in, (0, 3, 1, 2)).copy()

    # Convert all inputs to torch tensors
    noise_pred_uncond = torch.from_numpy(noise_pred_uncond)
    noise_pred_text = torch.from_numpy(noise_pred_text)
    latent_in = torch.from_numpy(latent_in)

    # Merge noise_pred_uncond and noise_pred_text based on user_text_guidance
    noise_pred = noise_pred_uncond + user_text_guidance * (noise_pred_text - noise_pred_uncond)

    # Run Scheduler step
    latent_out = scheduler.step(noise_pred, timestep, latent_in).prev_sample.numpy()

    # Convert latent_out from NCHW to NHWC
    latent_out = np.transpose(latent_out, (0, 2, 3, 1)).copy()

    return latent_out

# Function to get timesteps
def get_timestep(step):
    return np.int32(scheduler.timesteps.numpy()[step])

# Execute the Stable Diffusion pipeline
def model_execute(callback):
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    scheduler.set_timesteps(user_step)  # Setting up user provided time steps for Scheduler

    # Run Tokenizer
    cond_tokens = run_tokenizer(user_prompt)
    uncond_tokens = run_tokenizer(uncond_prompt)

    # Run Text Encoder on Tokens
    uncond_text_embedding = text_encoder.Inference(uncond_tokens)
    user_text_embedding = text_encoder.Inference(cond_tokens)

    # Initialize the latent input with random initial latent
    random_init_latent = torch.randn((1, 4, 64, 64), generator=torch.manual_seed(user_seed)).numpy()
    latent_in = random_init_latent.transpose(0, 2, 3, 1)

    time_emb_path = time_embedding_dir + str(user_step) + "\\"

    # Run the loop for user_step times
    for step in range(user_step):
        print(f'Step {step} Running...')

        timestep = get_timestep(step)   # time_embedding = get_time_embedding(timestep)
        file_path = time_emb_path + str(step) + ".raw"
        time_embedding = np.fromfile(file_path, dtype=np.float32)

        unconditional_noise_pred = unet.Inference(latent_in, time_embedding, uncond_text_embedding)
        conditional_noise_pred = unet.Inference(latent_in, time_embedding, user_text_embedding)

        latent_in = run_scheduler(unconditional_noise_pred, conditional_noise_pred, latent_in, timestep)
        callback(step)

    # Run VAE
    import datetime
    now = datetime.datetime.now()
    output_image = vae_decoder.Inference(latent_in)
    formatted_time = now.strftime("%Y_%m_%d_%H_%M_%S")

    if len(output_image) == 0:
        callback(None)
    else:
        image_size = 512
        if not os.path.exists("images"):
            os.mkdir("images")
        image_path = "images\\%s_%s_%s.jpg"%(formatted_time, str(user_seed), str(image_size))
        output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
        output_image = output_image.reshape(image_size, image_size, -1)
        Image.fromarray(output_image, mode="RGB").save(image_path)

        callback(image_path)

    PerfProfile.RelPerfProfileGlobal()

# Release all the models.
def model_destroy():
    global text_encoder
    global unet
    global vae_decoder

    del(text_encoder)
    del(unet)
    del(vae_decoder)

####################################################################

def SetQNNConfig():
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)


####################################################################

def modelExecuteCallback(result):
    if ((None == result) or isinstance(result, str)):   # None == Image generates failed. 'str' == image_path: generated new image path.
        if (None == result):
            result = "None"
        print("modelExecuteCallback result: " + result)
    else:
        result = (result + 1) * 100
        result = int(result / user_step)
        result = str(result)
        print("modelExecuteCallback result: " + result)


SetQNNConfig()

model_initialize()

time_start = time.time()

user_prompt = "Big white bird near river in high resolution, 4K"
uncond_prompt = "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry"
user_seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)
user_step = 20
user_text_guidance = 7.5

setup_parameters(user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance)

model_execute(modelExecuteCallback)

time_end = time.time()
print("time consumes for inference {}(s)".format(str(time_end - time_start)))

model_destroy()
