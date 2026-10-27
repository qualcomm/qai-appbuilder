//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_repetition_store_selftest.h"
#include "tool_call_repetition_store.h"
#include <vector>
#include "../model/model_config.h"

#include <chrono>
#include <string>
#include <thread>

namespace
{

class SelfTestRunner
{
public:
    explicit SelfTestRunner(std::ostream &out) : out_(out) {}

    void Check(const std::string &id, const std::string &description, bool condition)
    {
        ++total_;
        if (condition)
        {
            ++passed_;
        }
        out_ << "[" << (condition ? "PASS" : "FAIL") << "] " << id << " -- " << description << "\n";
    }

    int total() const { return total_; }
    int passed() const { return passed_; }

private:
    std::ostream &out_;
    int total_ = 0;
    int passed_ = 0;
};

void ConfigureStore(int ttl_seconds, int non_contiguous_ttl_seconds)
{
    ToolCallRepairConfig::RedundantToolCallConfig cfg;
    cfg.ttl_seconds = ttl_seconds;
    cfg.non_contiguous_ttl_seconds = non_contiguous_ttl_seconds;
    ToolCallRepetitionStore::GetInstance().Configure(cfg);
}

} // namespace

bool RunToolCallRepetitionSelfTest(std::ostream &out)
{
    auto &store = ToolCallRepetitionStore::GetInstance();
    SelfTestRunner runner(out);

    out << "==================== Tool Call Repetition Store Self-Test ====================\n";

    // ── 场景1：首次调用不触发（计数为1） ────────────────────────────────────
    {
        ConfigureStore(300, 3600);
        store.Clear();
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario1_session", "modelX");

        int c1 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario1_first_call_count", "首次调用计数应为1", c1 == 1);
        runner.Check("scenario1_first_call_not_detected", "首次调用GetRepeatCount应为1（未达重复阈值）",
                     store.GetRepeatCount(key) == 1);
    }

    // ── 场景2：相同签名第二次调用，计数累加到2（命中重复阈值） ───────────────
    {
        ConfigureStore(300, 3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario2_session", "modelX");
        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        int c2 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario2_second_identical_call_count", "相同签名第二次调用计数应为2", c2 == 2);
        runner.Check("scenario2_repeat_count_matches", "GetRepeatCount应与RecordCall返回值一致",
                     store.GetRepeatCount(key) == 2);
    }

    // ── 场景3：参数不同（签名不同）重置为1，不触发 ──────────────────────────
    {
        ConfigureStore(300, 3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario3_session", "modelX");
        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario3_pre_change_count", "切换参数前计数应为2", store.GetRepeatCount(key) == 2);

        int c3 = store.RecordCall(key, "read|{\"path\":\"b.txt\"}");
        runner.Check("scenario3_different_args_resets", "参数不同应重置计数为1", c3 == 1);
        runner.Check("scenario3_different_tool_resets", "不同工具名的签名也应重置计数为1",
                     store.RecordCall(key, "exec|{\"path\":\"b.txt\"}") == 1);
    }

    // ── 场景4：跨会话/模型不互相污染 ────────────────────────────────────────
    {
        ConfigureStore(300, 3600);
        const std::string keyA = ToolCallRepetitionStore::MakeKey("scenario4_session_same", "model_A");
        const std::string keyB = ToolCallRepetitionStore::MakeKey("scenario4_session_same", "model_B");
        const std::string keyC = ToolCallRepetitionStore::MakeKey("scenario4_session_other", "model_A");

        store.RecordCall(keyA, "read|{\"path\":\"a.txt\"}");
        store.RecordCall(keyA, "read|{\"path\":\"a.txt\"}");
        store.RecordCall(keyB, "read|{\"path\":\"a.txt\"}");
        store.RecordCall(keyC, "read|{\"path\":\"a.txt\"}");

        runner.Check("scenario4_keyA_repeated", "keyA两次相同调用计数应为2", store.GetRepeatCount(keyA) == 2);
        runner.Check("scenario4_keyB_independent", "同session不同model的keyB计数应独立为1",
                     store.GetRepeatCount(keyB) == 1);
        runner.Check("scenario4_keyC_independent", "不同session同model的keyC计数应独立为1",
                     store.GetRepeatCount(keyC) == 1);
    }

    // ── 场景5：空 key 不参与计数 ─────────────────────────────────────────────
    {
        const std::string empty_key1 = ToolCallRepetitionStore::MakeKey("", "modelX");
        const std::string empty_key2 = ToolCallRepetitionStore::MakeKey("scenario5_session", "");
        runner.Check("scenario5_make_key_empty_session", "session_key为空时MakeKey应返回空串", empty_key1.empty());
        runner.Check("scenario5_make_key_empty_model", "model_name为空时MakeKey应返回空串", empty_key2.empty());

        int r1 = store.RecordCall("", "read|{}");
        runner.Check("scenario5_record_empty_key_noop", "空key调用应返回0", r1 == 0);
        runner.Check("scenario5_empty_key_repeat_count_zero", "空key查询应返回0", store.GetRepeatCount("") == 0);
    }

    // ── 场景6：超过 ttl_seconds 后视为新序列重新计数（真实短 sleep） ─────────
    {
        ConfigureStore(/*ttl_seconds=*/1, /*non_contiguous_ttl_seconds=*/1);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario6_session", "modelX");
        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        int c2 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario6_pre_ttl_count", "TTL窗口内相同签名第二次调用计数应为2", c2 == 2);

        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario6_post_ttl_read_zero", "超过ttl_seconds后GetRepeatCount应返回0",
                     store.GetRepeatCount(key) == 0);

        int c3 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario6_post_ttl_restarts", "TTL过期后下一次调用应视为全新序列从1开始", c3 == 1);
    }

    store.Clear();

    // ── 场景7：非连续回访计数（中间穿插其它签名不清零） ──────────────────────
    {
        ConfigureStore(300, 3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario7_session", "modelX");

        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario7_first_total", "cmd_A首次出现非连续总次数应为1",
                     store.GetNonContiguousRepeatCount(key) == 1);

        store.RecordCall(key, "exec|{\"command\":\"cmd_B\"}");
        runner.Check("scenario7_other_signature_resets_contiguous", "插入cmd_B后连续计数应重置为1",
                     store.GetRepeatCount(key) == 1);
        runner.Check("scenario7_other_signature_total", "cmd_B首次出现非连续总次数应为1",
                     store.GetNonContiguousRepeatCount(key) == 1);

        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario7_revisit_total_accumulates", "绕回cmd_A后非连续总次数应累加为2",
                     store.GetNonContiguousRepeatCount(key) == 2);
        runner.Check("scenario7_revisit_contiguous_resets", "绕回cmd_A的连续计数仍应为1（未连续两次）",
                     store.GetRepeatCount(key) == 1);
    }

    // ── 场景8：非连续回访计数拥有独立于连续计数的 TTL（短连续TTL过期不清空非连续历史） ──
    {
        ConfigureStore(/*ttl_seconds=*/1, /*non_contiguous_ttl_seconds=*/300);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario8_session", "modelX");
        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        store.RecordCall(key, "exec|{\"command\":\"cmd_B\"}");
        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario8_pre_ttl_total", "短连续TTL窗口内绕回cmd_A的非连续总次数应为2",
                     store.GetNonContiguousRepeatCount(key) == 2);

        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario8_contiguous_ttl_expired", "短连续TTL过期后GetRepeatCount应返回0",
                     store.GetRepeatCount(key) == 0);
        runner.Check("scenario8_non_contiguous_survives_short_ttl",
                     "连续TTL过期不应清空非连续历史（因非连续TTL更长、尚未过期）",
                     store.GetNonContiguousRepeatCount(key) == 2);

        int c4 = store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario8_non_contiguous_accumulates_after_contiguous_expiry",
                     "连续TTL过期后再次绕回cmd_A，非连续总次数应继续累加为3",
                     store.GetNonContiguousRepeatCount(key) == 3);
        (void) c4;
    }

    store.Clear();

    // ── 场景9：非连续回访计数自身的 TTL 过期后才清空历史 ─────────────────────
    {
        ConfigureStore(/*ttl_seconds=*/300, /*non_contiguous_ttl_seconds=*/1);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario9_session", "modelX");
        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        store.RecordCall(key, "exec|{\"command\":\"cmd_B\"}");
        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario9_pre_ttl_total", "非连续TTL窗口内绕回cmd_A的非连续总次数应为2",
                     store.GetNonContiguousRepeatCount(key) == 2);

        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario9_post_ttl_zero", "超过non_contiguous_ttl_seconds后GetNonContiguousRepeatCount应返回0",
                     store.GetNonContiguousRepeatCount(key) == 0);

        store.RecordCall(key, "exec|{\"command\":\"cmd_A\"}");
        runner.Check("scenario9_restarts_after_ttl", "非连续TTL过期后下一次调用应视为全新历史从1开始（用GetNonContiguousRepeatCount校验，RecordCall返回值反映的是另一把连续性时钟，此处不适用）",
                     store.GetNonContiguousRepeatCount(key) == 1);
    }

    store.Clear();

    {
        ConfigureStore(300, 3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario10_session", "modelX");
        for (int i = 0; i < 10; ++i)
            store.RecordCall(key, "read|{\"path\":\"hot.txt\"}");
        store.RecordCall(key, "read|{\"path\":\"other.txt\"}");
        runner.Check("scenario10_total_follows_latest_signature",
                     "热点签名之后插入一次性签名，非连续总次数应跟随最近一次调用的签名（1），而不是历史最大值",
                     store.GetNonContiguousRepeatCount(key) == 1);
        store.RecordCall(key, "read|{\"path\":\"hot.txt\"}");
        runner.Check("scenario10_hot_signature_revisit_total",
                     "绕回热点签名后应返回其累计总次数11",
                     store.GetNonContiguousRepeatCount(key) == 11);
    }

    {
        const std::vector<std::string> loop = {"a", "b", "c", "a", "b", "a", "b", "a", "b"};
        const std::vector<std::string> healthy = {"a", "b", "c", "d", "e", "f"};
        runner.Check("scenario11_cycle_window_counts_repeats", "the recent window counts calls seen anywhere earlier",
                     ToolCallRepetitionStore::CountRepeatsInWindow(loop, 6) == 6);
        runner.Check("scenario11_cycle_window_ignores_old_calls", "calls outside the recent window are not counted",
                     ToolCallRepetitionStore::CountRepeatsInWindow(loop, 3) == 3);
        runner.Check("scenario11_cycle_window_healthy_zero", "a non-repeating recent window does not trigger",
                     ToolCallRepetitionStore::CountRepeatsInWindow(healthy, 6) == 0);

        std::vector<std::string> lap;
        for (int i = 0; i < 22; ++i)
            lap.push_back("call" + std::to_string(i));
        runner.Check("scenario11_cycle_first_lap_zero", "the first pass through a long sequence is not a repeat",
                     ToolCallRepetitionStore::CountRepeatsInWindow(lap, 6) == 0);
        std::vector<std::string> replay = lap;
        for (int i = 0; i < 3; ++i)
            replay.push_back(lap[i]);
        runner.Check("scenario11_cycle_long_period_below_threshold", "three replayed calls stay below the threshold of four",
                     ToolCallRepetitionStore::CountRepeatsInWindow(replay, 6) == 3);
        replay.push_back(lap[3]);
        runner.Check("scenario11_cycle_long_period_detected", "a period-22 replay reaches the threshold on the fourth replayed call",
                     ToolCallRepetitionStore::CountRepeatsInWindow(replay, 6) == 4);
    }

    store.Clear();

    // ── 场景12：PeekRepeatCount 只读预判，不写入计数状态 ─────────────────────
    {
        ConfigureStore(300, 3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario12_session", "modelX");

        runner.Check("scenario12_peek_unseen_key_is_one", "从未出现过的 key 预判应为1（等同首次调用）",
                     store.PeekRepeatCount(key, "read|{\"path\":\"a.txt\"}") == 1);

        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario12_peek_matches_next_record", "预判同一签名下一次应为2，且与RecordCall真实返回值一致",
                     store.PeekRepeatCount(key, "read|{\"path\":\"a.txt\"}") == 2);
        runner.Check("scenario12_peek_does_not_mutate", "多次预判不应改变已记录的连续计数",
                     store.GetRepeatCount(key) == 1);

        int c = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario12_peek_was_accurate", "预判值应与真正记录后的返回值一致", c == 2);

        runner.Check("scenario12_peek_different_signature_resets", "预判不同签名应为1（视作全新连续序列）",
                     store.PeekRepeatCount(key, "read|{\"path\":\"b.txt\"}") == 1);
    }

    // ── 场景13：PeekRepeatCount 不受连续性 TTL 影响（本地慢速模型单轮生成可能耗时
    // 数分钟甚至超过 ttl_seconds，仍应正确识别出这是真实重复，而不是被旧的按秒数
    // 过期的逻辑误判成"已经是全新的一轮"） ─────────────────────────────────
    {
        ConfigureStore(/*ttl_seconds=*/1, /*non_contiguous_ttl_seconds=*/3600);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario13_session", "modelX");
        store.RecordCall(key, "exec|{\"command\":\"slow_cmd\"}");
        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario13_peek_survives_slow_round",
                     "即使耗时已超过 ttl_seconds（模拟慢速本地模型单轮生成），预判仍应视为真实连续重复",
                     store.PeekRepeatCount(key, "exec|{\"command\":\"slow_cmd\"}") == 2);
    }

    store.Clear();

    out << "=================================================================================\n";
    out << "Total cases: " << runner.total() << ", passed: " << runner.passed()
        << " (" << (runner.total() > 0 ? (100.0 * runner.passed() / runner.total()) : 100.0) << "%)\n";
    out << "=================================================================================\n";

    return runner.passed() == runner.total();
}
