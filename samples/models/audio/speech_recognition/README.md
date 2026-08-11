# Speech Recognition

Automatic speech recognition (ASR) models that transcribe spoken audio to text.

## Models

| Model | Task | Language | Architecture | Speed |
|-------|------|----------|-------------|-------|
| [zipformer](zipformer/) | Streaming ASR | Chinese + English (mixed) | Streaming RNN-T (encoder / decoder / joiner), 3 NPU binaries | Very Fast (RTF ≈ 0.02) |
| [whisper_base_en](whisper_base_en/) | English ASR | English | Encoder-decoder, 6 layers, 8 heads | Moderate |
| [whisper_tiny_en](whisper_tiny_en/) | English ASR | English | Encoder-decoder, 4 layers, 6 heads | Fast |

- **Zipformer** uses float32/int32 native I/O and supports streaming chunked inference.
- **Whisper** models use float16 KV cache and support audio of any length (auto-chunked at 30 seconds).

## Quick Start

```bash
cd qai-appbuilder\samples

# Zipformer (Chinese + English, streaming, very fast)
python run_inference.py --model zipformer

# Zipformer with custom audio
python run_inference.py --model zipformer --args "--audio path\to\audio_16khz.wav"

# Whisper Base (English, more accurate)
python run_inference.py --model whisper_base_en

# Whisper Tiny (English, faster)
python run_inference.py --model whisper_tiny_en

# Whisper with custom audio file
python run_inference.py --model whisper_base_en --args "--audio_file path/to/audio.wav"
```

## Dependencies

### Zipformer
```
pip install qai_appbuilder numpy soundfile scipy kaldi_native_fbank
```

### Whisper (Base / Tiny)
```
pip install audio2numpy openai-whisper
```
