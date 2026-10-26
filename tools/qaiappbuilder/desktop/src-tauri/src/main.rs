// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// QAI ModelBuilder desktop shell. The window is only a container: it loads the SPA the
// backend already serves (http://localhost:<port>/), so the frontend is not changed at all.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
fn main() {
    qai_desktop_lib::run();
}
