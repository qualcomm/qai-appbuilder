# Examples — Licenses

> **Status: PLACEHOLDER prompts.** This Pack is currently a scaffold
> (no real TTS inference). The text prompts in `examples/prompts.json`
> are bundled and runnable in principle; only the synthesis itself is
> blocked. License terms below cover the prompt strings, not generated
> audio (which the user owns once v1.0 implementation lands).

The three example prompts in `examples/prompts.json` are:

| Prompt name | Source / Author | License | Notes |
|-------------|-----------------|---------|-------|
| `short-greeting` (`你好，今天天气真好。`) | Original placeholder, written for this Pack | CC0 / Public Domain | A single-clause greeting; smallest end-to-end latency demo. |
| `classical-poem` (`床前明月光…`) | 李白《静夜思》(Tang Dynasty, ~8th century CE) | Public Domain | Classical Chinese poem. Author Li Bai died in 762 CE; well past any modern copyright term in any jurisdiction. Demonstrates literary cadence, comma/period prosody, and the model's handling of slightly archaic phrasing. |
| `mixed-en-digits` (`今天 8 点 30 分有 3 个 meeting…`) | Original placeholder, written for this Pack | CC0 / Public Domain | Designed to exercise three frontend paths at once: (a) digit-to-Chinese number reading via cn2an, (b) embedded English word handling, and (c) pause prediction at the comma. |

When implementing v1.0, you may extend `examples/prompts.json` with
additional prompts; each new entry needs an attribution row above. Prefer
public-domain literary sources or original CC0-licensed strings so the
repo stays freely redistributable.

Recommended characteristics for additional prompts:

- Keep each one **≤ 150 Chinese characters** (well under the 500-char
  `inputSchema.constraints.maxChars` cap) so they synthesize quickly
  during smoke tests.
- Avoid personally identifiable information (real names, addresses,
  phone numbers).
- Avoid copyrighted song lyrics, modern poetry, or news article
  paragraphs unless you have explicit redistribution permission.
- Cover at least: (a) plain prose, (b) one heavily-punctuated case, and
  (c) one with embedded ASCII (digits / English word) to make the
  Chinese frontend's normalization paths visible.
