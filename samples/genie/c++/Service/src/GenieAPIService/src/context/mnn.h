//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _MNNBUILDER_H
#define _MNNBUILDER_H

#include "context_base.h"

class MNNContext : public ContextBase
{
public:
    explicit MNNContext(const IModelConfig &info);

    ~MNNContext() override;

    bool Query(const ModelInput &, const Callback& callback) override;

    bool Stop() override;

    json HandleProfile() override;

private:
    class Impl;
    Impl* impl_;
    bool m_isLoaded = false;
    bool m_stop = false;
};

#endif
