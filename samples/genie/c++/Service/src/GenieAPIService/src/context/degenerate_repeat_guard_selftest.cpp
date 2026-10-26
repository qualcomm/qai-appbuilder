#include "degenerate_repeat_guard_selftest.h"
#include "degenerate_repeat_guard.h"

#include <ostream>
#include <string>
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

}

bool RunDegenerateRepeatGuardSelfTest(std::ostream &out)
{
    SelfTestRunner runner(out);
    out << "==================== Degenerate Repeat Guard Self-Test ====================\n";

    runner.Check("no_repeat", "无重复尾部返回0",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 3, 4}, 8) == 0);
    runner.Check("period_one", "周期1二次重复命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({7, 7}, 8) == 1);
    runner.Check("period_two", "周期2二次重复命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 1, 2}, 8) == 2);
    runner.Check("period_eight", "周期8二次重复命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 3, 4, 5, 6, 7, 8,
                                                               1, 2, 3, 4, 5, 6, 7, 8}, 8) == 8);
    runner.Check("one_block_only", "只有一个周期块时不命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 3, 4}, 4) == 0);
    runner.Check("smallest_period", "多个周期同时成立时返回最小周期",
                 degenerate_repeat_guard::FindRepeatedPeriod({4, 4, 4, 4}, 4) == 1);
    runner.Check("max_period_boundary", "max_period等于可用周期时命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 3, 1, 2, 3}, 3) == 3);
    runner.Check("max_period_excludes", "超过max_period的周期不命中",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 2, 3, 1, 2, 3}, 2) == 0);
    runner.Check("empty", "空序列返回0",
                 degenerate_repeat_guard::FindRepeatedPeriod({}, 8) == 0);
    runner.Check("disabled", "非正max_period返回0",
                 degenerate_repeat_guard::FindRepeatedPeriod({1, 1}, 0) == 0);

    out << "Passed " << runner.passed() << "/" << runner.total() << "\n";
    return runner.passed() == runner.total();
}
