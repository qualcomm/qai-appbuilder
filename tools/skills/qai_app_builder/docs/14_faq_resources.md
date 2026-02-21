# 常见问题与参考资源

## 7. 常见问题

### 7.1 模型加载失败

**问题**：`ModelInitialize` 返回 `False` 或失败

**解决方案**：

```python
from qai_appbuilder import QNNConfig, LogLevel, Runtime
from pathlib import Path

# 1. 启用详细日志
QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.DEBUG,  # 使用 DEBUG 级别
    log_path="./debug.log"     # 输出到文件
)

# 2. 检查文件是否存在
model_path = Path("models/my_model.bin")
if not model_path.exists():
    print(f"错误：模型文件不存在: {model_path}")
    exit()
```

### 7.2 推理结果不正确

**问题**：推理输出与预期不符

**解决方案**：

```python
# 1. 验证模型信息
print(f"输入形状: {model.getInputShapes()}")
print(f"输出形状: {model.getOutputShapes()}")
print(f"输入数据类型: {model.getInputDataType()}")
print(f"输出数据类型: {model.getOutputDataType()}")

# 2. 验证输入数据
expected_shape = model.getInputShapes()[0]
print(f"期望形状: {expected_shape}")
print(f"实际形状: {input_data.shape}")
print(f"实际数据类型: {input_data.dtype}")

# 3. 检查数据范围
print(f"输入数据范围: [{input_data.min()}, {input_data.max()}]")

# 4. 检查输出
output = model.Inference([input_data])
print(f"输出形状: {output[0].shape}")
print(f"输出数据类型: {output[0].dtype}")
print(f"输出数据范围: [{output[0].min()}, {output[0].max()}]")
```

### 7.3 内存泄漏

**Python 解决方案**：

```python
import gc

# 显式删除模型
model = QNNContext(...)
# ... 使用模型 ...
del model
gc.collect()  # 强制垃圾回收
```

**C++ 解决方案**：

```cpp
// 必须手动释放输出缓冲区
for (auto buffer : outputBuffers) {
    free(buffer);  // 释放每个输出缓冲区
}
outputBuffers.clear();
outputSize.clear();

// 销毁模型
appBuilder.ModelDestroy(model_name);
```

### 7.4 Native 模式数据类型不匹配

**问题**：Native 模式下数据类型错误导致推理失败

**解决方案**：

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. 使用 Native 模式初始化
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. 查询模型要求的数据类型
input_dtypes = model.getInputDataType()
input_shapes = model.getInputShapes()

print(f"模型要求的输入数据类型: {input_dtypes}")
print(f"模型要求的输入形状: {input_shapes}")

# 3. 创建数据类型映射
dtype_map = {
    'float16': np.float16,
    'fp16': np.float16,
    'float32': np.float32,
    'fp32': np.float32,
    'float': np.float32,
    'int8': np.int8,
    'uint8': np.uint8,
    'int16': np.int16,
    'uint16': np.uint16,
    'int32': np.int32,
    'uint32': np.uint32,
    'bool': np.bool_
}

# 4. 根据模型要求准备数据
input_dtype_str = input_dtypes[0].lower()
input_dtype = dtype_map.get(input_dtype_str, np.float32)

print(f"使用数据类型: {input_dtype}")

# 5. 创建正确类型的输入数据
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

print(f"输入数据类型: {input_data.dtype}")
print(f"输入数据形状: {input_data.shape}")

# 6. 执行推理
output = model.Inference([input_data])

print(f"输出数据类型: {output[0].dtype}")
print(f"输出数据形状: {output[0].shape}")
```

### 7.5 C++ 链接错误

**问题**：LNK2038、LNK2001 或其他链接错误

**解决方案**：

确保 Visual Studio 项目配置正确：

1. **运行时库**（最常见问题）
   
   - 项目属性 → C/C++ → 代码生成 → 运行时库
   - 必须设置为：**多线程 DLL (/MD)**

2. **平台**
   
   - 项目属性 → 常规 → 平台
   - 设置为：**ARM64**（对于 WoS 平台）

3. **配置**
   
   - 使用 **Release** 配置（而非 Debug）

4. **C++ 标准**
   
   - 项目属性 → C/C++ → 语言 → C++ 语言标准
   - 设置为：**ISO C++17** 或更高

5. **字符集**
   
   - 项目属性 → 高级 → 字符集
   - 设置为：**使用 Unicode 字符集**

### 7.6 性能不佳

**问题**：推理速度慢于预期

**诊断和解决**：

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel, DataType, PerfProfile
import time

# 1. 确保使用 HTP（NPU）而非 CPU
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,  # 确保是 HTP
    log_level=LogLevel.INFO,
    profiling_level=ProfilingLevel.BASIC  # 启用性能分析
)

# 2. 使用 Native 模式
model = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 3. 使用 Burst 模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 4. 测试性能
start_time = time.time()
for _ in range(100):
    output = model.Inference([input_data])
end_time = time.time()

avg_time = (end_time - start_time) / 100
print(f"平均推理时间: {avg_time * 1000:.2f} ms")

PerfProfile.RelPerfProfileGlobal()
```

---

## 8. 参考资源

### 8.1 官方文档和资源

- **GitHub 仓库**：https://github.com/quic/ai-engine-direct-helper
- **Qualcomm AI Hub**：https://aihub.qualcomm.com/
- **AI Dev Home**：https://www.aidevhome.com/
- **Qualcomm® AI Runtime SDK**：https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

### 8.2 教程和博客

- [QAI_AppBuilder: 让本地 AI 部署触手可及！](https://docs.qualcomm.com/bundle/publicresource/80-94755-1_REV_AA_QAI_AppBuilder_-_WoS.pdf)
- [大语言模型系列(1): 3分钟上手，在骁龙AI PC上部署DeepSeek!](https://blog.csdn.net/csdnsqst0050/article/details/149425691)
- [大语言模型系列(2): 本地 OpenAI 兼容 API 服务的配置与部署](https://blog.csdn.net/csdnsqst0050/article/details/150208814)
- [高通平台大语言模型精选](https://www.aidevhome.com/?id=51)
- [QAI AppBuilder on Linux (QCS6490)](https://docs.radxa.com/en/dragon/q6a/app-dev/npu-dev/qai-appbuilder)

### 8.3 示例代码

- **Python 示例**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python
  
  - Real-ESRGAN（图像超分辨率）
  - YOLOv8（目标检测）
  - Whisper（语音识别）
  - Stable Diffusion（文生图）
  - BEiT（图像分类）
  - OpenPose（姿态估计）
  - Depth Anything（深度估计）
  - 等 20+ 个示例

- **C++ 示例**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/c++
  
  - Real-ESRGAN
  - BEiT（图像分类）

- **WebUI 应用**：https://github.com/quic/ai-engine-direct-helper/tree/main/samples/webui
  
  - ImageRepairApp（图像修复）
  - StableDiffusionApp（文生图）
  - GenieWebUI（LLM 对话）

### 8.4 模型资源

- **AI Hub 模型库**：https://aihub.qualcomm.com/compute/models
- **AI Dev Home 模型库**：https://www.aidevhome.com/data/models/
- **Qwen2 7B SSD**：https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd_250702.html
- **DeepSeek-R1-Distill-Qwen-7B**：https://aiot.aidlux.com/zh/models/detail/78

---

