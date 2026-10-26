// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

//! `desktop-settings.json` under the data dir: the remembered answer to the close question.
//! `close_action`: "ask" (default) | "quit" | "tray".
use std::path::{Path, PathBuf};

fn file(root: &Path) -> PathBuf {
    root.join("data").join("config").join("desktop-settings.json")
}

pub fn load_close_action(root: &Path) -> String {
    std::fs::read_to_string(file(root))
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| v.get("close_action").and_then(|a| a.as_str().map(str::to_owned)))
        .filter(|a| a == "quit" || a == "tray" || a == "ask")
        .unwrap_or_else(|| "ask".to_string())
}

pub fn save_close_action(root: &Path, action: &str) {
    let f = file(root);
    let mut v = std::fs::read_to_string(&f)
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| serde_json::json!({}));
    v["close_action"] = serde_json::Value::String(action.to_string());
    if let Some(d) = f.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let _ = std::fs::write(&f, serde_json::to_string_pretty(&v).unwrap_or_default());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_and_default() {
        let d = std::env::temp_dir().join(format!("qai_settings_t_{}", std::process::id()));
        assert_eq!(load_close_action(&d), "ask");
        save_close_action(&d, "tray");
        assert_eq!(load_close_action(&d), "tray");
        save_close_action(&d, "bogus-value-is-stored-but-ignored-on-read");
        assert_eq!(load_close_action(&d), "ask");
        std::fs::remove_dir_all(&d).ok();
    }
}
