# QAI AppBuilder — Community Apps 🌍

On-device AI apps built by **the community**, powered by the Qualcomm NPU via
[QAI AppBuilder](https://github.com/qualcomm/qai-appbuilder).

> This is the community showcase. Qualcomm-curated official apps live in
> [`../samples/`](../samples/). Anyone can add your app here after post its information in
> [Discussions → Show & Tell](https://github.com/qualcomm/qai-appbuilder/discussions/categories/show-and-tell).

## 🖼️ Gallery

<!-- GALLERY:START -->
<table>
  <tr>
    <td align="center" valign="top" width="33%"><a href="../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/"><img src="../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/assets/screenshot.png" alt="Real-ESRGAN x4plus" width="260"></a><br><a href="../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/"><b>Real-ESRGAN x4plus</b></a><br><sub><code>vision</code> · by shengtin</sub><br><sub>Upscale any image 4× with Real-ESRGAN x4plus, running fully on the Snapdragon NPU (HTP) via QAI AppBuilder.</sub></td>
    <td align="center" valign="top" width="33%"><a href="../samples/ComputerVision/video_object_tracking/track_anything/"><img src="../samples/ComputerVision/video_object_tracking/track_anything/assets/screenshot.png" alt="Track-Anything" width="260"></a><br><a href="../samples/ComputerVision/video_object_tracking/track_anything/"><b>Track-Anything</b></a><br><sub><code>vision</code> · by <a href="https://github.com/tim202503">@tim202503</a></sub><br><sub>Click a target in any video and track it frame-by-frame with XMem segmentation, running fully on the Snapdragon NPU.</sub></td>
  </tr>
</table>
<!-- GALLERY:END -->

> The [`index.html`](index.html) with category filters and
> a contributor wall is a single-page app that reads `apps.json` at runtime.
> **GitHub does not execute it** in the file browser, please view it via one of below 2 ways:
>
> - **Locally**: `cd CommunityApps && python -m http.server 8000` → open
>   <http://localhost:8000/index.html> (opening `index.html` via `file://`
>   directly won't work — the browser blocks the `fetch('apps.json')`).
> - **Quick online preview** (no Pages setup): open it through
>   [htmlpreview.github.io](https://htmlpreview.github.io/) — paste the
>   `index.html` GitHub URL.

## 📇 App Index

<!-- APPS_TABLE:START -->
| App | Category | Author | Description |
|-----|----------|--------|-------------|
| [Real-ESRGAN x4plus](../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/) | vision | shengtin | Upscale any image 4× with Real-ESRGAN x4plus, running fully on the Snapdragon NPU (HTP) via QAI AppBuilder. |
| [Track-Anything](../samples/ComputerVision/video_object_tracking/track_anything/) | vision | [@tim202503](https://github.com/tim202503) | Click a target in any video and track it frame-by-frame with XMem segmentation, running fully on the Snapdragon NPU. |
<!-- APPS_TABLE:END -->

## 🙌 App Contributors

<!-- CONTRIBUTORS:START -->
shengtin, [@tim202503](https://github.com/tim202503)
<!-- CONTRIBUTORS:END -->


## 🧩 Skill Index


| Skill | Location | Author | Description |
|-------|----------|--------|-------------|
| 🧞 GenieAPIService Docs | [tools/…/genie_api_service](../tools/skills/knowledge-skills/genie_api_service/) | zhanweiw | OpenAI-compatible local LLM/VLM API service docs — run models on Qualcomm WoS / Android / Linux via NPU(HTP)/CPU. |
| 📘 QAI AppBuilder Docs | [tools/…/qai_app_builder](../tools/skills/knowledge-skills/qai_app_builder/) | zhanweiw | QAI AppBuilder framework docs — deploy QNN (`*.bin`) models on the Qualcomm NPU (HTP). |
| AI Hub Model Run | [tools/…/aihub-model-run](../tools/qaimodelbuilder/skills/aihub-model-run/) | zhanweiw | Download pre-exported models from Qualcomm AI Hub and run on-device (NPU/HTP) inference via qai_appbuilder. |
| Data Analyst | [tools/…/data-analyst](../tools/qaimodelbuilder/skills/data-analyst/) | zhanweiw | Analyze CSV/JSON data, compute statistics, and generate visualizations & insight reports. |

### 🙌 Skill Contributors

zhanweiw

