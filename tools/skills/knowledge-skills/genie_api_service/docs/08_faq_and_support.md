# GenieAPIService 常见问题和技术支持

## 常见问题

### 1. 服务启动失败

**问题**：运行 `GenieAPIService.exe` 时提示找不到 DLL 文件。

**解决方案**：
- 确保已安装 [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160)
- 检查 QAIRT SDK 是否正确安装
- 确认所有依赖的 `.dll` 文件都在同一目录下

### 2. 模型加载失败

**问题**：服务启动后提示 "Failed to load model"。

**解决方案**：
- 检查 `config.json` 文件路径是否正确
- 确认模型文件完整且未损坏
- 检查系统内存是否足够（至少 16GB）
- 查看日志文件获取详细错误信息

### 3. NPU 不可用

**问题**：服务运行在 CPU 上而不是 NPU。

**解决方案**：
- 确认设备支持 Qualcomm Snapdragon NPU
- 检查 QAIRT SDK 版本是否正确
- 在 `config.json` 中设置 `"device": "npu"`
- 更新设备驱动程序

### 4. 推理速度慢

**问题**：模型推理速度比预期慢。

**解决方案**：
- 确认模型运行在 NPU 而不是 CPU
- 检查系统资源占用情况
- 尝试减小 `context_size` 或 `max_tokens`
- 关闭不必要的后台应用程序

### 5. 流式输出不工作

**问题**：设置 `stream=true` 但没有流式输出。

**解决方案**：
- 确认客户端正确处理 SSE（Server-Sent Events）
- 检查网络连接和防火墙设置
- 尝试使用非流式模式测试服务是否正常

### 6. 多模态模型无法识别图像

**问题**：发送图像后模型无响应或报错。

**解决方案**：
- 确认图像已正确编码为 base64
- 检查图像格式是否支持（PNG、JPEG）
- 确认使用的是多模态模型（如 qwen2.5vl3b）
- 检查消息格式是否正确（需要 `{question, image}` 格式）

### 7. Android 服务自动停止

**问题**：Android 上的服务运行一段时间后自动停止。

**解决方案**：
- 在设置中允许应用后台运行
- 关闭电池优化
- 将应用添加到白名单
- 确保有足够的存储空间

### 8. 工具调用不生效

**问题**：发送 `tools` 参数但模型不调用工具。

**解决方案**：
- 确认模型支持 Function Calling
- 检查工具定义格式是否正确
- 尝试设置 `tool_choice="auto"` 或指定工具名称
- 查看模型是否需要特定的提示词格式

### 9. 历史记录丢失

**问题**：重启服务后对话历史丢失。

**解决方案**：
- 使用 `-n` 参数设置保存的历史轮数
- 定期调用 `/v1/history` 接口备份历史

### 10. 端口被占用

**问题**：启动服务时提示端口 8910 已被占用。

**解决方案**：
- 使用 `-p` 参数指定其他端口
- 检查是否有其他 GenieAPIService 实例在运行
- 使用 `netstat -ano | findstr 8910` 查找占用端口的进程

### 11. LoRA 适配器加载失败

**问题**：使用 `--adapter` 参数后服务报错。

**解决方案**：
- 确认 LoRA 文件路径正确
- 检查 LoRA 文件与基础模型兼容
- 尝试调整 `--lora_alpha` 参数
- 查看日志获取详细错误信息

### 12. 输出文本乱码

**问题**：模型输出包含乱码或特殊字符。

**解决方案**：
- 确认终端支持 UTF-8 编码
- 检查 tokenizer 文件是否正确
- 尝试使用不同的客户端测试
- 更新模型文件到最新版本

---

## 技术支持

### 获取帮助

如果您在使用 GenieAPIService 时遇到问题，可以通过以下方式获取帮助：

1. **查看文档**
   - [GitHub 仓库](https://github.com/quic/ai-engine-direct-helper)
   - [API 文档](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/docs/API.md)
   - [示例代码](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient)

2. **提交问题**
   - [GitHub Issues](https://github.com/quic/ai-engine-direct-helper/issues)

### 报告 Bug

报告 Bug 时，请提供以下信息：

1. **环境信息**
   ```
   - 操作系统：Windows 11 / Android 13 / Ubuntu 22.04
   - 设备型号：Surface Pro X / Samsung Galaxy S23
   - GenieAPIService 版本：2.1.4
   - QAIRT 版本：2.42.0
   ```

2. **问题描述**
   - 详细描述问题现象
   - 预期行为和实际行为
   - 复现步骤

3. **日志信息**
   - 启动日志
   - 错误日志
   - 相关的 API 请求和响应

4. **配置文件**
   - `config.json` 内容
   - 启动参数

### 贡献代码

欢迎为 GenieAPIService 贡献代码：

1. Fork 仓库
2. 创建特性分支
3. 提交更改
4. 推送到分支
5. 创建 Pull Request

### 许可证

GenieAPIService 使用 BSD-3-Clause 许可证。详见 [LICENSE](https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE) 文件。

### 联系方式

- **项目主页**：https://github.com/quic/ai-engine-direct-helper
- **问题反馈**：https://github.com/quic/ai-engine-direct-helper/issues
