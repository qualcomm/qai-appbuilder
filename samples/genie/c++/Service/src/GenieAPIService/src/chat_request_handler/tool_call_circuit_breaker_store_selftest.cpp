//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "tool_call_circuit_breaker_store_selftest.h"
#include "tool_call_circuit_breaker_store.h"
#include "../model/model_config.h"

#include <chrono>
#include <string>
#include <thread>
#include <vector>

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

void ConfigureStore(int threshold, int cooldown_seconds)
{
    ToolCallRepairConfig::CircuitBreakerConfig cfg;
    cfg.consecutive_layer3_threshold = threshold;
    cfg.cooldown_seconds = cooldown_seconds;
    ToolCallCircuitBreakerStore::GetInstance().Configure(cfg);
}

} // namespace

bool RunToolCallCircuitBreakerSelfTest(std::ostream &out)
{
    auto &store = ToolCallCircuitBreakerStore::GetInstance();
    SelfTestRunner runner(out);

    out << "==================== Tool Call Circuit Breaker Self-Test ====================\n";

    // ── 场景1：单 session+model 连续触发达阈值 → 降级；冷却窗口内继续累加 ──────
    {
        ConfigureStore(/*threshold=*/3, /*cooldown_seconds=*/300);
        store.Clear();
        const std::string key = ToolCallCircuitBreakerStore::MakeKey("scenario1_session", "modelX");

        int c1 = store.RecordLayer3Trigger(key);
        runner.Check("scenario1_first_trigger_count", "首次触发计数应为1", c1 == 1);
        runner.Check("scenario1_first_trigger_no_downgrade", "首次触发未达阈值不应降级",
                     !store.ShouldDowngradeToolDeclaration(key));

        store.RecordLayer3Trigger(key);
        runner.Check("scenario1_second_trigger_no_downgrade", "第二次触发仍未达阈值不应降级",
                     !store.ShouldDowngradeToolDeclaration(key));

        int c3 = store.RecordLayer3Trigger(key);
        runner.Check("scenario1_third_trigger_count", "第三次触发计数应为3", c3 == 3);
        runner.Check("scenario1_third_trigger_downgrade", "连续3次达阈值应触发降级",
                     store.ShouldDowngradeToolDeclaration(key));

        int c4 = store.RecordLayer3Trigger(key);
        runner.Check("scenario1_fourth_trigger_keeps_accumulating", "冷却窗口内继续触发应继续累加而非重置",
                     c4 == 4);
        runner.Check("scenario1_fourth_trigger_still_downgrade", "超过阈值后仍应保持降级",
                     store.ShouldDowngradeToolDeclaration(key));
    }

    // ── 场景2：RecordSuccess 清零打断连续序列 ─────────────────────────────
    {
        ConfigureStore(3, 300);
        const std::string key = ToolCallCircuitBreakerStore::MakeKey("scenario2_session", "modelX");
        store.RecordLayer3Trigger(key);
        store.RecordLayer3Trigger(key);
        store.RecordLayer3Trigger(key);
        runner.Check("scenario2_pre_success_downgrade", "触发3次后应处于降级状态",
                     store.ShouldDowngradeToolDeclaration(key));

        store.RecordSuccess(key);
        runner.Check("scenario2_post_success_no_downgrade", "Layer0/1/2成功后应清零并解除降级",
                     !store.ShouldDowngradeToolDeclaration(key));

        int c = store.RecordLayer3Trigger(key);
        runner.Check("scenario2_post_success_next_trigger_restarts_from_one", "清零后下一次触发应从1重新计数",
                     c == 1);
    }

    // ── 场景3：不同session/模型交错，计数归属互不干扰 ──────────────────────
    {
        ConfigureStore(/*threshold=*/2, 300);
        const std::string keyA = ToolCallCircuitBreakerStore::MakeKey("scenario3_session_same", "model_A");
        const std::string keyB = ToolCallCircuitBreakerStore::MakeKey("scenario3_session_same", "model_B"); // 同session不同model
        const std::string keyC = ToolCallCircuitBreakerStore::MakeKey("scenario3_session_other", "model_A"); // 不同session同model

        store.RecordLayer3Trigger(keyA);
        store.RecordLayer3Trigger(keyA); // keyA: 2次 -> 达阈值
        store.RecordLayer3Trigger(keyB); // keyB: 1次
        store.RecordLayer3Trigger(keyC); // keyC: 1次

        runner.Check("scenario3_keyA_downgrade", "keyA连续2次达阈值应降级", store.ShouldDowngradeToolDeclaration(keyA));
        runner.Check("scenario3_keyB_not_downgrade", "同session不同model的keyB计数应独立，未达阈值不应降级",
                     !store.ShouldDowngradeToolDeclaration(keyB));
        runner.Check("scenario3_keyC_not_downgrade", "不同session同model的keyC计数应独立，未达阈值不应降级",
                     !store.ShouldDowngradeToolDeclaration(keyC));

        store.RecordLayer3Trigger(keyB); // keyB: 2次 -> 达阈值
        runner.Check("scenario3_keyB_downgrade_after_second", "keyB第二次触发后应独立达阈值降级",
                     store.ShouldDowngradeToolDeclaration(keyB));
        runner.Check("scenario3_keyC_still_not_downgrade", "keyC不受keyA/keyB影响仍不应降级",
                     !store.ShouldDowngradeToolDeclaration(keyC));
        runner.Check("scenario3_keyA_still_downgrade", "keyA不受keyB/keyC影响仍应保持降级",
                     store.ShouldDowngradeToolDeclaration(keyA));
    }

    // ── 场景4：空 key（无法可靠归属会话/模型）不参与熔断计数 ────────────────
    {
        const std::string empty_key1 = ToolCallCircuitBreakerStore::MakeKey("", "modelX");
        const std::string empty_key2 = ToolCallCircuitBreakerStore::MakeKey("scenario4_session", "");
        runner.Check("scenario4_make_key_empty_session", "session_key为空时MakeKey应返回空串", empty_key1.empty());
        runner.Check("scenario4_make_key_empty_model", "model_name为空时MakeKey应返回空串", empty_key2.empty());

        int r1 = store.RecordLayer3Trigger("");
        runner.Check("scenario4_record_empty_key_noop", "空key触发不应计数，应返回0", r1 == 0);
        store.RecordLayer3Trigger("");
        store.RecordLayer3Trigger("");
        runner.Check("scenario4_empty_key_never_downgrade", "空key无论触发多少次都不应降级（查无记录）",
                     !store.ShouldDowngradeToolDeclaration(""));
        store.RecordSuccess(""); // 不应崩溃
        runner.Check("scenario4_record_success_empty_key_no_crash", "空key调用RecordSuccess不应崩溃", true);
    }

    // ── 场景5：并发多线程触发同一 key，验证无丢更新（mutex 正确性） ─────────
    {
        const std::string key = ToolCallCircuitBreakerStore::MakeKey("scenario5_session", "modelX");
        constexpr int kThreadCount = 20;

        ConfigureStore(/*threshold=*/kThreadCount, 300);
        store.Clear();

        std::vector<std::thread> threads;
        threads.reserve(kThreadCount);
        for (int i = 0; i < kThreadCount; ++i)
        {
            threads.emplace_back([&store, &key]() { store.RecordLayer3Trigger(key); });
        }
        for (auto &t : threads)
        {
            t.join();
        }

        runner.Check("scenario5_concurrent_lower_bound",
                     "20个并发触发后计数应达到20（threshold=20下应降级，证明count>=20）",
                     store.ShouldDowngradeToolDeclaration(key));

        // 复用同一 key（elapsed 仍在冷却窗口内），把阈值调高到 21 后重新查询：
        // 若之前存在"丢更新"（count<20），上面的下界断言已能揭示；若存在"重复计数"
        // （count>20），则本断言会失败——两条断言夹逼出 count 恰好等于 20，
        // 即 20 次并发调用无一丢失、无一重复，mutex 保护正确。
        ConfigureStore(/*threshold=*/kThreadCount + 1, 300);
        runner.Check("scenario5_concurrent_upper_bound",
                     "阈值调至21后不应降级（证明count<=20，与下界夹逼出count恰好等于20，无丢更新/无重复计数）",
                     !store.ShouldDowngradeToolDeclaration(key));
    }

    // ── 场景6：冷却窗口过期后自动解除熔断并重新计数（真实短 sleep） ─────────
    {
        ConfigureStore(/*threshold=*/3, /*cooldown_seconds=*/1);
        const std::string key = ToolCallCircuitBreakerStore::MakeKey("scenario6_session", "modelX");
        store.RecordLayer3Trigger(key);
        store.RecordLayer3Trigger(key);
        store.RecordLayer3Trigger(key);
        runner.Check("scenario6_pre_cooldown_downgrade", "冷却窗口内连续3次触发应降级",
                     store.ShouldDowngradeToolDeclaration(key));

        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario6_post_cooldown_auto_clear", "超过cooldown_seconds后查询应自动解除熔断",
                     !store.ShouldDowngradeToolDeclaration(key));

        int c = store.RecordLayer3Trigger(key);
        runner.Check("scenario6_post_cooldown_next_trigger_restarts", "冷却过期后下一次触发应视为全新序列从1开始",
                     c == 1);
        runner.Check("scenario6_post_cooldown_next_trigger_no_downgrade", "重新计数后未达阈值不应降级",
                     !store.ShouldDowngradeToolDeclaration(key));
    }

    // ── 场景7：GetConsecutiveCount() 只读旁路接口（供 TaskMemoBuilder 查询） ─────
    {
        ConfigureStore(/*threshold=*/3, /*cooldown_seconds=*/1);
        store.Clear();

        runner.Check("scenario7_empty_key_returns_zero", "空key应返回0", store.GetConsecutiveCount("") == 0);

        const std::string unknown_key = ToolCallCircuitBreakerStore::MakeKey("scenario7_unknown_session", "modelX");
        runner.Check("scenario7_unseen_key_returns_zero", "查无记录的key应返回0",
                     store.GetConsecutiveCount(unknown_key) == 0);

        const std::string key = ToolCallCircuitBreakerStore::MakeKey("scenario7_session", "modelX");
        store.RecordLayer3Trigger(key);
        store.RecordLayer3Trigger(key);
        int c = store.RecordLayer3Trigger(key);
        runner.Check("scenario7_count_matches_trigger_return", "GetConsecutiveCount应与RecordLayer3Trigger的返回值一致",
                     store.GetConsecutiveCount(key) == c);

        int first_read = store.GetConsecutiveCount(key);
        int second_read = store.GetConsecutiveCount(key);
        runner.Check("scenario7_readonly_does_not_mutate", "重复查询不应改变计数（只读，不mutate任何状态）",
                     first_read == second_read && second_read == c);
        runner.Check("scenario7_readonly_does_not_affect_downgrade", "只读查询不应影响ShouldDowngradeToolDeclaration的判定",
                     store.ShouldDowngradeToolDeclaration(key));

        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        runner.Check("scenario7_post_cooldown_returns_zero", "超过cooldown_seconds后应返回0（与ShouldDowngradeToolDeclaration的自动解除熔断语义一致）",
                     store.GetConsecutiveCount(key) == 0);
    }

    // 自测结束后清空，避免残留状态影响宿主进程后续行为（若该 CLI 分支之后未直接退出）
    store.Clear();

    out << "=============================================================================\n";
    out << "Total cases: " << runner.total() << ", passed: " << runner.passed()
        << " (" << (runner.total() > 0 ? (100.0 * runner.passed() / runner.total()) : 100.0) << "%)\n";
    out << "=============================================================================\n";

    return runner.passed() == runner.total();
}
