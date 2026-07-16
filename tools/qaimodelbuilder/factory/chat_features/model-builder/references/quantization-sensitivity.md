# 量化敏感度速查表（转换前风险预判）

> **知识定位**：服务"帮用户转换自建 ONNX 模型时的**转换前风险预判**"——按模型架构族预判该用什么精度、可能踩什么坑、如何应对。这是本体系当前**完全没有**的决策能力。
> **抽取内核**：聚合大量端侧模型的真实 per-output cosine 实测数据，蒸馏出**架构族级**的量化敏感度规律（芯片无关可迁移）+ w8a16 使用准则 + 转换前决策流程。
> **数据源环境**：SM8850（HTP **V81**, soc_id 87, VTCM 8MB, burst）+ QAIRT SDK **2.45**；主机端流水线 `qairt-converter → qairt-quantizer → qnn-context-binary-generator → qnn-net-run`；精度判据 = ONNXRuntime CPU vs 设备输出的逐输出 cosine（取最小值），≥0.99 视为通过。

> ## ⚠️ 醒目局限声明（务必先读）
> 1. **数据源单芯片**：全部实测来自 SM8850 / HTP V81 / SDK 2.45。**精度/量化敏感度规律可跨芯片迁移**（架构量化友好性是数值本质，不是芯片专属）；但**加速倍数（latency）仅供定性参考**——绝对毫秒值是 SM8850 专属，本表已剔除，只保留"加速/减速方向"的定性描述。
> 2. **校准细节未知**：源数据未说明量化校准集/算法。**部分 `cos=0` 或极低 cosine 可能是"未调优的默认 PTQ"或"算子表达/后处理问题"，而非该架构绝对不可量化**。低 cosine 是"高危信号"而非"判死刑"——遇到时应先做逐层定位（见 conversion-troubleshooting 子 SKILL），排除默认 PTQ 未调优后再下结论。
> 3. **cosine 阈值语义**：≥0.99 = 精度通过；0.9~0.99 = 需调优（w8a16/混合精度可能救回）；<0.9 尤其 <0.5 或 =0 = 高危（架构级敏感或后处理头崩溃）。`nan` = 数值链路崩坏（常见于含 exp/div 的后处理或 embedding 归一化）。

---

## 一、按架构族的量化敏感度速查表

> 列含义：**推荐精度** = 首选量化档；**预期风险** = 直接量化最可能的问题；**应对策略** = 转换前/失败后的处置；**实测 cosine 证据锚点** = 来自实测的真实测量（同一模型不同精度对比，最能说明规律）。

| 架构族 | 推荐精度 | 预期风险 | 应对策略 | 实测 cosine 证据锚点（SM8850/V81/2.45） |
|---|---|---|---|---|
| **纯 CNN 分类骨干**（ResNet/SqueezeNet/GoogLeNet/RegNet/ResNeXt/Inception/WideResNet/DenseNet/DLA）| **w8a8** 优先 | 深/窄骨干个别掉到 0.95~0.98，需 w8a16 补 | w8a8 起步；掉点→w8a16→混合精度只保敏感层 FP16 | resnet18 w8a8=**0.9925**；wideresnet50 w8a8=0.9911；resnet50 w8a8=0.9585(需补)；densenet121 w8a16=0.9965 |
| **轻量/紧凑 CNN**（MobileNet-v2/ShuffleNet/MNASNet/EfficientNet-B*）| **w8a8 谨慎**，验证后用 | 深度可分离卷积 + 大动态范围激活对量化敏感，w8a8 常 0.94~0.97 | 逐层定位敏感 depthwise/SE 层，混合精度回退 | mobilenet_v2 w8a8=0.9729；mnasnet05 w8a8=0.9553；shufflenet_v2 w8a8=0.9455；efficientnet_b0 w8a16=0.9256 |
| **超分 / 去噪 CNN**（XLSR/QuickSRNet/SESR/DnCNN/Real-ESRGAN/NAFSSR/ESRGAN）| **w8a8 友好** | 极少数 GAN 类超分 w8a16 反崩（数值范围大）| w8a8 首选（本族量化最友好）；ESRGAN 类用 float 或谨慎量化 | dncnn w8a8=**0.99999**（float→w8a8 提速约 13.8×，定性）；xlsr w8a8=0.9985；quicksrnetsmall w8a8=0.9995；real_esrgan_x4plus w8a8=0.9971；⚠️ esrgan w8a16=**0.6298**（反崩） |
| **3D CNN 视频分类**（ResNet-3D/ResNet-2Plus1D/ResNet-Mixed）| **w8a8 友好** | 少；主要风险在 context-binary 生成（见 conversion-troubleshooting）| w8a8 首选 | resnet_2plus1d w8a8=0.9981；resnet_3d w8a8=0.9946 |
| **检测 / 分割"后处理头"**（YOLO 全系 v3~v11/v26、YOLOv8-OBB/-Seg、DeepLab、FCN、CenterNet、PPE/gear_guard、mediapipe_face/hand、selfie-seg）| **只转骨干 + CPU 侧 decode** | **极高危：连 float 都可能 cos≈0**。anchor/NMS/grid-decode/sigmoid-exp 后处理链在 HTP 上数值崩坏 | **切图**：ONNX 只导出到骨干特征输出，anchor decode / NMS / mask 组装放 CPU 侧后处理；切勿整图量化 | yolov8_det **float=0.045 / w8a8=0.046 / w8a16=0.259**（float 就崩）；yolov11_det float=0.017；deeplab_xception float=0.0；fcn_resnet50 float=0.0；mediapipe_selfie 全精度=0.0 |
| **Transformer 分类 / 含 SE 注意力**（BeiT/Swin*/LeViT/EfficientViT/EfficientFormer/ConvNext/MobileNet-v3(含 SE)）| **float 或 w8a16**，量化前必验证 | **w8a16 也常崩**：LayerNorm/Softmax/SE-gating/GELU 对激活量化极敏感 | float 能过就用 float；必须量化→w8a16 起步，逐层定位崩点做局部 FP16 回退 | beit w8a16=**0.376**；mobilenet_v3_small w8a16=**0.343**；levit w8a16=0.305；efficientvit_l2_cls w8a16=**0.0**；efficientformer w8a16=0.067；mobilenet_v3_large w8a16=0.711 |
| **深/大 Transformer / DETR / Depth ViT**（DETR-ResNet*/Depth-Anything V1~V3/CREStereo/Video-MAE/Segformer）| **float**；Segformer 例外可 w8a8 | 结构大、注意力多，量化风险高；float 精度极好但慢 | 优先 float 上板；Segformer 系实测可 w8a8/w8a16 | segformer_base w8a8=0.9986 / w8a16=0.9994（少见量化友好的 Transformer 分割）；depth_anything w8a16=0.9999；detr_resnet50_dc5 float=0.9999 |
| **HF BERT / 文本 embedding**（albert/bert/distilbert/electra/mobilebert/minilm/nomic-embed）| **需上游改写后再转** | **HTP 直接拒 int32 Gather**（MLM/embedding lookup），转换阶段即失败；能过的 embedding 也可能 cos=nan | 上游把 embedding 的 int32 index Gather 改写为 HTP 可接受形式（见 conversion-troubleshooting 的 int32 Gather 条）；先解决转换失败再谈精度 | nomic_embed_text float=**nan**；bert/albert/distilbert/electra/mobilebert 全部转换失败（int32 Gather 拒绝） |
| **人脸 / 关键点小网**（facemap_3dmm/face_attrib_net/hrnet_face/eyegaze/facemap）| **w8a8 视具体网** | 带回归头/landmark 的对量化中等敏感 | w8a8 试跑，回归头掉点→w8a16 | facemap_3dmm w8a8=0.9962；face_attrib_net w8a8=0.999997（但同模型 float=0.80，说明其量化路径反更稳）；hrnet_face w8a8=0.9547(需补) |

---

## 二、w8a16 规律（不是解药，是"温和改善"）

全量实测数据支撑以下 w8a16 使用准则：

1. **w8a16 普遍优于 w8a8，但常"从崩到仍不够"**——它抬高激活位宽缓解激活量化误差，对 Transformer/SE/后处理头**改善方向正确但幅度有限**，很多案例只是把 cos 从 0.3 抬到 0.6~0.9，仍不过 0.99 线。
   - 证据：beit w8a16=0.376（相对随机已好但远不够）；efficientformer w8a16=0.067；yolov8_det w8a16=0.259（比 w8a8=0.046 好，仍崩）。
2. **w8a16 不保证提速，甚至可能反而更慢**——a16 激活的搬运/计算开销在某些图上超过 a8。
   - 证据锚点：`foot_track_net` **w8a16 latency 反比 float 慢**（float=33.4 → w8a16=45.9，方向性结论，绝对值仅 SM8850 定性）。
3. **正确用法**：w8a16 当作"w8a8 掉点后的第一档补救"，而非默认档。若 w8a16 仍不过线，下一步是**混合精度**（只把逐层定位出的敏感层设 FP16，其余保 INT8），而不是继续在全局 w8a16 上打转。

---

## 三、转换前决策流程（把上表用起来）

面对一个**自建 ONNX 模型**，转换前按此预判：

1. **识别架构族** → 对照上表定"推荐精度"起点。
2. **有无检测/分割后处理头（anchor decode / NMS / grid / mask 组装 / sigmoid-exp 链）？**
   - 有 → **先切图**：ONNX 导出止于骨干特征，后处理放 CPU。这是本表最高价值的单条规律——检测/分割整图量化几乎必崩（float 都可能 cos≈0）。
3. **有无 int32 index 的 Gather（embedding lookup / MLM head）？**
   - 有 → HTP 会在转换阶段拒绝，需上游改写（见 conversion-troubleshooting 子 SKILL）。
4. **是 Transformer / 含 SE-gating / LayerNorm-Softmax 密集的分类头？**
   - 是 → 优先 float；必须量化按 w8a8→w8a16→混合精度 FP16 逐档升级，每档验 cosine。
5. **是纯 CNN 骨干 / 超分 / 去噪 / 3D CNN？**
   - 是 → 直接 w8a8，掉点少见；个别深窄骨干补 w8a16。
6. **每一档都实测 cosine**（≥0.99 才算过），低 cosine 先排除"默认 PTQ 未调优"再判架构敏感（见局限声明 §2）。

> **一句话记忆**：*骨干/超分/去噪/3D-CNN 放心 w8a8；检测/分割务必切骨干+CPU decode；Transformer/SE 先 float、量化必逐档验；BERT embedding 先解决 int32 Gather；w8a16 是补救不是解药、且可能更慢。*
