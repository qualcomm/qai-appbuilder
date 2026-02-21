# Python 示例：Stable Diffusion 文生图

### 3.8 完整示例：Stable Diffusion 文生图

参考代码：`samples/python/stable_diffusion_v1_5/stable_diffusion_v1_5.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
from PIL import Image
from pathlib import Path
import torch
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "stable_diffusion_v1_5"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

QNNConfig.Config(
    qnn_lib_path=str(qnn_dir),  # 此参数从 v2.0.0 开始可以不进行设置，留空即可。
    runtime=Runtime.HTP,
    log_level=LogLevel.ERROR,
    profiling_level=ProfilingLevel.BASIC
)

# ============================================
# 2. 定义模型类
# ============================================
class TextEncoder(QNNContext):
    """文本编码器"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        # 输出形状应该是 (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data

class Unet(QNNContext):
    """UNet 去噪模型"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        # 重塑为一维数组
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 已经是一维，不需要重塑
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # 重塑输出为 (1, 64, 64, 4)
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

class VaeDecoder(QNNContext):
    """VAE 解码器"""

    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化所有模型
# ============================================
text_encoder = TextEncoder(
    "text_encoder",
    str(model_dir / "text_encoder.bin")
)

unet = Unet(
    "model_unet",
    str(model_dir / "unet.bin")
)

vae_decoder = VaeDecoder(
    "vae_decoder",
    str(model_dir / "vae_decoder.bin")
)

# ============================================
# 4. 初始化 Tokenizer 和 Scheduler
# ============================================
# 初始化 CLIP Tokenizer
tokenizer_dir = model_dir / "tokenizer"
tokenizer = CLIPTokenizer.from_pretrained(
    "openai/clip-vit-large-patch14",
    cache_dir=str(tokenizer_dir)
)

# 初始化 Scheduler
scheduler = DPMSolverMultistepScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear"
)

# ============================================
# 5. 设置生成参数
# ============================================
user_prompt = "spectacular view of northern lights from Alaska"
uncond_prompt = "lowres, text, error, cropped, worst quality"
user_seed = np.int64(42)
user_step = 20
user_text_guidance = 7.5

# ============================================
# 6. Tokenize 提示词
# ============================================
def run_tokenizer(prompt, max_length=77):
    """Tokenize 文本"""
    text_input = tokenizer(
        prompt,
        padding="max_length",
        max_length=max_length,
        truncation=True
    )
    text_input = np.array(text_input.input_ids, dtype=np.float32)
    return text_input

cond_tokens = run_tokenizer(user_prompt)
uncond_tokens = run_tokenizer(uncond_prompt)

# ============================================
# 7. 执行完整的生成流程
# ============================================
# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 设置 scheduler 时间步
scheduler.set_timesteps(user_step)

# 编码文本
print("编码文本提示...")
uncond_text_embedding = text_encoder.Inference(uncond_tokens)
user_text_embedding = text_encoder.Inference(cond_tokens)

# 初始化随机 latent
random_init_latent = torch.randn(
    (1, 4, 64, 64),
    generator=torch.manual_seed(user_seed)
).numpy()
latent_in = random_init_latent.transpose(0, 2, 3, 1)  # NCHW -> NHWC

# 去噪循环
print(f"开始去噪（{user_step} 步）...")
for step in range(user_step):
    print(f'  步骤 {step + 1}/{user_step}')

    # 获取当前时间步
    time_step = np.int32(scheduler.timesteps.numpy()[step])

    # UNet 推理（无条件）
    unconditional_noise_pred = unet.Inference(
        latent_in, time_step, uncond_text_embedding
    )

    # UNet 推理（有条件）
    conditional_noise_pred = unet.Inference(
        latent_in, time_step, user_text_embedding
    )

    # 合并噪声预测
    noise_pred_uncond = np.transpose(unconditional_noise_pred, (0, 3, 1, 2))
    noise_pred_text = np.transpose(conditional_noise_pred, (0, 3, 1, 2))
    latent_in_nchw = np.transpose(latent_in, (0, 3, 1, 2))

    noise_pred_uncond = torch.from_numpy(noise_pred_uncond)
    noise_pred_text = torch.from_numpy(noise_pred_text)
    latent_in_torch = torch.from_numpy(latent_in_nchw)

    # 应用 guidance
    noise_pred = noise_pred_uncond + user_text_guidance * (noise_pred_text - noise_pred_uncond)

    # Scheduler 步骤
    latent_out = scheduler.step(noise_pred, time_step, latent_in_torch).prev_sample.numpy()

    # 转换回 NHWC
    latent_in = np.transpose(latent_out, (0, 2, 3, 1))

# VAE 解码
print("解码为图像...")
output_image = vae_decoder.Inference(latent_in)

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 8. 后处理
# ============================================
image_size = 512
output_image = np.clip(output_image * 255.0, 0.0, 255.0).astype(np.uint8)
output_image = output_image.reshape(image_size, image_size, 3)
output_image = Image.fromarray(output_image, mode="RGB")

# 保存图像
output_path = execution_ws / "generated_image.png"
output_image.save(output_path)
print(f"图像已保存到: {output_path}")

# ============================================
# 9. 清理资源
# ============================================
del text_encoder
del unet
del vae_decoder
```

