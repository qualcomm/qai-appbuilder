//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "../src/GenieAPIService/src/context/degenerate_repeat_guard.h"

#include <cctype>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

namespace
{

int g_failed = 0;
int g_total = 0;

void Check(const std::string &what, bool cond, const std::string &detail = std::string())
{
    ++g_total;
    if (cond)
    {
        std::printf("  [ ok ] %s %s\n", what.c_str(), detail.c_str());
        return;
    }
    ++g_failed;
    std::printf("  [FAIL] %s %s\n", what.c_str(), detail.c_str());
}

std::vector<std::string> Tokenize(const std::string &text)
{
    std::vector<std::string> tokens;
    size_t i = 0;
    while (i < text.size())
    {
        size_t j = i;
        if (text[j] == ' ')
        {
            ++j;
        }
        if (j < text.size() && std::isalpha(static_cast<unsigned char>(text[j])))
        {
            while (j < text.size() && std::isalpha(static_cast<unsigned char>(text[j])))
            {
                ++j;
            }
            tokens.push_back(text.substr(i, j - i));
            i = j;
            continue;
        }
        tokens.push_back(text.substr(i, 1));
        ++i;
    }
    return tokens;
}

struct Outcome
{
    int abort_at = 0;
    int period = 0;
};

Outcome Run(const std::string &text, bool enabled = true, int max_period = 128, size_t min_span = 256)
{
    degenerate_repeat_guard::StreamGuard guard(enabled, max_period, min_span);
    std::map<std::string, int32_t> vocab;
    Outcome outcome;
    int index = 0;
    for (const auto &token : Tokenize(text))
    {
        auto it = vocab.find(token);
        if (it == vocab.end())
        {
            it = vocab.emplace(token, static_cast<int32_t>(vocab.size())).first;
        }
        ++index;
        const auto decision = guard.Observe(it->second, token.size());
        if (decision.abort)
        {
            outcome.abort_at = index;
            outcome.period = decision.period;
            return outcome;
        }
    }
    return outcome;
}

std::string Repeat(const std::string &unit, int times)
{
    std::string out;
    for (int i = 0; i < times; ++i)
    {
        out += unit;
    }
    return out;
}

const std::string kSentence =
    "The model repeats this exact sentence again and again without ever making any progress at all. ";
const std::string kParagraph =
    "Inception v3 expects a 299x299 RGB input and produces 1000 class logits, so the exported graph "
    "needs a fixed input shape and a softmax head that is stripped before quantization to W8A8 format. "
    "Calibration data should be representative of the deployment domain; otherwise accuracy drops. ";

void LegitimateRepeatsAreNotStopped()
{
    std::printf("== legitimate local repeats\n");
    const std::vector<std::pair<std::string, std::string>> cases = {
        {"1000 in prose", "Output is 1000 classes"},
        {"2000", "Output is 2000 classes"},
        {"999", "Output is 999 classes"},
        {"0.000123", "value 0.000123"},
        {"directory 1111", "C:\\WoS_AI\\1111\\model"},
        {"0000", "id=0000"},
        {"0.0.0.0", "bind 0.0.0.0:8080"},
        {"1.1.1.1", "dns 1.1.1.1"},
        {"80 dashes", Repeat("-", 80)},
        {"100 equals", Repeat("=", 100)},
        {"table separator", "|---|---|---|---|"},
        {"blank lines", "a\n\n\nb"},
        {"16 space indent", "x" + Repeat(" ", 16) + "y"},
        {"ellipsis", "wait..."},
        {"closing brackets", "a[[[[1]]]]"},
        {"closing parens", "f(g(h(i(1))))"},
        {"zero array", "[0, 0, 0, 0, 0, 0, 0, 0]"},
        {"64 zeros", Repeat("0", 64)},
        {"100 zeros", Repeat("0", 100)},
        {"base64 tail", "QUJDAAAA=="},
        {"same code line x3", Repeat("x += 1\n", 3)},
        {"same table row x5", Repeat("| a | b |\n", 5)},
        {"sentence x2", kSentence + kSentence},
        {"paragraph x2", kParagraph + kParagraph},
    };
    for (const auto &item : cases)
    {
        const auto outcome = Run(item.second);
        Check(item.first, outcome.abort_at == 0, outcome.abort_at ? "stopped at " + std::to_string(outcome.abort_at) : "");
    }
}

void RealLoopsAreStopped()
{
    std::printf("== real degenerate loops\n");
    struct LoopCase
    {
        std::string name;
        std::string text;
        int max_abort_at;
    };
    const std::vector<LoopCase> cases = {
        {"zero x300", Repeat("0", 300), 130},
        {"newline x400", Repeat("\n", 400), 130},
        {"'ha ' x100", Repeat("ha ", 100), 50},
        {"'the ' x100", Repeat("the ", 100), 40},
        {"', ' x200", Repeat(", ", 200), 260},
        {"sentence x4", " " + Repeat(kSentence, 4), 60},
        {"paragraph x4", " " + Repeat(kParagraph, 4), 3 * static_cast<int>(Tokenize(kParagraph).size()) + 2},
    };
    for (const auto &item : cases)
    {
        const auto outcome = Run(item.text);
        Check(item.name, outcome.abort_at > 0 && outcome.abort_at <= item.max_abort_at,
              "stopped at " + std::to_string(outcome.abort_at) + " (limit " + std::to_string(item.max_abort_at) + ")");
    }
    Check("zero x300 reports period 1", Run(Repeat("0", 300)).period == 1);
}

void EdgeCases()
{
    std::printf("== edge cases\n");
    Check("disabled guard never stops", Run(Repeat("0", 1000), false).abort_at == 0);
    Check("zero max_period never stops", Run(Repeat("0", 1000), true, 0).abort_at == 0);
    Check("zero min_span never stops", Run(Repeat("0", 1000), true, 128, 0).abort_at == 0);
    Check("period above max_period is ignored",
          Run(Repeat("abc def ghi jkl mno pqr stu vwx ", 40), true, 4).abort_at == 0);
    Check("larger min_span delays the stop",
          Run(Repeat("0", 1000), true, 128, 1024).abort_at > Run(Repeat("0", 1000)).abort_at);

    degenerate_repeat_guard::StreamGuard guard(true, 8, 64);
    int aborted_at = 0;
    for (int i = 0; i < 5000 && aborted_at == 0; ++i)
    {
        const bool in_loop = i >= 4000;
        const int32_t id = in_loop ? 7 : static_cast<int32_t>((i * 2654435761u) % 100003u) + 100;
        if (guard.Observe(id, 1).abort)
        {
            aborted_at = i;
        }
    }
    Check("ring buffer wrap keeps detection exact", aborted_at >= 4000 && aborted_at <= 4000 + 64,
          "stopped at " + std::to_string(aborted_at));

    degenerate_repeat_guard::StreamGuard zero_width(true, 8, 64);
    bool stopped = false;
    for (int i = 0; i < 1000; ++i)
    {
        stopped = stopped || zero_width.Observe(5, 0).abort;
    }
    Check("zero-width pieces never trip the byte span", !stopped);
}

}

int main()
{
    LegitimateRepeatsAreNotStopped();
    RealLoopsAreStopped();
    EdgeCases();
    std::printf("\n%d checks, %d failed\n", g_total, g_failed);
    return g_failed == 0 ? 0 : 1;
}
