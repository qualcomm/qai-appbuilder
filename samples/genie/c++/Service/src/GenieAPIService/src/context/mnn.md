# `mnn.*`：MNN/CPU 后端内存防护

MNN/CPU 后端的内存预检查与运行时熔断都在 `mnn.cpp`（`MNNContext`）与 `model_manager.cpp` 的 `MnnVerifier` 里。跨后端 OOM 表现对比、`skipped`/`crashed`/`ignorable` 的分类原则等项目级设计见 `docs/architecture.md`；测试侧对 MNN OOM 崩溃/优雅失败的分类判定见 `test/test_service.md`。

## MNN 内存预检查

`MnnVerifier::CreateIfVerifiedImpl`（`model_manager.cpp`）在真正加载模型前调用 `MNNContext::EstimateMnnMemoryRequirement`（`context/mnn.cpp`）预估所需内存，与当前可用物理内存比较；不足时直接返回 `nullptr`（加载失败返回给客户端，而不是让进程崩溃）。判定为内存不足时会通过 `failure_reason=insufficient_memory`/`failure_detail` 字段暴露给客户端。

预估公式：
```
required = Σ(model_path 下所有 *.mnn 文件大小) × kMnnMemoryEstimateFactor(1.3) + kMnnMemoryEstimateMarginBytes(固定 2 GB)
effective_available = GetAvailablePhysicalMemoryBytes() - EstimateOtherLoadedModelsMemoryBytes()
```
- `kMnnMemoryEstimateFactor`（1.3x）覆盖 KV cache / 激活值等运行时开销；`kMnnMemoryEstimateMarginBytes`（固定 2 GB）覆盖未被 `.mnn` 扩展名匹配到的辅助文件及其余运行时开销。
- `EstimateOtherLoadedModelsMemoryBytes()` 遍历 `loaded_models_` 注册表，对每个其它已加载模型复用同一公式估算占用并汇总，从可用物理内存中扣除。

## 运行时低内存熔断（`MNNContext::Query()`）

**残余局限性（部分已通过运行时防护缓解）**：加载前预检查无法覆盖运行时峰值分配（长上下文下 KV cache 线性增长）；跨模型占用估算本身也是近似值。为此 `MNNContext::Query()` 的生成循环已增加运行时低内存熔断：每生成 `kMnnLowMemoryCheckIntervalTokens`（16）个 token 采样一次可用物理内存，低于 `kMnnLowMemoryAbortThresholdBytes`（1GB）时记录 `kError` 日志并提前优雅结束生成（`m_stop=true`），把“被操作系统直接杀死进程”转化为服务自身可控的优雅提前终止。两层防护叠加后仍不能保证**完全消除** OOM 崩溃概率（若内存在两次采样之间被瞬间打满仍可能来不及响应），只能进一步**降低**其触发频率。

## 陷阱：内存预检查只在“加载前”生效

- **MNN 内存预检查只在“加载前”生效**：不能防止运行时长上下文导致的 KV cache 峰值 OOM；调整 `kMnnMemoryEstimateFactor`/`kMnnMemoryEstimateMarginBytes` 时要用真实模型（如 `gpt-oss-20b-MNN`，权重约 13GB）验证不会误判拒绝加载。
