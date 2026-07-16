# 验证纪律：三条验证铁律（跨领域，转换/精度/性能通用）

> **知识定位**：精度/性能调优里最省时间的三条元纪律——先廉价证伪再昂贵重建、契约以编译产物为真值、host 过≠设备过。放在 references 因这是转换/验证跨领域纪律，不专属某一子能力。
> **抽取内核**：① 分钟级证伪先于昂贵 rebuild（一次 rebuild+device run ~1.5h，一次 numpy 检查几分钟）；② 编译产物（`.bin`/DLC）元数据是真值，优先于导出代码（两者会 drift）；③ host 指标过≠设备过 + 分层归因阶梯 + 把结构化 trace 喂 LLM 问瓶颈的分析范式。
> **剥离外壳**：某 LLM 优化流水线的 PPL/KV-cache/自回归/C++ runner 等 LLM 外壳剥掉；某 Android trace 分析工具本身不适用本场景，只吸收"LLM + 结构化 trace 问瓶颈"的范式。与 AGENTS.md §5 State-Truth-First、§4 发现缺陷必须修复互相印证。

## Contents
- 铁律一：分钟级证伪先于昂贵 rebuild
- 铁律二：编译产物元数据是真值
- 铁律三：host 过≠设备过 + 分层归因 + AI 读 trace

---

## 铁律一：分钟级证伪先于昂贵 rebuild

**任何"改精度/改配置能改善结果"的假设，先用几分钟的廉价检查证伪它，再投入 ~30min 重建 + ~60min 设备验证。**

### 招牌"5 分钟杀手检查"：重量化到设备 scale 测 cosine

> 把 FP32 目标张量按设备实际使用的 scale/offset 重新量化一遍，再测它与原 FP32 的 cosine。若 cos ≈ 0.999，说明量化网格本身近乎无损——此时"提高精度/加大位宽"根本救不了，整条路线可砍。

逻辑：若按设备真实 scale 重量化后与 FP32 已 cos≈0.999，这个张量的**表示精度不是瓶颈**，再堆 FP16/INT16 只是在已足够精的网格上折腾（FP16 的 ~10bit 尾数甚至比 uint16 的 65536 级网格更粗，反而更差）。通用形态（自建 ONNX 小模型）：怀疑某层精度不够时，先在 host 用 numpy 把该层 FP32 参考张量按设备量化参数 round-trip，测 cos-to-FP32；≈0.999 就别在"表示精度"上投重建。

其它分钟级证伪形态：单块/单步跑一遍代替全量跑；文件级解剖（见铁律二）代替重新导出；静态检查（形状/dtype/scale 对不对）代替上机。

**铁律措辞**：每一个假设都要先配一个分钟级检查（文件解剖 / 单块 / numpy 重量化 / 静态）来确认或否定它，之后才允许投入一次昂贵的重建 + 设备验证。

## 铁律二：编译产物元数据是真值，优先于导出代码

**I/O 契约、tensor 的 scale/offset、dtype、shape，一律从编译产物（`.bin` / DLC）反查，绝不从导出脚本/ONNX 导出代码重推——因为两者会 drift。**

- 从导出脚本"重新推导"量化参数，假设了"导出时参数 == 部署产物里参数"——**这个假设经常不成立**：量化工具链可能在编译阶段重新校准、改写 scale/bitwidth，导出代码里的数字与 `.bin` 里烧进去的不一致。
- **产物是最终真正被设备执行的东西**，故产物元数据 = ground truth，分析产物其说服力等同于一次设备运行。

怎么反查（WoS PC 可用）：
- `qnn-context-binary-utility --context_binary <bin> --json_file <out>.json` 导出每 tensor 的 `name`/`dataType`/`dimensions`/`quantizeParams.scaleOffset`。
- DLC→JSON（如 `qairt-dlc-to-json`）导出每 tensor scale/offset/dtype，确认量化工具是否**尊重了**你给的 encodings（还是重新校准/漂移了）、host 侧 encoding 是否**真的到达了设备**。
- **反量化公式也从产物读**：`fp32 = (quant + offset) * scale`，scale/offset 必须从 JSON 现取，绝不硬编码。

与 AGENTS.md §5 印证：契约的唯一真值源是编译产物，导出脚本只是它的一个（可能已漂移的）上游。

## 铁律三：host 指标过 ≠ 设备过；用 AI 分析 trace

### host 通过不代表设备通过

host 侧仿真常**不建模设备的定点累加器/定点计算路径**（它在算子内部用浮点跑，只在张量边界插量化），因此 host 与 device 存在**结构性差异**，"device == host 仿真"可能根本不可达。报告纪律：
- 报精度/性能**必须在设备上实测**，不能拿 host 数字当设备结论；
- 对标用**同协议、同批次**的 host 参考；
- device 比 host 差很多时，先怀疑"对标本身不忠实"（host 没建模定点累加），再无限归因为设备 bug；必要时重定义对标（给 host 参考注入设备侧定点行为）才有意义。

### 分层归因阶梯（device 与 host 对不上时，先廉价后昂贵）

1. **解剖真实产物（零设备成本）**：从 `.bin`/DLC 取每 tensor scale/offset/dtype，确认工具链是否漂移。
2. **三方 step 对比（FP32 / host 量化仿真 / 设备）** 在同一批输入上比，把"普通量化损失（仿真也有）"与"设备特有差异"分开——只比 device-vs-FP32 会混淆两者。
3. **逐步/逐层趋势**：沿计算链逐步比 device-vs-FP32 与 sim-vs-FP32 的 cosine；若 device 那条随步数下滑而 sim 平稳，则漂移是设备的（累加/状态）。
4. **设备中间张量 dump（真机定位器）**：dump 设备侧中间张量（用张量自己的 encoding 反量化）与 host FP32 逐层/逐通道比；**dump 要纯增量、确保 dump 前后主输出 bit 不变（md5 校验）**，只回传小的中间张量。
5. **廉价重量化上界（numpy，分钟级）**：即铁律一的 5 分钟杀手检查——投任何重建前先跑。

### AI 分析 trace 范式

把结构化 profiling trace（如 `chromeTrace.json`）喂给 LLM，用自然语言直接问"瓶颈在哪 / 哪个阶段最耗时 / 哪里卡顿"。这是对 profiling 分析手段的补充。

## 与现有 SKILL 的关系

现有 profiling 讲怎么采 trace、accuracy 侧讲怎么发现哪层差；本 reference 补贯穿两者的**验证工作原则**（先廉价证伪再昂贵重建、契约以产物为真值、host≠device + 分层归因 + AI 读 trace），是方法论层的纪律，与具体采集/定位步骤互补，不重复。
