# Applications

Complete AI applications built on QAI AppBuilder. Unlike the single-model scripts under
[`samples/models/`](../models/), each application combines one or more models into an end-to-end
experience. Each folder has its own README with setup and usage instructions.

| Application | Folder | Platform | Description |
| ----------- | ------ | -------- | ----------- |
| **Speech Translator** | [speech-translator](speech-translator/) | Windows on Snapdragon | On-device Chinese <-> English speech-to-speech translation: Zipformer ASR -> OpusMT -> PiperTTS-EN / MeloTTS-ZH, all on the NPU. Input from microphone, WAV file or text. |
| **WebUI apps** | [webui](webui/) | Windows on Snapdragon | Gradio web apps: ImageRepairApp (Real-ESRGAN super-resolution), StableDiffusionApp (text-to-image), GenieWebUI (LLM chat via GenieAPIService). |
| **Genie Flet UI** | [genie-flet-ui](genie-flet-ui/) | Windows, Android | Flet-based desktop / mobile UI for chatting with a local LLM. |
| **StorySeed** | [story-seed](story-seed/) | Windows on Snapdragon | Generates a short English story and image with generative AI and posts them to Xiaohongshu automatically. |
| **Android apps** | [android](android/) | Android (Snapdragon) | GenieChat (LLM / VLM chat), SuperResolution (image upscaling), TFLite Object Detection (Java TFLite with the QNN LiteRT delegate), and an ExecuTorch integration point. |

## Speech Translator at a glance

```bat
cd samples\apps\speech-translator

:: Chinese speech from the microphone -> English speech
python SpeechTranslator.py

:: English WAV file -> Chinese speech
python SpeechTranslator.py --direction en2zh --audio in.wav

:: Text only (no microphone, no ASR model)
python SpeechTranslator.py --text "你好，世界"
```

Models are downloaded automatically on first use (`.bin` QNN context binaries, chosen by chip:
Snapdragon X Elite or X2 Elite). See the [Speech Translator README](speech-translator/README.md)
for requirements, all options and troubleshooting.
