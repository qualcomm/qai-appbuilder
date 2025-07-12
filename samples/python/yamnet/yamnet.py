# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import argparse

import csv
from pathlib import Path
from typing import Callable
import numpy as np
import resampy
import soundfile as sf
import torch
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

from qai_hub_models.models.yamnet.model import (
    MODEL_ASSET_VERSION,
    YAMNET_PROXY_REPO_COMMIT,
    YAMNET_PROXY_REPOSITORY,
)
from qai_hub_models.utils.asset_loaders import  SourceAsRoot

####################################################################

SAMPLE_RATE = 16000
CHUNK_LENGTH = 0.98

MODEL_ID = "mm65xwe5n"
MODEL_NAME = "yamnet"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
YAMNET_CLASSES_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/yamnet_class_map.csv"
YAMNET_CLASSES_FILE = "yamnet_class_map.csv"

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
model_path = model_dir + "\\" + MODEL_NAME + ".bin"

yamnet_classes_path = model_dir + "\\" + YAMNET_CLASSES_FILE

input_wav_path = execution_ws + "\\input.wav"
INPUT_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/speech_whistling2.wav"
####################################################################

yamnet=None

# YAMNET class which inherited from the class QNNContext.
class YamNet(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data
    

def model_download():
    ret = True
    if not os.path.exists(yamnet_classes_path):
        ret = install.download_url(YAMNET_CLASSES_URL, yamnet_classes_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()


def Init():
    global yamnet

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for yamnet objects.
    yamnet = YamNet("yamnet", model_path)



def Inference(input_audio_path):
    # Load the audio.
    audio, audio_sample_rate = load_audiofile(input_audio_path)


    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    accu = []
    for segment in chunk_and_resample_audio(audio, audio_sample_rate):
        segment = torch.tensor(segment)
        patches, spectrogram = preprocessing_yamnet_from_source(segment)
        input_patches = patches.numpy()
        raw_pred = yamnet.Inference(input_patches)
        accu.append(raw_pred)
    accu = np.stack(accu)


    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show the Top 5 predictions for this audio
    result=post_process(accu)

    return result

def post_process(accuracy):
    # Average them along time to get an overall classifier output for the clip.
    mean_scores = np.mean(accuracy, axis=0)
    mean_scores = mean_scores[0]
    top_N = 5
    # Report the highest-scoring classes.
    top_class_indices = np.argsort(mean_scores)[::-1][:top_N]
    # Label each audio one-by-one, for all the chunks,
    actions = parse_category_meta()
    top5_classes =  [actions[prediction] for prediction in top_class_indices]

    top5_classes_str= " | ".join(top5_classes)

    print(f"Top 5 predictions:\n{top5_classes_str}\n")
    return top5_classes_str


def Release():
    global yamnet

    # Release the resources.
    del(yamnet)



        
def preprocessing_yamnet_from_source(waveform_for_torch: torch.Tensor):
    """
    Args:
        waveform (torch.Tensor): Tensor of audio of dimension (..., time)

    Returns:
        patches : batched torch tsr of shape [N, C, T]
        spectrogram :  Mel frequency spectrogram of size (..., ``n_mels``, time)
    """

    with SourceAsRoot(
        YAMNET_PROXY_REPOSITORY,
        YAMNET_PROXY_REPO_COMMIT,
        "yamnet",
        MODEL_ASSET_VERSION,
    ):
        from torch_audioset.data.torch_input_processing import (
            WaveformToInput as TorchTransform,
        )

        #  This is a _log_ mel-spectrogram transform that adheres to the transform
        #  used by Google's vggish model input processing pipeline
        patches, spectrogram = TorchTransform().wavform_to_log_mel(
            waveform_for_torch, SAMPLE_RATE
        )

    return patches, spectrogram


def parse_category_meta():

    """Read the class name definition file and return a list of strings."""
    accu = []
    with open(yamnet_classes_path) as csv_file:
        reader = csv.reader(csv_file)
        next(reader)  # Skip header
        for (inx, category_id, category_name) in reader:
            accu.append(category_name)
    return accu


def chunk_and_resample_audio(
    audio: np.ndarray,
    audio_sample_rate: int,
    model_sample_rate=SAMPLE_RATE,
    model_chunk_seconds=CHUNK_LENGTH,
) -> list[np.ndarray]:
    """
    Parameters
    ----------
    audio: str
        Raw audio numpy array of shape [# of samples]

    audio_sample_rate: int
        Sample rate of audio array, in samples / sec.

    model_sample_rate: int
        Sample rate (samples / sec) required to run Yamnet. The audio file
        will be resampled to use this rate.

    model_chunk_seconds: int
        Split the audio in to N sequences of this many seconds.
        The final split may be shorter than this many seconds.

    Returns
    -------
    List of audio arrays, chunked into N arrays of model_chunk_seconds seconds.
    """
    if audio_sample_rate != model_sample_rate:
        audio = resampy.resample(audio, audio_sample_rate, model_sample_rate)
        audio_sample_rate = model_sample_rate
    number_of_full_length_audio_chunks = int(
        audio.shape[1] // audio_sample_rate // model_chunk_seconds
    )
    last_sample_in_full_length_audio_chunks = int(
        audio_sample_rate * number_of_full_length_audio_chunks * model_chunk_seconds
    )
    if number_of_full_length_audio_chunks == 0:
        return [audio]

    return [
        *np.array_split(
            audio[:, :last_sample_in_full_length_audio_chunks],
            number_of_full_length_audio_chunks,
            axis=1,
        ),
    ]


def load_audiofile(path: str | Path):
    """
    Decode the WAV file.
        Parameters:
            path: Path of the input audio.

        Returns:
            x: Reads audio sample from path and converts to torch tensor.
            sr : sampling rate of audio samples

    """
    x, sr = sf.read(path, dtype="int16", always_2d=True)
    x = x / 2**15
    x = x.T.astype(np.float32)
    # Convert to mono and the sample rate expected by YAMNet.
    if x.shape[0] > 1:
        x = np.mean(x, axis=1)
    return x, sr


def main(input = None):

    if input is None:
        if not os.path.exists(input_wav_path):
            ret = True
            ret = install.download_url(INPUT_WAV_PATH_URL, input_wav_path)
        input = input_wav_path

    Init()

    result = Inference(input)

    Release()

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)