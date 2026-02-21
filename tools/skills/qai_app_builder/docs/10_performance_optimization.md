# 性能优化指南

### PerfProfile - 性能模式管理

`PerfProfile` 用于控制 HTP (NPU) 的性能模式。

#### 性能模式对比

| 模式                 | 说明    | 功耗  | 性能  | 适用场景       |
| ------------------ | ----- | --- | --- | ---------- |
| `DEFAULT`          | 默认模式  | 低   | 中   | 不改变性能配置    |
| `HIGH_PERFORMANCE` | 高性能模式 | 中   | 高   | 持续高负载推理    |
| `BURST`            | 突发模式  | 高   | 最高  | 短时间内需要最高性能 |

#### 使用方法

```python
from qai_appbuilder import PerfProfile, QNNContext

model = QNNContext(...)

# ============================================
# 方法 1：全局设置（推荐用于批量推理）
# ============================================
# 设置为突发模式
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# 执行多次推理（都使用突发模式）
for i in range(100):
    # 注意：perf_profile 参数设置为 DEFAULT 以使用全局配置
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

# 释放性能模式
PerfProfile.RelPerfProfileGlobal()

# ============================================
# 方法 2：单次推理设置
# ============================================
# 每次推理时指定性能模式
output = model.Inference([input_data], perf_profile=PerfProfile.BURST)
```

⚠️ **重要提示**：

- 使用 `SetPerfProfileGlobal()` 后，`Inference()` 的 `perf_profile` 参数应设置为 `PerfProfile.DEFAULT`
- 如果在 `Inference()` 中指定其他性能模式，会覆盖全局设置

### Native 模式详解（高性能）

Native 模式直接使用模型的原生数据类型，避免数据转换，**显著提升性能**。

#### 支持的数据类型

- `int8` / `uint8`
- `int16` / `uint16`
- `int32` / `uint32`
- `float16` (fp16) - 最常用
- `float32` (fp32)
- `bool`

#### Native 模式使用步骤

```python
from qai_appbuilder import QNNContext, DataType
import numpy as np

# 1. 创建模型时指定 native 模式
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)

# 2. 查询模型所需的数据类型
input_dtypes = model.getInputDataType()
output_dtypes = model.getOutputDataType()
print(f"Input data types: {input_dtypes}")   # 例如：['float16']
print(f"Output data types: {output_dtypes}") # 例如：['float16']

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

input_shapes = model.getInputShapes()
input_data = np.random.randn(*input_shapes[0]).astype(input_dtype)

# 5. 执行推理
outputs = model.Inference([input_data])

# 6. 输出也是原生类型
print(f"Output dtype: {outputs[0].dtype}")  # 例如：float16
```

---


## 性能优化

### 使用 Native 模式（推荐）

**性能提升**：Native 模式可以减少 10%-200% 的数据转换开销。

```python
# ❌ Float 模式（默认）- 有转换开销
model_float = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.FLOAT,
    output_data_type=DataType.FLOAT
)

# ✅ Native 模式 - 无转换开销，性能更好
model_native = QNNContext(
    model_name="model",
    model_path="model.bin",
    input_data_type=DataType.NATIVE,
    output_data_type=DataType.NATIVE
)
```

### 使用 Burst 性能模式

在需要最高性能的场景下使用 Burst 模式。

```python
from qai_appbuilder import PerfProfile

# ✅ 全局设置（用于批量推理）
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

for i in range(100):
    output = model.Inference([input_data], perf_profile=PerfProfile.DEFAULT)

PerfProfile.RelPerfProfileGlobal()
```

### 批量推理优化

```python
# ❌ 不推荐：每次都初始化模型
for data in dataset:
    model = QNNContext(...)
    output = model.Inference([data])
    del model

# ✅ 推荐：只初始化一次
model = QNNContext(...)

PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
for data in dataset:
    output = model.Inference([data])
PerfProfile.RelPerfProfileGlobal()

del model
```

### 使用 ARM64 Python（Windows）

在 Windows on Snapdragon 平台上，ARM64 Python 比 x64 Python 性能更好。

```bash
# 下载 ARM64 Python
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe

# 安装 ARM64 版本的 QAI AppBuilder
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```
