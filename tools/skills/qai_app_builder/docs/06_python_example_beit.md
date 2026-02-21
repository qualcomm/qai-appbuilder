# Python 示例：图像分类 (BEiT)

### 3.6 完整示例：图像分类（BEiT）

参考代码：`samples/python/beit/beit.py`

```python
from qai_appbuilder import (
    QNNContext, QNNConfig, Runtime, LogLevel, 
    ProfilingLevel, PerfProfile
)
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from pathlib import Path
import os

# ============================================
# 1. 配置环境
# ============================================
execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"

MODEL_NAME = "beit"
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
# 2. 定义 BEiT 模型类
# ============================================
class Beit(QNNContext):
    """BEiT 图像分类模型"""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ============================================
# 3. 初始化模型
# ============================================
IMAGE_SIZE = 224
model_path = model_dir / f"{MODEL_NAME}.bin"

beit = Beit("beit", str(model_path))

# ============================================
# 4. 图像预处理
# ============================================
def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """预处理 PIL 图像"""
    preprocess = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ])

    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

# ============================================
# 5. 执行推理
# ============================================
input_image_path = execution_ws / "input.jpg"

# 读取和预处理图像
image = Image.open(input_image_path)
image = preprocess_PIL_image(image).numpy()
image = np.transpose(image, (0, 2, 3, 1))  # NCHW -> NHWC

# 设置突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行推理
output_data = beit.Inference(image)

# 重置性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 6. 后处理
# ============================================
# 转换为 torch tensor 并应用 softmax
output = torch.from_numpy(output_data).squeeze(0)
probabilities = torch.softmax(output, dim=0)

# 获取 Top-5 预测
top5_prob, top5_catid = torch.topk(probabilities, 5)

print("\nTop 5 预测结果:")
for i in range(5):
    print(f"{i+1}. 类别 {top5_catid[i]}: {top5_prob[i].item():.6f}")

# ============================================
# 7. 清理资源
# ============================================
del beit
```

