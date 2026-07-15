# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = "1"  # Disable 'cache-system uses symlinks' warning.
os.environ['HF_ENDPOINT'] = "https://hf-api.gitee.com"

import time
import argparse
import numpy as np
from pathlib import Path

from qai_appbuilder import (QNNContext, QNNShareMemory, Runtime, LogLevel,
                             ProfilingLevel, PerfProfile, QNNConfig)

from _stable_diffusion import (
    detect_platform,
    parse_chipset_arg,
    set_qnn_config,
    download_sd_models,
    load_tokenizer,
    run_tokenizer,
    make_scheduler,
    get_timestep,
    run_scheduler,
    validate_and_prepare_parameters,
    generate_initial_latent,
    decode_vae_output,
    save_output_image,
    TextEncoderBase,
    UnetBase,
    VaeDecoderBase,
)

####################################################################

MODEL_NAME              = "stable_diffusion_v2_1"
MODEL_NAME_VAE          = "stable_diffusion_v2_1_VAE"
MODEL_NAME_UNET         = "stable_diffusion_v2_1_UNET"
MODEL_NAME_TEXT         = "stable_diffusion_v2_1_TEXT"
HUB_ID_H                = "ox06ibpbkxb4pr0mcyfe7wqgx5pf5r0cm3rf3dzi"
TEXT_ENCODER_MODEL_NAME = MODEL_NAME + "_quantized-textencoderquantizable-qualcomm_snapdragon_x_elite.bin"
UNET_MODEL_NAME         = MODEL_NAME + "_quantized-unetquantizable-qualcomm_snapdragon_x_elite.bin"
VAE_DECODER_MODEL_NAME  = MODEL_NAME + "_quantized-vaedecoderquantizable-qualcomm_snapdragon_x_elite.bin"

TOKENIZER_MODEL_NAME    = "stabilityai/stable-diffusion-2-1-base"
TOKENIZER_HELP_URL      = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/generativeAI/Image_Generation/stable_diffusion_v2_1/README.md"
MODEL_HELP_URL          = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/generativeAI/Image_Generation/stable_diffusion_v2_1/README.md"

####################################################################

SOC_ID   = parse_chipset_arg()
PLATFORM = detect_platform()
print(f"SOC_ID: {SOC_ID}")
print(f"[INFO] Detected platform: {PLATFORM}")

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))

# Model paths
model_dir           = execution_ws / "models"
tokenizer_dir       = model_dir / "tokenizer"
time_embedding_dir  = model_dir / "time-embedding"

text_encoder_model_path = model_dir / TEXT_ENCODER_MODEL_NAME
unet_model_path         = model_dir / UNET_MODEL_NAME
vae_decoder_model_path  = model_dir / VAE_DECODER_MODEL_NAME

# Runtime state
tokenizer  = None
scheduler  = None
tokenizer_max_length = 77

text_encoder = None
unet         = None
vae_decoder  = None
share_memory = None

# Generation parameters
user_prompt        = ""
uncond_prompt      = ""
user_seed          = np.int64(0)
user_step          = 20
user_text_guidance = 7.5

model_inited = False

####################################################################
# Model classes (v2.1-specific: TextEncoder outputs 1024-dim embeddings)

class TextEncoder(TextEncoderBase):
    _embed_dim = 1024  # SD v2.1 text embedding dimension

class Unet(UnetBase):
    pass

class VaeDecoder(VaeDecoderBase):
    pass

####################################################################

def model_initialize():
    global model_inited
    global scheduler, tokenizer, text_encoder, unet, vae_decoder, share_memory

    if model_inited:
        return True

    set_qnn_config()
    model_download()

    tokenizer = load_tokenizer(
        TOKENIZER_MODEL_NAME, tokenizer_dir, TOKENIZER_HELP_URL,
        use_subfolder=True   # SD v2.1 tokenizer is stored in a subfolder
    )

    text_encoder = TextEncoder("text_encoder", str(text_encoder_model_path))
    unet         = Unet("model_unet",           str(unet_model_path))
    vae_decoder  = VaeDecoder("vae_decoder",    str(vae_decoder_model_path))

    share_memory = QNNShareMemory("share_memory", 1024 * 1024 * 50)  # 50 MB

    scheduler = make_scheduler()

    model_inited = True
    return True


def setup_parameters(prompt, un_prompt, seed, step, text_guidance):
    global user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance

    user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance = \
        validate_and_prepare_parameters(prompt, un_prompt, seed, step, text_guidance)


def model_execute(callback, image_path, show_image=True, save_image=True):
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    scheduler.set_timesteps(user_step)

    # Tokenize prompts
    cond_tokens   = run_tokenizer(tokenizer, user_prompt,   tokenizer_max_length)
    uncond_tokens = run_tokenizer(tokenizer, uncond_prompt, tokenizer_max_length)

    # Text encoding (copy to avoid in-place modification by QNN)
    uncond_text_embedding = text_encoder.Inference(uncond_tokens).copy()
    user_text_embedding   = text_encoder.Inference(cond_tokens).copy()

    # Initial latent
    latent_in = generate_initial_latent(seed=user_seed)

    # Denoising loop
    for step in range(user_step):
        print(f'Step {step} Running...')
        time_step = get_timestep(scheduler, step)

        unconditional_noise_pred = unet.Inference(latent_in, time_step, uncond_text_embedding).copy()
        conditional_noise_pred   = unet.Inference(latent_in, time_step, user_text_embedding).copy()

        latent_in = run_scheduler(scheduler, unconditional_noise_pred, conditional_noise_pred,
                                  latent_in, time_step, user_text_guidance)
        callback(step)

    # VAE decode
    output_raw = vae_decoder.Inference(latent_in)

    if len(output_raw) == 0:
        callback(None)
    else:
        output_image = decode_vae_output(output_raw)

        if save_image:
            saved_path = save_output_image(output_image, image_path, user_seed)
            callback(str(saved_path))

        if show_image:
            output_image.show()

    PerfProfile.RelPerfProfileGlobal()

    if save_image:
        return saved_path
    else:
        return output_image


def model_destroy():
    global text_encoder, unet, vae_decoder, share_memory
    del text_encoder
    del unet
    del vae_decoder
    del share_memory


def model_download():
    download_sd_models(
        PLATFORM, SOC_ID, HUB_ID_H,
        MODEL_NAME, MODEL_HELP_URL,
        MODEL_NAME_VAE,  vae_decoder_model_path,
        MODEL_NAME_UNET, unet_model_path,
        MODEL_NAME_TEXT, text_encoder_model_path,
    )

####################################################################

def modelExecuteCallback(result):
    if result is None or isinstance(result, str):
        if result is None:
            print("Image generation failed.")
        else:
            print("Image saved to '{}'".format(result))
    else:
        pct = int((result + 1) * 100 / user_step)
        # print(f"modelExecuteCallback result: {pct}%")

####################################################################

if __name__ == "__main__":
    DEFAULT_PROMPT = "spectacular view of northern lights from Alaska"

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, type=str)
    args = parser.parse_args()

    model_initialize()

    time_start = time.time()

    setup_parameters(
        args.prompt,
        "lowres, text, error, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark",
        np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64),
        20,
        7.5,
    )
    model_execute(modelExecuteCallback, execution_ws / "images", True, True)

    time_end = time.time()
    print("time consumes for inference {}(s)".format(str(time_end - time_start)))

    model_destroy()
