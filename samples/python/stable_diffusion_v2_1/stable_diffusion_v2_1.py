# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = "1"  # Disable 'cache-system uses symlinks' warning.
os.environ['HF_ENDPOINT'] = "https://hf-api.gitee.com"
import utils.install as install
import time
from PIL import Image
import numpy as np
import torch
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
from diffusers.models.embeddings import get_timestep_embedding, TimestepEmbedding
import argparse

from qai_appbuilder import (QNNContext, QNNContextProc, QNNShareMemory, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, timer)


####################################################################

MODEL_NAME                  = "stable_diffusion_v2_1"
TEXT_ENCODER_MODEL_NAME     = MODEL_NAME + "_quantized-textencoderquantizable-qualcomm_snapdragon_x_elite.bin"
UNET_MODEL_NAME             = MODEL_NAME + "_quantized-unetquantizable-qualcomm_snapdragon_x_elite.bin"
VAE_DECODER_MODEL_NAME      = MODEL_NAME + "_quantized-vaedecoderquantizable-qualcomm_snapdragon_x_elite.bin"

TIMESTEP_EMBEDDING_MODEL_ID = "m0q96xyyq"
TOKENIZER_MODEL_NAME        = "stabilityai/stable-diffusion-2-1-base"
TOKENIZER_HELP_URL          = "https://github.com/quic/ai-engine-direct-helper/blob/main/samples/python/" + MODEL_NAME + "/README.md#clip-vit-l14-model"

####################################################################

execution_ws = os.getcwd()

qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

#Model pathes.
model_dir = execution_ws + "\\models"
sd_dir = model_dir
tokenizer_dir = model_dir + "\\tokenizer\\"
time_embedding_dir = model_dir + "\\time-embedding\\"

text_encoder_model_path = sd_dir + "\\" + TEXT_ENCODER_MODEL_NAME
unet_model_path = sd_dir + "\\" + UNET_MODEL_NAME
vae_decoder_model_path = sd_dir + "\\" + VAE_DECODER_MODEL_NAME

tokenizer = None
scheduler = None
tokenizer_max_length = 77   # Define Tokenizer output max length (must be 77)

# model objects.
text_encoder = None
unet = None
vae_decoder = None
share_memory = None

# Any user defined prompt
user_prompt = ""
uncond_prompt = ""
user_seed = np.int64(0)
user_step = 20              # User defined step value, any integer value in {20, 30, 50}
user_text_guidance = 7.5    # User define text guidance, any float value in [5.0, 15.0]

model_inited = False

####################################################################

class TextEncoder(QNNContextProc):
    def Inference(self, share_mem, input_data):
        input_datas=[input_data]
        output_data = super().Inference(share_mem, input_datas)[0]

        # Output of Text encoder should be of shape (1, 77, 1024)
        output_data = output_data.reshape((1, 77, 1024))
        return output_data

class Unet(QNNContextProc):
    def Inference(self, share_mem, input_data_1, input_data_2, input_data_3):
        # We need to reshape the array to 1 dimensionality before send it to the network. 'input_data_2' already is 1 dimensionality, so doesn't need to reshape.
        input_data_1 = input_data_1.reshape(input_data_1.size)
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas=[input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(share_mem, input_datas)[0]

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
    global model_inited
    global scheduler, tokenizer, text_encoder, unet, vae_decoder, share_memory

    if model_inited == True:
        return True

    result = True

    SetQNNConfig()

    model_download()

    # model names
    model_text_encoder  = "text_encoder"
    model_unet          = "model_unet"
    model_vae_decoder   = "vae_decoder"

    # Initializing the Tokenizer
    try:
        if os.path.exists(tokenizer_dir) and not os.path.exists(tokenizer_dir + "\\.locks") :
            tokenizer = CLIPTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
        elif os.path.exists(tokenizer_dir):     # Speed up the model loading if the model is ready. Avoiding to check it through network.
            tokenizer = CLIPTokenizer.from_pretrained(TOKENIZER_MODEL_NAME, subfolder="tokenizer", revision="main", cache_dir=tokenizer_dir, local_files_only=True)
        else:
            tokenizer = CLIPTokenizer.from_pretrained(TOKENIZER_MODEL_NAME, subfolder="tokenizer", revision="main", cache_dir=tokenizer_dir)
    except Exception as e:
        # print(e)
        fail = "\nFailed to download tokenizer model. Please prepare the tokenizer data according to the guide below:\n" + TOKENIZER_HELP_URL + "\n"
        print(fail)
        exit()

    # Instance for TextEncoder 

    text_encoder = TextEncoder(model_text_encoder, "model_process", text_encoder_model_path)

    # Instance for Unet 
    unet = Unet(model_unet, "model_process", unet_model_path)

    # Instance for VaeDecoder 
    vae_decoder = VaeDecoder(model_vae_decoder, vae_decoder_model_path)

    share_memory = QNNShareMemory("share_memory", 1024 * 1024 * 50) # 50M

    # Scheduler - initializing the Scheduler.
    scheduler = DPMSolverMultistepScheduler(num_train_timesteps=1000, beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear")

    model_inited = True

    return result

def run_tokenizer(prompt):
    text_input = tokenizer(prompt, padding="max_length", max_length=tokenizer_max_length, truncation=True)
    text_input = np.array(text_input.input_ids, dtype=np.float32)
    return text_input

# These parameters can be configured through GUI 'settings'.
def setup_parameters(prompt, un_prompt, seed, step, text_guidance):

    global user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance

    user_prompt = prompt
    uncond_prompt = un_prompt
    user_seed = np.int64(seed)
    user_step = step
    user_text_guidance = text_guidance
    
    if user_seed == -1:
        user_seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)

    assert isinstance(user_seed, np.int64) == True, "user_seed should be of type int64"
    assert isinstance(user_step, int) == True, "user_step should be of type int"
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
def model_execute(callback, image_path, show_image = True):
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    scheduler.set_timesteps(user_step)  # Setting up user provided time steps for Scheduler

    # Run Tokenizer
    cond_tokens = run_tokenizer(user_prompt)
    uncond_tokens = run_tokenizer(uncond_prompt)

    # Run Text Encoder on Tokens
    uncond_text_embedding = text_encoder.Inference(share_memory, uncond_tokens)
    uncond_text_embedding = uncond_text_embedding.copy()

    user_text_embedding = text_encoder.Inference(share_memory, cond_tokens)
    user_text_embedding = user_text_embedding.copy()
	
    # Initialize the latent input with random initial latent
    random_init_latent = torch.randn((1, 4, 64, 64), generator=torch.manual_seed(user_seed)).numpy()
    latent_in = random_init_latent.transpose(0, 2, 3, 1)

    time_emb_path = time_embedding_dir + str(user_step) + "\\"

    # Run the loop for user_step times
    for step in range(user_step):
        time_embedding = None

        print(f'Step {step} Running...')

        time_step = get_timestep(step)

        unconditional_noise_pred = unet.Inference(share_memory, latent_in, time_step, uncond_text_embedding)
        unconditional_noise_pred = unconditional_noise_pred.copy()

        conditional_noise_pred = unet.Inference(share_memory, latent_in, time_step, user_text_embedding)
        conditional_noise_pred = conditional_noise_pred.copy()

        latent_in = run_scheduler(unconditional_noise_pred, conditional_noise_pred, latent_in, time_step)

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

        if not os.path.exists(image_path):
            os.makedirs(image_path, exist_ok=True)
        image_path = image_path + "\\%s_%s_%s.jpg"%(formatted_time, str(user_seed), str(image_size))

        output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
        output_image = output_image.reshape(image_size, image_size, -1)
        output_image = Image.fromarray(output_image, mode="RGB")
        output_image.save(image_path)
        
        if show_image:
            output_image.show()

        callback(image_path)

    PerfProfile.RelPerfProfileGlobal()

    return image_path

# Release all the models.
def model_destroy():
    global text_encoder, unet, vae_decoder, share_memory

    del(text_encoder)
    del(unet)
    del(vae_decoder)

    del(share_memory)

def SetQNNConfig():
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.ERROR, ProfilingLevel.BASIC)

####################################################################

def modelExecuteCallback(result):
    if ((None == result) or isinstance(result, str)):   # None == Image generates failed. 'str' == image_path: generated new image path.
        if (None == result):
            result = "None"
        else:
            print("Image saved to '" + result + "'")
    else:
        result = (result + 1) * 100
        result = int(result / user_step)
        result = str(result)
        # print("modelExecuteCallback result: " + result)

####################################################################

def model_download():
    ret = True

    desc = "Please download Stable-Diffusion-v2.1 model from https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1_quantized and save them to path 'samples\\python\\stable_diffusion_v2_1\\models'.\n"
    if not os.path.exists(text_encoder_model_path) or not os.path.exists(unet_model_path) or not os.path.exists(vae_decoder_model_path):
        print(desc)
        exit()

    if not ret:
        if not os.path.exists(time_embedding_dir):  # There is no timestep_embedding data, exit process.
            exit()

####################################################################


if __name__ == "__main__":
    DEFAULT_PROMPT = "spectacular view of northern lights from Alaska"

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, type=str)
    args = parser.parse_args()

    model_initialize()

    time_start = time.time()

    user_prompt = args.prompt
    uncond_prompt = "lowres, text, error, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark"
    user_seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)
    user_step = 20
    user_text_guidance = 7.5

    setup_parameters(user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance)
    model_execute(modelExecuteCallback, execution_ws + "\\images")

    time_end = time.time()
    print("time consumes for inference {}(s)".format(str(time_end - time_start)))

    model_destroy()
