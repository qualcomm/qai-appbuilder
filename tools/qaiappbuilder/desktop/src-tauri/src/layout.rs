//! Where things are: the app root (the tree with `src/`, `apps/` and `factory/`), the data root, the venv
//! python and the backend port. Mirrors what `Start.bat` does so both launchers use the same environment.
use std::path::{Path, PathBuf};

/// Walk up from the exe until a directory holds `src/`, `apps/` and `factory/config/ports.json`.
pub fn find_app_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent()?.to_path_buf();
    for _ in 0..8 {
        if dir.join("src").is_dir() && dir.join("apps").is_dir() && dir.join("factory/config/ports.json").is_file() {
            return Some(dir);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

/// `data/config/host_arch` (first word) -> "arm64" | "x64"; without the file, the OS architecture.
pub fn host_arch(root: &Path) -> &'static str {
    if let Ok(s) = std::fs::read_to_string(root.join("data/config/host_arch")) {
        match s.split_whitespace().next().map(|w| w.to_ascii_lowercase()).as_deref() {
            Some("x64") => return "x64",
            Some("arm64") => return "arm64",
            _ => {}
        }
    }
    if cfg!(target_arch = "aarch64") { "arm64" } else { "x64" }
}

/// `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_<arch>_313\Scripts\python.exe`, or None when it does not exist.
pub fn venv_python(root: &Path) -> Option<PathBuf> {
    let local = std::env::var_os("LOCALAPPDATA")?;
    let p = PathBuf::from(local)
        .join("QAIModelBuilder")
        .join("envs")
        .join(format!(".venv_{}_313", host_arch(root)))
        .join("Scripts")
        .join("python.exe");
    p.is_file().then_some(p)
}

/// `"backend": <n>` from factory/config/ports.json (8989 when unreadable). The port is pinned by the Okta SSO
/// redirect_uri, so the shell never picks another one.
pub fn backend_port(root: &Path) -> u16 {
    std::fs::read_to_string(root.join("factory/config/ports.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| v.get("backend").and_then(|b| b.as_u64()))
        .and_then(|n| u16::try_from(n).ok())
        .unwrap_or(8989)
}

pub fn log_dir(root: &Path) -> PathBuf {
    root.join("data").join("logs")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_arch_reads_first_word() {
        let d = std::env::temp_dir().join(format!("qai_layout_t_{}", std::process::id()));
        std::fs::create_dir_all(d.join("data/config")).unwrap();
        std::fs::write(d.join("data/config/host_arch"), "x64 extra\n").unwrap();
        assert_eq!(host_arch(&d), "x64");
        std::fs::write(d.join("data/config/host_arch"), "ARM64\n").unwrap();
        assert_eq!(host_arch(&d), "arm64");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn port_falls_back_to_8989() {
        assert_eq!(backend_port(Path::new("Z:/does/not/exist")), 8989);
    }
}
