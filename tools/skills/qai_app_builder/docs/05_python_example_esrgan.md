# Python 示例：图像超分辨率 (Real-ESRGAN)

### 完整示例：图像超分辨率（Real-ESRGAN）

参考代码：`samples/python/real_esrgan_x4plus/real_esrgan_x4plus.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
from PIL import Image
from pathlib import Path
import os

# ============================================
# 1. 配置 QNN 环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

# 处理不同的工作目录情况
if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "real_esrgan_x4plus"
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
# 2. 定义模型类（继承自 QNNContext）
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN 图像超分辨率模型"""

    def Inference(self, input_data):
        """重写 Inference 方法以简化调用"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化模型
# ============================================
IMAGE_SIZE = 512
model_path = model_dir / f"{MODEL_NAME}.bin"

# 创建模型实例
realesrgan = RealESRGan("realesrgan", str(model_path))

# 查询模型信息（可选）
print(f"模型名称: {realesrgan.getGraphName()}")
print(f"输入形状: {realesrgan.getInputShapes()}")
print(f"输出形状: {realesrgan.getOutputShapes()}")
print(f"输入数据类型: {realesrgan.getInputDataType()}")
print(f"输出数据类型: {realesrgan.getOutputDataType()}")

# ============================================
# 4. 图像预处理辅助函数
# ============================================
def pil_resize_pad(image, target_size):
    """调整图像大小并填充到目标尺寸"""
    orig_width, orig_height = image.size
    target_width, target_height = target_size

    # 计算缩放比例
    scale = min(target_width / orig_width, target_height / orig_height)
    new_width = int(orig_width * scale)
    new_height = int(orig_height * scale)

    # 调整大小
    image = image.resize((new_width, new_height), Image.LANCZOS)

    # 创建新图像并填充
    new_image = Image.new('RGB', target_size, (0, 0, 0))
    paste_x = (target_width - new_width) // 2
    paste_y = (target_height - new_height) // 2
    new_image.paste(image, (paste_x, paste_y))

    padding = (paste_x, paste_y)
    return new_image, scale, padding

def pil_undo_resize_pad(image, original_size, scale, padding):
    """移除填充并恢复到原始尺寸"""
    # 裁剪填充
    width, height = image.size
    left = padding[0] * 4
    top = padding[1] * 4
    right = width - padding[0] * 4
    bottom = height - padding[1] * 4
    image = image.crop((left, top, right, bottom))

    # 调整到原始尺寸
    image = image.resize(original_size, Image.LANCZOS)
    return image

# ============================================
# 5. 执行推理
# ============================================
input_image_path = execution_ws / "input.jpg"
output_image_path = execution_ws / "output.png"

# 读取和预处理图像
orig_image = Image.open(input_image_path)
image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

image = np.array(image)
image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)  # 归一化

# 设置 HTP 为突发模式以获得最佳性能
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行推理
output_image = realesrgan.Inference(image)

# 重置 HTP 性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. 后处理
# ============================================
# 重塑输出形状
output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)

# 反归一化
output_image = np.clip(output_image, 0.0, 1.0)
output_image = (output_image * 255).astype(np.uint8)

# 转换为 PIL 图像
output_image = Image.fromarray(output_image)

# 移除填充并恢复原始尺寸
image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
image_padding = (padding[0] * 4, padding[1] * 4)
output_image = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

# 保存结果
output_image.save(output_image_path)
print(f"超分辨率图像已保存到: {output_image_path}")

# ============================================
# 7. 清理资源
# ============================================
del realesrgan
```
