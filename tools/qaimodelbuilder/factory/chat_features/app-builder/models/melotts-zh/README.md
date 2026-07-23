# melotts-zh — App Builder Pack (v2.0 · Full NPU)

MeloTTS Chinese (myshell-ai/MeloTTS) for Mandarin text-to-speech on
Snapdragon X Elite — **full NPU inference** (bert_wrapper + encoder +
flow + decoder, all on HTP). Mixed-precision quantization (encoder/bert
float32, flow/decoder w8a16). Single voice (speaker_id=1), variable
speed 0.5×–2.0×, 44.1 kHz mono PCM-16 output with text-audio alignment
for SRT/VTT export.

This Pack is fully isolated from `features/model-builder/` (no cross-
imports — that boundary is enforced by the App Builder isolation CI
script).

## Architecture — Full NPU pipeline

```
text → CPU G2P (jieba + pypinyin + cn2an + g2p_en)
     → NPU bert_wrapper.bin → ja_bert [768, T]
     → NPU encoder.bin → m_p, logs_p, w_ceil, y_lengths, x_mask, g
     → numpy generate_attn_squeezed (attention alignment matrix)
     → NPU flow.bin (or flow_short.bin for short sentences) → z latent
     → NPU decoder.bin × N chunks (T=128, streaming decode) → audio
     → 44.1 kHz mono WAV
```

### NPU Model Files (5 total, ~386 MB)

| File | Size | Role | Quantization |
|------|------|------|------|
| `bert_wrapper.bin` | 300 MB | BERT prosody features (bert-base-multilingual-uncased) | float32 |
| `encoder.bin` | 18 MB | Text encoder (enc_p + SDP + DP) | float32 |
| `flow.bin` | 29 MB | Flow reverse pass (z_p → z), long path | w8a16 (uint16) |
| `flow_short.bin` | ~15 MB | Flow reverse pass, short path (phone_len ≤ 256) | w8a16 (uint16) |
| `decoder.bin` | 24 MB | HiFi-GAN vocoder (T=128), streaming chunks | w8a16 (uint16) |

### Performance (Snapdragon X Elite, HTP v73)

| Sentence | Audio Length | Wall Time | Realtime Factor |
|----------|-------------|-----------|-----------------|
| Short ("你好。") | 0.81 s | ~284 ms | 2.9× |
| Medium (13 chars) | 3.97 s | ~765 ms | 5.2× |
| Long (26 chars) | 5.58 s | ~926 ms | 6.0× |

## File Layout

```
melotts-zh/
├── manifest.json              # Pack manifest (schema v1)
├── runner.py                  # Inference entry point (runner_protocol)
├── requirements.txt           # Python dependencies
├── SKILL.md                   # LLM instruction (injected when discussing results)
├── skill.policy.json          # File access whitelist
├── README.md                  # This file
├── bert_tokenizer_local.py    # Local BERT tokenizer (reads binary vocab)
├── melo_symbol_to_id.json     # Symbol-to-ID mapping for phonemes
├── melo_zh_local/             # Chinese G2P package
│   ├── __init__.py
│   ├── chinese.py             # Main G2P pipeline
│   ├── english.py             # English word G2P (CMUDict)
│   ├── symbols.py             # Phoneme inventory
│   ├── tone_sandhi.py         # Tone sandhi rules
│   └── opencpop-strict.txt    # Pinyin-to-phoneme lexicon
├── assets/                    # Legacy placeholder (not used by runner)
│   ├── bert/
│   └── ...
├── examples/                  # Example prompts
└── weights/                   # Symlink target for model .bin files
```

## What you need on the device

To run successful TTS inference:

1. **ARM64 Python 3.13 venv** with `qai_appbuilder>=2.45.0`, `numpy`,
   `jieba`, `pypinyin`, `cn2an`, `g2p_en` (see `requirements.txt`)
2. **QAIRT 2.45+ runtime DLLs** on PATH
3. **5 model .bin files** at `<repo>/models/melotts-zh/`:
   - `bert_wrapper.bin`, `encoder.bin`, `flow.bin`, `flow_short.bin`, `decoder.bin`
4. **2 tokenizer .bin files** at `<repo>/models/melotts-zh/`:
   - `bert_zh_tokenizer.bin`, `bert_normalizer.bin`

## Key Design Decisions

- **speaker_id is always 1** — the only valid speaker for ZH model
- **bert[1024] input is always zeros** — ZH_MIX_EN path doesn't use the large BERT
- **lang_ids use intersperse** — `[0, 3, 0, 3, ...]` (blank=0, phone=3)
- **Decoder streaming** — T=128 chunks, main_len=104, overlap=12
- **flow_short dual-path** — short sentences (phone_len ≤ 256, y_len ≤ 768) use a faster flow model
- **No transformers dependency** — BERT tokenization via local binary parser

## Smoke Test

```pwsh
$env:PYTHONPATH = "$PWD\features\app-builder\shared;$PWD\features\app-builder\models\melotts-zh"
$env:PYTHONIOENCODING = "utf-8"
'{"runId":"r-test","modelId":"melotts-zh","inputs":{"text":"你好"},"params":{"speed":1.0},"options":{},"packDir":"features/app-builder/models/melotts-zh","repoRoot":"."}' | python features\app-builder\models\melotts-zh\runner.py
# Without weights: emits {"type":"error","code":"WEIGHTS_NOT_INSTALLED",...} → exits 1
```
