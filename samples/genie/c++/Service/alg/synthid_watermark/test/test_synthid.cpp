// Minimal golden-vector test for the SynthID token hook's LCG hash chain.
// Compiled by directly including the implementation file's translation unit
// so the (anonymous-namespace) hash primitives are visible without exporting
// them through the plugin ABI. Values below are the reference vectors from
// the customer's llama.cpp patches (0001-add-watermark-sampler.patch /
// 0002-add-SynthIDTextWatermarkDetector-table.patch self_test()), so a match
// here proves this C++ port is bit-for-bit compatible with both the
// customer's C++ sampler and the companion Python detector.

#include "../synthid_watermark.cpp"

#include <cstdio>
#include <string>

namespace {

int g_failures = 0;

void Expect(bool cond, const char* what) {
    if (!cond) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        g_failures++;
    } else {
        std::printf("OK: %s\n", what);
    }
}

std::string GBitString(uint64_t ctx_hash, const uint64_t* layer_keys, int layer, int n) {
    std::string s;
    for (int t = 0; t < n; t++) {
        const uint64_t x = SynthidLcg(ctx_hash, static_cast<uint64_t>(t));
        const uint64_t k = SynthidLcg(x, layer_keys[layer]) % kSynthidTableSize;
        s += SynthidGBit(k) ? '1' : '0';
    }
    return s;
}

} // namespace

int main() {
    // ---- private mode (secret key derives the layer keys) ----
    const std::string key = "lenovo-wm-test-key";
    const uint64_t key_hash = Fnv1a64(key);
    Expect(key_hash == 16001768701284803778ULL, "fnv1a64(\"lenovo-wm-test-key\")");

    uint64_t private_keys[kSynthidNLayers];
    for (int l = 0; l < kSynthidNLayers; l++) {
        private_keys[l] = WatermarkMix(key_hash ^ static_cast<uint64_t>(l + 1));
    }
    Expect(private_keys[0] == 13878482867834397182ULL &&
           private_keys[1] == 10785079625316710909ULL &&
           private_keys[2] == 12313749837369541303ULL,
           "private mode layer_keys[0..2]");

    uint64_t ctx_hash = 1;
    for (const int32_t t : std::vector<int32_t>{100, 200, 300, 400}) {
        ctx_hash = SynthidLcg(ctx_hash, static_cast<uint64_t>(static_cast<uint32_t>(t)));
    }
    Expect(ctx_hash == 15903612838876140933ULL, "private mode ctx_hash([100,200,300,400])");

    Expect(GBitString(ctx_hash, private_keys, 0, 10) == "0101111011", "private mode g(t,layer=0) t=0..9");
    Expect(GBitString(ctx_hash, private_keys, 1, 10) == "0001110010", "private mode g(t,layer=1) t=0..9");

    // ---- public mode (empty key -> HF/DeepMind default keys) ----
    Expect(kSynthidDefaultKeys[0] == 654 && kSynthidDefaultKeys[1] == 400 &&
           kSynthidDefaultKeys[2] == 836 && kSynthidDefaultKeys[3] == 123,
           "public mode layer_keys[0..3]");

    Expect(GBitString(ctx_hash, kSynthidDefaultKeys, 0, 10) == "1100110010", "public mode g(t,layer=0) t=0..9");
    Expect(GBitString(ctx_hash, kSynthidDefaultKeys, 1, 10) == "0000011110", "public mode g(t,layer=1) t=0..9");

    // ---- end-to-end: TokenHookApply collapses the distribution to one winner ----
    {
        GenieWatermarkTokenHook hook(32000, 0);
        std::memcpy(hook.layer_keys, kSynthidDefaultKeys, sizeof(hook.layer_keys));
        hook.prev = {100, 200, 300, 400};

        float logits[8] = {1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f};
        int32_t ids[8]  = {0, 1, 2, 3, 4, 5, 6, 7};
        TokenHookApply(&hook, logits, ids, 8);

        int finite_count = 0;
        for (float l : logits) {
            if (l != -INFINITY) {
                finite_count++;
            }
        }
        Expect(finite_count == 1, "TokenHookApply collapses candidates to exactly one winner");
    }

    if (g_failures == 0) {
        std::printf("all tests passed\n");
        return 0;
    }
    std::fprintf(stderr, "%d test(s) failed\n", g_failures);
    return 1;
}
