# Python 示例：语音识别 (Whisper)

### 完整示例：语音识别（Whisper）- Native 模式

参考代码：`samples/python/whisper_base_en/whisper_base_en.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile, DataType
)
import numpy as np
import torch
import audio2numpy as a2n
import samplerate
import whisper
from pathlib import Path
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "whisper_base_en"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义 Encoder 和 Decoder 类（使用 Native 模式）
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - 使用 Native 模式"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # 重塑输出
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

class Decoder(QNNContext):
    """Whisper Decoder - 使用 Native 模式"""

    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        input_datas = [x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]
        output_data = super().Inference(input_datas)

        # 重塑输出
        logits = output_data[0].reshape(1, 1, 51864)
        k_cache = output_data[1].reshape(6, 8, 64, 224)
        v_cache = output_data[2].reshape(6, 8, 224, 64)

        return logits, k_cache, v_cache

# ============================================
# 3. 初始化模型（Native 模式）
# ============================================
encoder_model_path = model_dir / "whisper_base_en-whisperencoder.bin"
decoder_model_path = model_dir / "whisper_base_en-whisperdecoder.bin"

# 使用 Native 模式初始化
encoder = Encoder(
    "whisper_encoder",
    str(encoder_model_path),
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

decoder = Decoder(
    "whisper_decoder",
    str(decoder_model_path),
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 查看模型的原生数据类型
print("\nEncoder 模型信息:")
print(f"  输入数据类型: {encoder.getInputDataType()}")
print(f"  输出数据类型: {encoder.getOutputDataType()}")

print("\nDecoder 模型信息:")
print(f"  输入数据类型: {decoder.getInputDataType()}")
print(f"  输出数据类型: {decoder.getOutputDataType()}")

# ============================================
# 4. Whisper 常量定义
# ============================================
TOKEN_SOT = 50257  # Start of transcript
TOKEN_EOT = 50256  # End of transcript
SAMPLE_RATE = 16000
CHUNK_LENGTH = 30  # seconds
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE

# ============================================
# 5. 音频预处理函数
# ============================================
def log_mel_spectrogram(audio_np: np.ndarray) -> np.ndarray:
    """计算 Mel 频谱图（返回 float16）"""
    audio = torch.from_numpy(audio_np)

    # 填充音频到固定长度
    padding = MAX_AUDIO_SAMPLES - len(audio)
    if padding > 0:
        audio = torch.nn.functional.pad(audio, (0, padding))

    # 计算 STFT
    n_fft = 400
    hop_length = 160
    window = torch.hann_window(n_fft)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    # 应用 Mel 滤波器（需要预先加载）
    # mel_filter = np.load("mel_filters.npz")["mel_80"]
    # mel_spec = torch.from_numpy(mel_filter) @ magnitudes

    # 计算 log mel spectrogram
    log_spec = torch.clamp(magnitudes, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    # 返回 float16（Native 模式）
    return log_spec.unsqueeze(0).to(dtype=torch.float16).cpu().numpy()

# ============================================
# 6. 执行推理
# ============================================
audio_path = execution_ws / "jfk.wav"

# 读取音频文件
audio, audio_sample_rate = a2n.audio_from_file(str(audio_path))

# 重采样到 16kHz
if audio_sample_rate != SAMPLE_RATE:
    audio = samplerate.resample(audio, SAMPLE_RATE / audio_sample_rate)

# 计算 Mel 频谱图（返回 float16）
mel_input = log_mel_spectrogram(audio)
print(f"mel_input: dtype={mel_input.dtype}, shape={mel_input.shape}")

# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Encoder 推理
print("执行 Encoder 推理...")
k_cache_cross, v_cache_cross = encoder.Inference(mel_input)

print(f"k_cache_cross: shape={k_cache_cross.shape}, dtype={k_cache_cross.dtype}")
print(f"v_cache_cross: shape={v_cache_cross.shape}, dtype={v_cache_cross.dtype}")

# ============================================
# 7. Decoder 推理（自回归生成）
# ============================================
# 初始化 Decoder 输入
x = np.array([[TOKEN_SOT]], dtype=np.int32)
index = np.array([[0]], dtype=np.int32)
k_cache_self = np.zeros((6, 8, 64, 224), dtype=np.float16)
v_cache_self = np.zeros((6, 8, 224, 64), dtype=np.float16)

decoded_tokens = [TOKEN_SOT]
max_tokens = 100

print("\n执行 Decoder 推理（自回归生成）...")
for i in range(max_tokens):
    index = np.array([[i]], dtype=np.int32)

    # Decoder 推理
    logits, k_cache_self, v_cache_self = decoder.Inference(
        x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
    )

    # 获取下一个 token
    next_token = np.argmax(logits[0, -1])
    decoded_tokens.append(int(next_token))

    # 检查是否结束
    if next_token == TOKEN_EOT:
        break

    # 更新输入
    x = np.array([[next_token]], dtype=np.int32)

    if (i + 1) % 10 == 0:
        print(f"  已生成 {i + 1} 个 tokens...")

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

print(f"\n生成完成，共 {len(decoded_tokens)} 个 tokens")

# ============================================
# 8. 解码 tokens 为文本
# ============================================
tokenizer = whisper.decoding.get_tokenizer(
    multilingual=False, language="en", task="transcribe"
)
text = tokenizer.decode(decoded_tokens[1:])  # 移除 TOKEN_SOT
print(f"转录结果: {text.strip()}")

# ============================================
# 9. 清理资源
# ============================================
del encoder
del decoder
```
