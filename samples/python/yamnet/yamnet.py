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
import torchaudio.transforms as ta_trans

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

from qai_hub_models.models.yamnet.model import (
    MODEL_ASSET_VERSION,
    YAMNET_PROXY_REPO_COMMIT,
    YAMNET_PROXY_REPOSITORY,
)
from qai_hub_models.utils.asset_loaders import  SourceAsRoot
from pathlib import Path

####################################################################

SAMPLE_RATE = 16000
CHUNK_LENGTH = 0.98

MODEL_ID = "mm65xwe5n"
MODEL_NAME = "yamnet"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
YAMNET_CLASSES_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/yamnet_class_map.csv"
YAMNET_CLASSES_FILE = "yamnet_class_map.csv"

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

yamnet_classes_path = model_dir / YAMNET_CLASSES_FILE

input_wav_path = execution_ws / "input.wav"
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
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for yamnet objects.
    yamnet = YamNet("yamnet", str(model_path))



def Inference(input_audio_path):
    # Load the audio.
    audio, audio_sample_rate = load_audiofile(input_audio_path)

    for segment in chunk_and_resample_audio(audio, audio_sample_rate):
        segment = torch.tensor(segment)
        patches, spectrogram = preprocessing_yamnet_from_source(segment)
        input_patches = patches.numpy()

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    accu = []

    raw_pred = yamnet.Inference(input_patches)
    accu.append(raw_pred)
    accu = np.stack(accu)


    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show the Top 5 predictions for this audio
    result=post_process(accu)

    return result

def post_process(accuracy):
    print("accuracy shape:", np.array(accuracy).shape)
    mean_scores = np.mean(accuracy, axis=1)  # Average over the time dimension
    #print("mean_scores shape after mean:", mean_scores.shape)

    # Squeeze out the batch or redundant dimensions and reduce to a 1D vector [C]
    mean_scores = np.squeeze(mean_scores)    # e.g. [C]
    mean_scores = mean_scores.ravel()        
    #print("mean_scores shape after squeeze/ravel:", mean_scores.shape)

    top_N = 5
    top_class_indices = np.argsort(mean_scores)[::-1][:top_N]  # 1D index
    #print("top_class_indices:", top_class_indices, top_class_indices.shape)

    actions = parse_category_meta()  # list[str],length is C
    top5_classes = [actions[int(idx)] for idx in top_class_indices]
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


    #  This is a _log_ mel-spectrogram transform that adheres to the transform
    #  used by Google's vggish model input processing pipeline
    patches, spectrogram = WaveformToInput().wavform_to_log_mel(
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

class CommonParams():
    # for STFT
    TARGET_SAMPLE_RATE = 16000
    STFT_WINDOW_LENGTH_SECONDS = 0.025
    STFT_HOP_LENGTH_SECONDS = 0.010

    # for log mel spectrogram
    NUM_MEL_BANDS = 64
    MEL_MIN_HZ = 125
    MEL_MAX_HZ = 7500
    LOG_OFFSET = 0.001  # NOTE 0.01 for vggish, and 0.001 for yamnet

    # convert input audio to segments
    PATCH_WINDOW_IN_SECONDS = 0.96

    # largest feedforward chunk size at test time
    VGGISH_CHUNK_SIZE = 128
    YAMNET_CHUNK_SIZE = 256

    # num of data loading threads
    NUM_LOADERS = 4
    
    #YAMNetParams
    PATCH_HOP_SECONDS = 0.48
    PATCH_WINDOW_SECONDS = 0.96

class VGGishLogMelSpectrogram(ta_trans.MelSpectrogram):
    '''
    This is a _log_ mel-spectrogram transform that adheres to the transform
    used by Google's vggish model input processing pipeline
    '''

    def forward(self, waveform):
        r"""
        Args:
            waveform (torch.Tensor): Tensor of audio of dimension (..., time)

        Returns:
            torch.Tensor: Mel frequency spectrogram of size (..., ``n_mels``, time)
        """
        specgram = self.spectrogram(waveform)
        # NOTE at mel_features.py:98, googlers used np.abs on fft output and
        # as a result, the output is just the norm of spectrogram raised to power 1
        # For torchaudio.MelSpectrogram, however, the default
        # power for its spectrogram is 2.0. Hence we need to sqrt it.
        # I can change the power arg at the constructor level, but I don't
        # want to make the code too dirty
        specgram = specgram ** 0.5

        mel_specgram = self.mel_scale(specgram)
        mel_specgram = torch.log(mel_specgram + CommonParams.LOG_OFFSET)
        return mel_specgram

class WaveformToInput(torch.nn.Module):
    #def __init__(self):
        #super().__init__()
    global mel_trans_ope
    audio_sample_rate = CommonParams.TARGET_SAMPLE_RATE
    window_length_samples = int(round(
        audio_sample_rate * CommonParams.STFT_WINDOW_LENGTH_SECONDS
    ))
    hop_length_samples = int(round(
        audio_sample_rate * CommonParams.STFT_HOP_LENGTH_SECONDS
    ))
    fft_length = 2 ** int(np.ceil(np.log(window_length_samples) / np.log(2.0)))
    assert window_length_samples == 400
    assert hop_length_samples == 160
    assert fft_length == 512
    mel_trans_ope = VGGishLogMelSpectrogram(
        CommonParams.TARGET_SAMPLE_RATE, n_fft=fft_length,
        win_length=window_length_samples, hop_length=hop_length_samples,
        f_min=CommonParams.MEL_MIN_HZ,
        f_max=CommonParams.MEL_MAX_HZ,
        n_mels=CommonParams.NUM_MEL_BANDS
    )
    # note that the STFT filtering logic is exactly the same as that of a
    # conv kernel. It is the center of the kernel, not the left edge of the
    # kernel that is aligned at the start of the signal.

    def __call__(self, waveform, sample_rate):
        '''
        Args:
            waveform: torch tsr [num_audio_channels, num_time_steps]
            sample_rate: per second sample rate
        Returns:
            batched torch tsr of shape [N, C, T]
        '''
        x = waveform.mean(axis=0, keepdims=True)  # average over channels
        resampler = ta_trans.Resample(sample_rate, CommonParams.TARGET_SAMPLE_RATE)
        x = resampler(x)
        x = mel_trans_ope(x)
        x = x.squeeze(dim=0).T  # # [1, C, T] -> [T, C]

        window_size_in_frames = int(round(
            CommonParams.PATCH_WINDOW_IN_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
        ))
        num_chunks = x.shape[0] // window_size_in_frames

        # reshape into chunks of non-overlapping sliding window
        num_frames_to_use = num_chunks * window_size_in_frames
        x = x[:num_frames_to_use]
        # [num_chunks, 1, window_size, num_freq]
        x = x.reshape(num_chunks, 1, window_size_in_frames, x.shape[-1])
        return x

    def wavform_to_log_mel(self, waveform, sample_rate):
        '''
        Args:
            waveform: torch tsr [num_audio_channels, num_time_steps]
            sample_rate: per second sample rate
        Returns:
            batched torch tsr of shape [N, C, T]
        '''
        x = waveform.mean(axis=0, keepdims=True)  # average over channels
        resampler = ta_trans.Resample(sample_rate, CommonParams.TARGET_SAMPLE_RATE)
        x = resampler(x)
        x = mel_trans_ope(x)
        x = x.squeeze(dim=0).T  # # [1, C, T] -> [T, C]
        spectrogram = x.cpu().numpy().copy()

        window_size_in_frames = int(round(
            CommonParams.PATCH_WINDOW_IN_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
        ))

        if CommonParams.PATCH_HOP_SECONDS == CommonParams.PATCH_WINDOW_SECONDS:
            num_chunks = x.shape[0] // window_size_in_frames

            # reshape into chunks of non-overlapping sliding window
            num_frames_to_use = num_chunks * window_size_in_frames
            x = x[:num_frames_to_use]
            # [num_chunks, 1, window_size, num_freq]
            x = x.reshape(num_chunks, 1, window_size_in_frames, x.shape[-1])
        else:  # generate chunks with custom sliding window length `patch_hop_seconds`
            patch_hop_in_frames = int(round(
                CommonParams.PATCH_HOP_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
            ))
            # TODO performance optimization with zero copy
            patch_hop_num_chunks = (x.shape[0] - window_size_in_frames) // patch_hop_in_frames + 1
            num_frames_to_use = window_size_in_frames + (patch_hop_num_chunks - 1) * patch_hop_in_frames
            x = x[:num_frames_to_use]
            x_in_frames = x.reshape(-1, x.shape[-1])
            x_output = np.empty((patch_hop_num_chunks, window_size_in_frames, x.shape[-1]))
            for i in range(patch_hop_num_chunks):
                start_frame = i * patch_hop_in_frames
                x_output[i] = x_in_frames[start_frame: start_frame + window_size_in_frames]
            x = x_output.reshape(patch_hop_num_chunks, 1, window_size_in_frames, x.shape[-1])
            x = torch.tensor(x, dtype=torch.float32)
        return x, spectrogram


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
    
