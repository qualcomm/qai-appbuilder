#include "degenerate_repeat_guard.h"

#include <algorithm>

namespace degenerate_repeat_guard
{

int FindRepeatedPeriod(const std::vector<int32_t> &ids, int max_period)
{
    if (max_period <= 0 || ids.size() < 2)
    {
        return 0;
    }

    const int upper_bound = std::min<int>(max_period, static_cast<int>(ids.size() / 2));
    const auto end = ids.end();
    for (int period = 1; period <= upper_bound; ++period)
    {
        const auto first = end - period * 2;
        const auto middle = end - period;
        if (std::equal(first, middle, middle, end))
        {
            return period;
        }
    }
    return 0;
}

}
