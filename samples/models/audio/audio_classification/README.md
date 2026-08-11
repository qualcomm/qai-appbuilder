# Audio Classification

Audio classification models that identify sound events and categories from audio input.

## Models

| Model | Task | Input | Output |
|-------|------|-------|--------|
| [yamnet](yamnet/) | Sound event classification | WAV audio file | Top-5 of 521 AudioSet sound categories |

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model yamnet
```
