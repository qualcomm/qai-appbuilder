# Audio Models

This directory contains Python inference samples for audio AI models running on the Snapdragon NPU (HTP) via QAI AppBuilder.

## Models

| Subcategory | Model | Task | Script |
|-------------|-------|------|--------|
| [Audio Classification](audio_classification/) | [yamnet](audio_classification/yamnet/) | WAV → top-5 of 521 AudioSet sound categories | `audio-classification/yamnet/python/yamnet.py` |
| [Audio Generation](audio_generation/) | [pipertts_en](audio_generation/pipertts_en/) | English text → WAV speech (22050 Hz) | `audio-generation/pipertts_en/python/pipertts_en.py` |
| [Speech Recognition](speech_recognition/) | [whisper_base_en](speech_recognition/whisper_base_en/) | WAV → English transcription (Base model) | `speech-recognition/whisper_base_en/python/whisper_base_en.py` |
| [Speech Recognition](speech_recognition/) | [whisper_tiny_en](speech_recognition/whisper_tiny_en/) | WAV → English transcription (Tiny model, faster) | `speech-recognition/whisper_tiny_en/python/whisper_tiny_en.py` |

## Quick Start

Run from the `samples/` directory:

```bash
# Audio classification
python run_inference.py --model yamnet

# Text-to-speech
python run_inference.py --model pipertts_en --args "--text 'Hello world.'"

# Speech recognition
python run_inference.py --model whisper_base_en --args "--audio_file path/to/audio.wav"
python run_inference.py --model whisper_tiny_en --args "--audio_file path/to/audio.wav"
```

## Dependencies

```
pip install soundfile soxr gruut audio2numpy openai-whisper
```
