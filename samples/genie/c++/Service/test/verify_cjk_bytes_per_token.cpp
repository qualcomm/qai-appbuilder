// 纯函数级正向验证：EstimateCjkAwareBytesPerToken()（src/common/utils.h）。
//
// 为什么需要这个独立文件而不是接进 test_service.py：该函数是纯字符串/数学计算，
// 不依赖 Genie 引擎/模型/网络，本可以用任意 C++ 编译器离线验证，不需要远程
// ARM64 机器、不需要起服务。Step 2 第三轮评审要求补一条钉住两侧不变量的正向验证
// （场景 6 的服务端对照已证明 tokenizer 可用时无法观测到该比例的差异，只能作为
// "回退分支才生效"这一事实的负面凭证，不能反过来证明函数本身算对了）。
//
// 下面的函数体是 src/common/utils.h::EstimateCjkAwareBytesPerToken() 的**逐字节
// 拷贝**（只是去掉了对 nlohmann/json.hpp 的间接依赖，那个依赖只是 utils.h 顶部
// 其它无关代码引入的，本函数本身从未用到 json）。若日后修改了 utils.h 里的实现，
// 必须同步更新这里，否则本文件测的就不是真实实现了——这是本方案的已知局限，
// 记录在 .junie/plans/skill-capacity-frontier.md 末尾。
//
// 复跑方式（已在远程 ARM64/MSVC 与预期一致的 g++/clang 环境均可用）：
//   MSVC（cl.exe，需先跑 VsDevCmd.bat）：
//     cl /std:c++17 /utf-8 /EHsc /Fe:verify_cjk.exe test/verify_cjk_bytes_per_token.cpp
//     ⚠ /utf-8 不可省略——实测踩坑：省略它时 MSVC 按系统代码页（非 UTF-8）
//     解释源码里的 u8"中文..." 字符串字面量，会把中文字节序列悄悄解析成非法
//     UTF-8/非 CJK 码点，导致 cjk_3.0_pure_chinese_ratio 这条断言从 3.0 假性
//     漂移成 4.0（即"整段判定为非 CJK"），看起来像函数本身有 bug，实际是编译期
//     源码编码问题，与被测函数无关。
//   g++/clang（Linux/WSL/MinGW）：
//     g++ -std=c++17 -O2 -o verify_cjk.exe test/verify_cjk_bytes_per_token.cpp
//   ./verify_cjk.exe          # 全部通过时退出码 0，否则打印 FAIL 行并以非零退出
//
// 实测记录（2026-08-30，远程 ARM64，MSVC 19.34.31948 arm64，/utf-8）：
// 6/6 PASS，包含 cjk_3.0_pure_chinese_ratio: actual=3.000000 expected~=3.000000。

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

inline double EstimateCjkAwareBytesPerToken(const std::string &text,
                                            double cjk_bytes_per_token,
                                            double ascii_bytes_per_token)
{
    if (text.empty() || cjk_bytes_per_token <= 0.0 || ascii_bytes_per_token <= 0.0)
    {
        return (ascii_bytes_per_token > 0.0) ? ascii_bytes_per_token : 4.0;
    }

    const uint8_t *data = reinterpret_cast<const uint8_t *>(text.data());
    size_t len = text.size();
    size_t cjk_bytes = 0;
    size_t i = 0;
    while (i < len)
    {
        uint8_t byte = data[i];
        int char_bytes = 1;
        uint32_t code_point = byte;
        if ((byte & 0x80) == 0)
        {
            char_bytes = 1;
            code_point = byte;
        }
        else if ((byte & 0xE0) == 0xC0)
        {
            char_bytes = 2;
            code_point = byte & 0x1F;
        }
        else if ((byte & 0xF0) == 0xE0)
        {
            char_bytes = 3;
            code_point = byte & 0x0F;
        }
        else if ((byte & 0xF8) == 0xF0)
        {
            char_bytes = 4;
            code_point = byte & 0x07;
        }
        else
        {
            ++i;
            continue;
        }
        for (int k = 1; k < char_bytes && i + k < len; ++k)
        {
            code_point = (code_point << 6) | (data[i + k] & 0x3F);
        }
        if ((code_point >= 0x4E00 && code_point <= 0x9FFF) ||
            (code_point >= 0x3400 && code_point <= 0x4DBF) ||
            (code_point >= 0x3000 && code_point <= 0x303F) ||
            (code_point >= 0xFF00 && code_point <= 0xFFEF))
        {
            cjk_bytes += static_cast<size_t>(char_bytes);
        }
        i += static_cast<size_t>(char_bytes);
    }

    size_t other_bytes = (len > cjk_bytes) ? (len - cjk_bytes) : 0;
    double estimated_tokens = static_cast<double>(cjk_bytes) / cjk_bytes_per_token +
                              static_cast<double>(other_bytes) / ascii_bytes_per_token;
    if (estimated_tokens <= 0.0)
    {
        return ascii_bytes_per_token;
    }
    return static_cast<double>(len) / estimated_tokens;
}

static int g_failures = 0;

static void expect_near(const char *case_name, double actual, double expected, double tol)
{
    double diff = actual > expected ? actual - expected : expected - actual;
    if (diff > tol)
    {
        std::printf("FAIL [%s]: actual=%.6f expected=%.6f (tol=%.6f)\n", case_name, actual, expected, tol);
        ++g_failures;
    }
    else
    {
        std::printf("PASS [%s]: actual=%.6f expected~=%.6f\n", case_name, actual, expected);
    }
}

int main()
{
    // 不变量 (i)：两值同为 4.0 时恒返回 4.0，逐字节等于 P1 引入前的统一 length()/4。
    expect_near("both_4.0_ascii_text",
                EstimateCjkAwareBytesPerToken("The quick brown fox jumps over the lazy dog.", 4.0, 4.0),
                4.0, 1e-9);
    expect_near("both_4.0_pure_chinese",
                EstimateCjkAwareBytesPerToken(u8"这是一段纯中文测试文本用于验证换算比例是否正确", 4.0, 4.0),
                4.0, 1e-9);
    expect_near("both_4.0_mixed",
                EstimateCjkAwareBytesPerToken(u8"mixed 混合 text 文本", 4.0, 4.0),
                4.0, 1e-9);

    // 不变量 (ii)：纯中文输入下，默认 cjk_bytes_per_token=3.0（每个中文字符 3 字节，
    // 约等于 1 token），返回值应贴近 3.0（不应因量纲错配被算成 ~1.0，那正是第二轮
    // 修正前的 bug：错把"每 token 字符数"当"每 token 字节数"用，导致中文 token 数
    // 被高估约 3 倍、字符预算被压缩到 1/3——"过度压缩把有效信息压没"）。
    std::string pure_chinese = u8"这是一段纯中文测试文本用于验证换算比例是否正确不会过度压缩";
    double cjk_ratio = EstimateCjkAwareBytesPerToken(pure_chinese, 3.0, 4.0);
    expect_near("cjk_3.0_pure_chinese_ratio", cjk_ratio, 3.0, 0.05);

    // 佐证：若沿用第二轮修正前的错误量纲语义（把 3.0 当"字符数"用在字节输入上），
    // 纯中文文本会被算成约 1.0（3 字节的字符被当成 1 个"字符预算单位"消耗 3.0，
    // 等价于 1 字节 1 token），与本函数返回的 ~3.0 明显不同，证明当前实现已修正。
    double wrong_dimension_would_be = 1.0; // 历史 bug 的等价值，仅作对照说明，不参与断言
    (void)wrong_dimension_would_be;

    // 边界：空字符串、非法比例参数不崩溃，回退到 ascii_bytes_per_token。
    expect_near("empty_text_fallback", EstimateCjkAwareBytesPerToken("", 3.0, 4.0), 4.0, 1e-9);
    expect_near("invalid_cjk_ratio_fallback", EstimateCjkAwareBytesPerToken(u8"中文", 0.0, 4.0), 4.0, 1e-9);

    if (g_failures == 0)
    {
        std::printf("\nALL PASS (0 failures)\n");
        return 0;
    }
    std::printf("\n%d FAILURE(S)\n", g_failures);
    return 1;
}
