//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_repetition_store_selftest.h"
#include "tool_call_repetition_store.h"
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

void ConfigureStore(int ttl_seconds)
{
    ToolCallRepairConfig::RedundantToolCallConfig cfg;
    cfg.ttl_seconds = ttl_seconds;
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
        ConfigureStore(300);
        store.Clear();
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario1_session", "modelX");

        int c1 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario1_first_call_count", "首次调用计数应为1", c1 == 1);
        runner.Check("scenario1_first_call_not_detected", "首次调用GetRepeatCount应为1（未达重复阈值）",
                     store.GetRepeatCount(key) == 1);
    }

    // ── 场景2：相同签名第二次调用，计数累加到2（命中重复阈值） ───────────────
    {
        ConfigureStore(300);
        const std::string key = ToolCallRepetitionStore::MakeKey("scenario2_session", "modelX");
        store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        int c2 = store.RecordCall(key, "read|{\"path\":\"a.txt\"}");
        runner.Check("scenario2_second_identical_call_count", "相同签名第二次调用计数应为2", c2 == 2);
        runner.Check("scenario2_repeat_count_matches", "GetRepeatCount应与RecordCall返回值一致",
                     store.GetRepeatCount(key) == 2);
    }

    // ── 场景3：参数不同（签名不同）重置为1，不触发 ──────────────────────────
    {
        ConfigureStore(300);
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
        ConfigureStore(300);
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
        ConfigureStore(/*ttl_seconds=*/1);
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

    out << "=================================================================================\n";
    out << "Total cases: " << runner.total() << ", passed: " << runner.passed()
        << " (" << (runner.total() > 0 ? (100.0 * runner.passed() / runner.total()) : 100.0) << "%)\n";
    out << "=================================================================================\n";

    return runner.passed() == runner.total();
}
