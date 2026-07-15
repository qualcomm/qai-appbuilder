# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))
import install
import argparse

import csv
from pathlib import Path
from typing import Callable
import numpy as np
import soxr
import soundfile as sf
import torch

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)


# ---------------------------------------------------------------------
# torchaudio replacement helpers
#
# torchaudio has no prebuilt wheel for Windows on ARM64, so instead of
# depending on `torchaudio.transforms` we reimplement the two operations that
# YAMNet preprocessing needs, using only `torch` (+ `soxr` for resampling):
#   1. Spectrogram + MelScale  (== torchaudio.transforms.MelSpectrogram)
#   2. Resample                (== torchaudio.transforms.Resample)
# The math mirrors torchaudio's defaults (HTK mel scale, no mel normalization,
# centered reflect-padded STFT, Hann window) so the numerical results match.
# ---------------------------------------------------------------------


def _hz_to_mel_htk(freq: float) -> float:
    """Convert Hz to mel using the HTK formula (torchaudio default)."""
    return 2595.0 * np.log10(1.0 + freq / 700.0)


def _mel_to_hz_htk(mels: np.ndarray) -> np.ndarray:
    """Convert mel to Hz using the HTK formula (torchaudio default)."""
    return 700.0 * (10.0 ** (mels / 2595.0) - 1.0)


def _create_mel_filterbank(
    n_freqs: int,
    f_min: float,
    f_max: float,
    n_mels: int,
    sample_rate: int,
) -> torch.Tensor:
    """Build a mel filterbank matching torchaudio.functional.melscale_fbanks
    with mel_scale="htk" and norm=None.

    Returns
    -------
    torch.Tensor of shape (n_freqs, n_mels).
    """
    # frequency of each STFT bin
    all_freqs = np.linspace(0, sample_rate // 2, n_freqs)

    # equally spaced points on the mel scale
    m_min = _hz_to_mel_htk(f_min)
    m_max = _hz_to_mel_htk(f_max)
    m_pts = np.linspace(m_min, m_max, n_mels + 2)
    f_pts = _mel_to_hz_htk(m_pts)          # (n_mels + 2,)

    # slopes between each filter's frequency points
    f_diff = np.diff(f_pts)                 # (n_mels + 1,)
    slopes = f_pts[np.newaxis, :] - all_freqs[:, np.newaxis]  # (n_freqs, n_mels+2)

    down_slopes = -slopes[:, :-2] / f_diff[np.newaxis, :-1]
    up_slopes = slopes[:, 2:] / f_diff[np.newaxis, 1:]
    fb = np.maximum(0.0, np.minimum(down_slopes, up_slopes))  # (n_freqs, n_mels)

    return torch.from_numpy(fb.astype(np.float32))


def _resample(waveform: torch.Tensor, orig_sr: int, target_sr: int) -> torch.Tensor:
    """Resample a [channels, time] torch tensor using soxr.

    Replaces torchaudio.transforms.Resample. soxr resamples along axis=0, so
    the [channels, time] tensor is transposed before/after the call.
    """
    if orig_sr == target_sr:
        return waveform
    x = waveform.detach().cpu().numpy().astype(np.float32)
    y = soxr.resample(x.T, orig_sr, target_sr).T
    return torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32))

# Constants previously from qai_hub_models.models.yamnet.model
YAMNET_PROXY_REPOSITORY = "https://github.com/w-hc/torch_audioset.git"
YAMNET_PROXY_REPO_COMMIT = "e8852c5"
MODEL_ASSET_VERSION = 1

####################################################################

SAMPLE_RATE = 16000
CHUNK_LENGTH = 0.98

MODEL_ID = "mm65xwe5n"
MODEL_NAME = "yamnet"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/audio/Audio_Classification/yamnet/README.md"
YAMNET_CLASSES_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/yamnet_class_map.csv"
YAMNET_CLASSES_FILE = "yamnet_class_map.csv"

####################################################################

# Always resolve paths relative to this script's directory
execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

yamnet_classes_path = model_dir / YAMNET_CLASSES_FILE

input_wav_path = execution_ws / "input.wav"
INPUT_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/speech_whistling2.wav"
####################################################################

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv

print(f"SOC_ID: {SOC_ID}")

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
    ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail)

    if not ret:
        exit()


def Init():
    global yamnet

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

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
        # `audio` has shape [channels, samples] (channel-first), while soxr
        # resamples along axis=0 (the sample/time axis). Transpose to
        # [samples, channels] before resampling and transpose back after.
        audio = soxr.resample(
            audio.T, audio_sample_rate, model_sample_rate
        ).T
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

class VGGishLogMelSpectrogram(torch.nn.Module):
    '''
    This is a _log_ mel-spectrogram transform that adheres to the transform
    used by Google's vggish model input processing pipeline.

    Reimplemented with pure torch (no torchaudio dependency). It reproduces
    torchaudio.transforms.MelSpectrogram with the same defaults:
      * centered, reflect-padded Hann-window STFT
      * power spectrogram (power=2.0)
      * HTK mel scale, no mel normalization
    '''

    def __init__(self, sample_rate, n_fft, win_length, hop_length,
                 f_min, f_max, n_mels):
        super().__init__()
        self.n_fft = n_fft
        self.win_length = win_length
        self.hop_length = hop_length
        self.register_buffer("window", torch.hann_window(win_length))
        fb = _create_mel_filterbank(
            n_freqs=n_fft // 2 + 1,
            f_min=f_min,
            f_max=f_max,
            n_mels=n_mels,
            sample_rate=sample_rate,
        )
        self.register_buffer("mel_fb", fb)

    def forward(self, waveform):
        r"""
        Args:
            waveform (torch.Tensor): Tensor of audio of dimension (..., time)

        Returns:
            torch.Tensor: Mel frequency spectrogram of size (..., ``n_mels``, time)
        """
        # STFT -> power spectrogram (torchaudio default power=2.0)
        stft = torch.stft(
            waveform,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            win_length=self.win_length,
            window=self.window,
            center=True,
            pad_mode="reflect",
            normalized=False,
            return_complex=True,
        )
        specgram = stft.abs() ** 2   # (..., n_freqs, time)

        # NOTE at mel_features.py:98, googlers used np.abs on fft output and
        # as a result, the output is just the norm of spectrogram raised to power 1
        # For torchaudio.MelSpectrogram, however, the default
        # power for its spectrogram is 2.0. Hence we need to sqrt it.
        specgram = specgram ** 0.5

        # Apply mel filterbank: (n_freqs, n_mels)^T @ (..., n_freqs, time)
        # -> (..., n_mels, time)
        mel_specgram = torch.matmul(
            self.mel_fb.transpose(0, 1), specgram
        )
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
        x = _resample(x, sample_rate, CommonParams.TARGET_SAMPLE_RATE)
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
        x = _resample(x, sample_rate, CommonParams.TARGET_SAMPLE_RATE)
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
    
