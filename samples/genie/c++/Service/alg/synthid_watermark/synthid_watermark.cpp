// SynthID 30-layer tournament watermark plugin (alg=4 from the customer's
// llama.cpp patch set), packaged as an independent GenieAPIService watermark
// provider (see src/GenieAPIService/src/watermark/watermark_provider_host.h).
//
// Only GENIE_WM_CAP_TOKEN_HOOK is implemented; text_hook_* stay NULL.
//
// Algorithm summary (see the customer's 0001/0002/0003/0005 patches for the
// full derivation):
//   - context hash: h = LCG(1, t) folded over the last `ctx_len` accepted
//     tokens, LCG(h, d) = (h + d) * SYNTHID_LCG_MULT + 1
//   - per candidate token v: x = LCG(ctx_hash, v); per layer l: bit =
//     table[LCG(x, layer_keys[l]) % 65536]
//   - m=30 layer tournament: g_mass_l = sum(bit_l(v) * p(v)); p(v) *=
//     (1 + bit_l(v) - g_mass_l); the winner is the highest-probability
//     candidate after all 30 layers (closed-form equivalent of a real
//     elimination tournament, see 0006 for the temp<=0 argmax fix this
//     mirrors)
//   - repeated context masking (RCM): if the current context hash has
//     already been watermarked once in this generation, skip watermarking
//     entirely and leave the candidate logits untouched
//   - layer keys: HF/DeepMind public default keys when WATERMARK_KEY is
//     unset/empty (detectable by the official HF SynthIDTextWatermarkDetector);
//     otherwise derived from the secret key via watermark_mix

#include "watermark_provider_abi.h"
#include "synthid_table.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unordered_set>
#include <vector>

namespace {

constexpr int      kSynthidNLayers  = 30;
constexpr uint64_t kSynthidTableSize = 65536;
constexpr uint64_t kSynthidLcgMult   = 6364136223846793005ULL;
constexpr int      kDefaultCtxLen    = 4;

// DeepMind DEFAULT_WATERMARKING_CONFIG / HF reference detector keys (public mode).
constexpr uint64_t kSynthidDefaultKeys[kSynthidNLayers] = {
    654, 400, 836, 123, 340, 443, 597, 160, 57, 29,
    590, 639, 13, 715, 468, 990, 966, 226, 324, 585,
    118, 504, 421, 521, 129, 669, 732, 225, 90, 960,
};

uint64_t Fnv1a64(const std::string& s) {
    uint64_t h = 14695981039346656037ULL;
    for (const unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    return h;
}

uint64_t WatermarkMix(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

uint64_t SynthidLcg(uint64_t h, uint64_t d) {
    return (h + d) * kSynthidLcgMult + 1ULL;
}

int SynthidGBit(uint64_t idx) {
    return (kSynthidSamplingTable[idx >> 3] >> (idx & 7)) & 1;
}

int32_t ReadCtxLenFromEnv() {
    const char* v = std::getenv("WATERMARK_CTX");
    if (v == nullptr || *v == '\0') {
        return kDefaultCtxLen;
    }
    const int parsed = std::atoi(v);
    return parsed > 0 ? parsed : kDefaultCtxLen;
}

std::string ReadKeyFromEnv() {
    const char* v = std::getenv("WATERMARK_KEY");
    return v != nullptr ? std::string(v) : std::string();
}

} // namespace

// Opaque per-model-instance state (defined here, declared opaque in the ABI header).
struct GenieWatermarkTokenHook {
    int32_t ctx_len = kDefaultCtxLen;
    uint64_t layer_keys[kSynthidNLayers] = {};

    std::vector<int32_t> prev;             // last accepted tokens, capped at ctx_len
    std::unordered_set<uint64_t> seen_ctx; // repeated-context masking

    explicit GenieWatermarkTokenHook(uint32_t /*n_vocab*/, uint64_t /*seed*/) {
        ctx_len = ReadCtxLenFromEnv();

        const std::string key = ReadKeyFromEnv();
        if (key.empty()) {
            std::memcpy(layer_keys, kSynthidDefaultKeys, sizeof(layer_keys));
        } else {
            const uint64_t key_hash = Fnv1a64(key);
            for (int l = 0; l < kSynthidNLayers; l++) {
                layer_keys[l] = WatermarkMix(key_hash ^ static_cast<uint64_t>(l + 1));
            }
        }
    }

    uint64_t CtxHash() const {
        uint64_t h = 1;
        for (const int32_t t : prev) {
            h = SynthidLcg(h, static_cast<uint64_t>(static_cast<uint32_t>(t)));
        }
        return h;
    }

    int SynthidG(uint64_t ctx_hash, int32_t token, int layer) const {
        const uint64_t x = SynthidLcg(ctx_hash, static_cast<uint64_t>(static_cast<uint32_t>(token)));
        const uint64_t k = SynthidLcg(x, layer_keys[layer]) % kSynthidTableSize;
        return SynthidGBit(k);
    }
};

namespace {

GenieWatermarkTokenHook* TokenHookCreate(uint32_t n_vocab, uint64_t seed) {
    return new GenieWatermarkTokenHook(n_vocab, seed);
}

void TokenHookApply(GenieWatermarkTokenHook* hook, float* logits, const int32_t* ids, int32_t n) {
    if (hook == nullptr || logits == nullptr || ids == nullptr || n <= 0) {
        return;
    }

    const uint64_t ctx_hash = hook->CtxHash();

    // Repeated context masking: this context window was already watermarked
    // once in the current generation; leave the distribution untouched so the
    // downstream sampling chain behaves exactly as if watermarking were off.
    if (hook->seen_ctx.count(ctx_hash) > 0) {
        return;
    }
    hook->seen_ctx.insert(ctx_hash);

    // Softmax over the raw logits (temperature-agnostic: this node sits at the
    // very front of the sampling chain, before any temperature/top-k node).
    float max_logit = logits[0];
    for (int32_t i = 1; i < n; i++) {
        max_logit = std::max(max_logit, logits[i]);
    }

    std::vector<double> probs(n);
    double norm = 0.0;
    for (int32_t i = 0; i < n; i++) {
        probs[i] = std::exp(static_cast<double>(logits[i] - max_logit));
        norm += probs[i];
    }
    for (int32_t i = 0; i < n; i++) {
        probs[i] /= norm;
    }

    // m=30 layer tournament (closed-form update, see the file header comment).
    std::vector<uint8_t> g(n);
    for (int l = 0; l < kSynthidNLayers; l++) {
        double g_mass = 0.0;
        for (int32_t i = 0; i < n; i++) {
            g[i] = static_cast<uint8_t>(hook->SynthidG(ctx_hash, ids[i], l));
            g_mass += static_cast<double>(g[i]) * probs[i];
        }
        for (int32_t i = 0; i < n; i++) {
            probs[i] *= 1.0 + static_cast<double>(g[i]) - g_mass;
        }
    }

    // The tournament winner is the highest-probability candidate after all 30
    // layers; collapse the distribution to that single candidate so any
    // downstream sampler (greedy or stochastic) deterministically picks it.
    int64_t best = 0;
    double best_p = probs[0];
    for (int32_t i = 1; i < n; i++) {
        if (probs[i] > best_p) {
            best_p = probs[i];
            best = i;
        }
    }

    for (int32_t i = 0; i < n; i++) {
        if (i != best) {
            logits[i] = -INFINITY;
        }
    }
}

void TokenHookAccept(GenieWatermarkTokenHook* hook, int32_t accepted_token_id) {
    if (hook == nullptr) {
        return;
    }
    hook->prev.push_back(accepted_token_id);
    if (static_cast<int32_t>(hook->prev.size()) > hook->ctx_len) {
        hook->prev.erase(hook->prev.begin());
    }
}

void TokenHookReset(GenieWatermarkTokenHook* hook) {
    if (hook == nullptr) {
        return;
    }
    hook->prev.clear();
    hook->seen_ctx.clear();
}

void TokenHookFree(GenieWatermarkTokenHook* hook) {
    delete hook;
}

const GenieWatermarkProviderVTable kVTable = {
    /* struct_size          = */ sizeof(GenieWatermarkProviderVTable),
    /* abi_version           = */ GENIE_WATERMARK_ABI_VERSION,
    /* capabilities          = */ GENIE_WM_CAP_TOKEN_HOOK,
    /* token_hook_create     = */ TokenHookCreate,
    /* token_hook_apply      = */ TokenHookApply,
    /* token_hook_accept     = */ TokenHookAccept,
    /* token_hook_reset      = */ TokenHookReset,
    /* token_hook_free       = */ TokenHookFree,
    /* text_hook_apply       = */ nullptr,
    /* text_hook_free_string = */ nullptr,
};

} // namespace

#if defined(_WIN32) && defined(GENIE_WATERMARK_PLUGIN_IMPL)
extern "C" __declspec(dllexport) const GenieWatermarkProviderVTable* GenieWatermarkProvider_GetVTable(void) {
    return &kVTable;
}
#else
extern "C" const GenieWatermarkProviderVTable* GenieWatermarkProvider_GetVTable(void) {
    return &kVTable;
}
#endif
