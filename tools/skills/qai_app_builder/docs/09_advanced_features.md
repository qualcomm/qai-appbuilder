# 高级功能 (多图, 模型格式)

## 高级功能

### 1 多图模型支持

某些模型包含多个计算图，可以通过 `graphIndex` 参数选择。

```python
from qai_appbuilder import QNNContext

model = QNNContext(
    model_name="multi_graph_model",
    model_path="models/multi_graph_model.bin"
)

# 使用第一个图
output1 = model.Inference([input_data], graphIndex=0)

# 使用第二个图
output2 = model.Inference([input_data], graphIndex=1)

del model
```

### 2 支持的模型格式

QAI AppBuilder 支持多种模型格式，适用于不同的运行时和场景。

#### 2.1 支持的模型格式列表

| 格式     | 运行时     | 说明                      |
| ------ | ------- | ----------------------- |
| `.bin` | HTP/CPU | 预编译的二进制格式，加载速度最快（推荐）    |
| `.dlc` | HTP/CPU | 从 QAIRT 2.41.0 开始支持直接加载 |
| `.so`  | CPU     | 共享库格式，仅在 CPU 上运行        |

#### 2.2 DLC 模型直接加载

从 QAIRT 2.41.0 版本开始，支持直接加载 `.dlc` 模型文件。

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# ============================================
# 直接加载 .dlc 文件
# ============================================
model = QNNContext(
    model_name="my_model",
    model_path="models/my_model.dlc"  # 使用 .dlc 文件
)

# 首次运行时，会自动生成 my_model.dlc.bin 文件
# 后续运行会直接加载 .dlc.bin 以提高速度

output = model.Inference([input_data])

del model
```

**注意事项**：

- 首次加载 `.dlc` 文件时会自动转换为 `.dlc.bin`
- 转换后的 `.dlc.bin` 文件会保存在同一目录
- 后续运行会直接加载 `.dlc.bin` 文件，速度更快
- 如果需要重新转换，删除 `.dlc.bin` 文件即可

#### 2.3 SO 模型格式（CPU 运行时）

对于需要在 CPU 上运行的模型，可以使用 `.so` 格式：

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel

# 配置为 CPU 运行时
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.CPU,  # 使用 CPU 运行时
    log_level=LogLevel.INFO
)

# 加载 .so 模型文件
model = QNNContext(
    model_name="cpu_model",
    model_path="models/my_model.so"  # 使用 .so 文件
)

output = model.Inference([input_data])

del model
```
