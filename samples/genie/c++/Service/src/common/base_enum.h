//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef BASE_ENUM_H
#define BASE_ENUM_H

struct BaseEnum
{
    BaseEnum() = default;

    constexpr BaseEnum(int v) : v_{v} {}

    constexpr BaseEnum(const BaseEnum &other) : v_{other.v_} {}

    constexpr BaseEnum &operator=(const BaseEnum &other)
    {
        v_ = other.v_;
        return *this;
    }

    constexpr bool operator==(const BaseEnum &other) const
    {
        return v_ == other.v_;
    }

    constexpr bool operator!=(const BaseEnum &other) const
    {
        return v_ != other.v_;
    }


    constexpr BaseEnum &operator|=(const BaseEnum &other)
    {
        v_ |= other.v_;
        return *this;
    }

    constexpr bool operator&(const BaseEnum &other)
    {
        return v_ & other.v_;
    }

    constexpr explicit operator int() const
    {
        return v_;
    }

protected:
    int v_{};
};

#endif //BASE_ENUM_H
