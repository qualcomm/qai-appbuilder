---
skill_id: sdk-integrity-recovery
tier: base
triggers: ["0-byte qnn-context-binary-generator.exe", "WinError 193", "需要修改 SDK 文件", "B9", "SDK 文件损坏"]
sources: ["SKILL.md 179-217", "SKILL.md B8/B9"]
---

# SDK Integrity & Recovery (base)

> 🧭 通用诊断骨架见 [`../_diagnosis-framework.md`](../_diagnosis-framework.md)；本 SKILL 是"SDK 写保护/损坏恢复"领域的症状库。

> 🔴 **This is the most dangerous skill. The default answer to "modify an SDK file" is NO.**
> The QAIRT SDK (`C:\Qualcomm\AIStack\QAIRT\<version>\` = `$QAIRT_SDK_ROOT`/`$QNN_SDK_ROOT`) is a
> shared third-party install. Editing it corrupts shared state. **Never** treat an SDK file edit as a fix.
> If a file is damaged, RECOVER from a kept backup — do not "repair/regenerate" it.

## Responsibility

Enforce "Do Not Modify QAIRT SDK Files" (B9), diagnose a damaged `qnn-context-binary-generator.exe`
(0-byte / `WinError 193`) **read-only**, and recover the single damaged file from the kept SDK zip /
launcher-script backup without a ~2 GB reinstall.

## Trigger signals

- `[WinError 193] %1 is not a valid Win32 application` when launching the generator
- A generator/tool is 0-byte or corrupt
- You are about to conclude "the fix requires editing an SDK file" → **STOP, this is B9**

## Core knowledge

### Do NOT modify any file under the QAIRT SDK (hard B9)

Applies to: `.exe`/`.dll`/`.so`/`.lib`/`.cat`, all SDK-shipped Python modules, backend-extension JSON/config/headers, HTP runtime files.
Concluding "the fix needs an SDK file change" is **itself B9** → stop and ask the user for explicit, scoped permission citing the exact file path. Record approval (file + user msg + timestamp) before touching anything.

**Forbidden** (incl. via sub-agent/sub-shell): silent edits, "temporary" patches, hot-fix DLL replacement, rewriting SDK Python sources, in-place schema edits, any command writing into the SDK tree (`Copy-Item -Destination $SDK/...`, `del`, `Out-File`, `Move-Item`, `xcopy /Y`, `pip install --target=$SDK/...`, `python setup.py install` against SDK, `git apply`/`patch` under SDK), re-asking after "no" with reworded prompts.

**Correct workaround:** copy the SDK file into `${WORKDIR}`/workspace `output/`, edit the **copy**, point tooling at it via overrides (`--config_file`, `QNN_*` env vars, CLI flags, workspace-local `backend_extensions.json`). No override exists → escalate B9.

**Pre-flight check before every write/exec** — does the absolute write target start with `$QAIRT_SDK_ROOT` / `$QNN_SDK_ROOT` / `C:/Qualcomm/AIStack/QAIRT/...`? If yes → **STOP, trigger B9.** Three escape hatches:
1. **Relative path** — resolve against CWD first; `>`/`>>` clears target so failure leaves 0-byte file.
2. **CWD inside SDK** — all relative writes land in SDK; never set CWD inside SDK for write ops. Copy HTP runtime into **workspace** and run there.
3. **Indirect commands** — `cmd /c "... > file"`, sub-agents, `.bat`, Python `open(p,"w")`/`subprocess` write targets all subject to check.

> ✅ **Reading the SDK is allowed** (`dir`, `ls`, `read`, `grep`). After the op, did any SDK file's content/timestamp/size change? No → allowed. Yes → B9, stop.

The `C:\Qualcomm` tree is **write-protected at the tool layer** (ALWAYS ON): `write`/`edit`/`apply_patch`, `exec` write targets, and Python child-process writes are denied automatically.

### Diagnosing `WinError 193` / 0-byte generator (READ-ONLY only)

> 🔴 **Real incident (2026-06-16):** `bin/aarch64-windows-msvc/qnn-context-binary-generator.exe` overwritten to 0 bytes, then Step 3 reported `WinError 193`. Root cause: **command output landed on that exe** — exactly what B9 prevents. Most 0-byte generators are **damaged by overwrite**, not factory defects.

- **Do NOT misjudge `WinError 193` as "x64 Python cannot spawn ARM64 exe."** x64→ARM64 `subprocess`/`CreateProcess` works (both pipelines rely on this). Here `WinError 193` = exe itself 0-byte/corrupt. Do NOT "fix" by switching to `cmd /c` or altering the chain.
- **Diagnose read-only:** `Get-Item ... | Select Length`. If 0-byte → fall back per B8, then recover (below). Valid exe: non-zero, starts with `MZ`; launcher script: readable text starting with `#!`.

### Self-heal (automatic)

`qai_dev_gen_contextbin.py` self-heals before launch: re-extracts just the damaged exe from the kept SDK zip (`data/sdk/qairt/v<version>.zip` or `vendor/qairt/v<version>.zip`), then file-level backup — no 2 GB reinstall. If no usable zip → do Manual recovery.

### Manual SDK file recovery (fallback)

Two repair sources kept OUTSIDE `C:\Qualcomm`. Recover single file, then retry:

1. **Find kept SDK zip** (first that exists): `${APP_ROOT}\data\sdk\qairt\v<version>.zip` or `${APP_ROOT}\vendor\qairt\v<version>.zip`. `<version>` = basename of `$QAIRT_SDK_ROOT`.
2. **Identify damaged file's relative path** (e.g. `bin/aarch64-windows-msvc/qnn-context-binary-generator.exe`); inside zip nested under `QAIRT/<version>/`.
3. **Re-extract ONLY that file:**
   ```powershell
   $zip = "${APP_ROOT}\data\sdk\qairt\v<version>.zip"
   $suffix = "bin/aarch64-windows-msvc/qnn-context-binary-generator.exe"
   Add-Type -AssemblyName System.IO.Compression.FileSystem
   $za = [System.IO.Compression.ZipFile]::OpenRead($zip)
   $e = $za.Entries | Where-Object { $_.FullName.ToLower().EndsWith($suffix.ToLower()) } | Select-Object -First 1
   [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, "$env:QAIRT_SDK_ROOT\$($suffix -replace '/','\')", $true)
   $za.Dispose()
   ```
   > Set `QAI_PROTECTED_PATHS_BYPASS=1` ONLY around this single extract, then clear immediately.
4. **Damaged launcher script** (`qnn-onnx-converter`, `qairt-converter`, `qairt-quantizer`, `qnn-model-lib-generator`) — backup at `${APP_ROOT}\data\sdk\qairt-scripts\<arch>\<name>`; copy back (same bypass). Missing → extract from zip as step 3.
4b. **Damaged `qnn-context-binary-generator.exe`** — also backed up to `${APP_ROOT}\data\sdk\qairt-scripts\aarch64-windows-msvc\qnn-context-binary-generator.exe` (~4 MB, faster). Copy back (same bypass).
5. **Verify (READ-ONLY)** then re-run pipeline. If no zip and no backup → STOP, ask user to re-run `Setup.bat`.

## Related Blocking Conditions

- **B9** — fix needs editing SDK file → **STOP.** Document path + proposed change + root cause. Recover from zip/backup. Ask: *"This needs editing `<sdk_path>/<file>`. May I proceed? [y/N]"*. Act only after scoped **yes**.
- **B8** — context binary generation fails on Windows ARM. 0-byte generator = damaged SDK file → self-heal / manual recovery. Diagnose READ-ONLY.

## Escalation path

Recovery requiring writing into SDK beyond the single sanctioned extract, or no backup exists → STOP and ask (B9). Never edit, copy-over, rename, delete, or "regenerate" an SDK file as workaround.
