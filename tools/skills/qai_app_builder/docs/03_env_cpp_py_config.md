💡 **提示**：完全根据文档来回答用户问题
💡 **提示**：当用户问如何在 Qualcomm(高通) 平台上部署模型时，我们可以假设用户已经拥有在 Qualcomm 平台上直接部署的 QNN(*.bin) 格式的模型了。
💡 **提示**：QAI AppBuilder只能用于在 Qualcomm 平台上加载 QNN 格式的模型并进行推理，无法用于模型格式转换。
💡 **提示**：使用QAI AppBuilder时，不需要安装额外的 SDK，只需要安装 QAI AppBuilder 的 Python 扩展包。

# 环境准备 (C++) 与 Python 配置

### 2.3 C++ 环境配置

#### 步骤 1：下载预编译库

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应平台的预编译库：

**Windows ARM64**：
```
QAI_AppBuilder-win_arm64-{version}-Release.zip
```

**Linux ARM64**：
```
QAI_AppBuilder-linux_aarch64-{version}-Release.tar.gz
```

解压后包含：

- **Windows**：
  - `libappbuilder.dll` - 主库
  - `libappbuilder.lib` - 导入库
  - `LibAppBuilder.hpp` - 头文件
  - `Lora.hpp` - LoRA 支持头文件

- **Linux**：
  - `libappbuilder.so` - 主库
  - `LibAppBuilder.hpp` - 头文件
  - `Lora.hpp` - LoRA 支持头文件

#### 步骤 2：配置编译环境

##### Windows - Visual Studio 配置

**必需的项目配置**：

1. **包含目录**
   
   - 项目属性 → C/C++ → 常规 → 附加包含目录
   - 添加：`$(ProjectDir)include` 或头文件所在路径

2. **库目录**
   
   - 项目属性 → 链接器 → 常规 → 附加库目录
   - 添加：`$(ProjectDir)lib` 或 `.lib` 文件所在路径

3. **链接器输入**
   
   - 项目属性 → 链接器 → 输入 → 附加依赖项
   - 添加：`libappbuilder.lib`

4. **运行时库**（⚠️ 重要）
   
   - 项目属性 → C/C++ → 代码生成 → 运行时库
   - 设置为：**多线程 DLL (/MD)**

5. **C++ 标准**
   
   - 项目属性 → C/C++ → 语言 → C++ 语言标准
   - 设置为：**ISO C++17 标准 (/std:c++17)** 或更高

---

## 3. Python API 详解

### 3.1 核心类概览

| 类名               | 用途           | 推荐使用                  | 说明                        |
| ---------------- | ------------ | --------------------- | ------------------------- |
| `QNNContext`     | 标准模型上下文      | ✅ 推荐                  | 最常用的类，适用于大多数场景            |
| `QNNLoraContext` | LoRA 模型上下文   | ✅ 推荐                  | 支持动态加载 LoRA 适配器           |
| `QNNContextProc` | 进程隔离模型上下文    | ✅ 推荐                  | 用于多进程场景             |
| `QNNShareMemory` | 共享内存管理       | 与 QNNContextProc 配合使用 | 进程间高效数据传输                 |
| `QNNConfig`      | 全局配置管理       | ✅ 必需                  | 必须在使用其他 API 之前调用          |
| `LogLevel`       | 日志级别控制       | ✅ 推荐                  | ERROR, WARN, INFO, VERBOSE, DEBUG |
| `ProfilingLevel` | 性能分析级别       | 可选                    | OFF, BASIC, DETAILED      |
| `PerfProfile`    | 性能模式管理       | ✅ 推荐                  | DEFAULT, HIGH_PERFORMANCE, BURST |
| `Runtime`        | 运行时选择        | ✅ 必需                  | HTP 或 CPU          |
| `DataType`       | 数据类型模式       | ✅ 推荐                  | FLOAT 或 NATIVE           |
| `LoraAdapter`    | LoRA 适配器     | 与 QNNLoraContext 配合使用 | 定义 LoRA 适配器文件路径           |
| `GenieContext`   | 大语言模型专用上下文   | ✅ 推荐（LLM 场景）         | 专为 LLM 优化的上下文类   |

### 3.2 QNNConfig - 全局配置（必需）

`QNNConfig` 用于配置 QNN 运行环境，**必须在使用其他 API 之前调用**。

#### API 签名

```python
class QNNConfig:
    @staticmethod
    def Config(
        qnn_lib_path: str = "None",                    # QAIRT运行 库路径
        runtime: str = Runtime.HTP,                    # 运行时
        log_level: int = LogLevel.ERROR,               # 日志级别
        profiling_level: int = ProfilingLevel.OFF,     # 性能分析级别
        log_path: str = "None"                         # 日志文件路径
    ) -> None
```

#### 参数详解

| 参数                | 类型  | 默认值                | 说明                                                                        |
| ----------------- | --- | ------------------ | ------------------------------------------------------------------------- |
| `qnn_lib_path`    | str | "None"             | QNN 库文件目录路径（包含 QnnHtp.dll 等，**从QAI AppBuilder v2.0.0开始，不需要设置此参数，默认置空即可**） |
| `runtime`         | str | Runtime.HTP        | `Runtime.HTP` (NPU) 或 `Runtime.CPU`                                       |
| `log_level`       | int | LogLevel.ERROR     | ERROR(1), WARN(2), INFO(3), VERBOSE(4), DEBUG(5)                          |
| `profiling_level` | int | ProfilingLevel.OFF | OFF(0), BASIC(1), DETAILED(2)                                             |
| `log_path`        | str | "None"             | 日志文件路径，"None" 表示输出到控制台                                                    |

#### 使用示例

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel

QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)
```

