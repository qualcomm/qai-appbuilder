// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

//! The close question: Quit / Minimize to tray / (Cancel = Esc or the title-bar X), with a "remember" tick box.
//! A native TaskDialog with command links (needs the comctl32 v6 manifest, see ../build.rs + app.manifest).
use crate::i18n::T;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Choice {
    Quit,
    MinimizeToTray,
    Cancel,
}

const ID_MINIMIZE: i32 = 1001;
const ID_QUIT: i32 = 1002;

/// Ask. `remember` is true when the user ticked the box (only meaningful for Quit / MinimizeToTray).
#[cfg(windows)]
pub fn ask(t: &T) -> (Choice, bool) {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::Controls::*;
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

    fn w(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }
    // Command-link text: first line is the label, the rest is the note under it.
    let min_text = w(&format!("{}\n{}", t.btn_minimize, t.btn_minimize_note));
    let quit_text = w(&format!("{}\n{}", t.btn_quit, t.btn_quit_note));
    let title = w(t.close_title);
    let main = w(t.close_main);
    let content = w(t.close_content);
    let verify = w(t.remember);
    let buttons = [
        TASKDIALOG_BUTTON { nButtonID: ID_MINIMIZE, pszButtonText: PCWSTR(min_text.as_ptr()) },
        TASKDIALOG_BUTTON { nButtonID: ID_QUIT, pszButtonText: PCWSTR(quit_text.as_ptr()) },
    ];
    let mut cfg = TASKDIALOGCONFIG::default();
    cfg.cbSize = std::mem::size_of::<TASKDIALOGCONFIG>() as u32;
    // The dialog is owned by the window that is in front (the main window when it is visible).
    cfg.hwndParent = unsafe { GetForegroundWindow() };
    let _: HWND = cfg.hwndParent;
    cfg.dwFlags = TDF_USE_COMMAND_LINKS | TDF_ALLOW_DIALOG_CANCELLATION | TDF_POSITION_RELATIVE_TO_WINDOW;
    cfg.dwCommonButtons = TDCBF_CANCEL_BUTTON;
    cfg.pszWindowTitle = PCWSTR(title.as_ptr());
    cfg.pszMainInstruction = PCWSTR(main.as_ptr());
    cfg.pszContent = PCWSTR(content.as_ptr());
    cfg.cButtons = buttons.len() as u32;
    cfg.pButtons = buttons.as_ptr();
    cfg.nDefaultButton = ID_MINIMIZE;
    cfg.pszVerificationText = PCWSTR(verify.as_ptr());
    let mut pressed = 0i32;
    let mut checked = windows::core::BOOL(0);
    let r = unsafe { TaskDialogIndirect(&cfg, Some(&mut pressed), None, Some(&mut checked)) };
    if r.is_err() {
        // comctl32 v6 missing or the call failed: fall back to the safe, reversible choice.
        return (Choice::Cancel, false);
    }
    let choice = match pressed {
        ID_MINIMIZE => Choice::MinimizeToTray,
        ID_QUIT => Choice::Quit,
        _ => Choice::Cancel,
    };
    (choice, checked.as_bool() && choice != Choice::Cancel)
}

#[cfg(not(windows))]
pub fn ask(_t: &T) -> (Choice, bool) {
    (Choice::Quit, false)
}
