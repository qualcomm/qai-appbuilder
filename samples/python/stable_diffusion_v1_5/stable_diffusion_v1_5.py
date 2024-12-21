# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("..")
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = "1"  # Disable 'cache-system uses symlinks' warning.
os.environ['HF_ENDPOINT'] = "https://hf-api.gitee.com"
import utils.install as install
import time
from PIL import Image
import shutil
import cv2
import numpy as np
import torch
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
from diffusers.models.embeddings import get_timestep_embedding, TimestepEmbedding
import argparse

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, timer)


####################################################################

MODEL_NAME                  = "stable_diffusion_v1_5"
TEXT_ENCODER_MODEL_LINK     = MODEL_NAME + "_quantized/v1/QNN224/text_encoder.serialized.bin"
UNET_MODEL_LINK             = MODEL_NAME + "_quantized/v1/QNN224/unet.serialized.bin"
VAE_DECODER_MODEL_LINK      = MODEL_NAME + "_quantized/v1/QNN224/vae_decoder.serialized.bin"
TEXT_ENCODER_MODEL_NAME     = MODEL_NAME + "_quantized-textencoder_quantized.bin"
UNET_MODEL_NAME             = MODEL_NAME + "_quantized-unet_quantized.bin"
VAE_DECODER_MODEL_NAME      = MODEL_NAME + "_quantized-vaedecoder_quantized.bin"

TEXT_ENCODER_MODEL_SIZE     = 163275152
UNET_MODEL_SIZE             = 878473240
VAE_DECODER_MODEL_SIZE      = 59072424

TIMESTEP_EMBEDDING_MODEL_ID = "m7mrzdgxn"
TOKENIZER_MODEL_NAME        = "openai/clip-vit-large-patch14"
TOKENIZER_HELP_URL          = "https://github.com/quic/ai-engine-direct-helper/blob/main/samples/python/" + MODEL_NAME + "/README.md#clip-vit-l14-model"
TIMESTEP_HTLP_URL           = "https://github.com/quic/ai-engine-direct-helper/blob/main/samples/python/" + MODEL_NAME + "/README.md#time-embedding"

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

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
time_embedding_model_path = sd_dir + "\\" + MODEL_NAME + "_time-embedding.pt"

tokenizer = None
scheduler = None
tokenizer_max_length = 77   # Define Tokenizer output max length (must be 77)

# model objects.
text_encoder = None
unet = None
vae_decoder = None
time_embeddings = None

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
    global scheduler, tokenizer, text_encoder, unet, vae_decoder, time_embeddings

    result = True

    # model names
    model_text_encoder  = "text_encoder"
    model_unet          = "model_unet"
    model_vae_decoder   = "vae_decoder"

    # Initializing the Tokenizer
    try:
        if os.path.exists(tokenizer_dir) and not os.path.exists(tokenizer_dir + "\\.locks") :
            tokenizer = CLIPTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
        elif os.path.exists(tokenizer_dir):     # Speed up the model loading if the model is ready. Avoiding to check it through network.
            tokenizer = CLIPTokenizer.from_pretrained(TOKENIZER_MODEL_NAME, cache_dir=tokenizer_dir, local_files_only=True)
        else:
            tokenizer = CLIPTokenizer.from_pretrained(TOKENIZER_MODEL_NAME, cache_dir=tokenizer_dir)
    except Exception as e:
        # print(e)
        fail = "\nFailed to download tokenizer model. Please prepare the tokenizer data according to the guide below:\n" + TOKENIZER_HELP_URL + "\n"
        print(fail)
        exit()

    # Instance for TextEncoder 
    text_encoder = TextEncoder(model_text_encoder, text_encoder_model_path)

    # Instance for Unet 
    unet = Unet(model_unet, unet_model_path)

    # Instance for VaeDecoder 
    vae_decoder = VaeDecoder(model_vae_decoder, vae_decoder_model_path)

    if os.path.exists(time_embedding_model_path):
        time_embeddings = TimestepEmbedding(320, 1280)
        time_embeddings.load_state_dict(torch.load(time_embedding_model_path, weights_only=True))

    # Scheduler - initializing the Scheduler.
    scheduler = DPMSolverMultistepScheduler(num_train_timesteps=1000, beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear")

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

def get_time_embedding(timestep, time_embeddings):
    timestep = torch.tensor([timestep])
    t_emb = get_timestep_embedding(timestep, 320, True, 0)
    emb = time_embeddings(t_emb).detach().numpy()
    return emb

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
        time_embedding = None

        print(f'Step {step} Running...')

        time_step = get_timestep(step)

        if time_embeddings:
            time_embedding = get_time_embedding(time_step, time_embeddings)
        else:
            file_path = time_emb_path + str(step) + ".raw"
            time_embedding = np.fromfile(file_path, dtype=np.float32)

        unconditional_noise_pred = unet.Inference(latent_in, time_embedding, uncond_text_embedding)
        conditional_noise_pred = unet.Inference(latent_in, time_embedding, user_text_embedding)

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
        image_path = execution_ws + "\\images"
        if not os.path.exists(image_path):
            os.makedirs(image_path, exist_ok=True)
        image_path = image_path + "\\%s_%s_%s.jpg"%(formatted_time, str(user_seed), str(image_size))

        output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
        output_image = output_image.reshape(image_size, image_size, -1)
        output_image = Image.fromarray(output_image, mode="RGB")
        output_image.save(image_path)
        output_image.show()

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

    text_encoder_model_url = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/" + TEXT_ENCODER_MODEL_LINK
    unet_model_url =         "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/" + UNET_MODEL_LINK
    vae_decoder_model_url =  "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/" + VAE_DECODER_MODEL_LINK

    ret = install.download_url(text_encoder_model_url, text_encoder_model_path, TEXT_ENCODER_MODEL_SIZE)
    ret = install.download_url(unet_model_url, unet_model_path, UNET_MODEL_SIZE)
    ret = install.download_url(vae_decoder_model_url, vae_decoder_model_path, VAE_DECODER_MODEL_SIZE)

    desc = "Downloading timestep_embedding model... "
    fail = "\nFailed to download timestep_embedding model. Please prepare the timestep_embedding data according to the guide below:\n" + TIMESTEP_HTLP_URL + "\n"
    ret = install.download_qai_hubmodel(TIMESTEP_EMBEDDING_MODEL_ID, time_embedding_model_path, desc=desc, fail=fail, hub_id=install.HUB_ID_T)

    if not ret:
        if not os.path.exists(time_embedding_dir):  # There is no timestep_embedding data, exit process.
            exit()

####################################################################


if __name__ == "__main__":
    DEFAULT_PROMPT = "spectacular view of northern lights from Alaska"

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, type=str)
    args = parser.parse_args()

    SetQNNConfig()

    model_download()

    model_initialize()

    time_start = time.time()

    user_prompt = args.prompt
    uncond_prompt = "lowres, text, error, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark"
    user_seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)
    user_step = 20
    user_text_guidance = 7.5

    setup_parameters(user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance)
    model_execute(modelExecuteCallback)

    time_end = time.time()
    print("time consumes for inference {}(s)".format(str(time_end - time_start)))

    model_destroy()
