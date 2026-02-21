# 附录

## 参考资源

### 官方文档和资源

- **GitHub 仓库**：https://github.com/quic/ai-engine-direct-helper
- **Qualcomm® AI Runtime SDK**：https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

### 教程和博客

- [QAI AppBuilder Guide](https://github.com/quic/ai-engine-direct-helper/blob/main/docs/guide_zh.md)
- [GenieAPIService (OpenAI Compatible API Service)](https://github.com/quic/ai-engine-direct-helper/blob/main/docs/genie_guide_zh.md)
- [大语言模型系列(1): 3分钟上手，在骁龙AI PC上部署DeepSeek!](https://blog.csdn.net/csdnsqst0050/article/details/149425691)
- [大语言模型系列(2): 本地 OpenAI 兼容 API 服务的配置与部署](https://blog.csdn.net/csdnsqst0050/article/details/150208814)
- [大语言模型系列(3): Qwen2.5-VL-3B 多模态模型端侧部署](https://blog.csdn.net/csdnsqst0050/article/details/157474571)
- [高通平台大语言模型精选](https://www.aidevhome.com/?id=51)
- [QAI AppBuilder on Linux (QCS6490)](https://docs.radxa.com/en/dragon/q6a/app-dev/npu-dev/qai-appbuilder)

### 示例代码

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

### 模型资源

- **AI Hub 模型库**：https://aihub.qualcomm.com/compute/models
- **AI Dev Home 模型库**：https://www.aidevhome.com/data/models/
- **高通平台大语言模型精选**：https://www.aidevhome.com/?id=51
- **DeepSeek-R1-Distill-Qwen-7B**：https://aiot.aidlux.com/zh/models/detail/78


## 版本历史

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

## 许可证

QAI AppBuilder 采用 **BSD 3-Clause "New" or "Revised" License**。

详见：https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE

---

## 免责声明

本软件按"原样"提供，不提供任何明示或暗示的保证。作者和贡献者不对因使用本软件而产生的任何损害承担责任。代码可能不完整或测试不充分。用户需自行评估其适用性并承担所有相关风险。

**注意**：欢迎贡献。在关键系统中部署前请确保充分测试。

---

## 贡献和支持

### 报告问题

如果遇到问题，请访问：

- **GitHub Issues**：https://github.com/quic/ai-engine-direct-helper/issues

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
**最后更新**：2026年1月26日  
**适用于**：QAI AppBuilder v2.0.0+
