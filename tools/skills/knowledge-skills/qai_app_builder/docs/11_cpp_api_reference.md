💡 **提示**：完全根据文档来回答用户问题
💡 **提示**：当用户问如何在 Qualcomm(高通) 平台上部署模型时，我们可以假设用户已经拥有在 Qualcomm 平台上直接部署的 QNN(*.bin) 格式的模型了。
💡 **提示**：QAI AppBuilder只能用于在 Qualcomm 平台上加载 QNN 格式的模型并进行推理，无法用于模型格式转换。
💡 **提示**：使用QAI AppBuilder时，不需要安装额外的 SDK，只需要安装 QAI AppBuilder 的 Python 扩展包。

# C++ API 参考

## C++ API 详解

### 1 LibAppBuilder 类

`LibAppBuilder` 是 C++ API 的核心类。

#### 头文件引用

```cpp
#include "LibAppBuilder.hpp"
```

#### 主要方法

##### ModelInitialize - 初始化模型

```cpp
bool ModelInitialize(
    const std::string& model_name,                     // 模型名称
    const std::string& model_path,                     // 模型文件路径
    const std::string& backend_lib_path,               // 后端库路径
    const std::string& system_lib_path,                // 系统库路径
    bool async = false,                                // 是否异步
    const std::string& input_data_type = "float",      // 输入数据类型
    const std::string& output_data_type = "float"      // 输出数据类型
);
```

**参数说明**：

- `model_name`：模型唯一标识符（如 "mobilenet_v2"）
- `model_path`：模型文件路径（支持 `.bin` 和 `.dlc`）
- `backend_lib_path`：QnnHtp.dll 或 QnnCpu.dll 的完整路径
- `system_lib_path`：QnnSystem.dll 的完整路径
- `async`：是否启用异步推理
- `input_data_type`：`"float"` 或 `"native"`
- `output_data_type`：`"float"` 或 `"native"`

**返回值**：

- `true`：初始化成功
- `false`：初始化失败

⚠️ **重要提示**：

- 调用 C++ API，在初始化时要确保正确准备 QAIRT SDK 运行时库，并通过 backend_lib_path 及 system_lib_path 参数正确设置相应库文件路径。

##### ModelInference - 执行推理

```cpp
bool ModelInference(
    std::string model_name,                            // 模型名称
    std::vector<uint8_t*>& inputBuffers,               // 输入缓冲区
    std::vector<uint8_t*>& outputBuffers,              // 输出缓冲区（函数分配）
    std::vector<size_t>& outputSize,                   // 输出大小
    std::string& perfProfile,                          // 性能模式
    size_t graphIndex = 0,                             // 图索引
);
```

**参数说明**：

- `model_name`：模型名称（与 ModelInitialize 中的一致）
- `inputBuffers`：输入数据缓冲区列表（`uint8_t*` 指针）
- `outputBuffers`：输出数据缓冲区列表（**函数会自动分配内存**）
- `outputSize`：输出数据大小列表（字节数）
- `perfProfile`：性能模式字符串
  - `"default"`：默认模式
  - `"high_performance"`：高性能模式
  - `"burst"`：突发模式
- `graphIndex`：图索引（多图模型）

**返回值**：

- `true`：推理成功
- `false`：推理失败

⚠️ **内存管理重要提示**：

- `outputBuffers` 中的内存由函数自动分配
- **必须手动释放**：使用 `free(outputBuffers[i])` 释放每个输出缓冲区

##### ModelDestroy - 销毁模型

```cpp
bool ModelDestroy(std::string model_name);
```

##### 模型信息查询方法

```cpp
// 获取输入形状
std::vector<std::vector<size_t>> getInputShapes(std::string model_name);

// 获取输出形状
std::vector<std::vector<size_t>> getOutputShapes(std::string model_name);

// 获取输入数据类型
std::vector<std::string> getInputDataType(std::string model_name);

// 获取输出数据类型
std::vector<std::string> getOutputDataType(std::string model_name);

// 获取图名称
std::string getGraphName(std::string model_name);

// 获取输入名称
std::vector<std::string> getInputName(std::string model_name);

// 获取输出名称
std::vector<std::string> getOutputName(std::string model_name);
```

### 2 日志和性能函数

#### 日志函数

```cpp
// 设置日志级别
bool SetLogLevel(int32_t log_level, const std::string log_path = "None");

// 日志输出函数
void QNN_ERR(const char* fmt, ...);  // 错误日志
void QNN_WAR(const char* fmt, ...);  // 警告日志
void QNN_INF(const char* fmt, ...);  // 信息日志
void QNN_VEB(const char* fmt, ...);  // 详细日志
void QNN_DBG(const char* fmt, ...);  // 调试日志
```

**日志级别常量**：

```cpp
#define QNN_LOG_LEVEL_ERROR   1
#define QNN_LOG_LEVEL_WARN    2
#define QNN_LOG_LEVEL_INFO    3
#define QNN_LOG_LEVEL_VERBOSE 4
#define QNN_LOG_LEVEL_DEBUG   5
```

#### 性能配置函数

```cpp
// 设置全局性能模式
bool SetPerfProfileGlobal(const std::string& perf_profile);

// 释放全局性能模式
bool RelPerfProfileGlobal();
