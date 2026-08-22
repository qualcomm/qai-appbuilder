# Quantization Sensitivity Cheat Sheet (pre-conversion risk assessment)

> **Knowledge scope**: pre-conversion risk assessment when converting a user's own ONNX model — predict the right precision per architecture family, the likely failure modes, and the mitigation. This capability is currently missing elsewhere in the reference set.
> **Kernel**: aggregate per-output cosine measurements from a large pool of on-device models and distill them into **architecture-family-level** quantization-sensitivity rules (chip-independent, portable), a set of w8a16 usage guidelines, and a pre-conversion decision flow.
> **Data source**: SM8850 (HTP **V81**, soc_id 87, VTCM 8 MB, burst) + QAIRT SDK **2.45**; host pipeline `qairt-converter → qairt-quantizer → qnn-context-binary-generator → qnn-net-run`; accuracy criterion = per-output cosine (min across outputs) between ONNXRuntime CPU and device output, ≥0.99 = pass.

> ## ⚠️ Scope limits (read this first)
> 1. **Single-chip data source**: all measurements come from SM8850 / HTP V81 / SDK 2.45. **Accuracy / quantization-sensitivity rules DO transfer across chips** (quantization-friendliness of an architecture is a numerical property, not a chip property); but **speedup ratios (latency) are qualitative only** — absolute millisecond values are SM8850-specific and have been dropped from this table; only the direction ("faster" / "slower") is retained.
> 2. **Calibration details unknown**: the source data does not document the calibration set / algorithm. **Some `cos=0` or extremely low cosine numbers may reflect "untuned default PTQ" or "operator-expression / post-processing issue" rather than the architecture being absolutely unquantizable**. Low cosine is a **high-risk signal, not a death sentence** — when you hit one, do per-layer localization first (see the conversion-troubleshooting sub-skill) to rule out untuned default PTQ before drawing an architectural conclusion.
> 3. **Cosine thresholds**: ≥0.99 = pass; 0.9–0.99 = needs tuning (w8a16 / mixed precision may recover it); <0.9, especially <0.5 or =0 = high risk (architecture-level sensitivity or post-processing head collapse). `nan` = numerical chain breakage (common with post-processing that contains exp/div or with embedding normalization).

---

## 1. Quantization-sensitivity cheat sheet by architecture family

> Column meanings: **Recommended precision** = preferred starting quantization tier; **Expected risk** = the most likely failure of a direct quantization; **Mitigation** = pre-conversion / post-failure handling; **Measured cosine evidence** = real measurements (same model at different precisions is the most informative).

| Architecture family | Recommended precision | Expected risk | Mitigation | Measured cosine evidence (SM8850 / V81 / 2.45) |
|---|---|---|---|---|
| **Plain CNN classification backbones** (ResNet / SqueezeNet / GoogLeNet / RegNet / ResNeXt / Inception / WideResNet / DenseNet / DLA) | **w8a8** first | A few deep/narrow backbones fall to 0.95–0.98 and need w8a16 to recover | Start at w8a8; on drop → w8a16 → mixed precision keeping only the sensitive layers in FP16 | resnet18 w8a8=**0.9925**; wideresnet50 w8a8=0.9911; resnet50 w8a8=0.9585 (needs recovery); densenet121 w8a16=0.9965 |
| **Lightweight / compact CNNs** (MobileNet-v2 / ShuffleNet / MNASNet / EfficientNet-B*) | **w8a8 with caution** — validate before using | Depthwise-separable convolutions + wide activation dynamic range are quantization-sensitive; w8a8 often 0.94–0.97 | Localize sensitive depthwise / SE layers, fall back with mixed precision | mobilenet_v2 w8a8=0.9729; mnasnet05 w8a8=0.9553; shufflenet_v2 w8a8=0.9455; efficientnet_b0 w8a16=0.9256 |
| **Super-resolution / denoising CNNs** (XLSR / QuickSRNet / SESR / DnCNN / Real-ESRGAN / NAFSSR / ESRGAN) | **w8a8 friendly** | A handful of GAN-style SR models actually collapse at w8a16 (very wide value range) | w8a8 first choice (the friendliest family for quantization); use float or quantize cautiously for the ESRGAN family | dncnn w8a8=**0.99999** (float→w8a8 ≈13.8× faster, qualitative); xlsr w8a8=0.9985; quicksrnetsmall w8a8=0.9995; real_esrgan_x4plus w8a8=0.9971; ⚠️ esrgan w8a16=**0.6298** (regression) |
| **3D CNN video classification** (ResNet-3D / ResNet-2Plus1D / ResNet-Mixed) | **w8a8 friendly** | Few; main risk is at context-binary generation (see conversion-troubleshooting) | w8a8 first | resnet_2plus1d w8a8=0.9981; resnet_3d w8a8=0.9946 |
| **Detection / segmentation "post-processing heads"** (all YOLO v3–v11 / v26, YOLOv8-OBB / -Seg, DeepLab, FCN, CenterNet, PPE / gear_guard, mediapipe_face / hand, selfie-seg) | **Convert backbone only, decode on CPU** | **Extremely high risk: even float can hit cos≈0.** The anchor / NMS / grid-decode / sigmoid-exp post-processing chain breaks numerically on HTP | **Split the graph**: export ONNX up to the backbone feature outputs only; run anchor decode / NMS / mask assembly as CPU post-processing. Never quantize the whole graph. | yolov8_det **float=0.045 / w8a8=0.046 / w8a16=0.259** (float already broken); yolov11_det float=0.017; deeplab_xception float=0.0; fcn_resnet50 float=0.0; mediapipe_selfie 0.0 at every precision |
| **Transformer classifiers / SE-attention variants** (BeiT / Swin* / LeViT / EfficientViT / EfficientFormer / ConvNext / MobileNet-v3 (with SE)) | **float or w8a16** — always validate before quantizing | **w8a16 often breaks too**: LayerNorm / Softmax / SE-gating / GELU are extremely sensitive to activation quantization | Use float when it passes; if quantization is required, start at w8a16 and locally fall back sensitive spots to FP16 | beit w8a16=**0.376**; mobilenet_v3_small w8a16=**0.343**; levit w8a16=0.305; efficientvit_l2_cls w8a16=**0.0**; efficientformer w8a16=0.067; mobilenet_v3_large w8a16=0.711 |
| **Deep / large Transformer / DETR / Depth ViT** (DETR-ResNet* / Depth-Anything V1–V3 / CREStereo / Video-MAE / Segformer) | **float**; Segformer is an exception and can run w8a8 | Large structure with heavy attention → high quantization risk; float accuracy excellent but slow | Prefer float on-device; the Segformer family is measured to run at w8a8 / w8a16 | segformer_base w8a8=0.9986 / w8a16=0.9994 (a rare quantization-friendly Transformer segmentation); depth_anything w8a16=0.9999; detr_resnet50_dc5 float=0.9999 |
| **HuggingFace BERT / text embedding** (albert / bert / distilbert / electra / mobilebert / minilm / nomic-embed) | **Requires upstream rewrite before conversion** | **HTP rejects int32 Gather outright** (MLM / embedding lookup), so conversion fails; even embeddings that do convert can end at cos=nan | Rewrite the embedding's int32-index Gather upstream into an HTP-acceptable form (see the int32-Gather entry in conversion-troubleshooting); fix the conversion failure before discussing accuracy | nomic_embed_text float=**nan**; bert / albert / distilbert / electra / mobilebert — all fail conversion (int32 Gather rejected) |
| **Face / keypoint small networks** (facemap_3dmm / face_attrib_net / hrnet_face / eyegaze / facemap) | **w8a8, network-dependent** | Regression / landmark heads are moderately quantization-sensitive | Try w8a8; if the regression head drops → w8a16 | facemap_3dmm w8a8=0.9962; face_attrib_net w8a8=0.999997 (same model float=0.80 — its quantized path is actually more stable); hrnet_face w8a8=0.9547 (needs recovery) |

---

## 2. w8a16 rules (not a cure, just "mild improvement")

The full measurement corpus supports the following w8a16 usage rules:

1. **w8a16 is generally better than w8a8, but often "from broken to still-not-enough"** — raising the activation bit-width mitigates activation-quantization error; for Transformer / SE / post-processing heads the **direction is right but the magnitude is limited**. Many cases only push cosine from 0.3 up to 0.6–0.9, still short of the 0.99 line.
   - Evidence: beit w8a16=0.376 (better than random, still far short); efficientformer w8a16=0.067; yolov8_det w8a16=0.259 (better than w8a8=0.046, still broken).
2. **w8a16 does not guarantee a speedup and can actually be slower** — on some graphs the transfer / compute overhead of a16 activations exceeds a8.
   - Evidence anchor: `foot_track_net` **is measurably slower at w8a16 than at float** (float=33.4 → w8a16=45.9; directional conclusion — absolute values are SM8850-only, qualitative).
3. **Correct usage**: treat w8a16 as "the first recovery tier after w8a8 drops", not as the default. If w8a16 still does not pass, the next step is **mixed precision** (only the layers pinpointed as sensitive are set to FP16, the rest stays INT8) — not staying in global w8a16.

---

## 3. Pre-conversion decision flow (using the table above)

For a **user's own ONNX model**, predict as follows before conversion:

1. **Identify the architecture family** → look up the "Recommended precision" starting point in the table.
2. **Any detection / segmentation post-processing head (anchor decode / NMS / grid / mask assembly / sigmoid-exp chain)?**
   - Yes → **split the graph first**: export ONNX up to the backbone features only; run post-processing on CPU. This is the single highest-value rule in this table — quantizing the full detection / segmentation graph almost always breaks (even float can hit cos≈0).
3. **Any int32-index Gather (embedding lookup / MLM head)?**
   - Yes → HTP will reject it at the conversion stage; requires an upstream rewrite (see the conversion-troubleshooting sub-skill).
4. **Transformer / SE-gating / LayerNorm-Softmax-heavy classification head?**
   - Yes → prefer float; if quantization is required, step through w8a8 → w8a16 → mixed-precision FP16, validating cosine at each tier.
5. **Plain CNN backbone / SR / denoising / 3D CNN?**
   - Yes → jump straight to w8a8; drops are rare; a few deep-narrow backbones need w8a16 recovery.
6. **Measure cosine at every tier** (≥0.99 to pass); when cosine is low, rule out "untuned default PTQ" before concluding "architectural sensitivity" (see Scope Limits §2).

> **One-line memory aid**: *Backbones / SR / denoising / 3D-CNN → w8a8 with confidence; detection / segmentation → always split the backbone off and decode on CPU; Transformer / SE → prefer float, and validate every quantization tier; BERT embeddings → fix the int32 Gather first; w8a16 is a recovery tool, not a cure, and may even be slower.*
