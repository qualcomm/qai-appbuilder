# 快速开始与附录

## 9. 快速开始指南

### 9.1 第一个 Python 程序

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel
import numpy as np

# 1. 配置环境（必需）
QNNConfig.Config(
    qnn_lib_path="./qai_libs",
    runtime=Runtime.HTP,
    log_level=LogLevel.INFO
)

# 2. 加载模型
model = QNNContext(
    model_name="my_first_model",
    model_path="models/my_model.bin"
)

# 3. 准备输入
input_shape = model.getInputShapes()[0]
input_data = np.random.randn(*input_shape).astype(np.float32)

# 4. 执行推理
output = model.Inference([input_data])

# 5. 查看结果
print(f"输出形状: {output[0].shape}")
print(f"输出数据类型: {output[0].dtype}")

# 6. 清理
del model
```

### 9.2 第一个 C++ 程序

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>

int main() {
    // 1. 设置日志
    SetLogLevel(QNN_LOG_LEVEL_INFO);

    // 2. 创建 AppBuilder
    LibAppBuilder appBuilder;

    // 3. 初始化模型
    bool success = appBuilder.ModelInitialize(
        "my_first_model",
        "models/my_model.bin",
        "qai_libs/QnnHtp.dll",
        "qai_libs/QnnSystem.dll"
    );

    if (!success) {
        QNN_ERR("模型初始化失败");
        return -1;
    }

    // 4. 准备输入
    auto inputShapes = appBuilder.getInputShapes("my_first_model");
    size_t input_size = 1;
    for (auto dim : inputShapes[0]) {
        input_size *= dim;
    }

    float* input_data = new float[input_size];
    // ... 填充输入数据 ...

    std::vector<uint8_t*> inputBuffers;
    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_data));

    // 5. 执行推理
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    std::string perfProfile = "burst";

    success = appBuilder.ModelInference(
        "my_first_model",
        inputBuffers,
        outputBuffers,
        outputSize,
        perfProfile
    );

    if (!success) {
        QNN_ERR("推理失败");
        delete[] input_data;
        return -1;
    }

    // 6. 处理输出
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);
    // ... 处理输出 ...

    // 7. 清理
    delete[] input_data;
    for (auto buffer : outputBuffers) {
        free(buffer);
    }
    appBuilder.ModelDestroy("my_first_model");

    return 0;
}
```

---

## 10. 版本历史

### v2.0.0（2025年1月 - 重大更新）

**主要新特性**：

- ✅ **简化部署**：Python 扩展包含所有必需的依赖库（包括 QAIRT SDK 运行时）
- ✅ **多模态支持**：对多模态模型 (Qwen2.5-3B-VL / Qwen2.5-3B-omini) 的支持。
- ✅ **DLC 支持**：支持直接加载 `.dlc` 模型文件（QAIRT 2.41.0+）
- ✅ **LLM 优化**：新增 `GenieContext` 类，专为大语言模型优化
- ✅ **性能提升**：改进 Native 模式，减少数据转换开销
- ✅ **增强分析**：改进性能分析功能，提供更详细的性能数据

**API 变更**：

- `QNNConfig.Config()` 的 `qnn_lib_path` 参数现在可选（默认使用内置库）
- `QNNContext` 的 `backend_lib_path` 和 `system_lib_path` 参数现在可选
- 新增 `GenieContext` 类用于 LLM 推理

**已知问题**：

- 某些 ARM64 Python 扩展可能需要手动编译
- Linux 平台上某些模型可能需要设置 `ADSP_LIBRARY_PATH`

### v1.x（历史版本）

**v1.5.0**（2024年12月）：
- 添加 LoRA 适配器支持
- 改进多图模型支持
- 优化内存管理

**v1.0.0**（2024年10月）：
- 首次正式发布
- 支持 Python 和 C++ API
- 支持 Windows 和 Linux 平台
- 支持 HTP 和 CPU 运行时

---

## 11. 许可证

QAI AppBuilder 采用 **BSD 3-Clause "New" or "Revised" License**。

详见：https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE

---

## 12. 免责声明

本软件按"原样"提供，不提供任何明示或暗示的保证。作者和贡献者不对因使用本软件而产生的任何损害承担责任。代码可能不完整或测试不充分。用户需自行评估其适用性并承担所有相关风险。

**注意**：欢迎贡献。在关键系统中部署前请确保充分测试。

---

## 13. 贡献和支持

### 报告问题

如果遇到问题，请访问：

- **GitHub Issues**：https://github.com/quic/ai-engine-direct-helper/issues
- **GitHub Discussions**：https://github.com/quic/ai-engine-direct-helper/discussions

### 贡献代码

欢迎提交 Pull Request！请参阅：

- **贡献指南**：https://github.com/quic/ai-engine-direct-helper/blob/main/CONTRIBUTING.md
- **行为准则**：https://github.com/quic/ai-engine-direct-helper/blob/main/CODE-OF-CONDUCT.md

---

<div align="center">
  <p>⭐ 如果这个项目对你有帮助，请给我们一个 Star！</p>
  <p>📧 有问题或建议？访问 <a href="https://github.com/quic/ai-engine-direct-helper">GitHub 仓库</a></p>
</div>

---

**文档版本**：2.1  
**最后更新**：2025年1月26日  
**适用于**：QAI AppBuilder v2.0.0+
