# env_check.ps1 — onnx-to-htp pre-flight environment probe
# 合并自 qnn-onnx-to-htp-small/env_check.ps1 + qnn-onnx-to-htp-v3 的环境探测部分。
# 一次性探测 QAIRT SDK 目录布局、Python 候选、cmake、VS 编译器，避免"环境发现"
# 变成实际瓶颈（qnn-onnx-to-htp-small/LESSONS.md 记录的最大教训）。
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File env_check.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File env_check.ps1 `
#       -QairtRoot C:\Qualcomm\AIStack\QAIRT\2.48.40.260702 `
#       -Python C:\venv310x\Scripts\python.exe
#
# NOTE: always run with -File. Inline `powershell -Command "..."` strings lose
# `$_`/`$var` when passed through cmd.exe (see SKILL.md 坑表 #6).
# This file is ASCII-only on purpose (PS 5.1 reads BOM-less .ps1 as GBK on some hosts).

param(
  [string]$QairtRoot = "C:\Qualcomm\AIStack\QAIRT\2.48.40.260702",
  [string]$Python    = "",    # optional: check only this interpreter
  [int]$ScanDepth    = 6      # depth for python.exe fallback scan (deeper = much slower)
)
$ErrorActionPreference = "Continue"

Write-Host "=== 1. QAIRT SDK dirs ==="
if (-not (Test-Path $QairtRoot)) {
  Write-Host "  QairtRoot NOT FOUND: $QairtRoot"
} else {
  Write-Host "  QairtRoot OK: $QairtRoot"
}
$need = @(
  "bin\arm64x-windows-msvc", "bin\aarch64-windows-msvc", "bin\x86_64-windows-msvc",
  "lib\arm64x-windows-msvc", "lib\aarch64-windows-msvc", "lib\x86_64-windows-msvc",
  "lib\hexagon-v81", "lib\hexagon-v79", "lib\hexagon-v75", "lib\hexagon-v73", "lib\hexagon-v68",
  "lib\python"
)
foreach ($d in $need) {
  $e = Test-Path (Join-Path $QairtRoot $d)
  Write-Host ("  {0} : {1}" -f $d, $e)
}

Write-Host ""
Write-Host "=== 2. converter pyd versions (Python interpreter must match a suffix) ==="
$be = Join-Path $QairtRoot "lib\python\qti\aisw\converters\backend"
if (Test-Path $be) {
  Get-ChildItem $be -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $pyds = Get-ChildItem $_.FullName -Filter "*.pyd" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
    Write-Host ("  {0} : {1}" -f $_.Name, ($pyds -join ", "))
  }
} else {
  Write-Host "  (backend dir not found under lib\python -- SDK layout may differ by version)"
}

Write-Host ""
Write-Host "=== 3. Python candidates (version + key packages) ==="
$cands = @()
if ($Python) {
  $cands += $Python
} else {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py) { $cands += $py.Source; Write-Host "  (PATH python: $($py.Source) -- verify it matches a pyd suffix above!)" }
  $pyl = Get-Command py -ErrorAction SilentlyContinue
  if ($pyl) {
    try {
      $list = & py -0p 2>$null
      foreach ($line in $list) {
        if ($line -match '(\S+\.exe)\s*$') { $cands += $Matches[1] }
      }
    } catch {}
  }
  # bounded fallback scan (common venv locations only, not a full disk scan)
  foreach ($root in @("C:\Users", "C:\Qualcomm", "C:\WoS_AI")) {
    if (Test-Path $root) {
      Get-ChildItem -Path $root -Recurse -Depth $ScanDepth -Filter "python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "Scripts\\python\.exe$" -or $_.FullName -match "envs\\.*\\python\.exe$" } |
        Select-Object -First 10 -ExpandProperty FullName |
        ForEach-Object { $cands += $_ }
    }
  }
}
$cands = $cands | Select-Object -Unique
foreach ($c in $cands) {
  if (-not (Test-Path $c)) { continue }
  Write-Host "  --- $c ---"
  try {
    $ver = & $c --version 2>&1
    Write-Host "    version: $ver"
    $pkgs = & $c -c "
import importlib
for p in ['onnx','onnxruntime','numpy','cmake']:
    try:
        m = importlib.import_module(p)
        print(f'    pkg: {p:20s} {getattr(m, chr(95)+chr(95)+\"version\"+chr(95)+chr(95), \"?\")}')
    except ImportError:
        print(f'    pkg: {p:20s} NOT INSTALLED')
" 2>&1
    Write-Host $pkgs
  } catch {
    Write-Host "    (failed to query: $_)"
  }
}

Write-Host ""
Write-Host "=== 4. cmake / compiler on PATH ==="
foreach ($c in @("cmake", "clang-cl", "cl")) {
  $g = Get-Command $c -ErrorAction SilentlyContinue
  if ($g) { Write-Host "  $c : $($g.Source)" } else { Write-Host "  $c : NOT on PATH (run_qnn_deploy.ps1 auto-fixes this via -Cmake/-VsRoot or vcvarsall)" }
}

Write-Host ""
Write-Host "=== 5. VS install roots (for auto compiler search) ==="
foreach ($v in @(
  "C:\Program Files\Microsoft Visual Studio\2022",
  "C:\Program Files (x86)\Microsoft Visual Studio\2019"
)) {
  Write-Host ("  {0} : {1}" -f $v, (Test-Path $v))
}

Write-Host ""
Write-Host "=== 6. VTCM hint (informational -- decides FP32 vs FP16 feasibility) ==="
Write-Host "  VTCM budget is only known from a real qnn-net-run attempt's netrun.log"
Write-Host "  (look for a line like 'VTCM: total_sz=<bytes>'). This script does not"
Write-Host "  probe it directly -- run_qnn_deploy.ps1's auto-fallback handles it at runtime."

Write-Host ""
Write-Host "=== Done. Copy all output above and use it to fill run_qnn_deploy.ps1 params. ==="
