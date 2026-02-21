# Python 核心 API 详解

### 3.3 QNNContext - 标准模型上下文（核心类）

`QNNContext` 是最常用的类，用于加载模型、执行推理和管理模型生命周期。

#### 构造函数

```python
class QNNContext:
    def __init__(
        self,
        model_name: str = "None",                      # 模型名称（唯一标识）
        model_path: str = "None",                      # 模型文件路径
        backend_lib_path: str = "None",                # 后端库路径（可选）
        system_lib_path: str = "None",                 # 系统库路径（可选）
        is_async: bool = False,                        # 是否异步执行
        input_data_type: str = DataType.FLOAT,         # 输入数据类型
        output_data_type: str = DataType.FLOAT         # 输出数据类型
    ) -> None
```

#### 参数详解

| 参数                 | 类型   | 默认值            | 说明                                                                   |
| ------------------ | ---- | -------------- | -------------------------------------------------------------------- |
| `model_name`       | str  | "None"         | 模型唯一标识符，用于区分不同模型                                                     |
| `model_path`       | str  | "None"         | 模型文件路径（支持 `.bin` 和 `.dlc` 格式）                                        |
| `backend_lib_path` | str  | "None"         | QnnHtp.dll 或 QnnCpu.dll 路径（可选，**从QAI AppBuilder v2.0.0开始，不需要设置此参数**） |
| `system_lib_path`  | str  | "None"         | QnnSystem.dll 路径（可选，**从QAI AppBuilder v2.0.0开始，不需要设置此参数**）           |
| `is_async`         | bool | False          | 是否启用异步推理                                                             |
| `input_data_type`  | str  | DataType.FLOAT | `DataType.FLOAT` 或 `DataType.NATIVE`                                 |
| `output_data_type` | str  | DataType.FLOAT | `DataType.FLOAT` 或 `DataType.NATIVE`                                 |

💡 **提示**：从QAI AppBuilder **v2.0.0** 开始，不需要设置参数：`backend_lib_path` 和 `system_lib_path` 。

#### 核心方法

##### Inference - 执行推理

```python
def Inference(
    self,
    input: List[np.ndarray],                           # 输入数据列表
    perf_profile: str = PerfProfile.DEFAULT,           # 性能模式
    graphIndex: int = 0                                # 图索引
) -> List[np.ndarray]                                  # 返回输出列表
```

**参数说明**：

- `input`：输入数据列表，每个元素是一个 NumPy 数组
- `perf_profile`：性能模式(不推荐使用此参数。)
  - `PerfProfile.DEFAULT`：默认模式（不改变性能配置）
  - `PerfProfile.HIGH_PERFORMANCE`：高性能模式
  - `PerfProfile.BURST`：突发模式（最高性能）
- `graphIndex`：图索引（用于多图模型，默认为 0）

**返回值**：

- 输出数据列表，每个元素是一个 NumPy 数组

💡 **提示**：不推荐使用perf_profile参数，建议通过配对使用PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST) 、PerfProfile.RelPerfProfileGlobal()来实现设置 NPU 为高性能模式。

##### 模型信息查询方法

```python
# 获取输入形状（例如：[[1, 3, 224, 224]]）
def getInputShapes(self) -> List[List[int]]

# 获取输出形状（例如：[[1, 1000]]）
def getOutputShapes(self) -> List[List[int]]

# 获取输入数据类型（例如：['float32'] 或 ['float16']）
def getInputDataType(self) -> List[str]

# 获取输出数据类型（例如：['float32'] 或 ['float16']）
def getOutputDataType(self) -> List[str]

# 获取图名称
def getGraphName(self) -> str

# 获取输入张量名称（例如：['input']）
def getInputName(self) -> List[str]

# 获取输出张量名称（例如：['output']）
def getOutputName(self) -> List[str]
```

### 3.4 继承 QNNContext 的最佳实践

示例代码，**继承 `QNNContext` 类**来封装特定模型的逻辑。

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, PerfProfile
import numpy as np
import os

# 配置环境
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN
)

# ============================================
# 方式 1：简单继承（最常用）
# ============================================
class RealESRGan(QNNContext):
    """Real-ESRGAN 图像超分辨率模型"""

    def Inference(self, input_data):
        """重写 Inference 方法以简化调用"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# 使用自定义模型类
model_path = "models/real_esrgan_x4plus.bin"
realesrgan = RealESRGan("realesrgan", model_path)

# 执行推理
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
output = realesrgan.Inference(input_data)
PerfProfile.RelPerfProfileGlobal()

del realesrgan

# ============================================
# 方式 2：多输入模型
# ============================================
class Unet(QNNContext):
    """UNet 去噪模型 - 多输入"""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        """接受多个输入"""
        # 重塑为一维数组
        input_data_1 = input_data_1.reshape(input_data_1.size)
        # input_data_2 已经是一维，不需要重塑
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        # 重塑输出
        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data

# 使用
unet = Unet("unet", "models/unet.bin")
output = unet.Inference(latent, timestep, text_embedding)
del unet

# ============================================
# 方式 3：多输出模型
# ============================================
class Encoder(QNNContext):
    """Whisper Encoder - 多输出"""

    def Inference(self, input_data):
        """返回多个输出"""
        input_datas = [input_data]
        output_data = super().Inference(input_datas)

        # 重塑每个输出
        k_cache_cross = output_data[0].reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1].reshape(6, 8, 1500, 64)

        return k_cache_cross, v_cache_cross

# 使用
encoder = Encoder("whisper_encoder", "models/encoder.bin")
k_cache, v_cache = encoder.Inference(mel_input)
del encoder
```

