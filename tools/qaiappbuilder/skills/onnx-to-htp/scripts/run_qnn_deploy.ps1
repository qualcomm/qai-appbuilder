# run_qnn_deploy.ps1 -- ONNX -> QNN -> HTP NPU one-click deploy + verify
# Merged from qnn-onnx-to-htp-small (FP32 bit-exact path) + qnn-onnx-to-htp-v3
# (FP16/VTCM auto-fallback, quantization, dynamic dims). See SKILL.md for the
# full six-step explanation and pitfall table.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File run_qnn_deploy.ps1 `
#       -Onnx C:\work\model.onnx `
#       -WorkDir C:\work\model_workdir `
#       [-QairtRoot C:\Qualcomm\AIStack\QAIRT\2.48.40.260702] `
#       [-Python C:\path\to\python.exe] `
#       [-Precision auto|fp32|fp16] `
#       [-Quantize int8|a16w8] [-CalibList calib_list.txt] `
#       [-InputList input_list.txt] `
#       [-Cmake C:\Tools\CMake\bin] [-VsRoot 'C:\Program Files\Microsoft Visual Studio'] `
#       [-SkipContextBinary]
#
# NOTE: always run with -File (not -Command) -- see SKILL.md pitfall #6.
# This file is ASCII-only on purpose (PS 5.1 reads BOM-less .ps1 as GBK on some hosts).

param(
  [Parameter(Mandatory=$true)][string]$Onnx,
  [Parameter(Mandatory=$true)][string]$WorkDir,
  [string]$QairtRoot = "C:\Qualcomm\AIStack\QAIRT\2.48.40.260702",
  [string]$Python    = "",
  [ValidateSet("auto","fp32","fp16")][string]$Precision = "auto",
  [ValidateSet("","int8","a16w8")][string]$Quantize = "",
  [string]$CalibList = "",
  [string]$InputList = "",
  [string]$Cmake = "",
  [string]$VsRoot = "C:\Program Files\Microsoft Visual Studio",
  [switch]$SkipContextBinary
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Fail($msg) {
  Write-Host ""
  Write-Host "FAIL: $msg" -ForegroundColor Red
  Write-Host "See SKILL.md 坑表 for known symptoms; diagnostic logs are under $WorkDir\logs\"
  exit 1
}

if (-not (Test-Path $Onnx)) { Fail "ONNX file not found: $Onnx" }
if (-not (Test-Path $QairtRoot)) { Fail "QairtRoot not found: $QairtRoot" }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkDir\logs" | Out-Null

# --- Resolve Python interpreter (converter is a pure-python tool) ---
if (-not $Python) {
  $cand = Get-Command python -ErrorAction SilentlyContinue
  if ($cand) { $Python = $cand.Source } else { Fail "No -Python given and no python.exe on PATH. Run env_check.ps1 first." }
}
if (-not (Test-Path $Python)) { Fail "Python not found: $Python" }
Write-Host "Using Python: $Python"

# --- Resolve architecture-specific tool dirs (layout differs by SDK version) ---
function Resolve-ArchDir($base, $candidates) {
  foreach ($c in $candidates) {
    $p = Join-Path $QairtRoot (Join-Path $base $c)
    if (Test-Path $p) { return $p }
  }
  return $null
}
$ConverterBinDir = Resolve-ArchDir "bin" @("arm64x-windows-msvc","aarch64-windows-msvc","x86_64-windows-msvc")
$X64BinDir       = Resolve-ArchDir "bin" @("x86_64-windows-msvc")
$X64LibDir       = Resolve-ArchDir "lib" @("x86_64-windows-msvc")
if (-not $ConverterBinDir) { Fail "Could not find converter bin dir under $QairtRoot\bin\<arch>. Run env_check.ps1." }
if (-not $X64BinDir -or -not $X64LibDir) { Fail "Could not find x86_64-windows-msvc bin/lib dir -- qnn-net-run MUST run via the x64 toolchain (ARM64 toolchain crashes at prepare, see SKILL.md pitfall #1)." }

$env:PYTHONPATH = Join-Path $QairtRoot "lib\python"
$env:QNN_SDK_ROOT = $QairtRoot
$env:QAIRT_SDK_ROOT = $QairtRoot

# --- Ensure cmake/compiler on PATH for qnn-model-lib-generator (pitfall #10) ---
if ($Cmake -and (Test-Path $Cmake)) { $env:PATH = "$Cmake;$env:PATH" }
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
  # try to discover cmake shipped inside a venv's site-packages (cmake pip package)
  $pyDir = Split-Path -Parent $Python
  $venvCmake = Join-Path $pyDir "cmake.exe"
  if (Test-Path $venvCmake) {
    $env:PATH = "$pyDir;$env:PATH"
    Write-Host "Added venv cmake to PATH: $venvCmake"
  } else {
    Write-Host "WARNING: cmake not found on PATH. qnn-model-lib-generator's internal build may fail." -ForegroundColor Yellow
    Write-Host "Pass -Cmake <dir> or install cmake in the converter venv (pip install cmake)."
  }
}
if (-not (Get-Command cl -ErrorAction SilentlyContinue) -and -not (Get-Command clang-cl -ErrorAction SilentlyContinue)) {
  Write-Host "WARNING: no cl.exe/clang-cl.exe on PATH. If model-lib-generator fails to compile," -ForegroundColor Yellow
  Write-Host "run this script from a 'Developer PowerShell for VS' or set -VsRoot correctly."
}

function Run-Step($name, $exe, $argList) {
  Write-Host ""
  Write-Host "=== $name ===" -ForegroundColor Cyan
  Write-Host "  $exe $($argList -join ' ')"
  $logFile = Join-Path "$WorkDir\logs" "$($name.Replace(' ','_')).log"
  & $exe @argList *> $logFile
  $rc = $LASTEXITCODE
  Get-Content $logFile -Tail 30 | ForEach-Object { Write-Host "  | $_" }
  return @{ rc = $rc; log = $logFile }
}

function Try-Convert($precision) {
  $outPrefix = Join-Path $WorkDir "model_$precision"
  $convArgs = @("-i", $Onnx, "-o", $outPrefix)
  if ($precision -eq "fp16") { $convArgs += @("--float_bitwidth", "16") }
  if ($Quantize) {
    if (-not $CalibList) { Fail "-Quantize $Quantize requires -CalibList <path>." }
    if (-not (Test-Path $CalibList)) { Fail "CalibList not found: $CalibList" }
    $convArgs += @("--input_list", $CalibList)
    Write-Host "Quantization requested ($Quantize) -- exact converter flags vary by SDK version;"
    Write-Host "if this step errors on an unrecognized flag, check <qairt_sdk_root>\docs for this SDK's"
    Write-Host "qairt-quantizer / qnn-onnx-converter --help, or switch to the model-builder skill's"
    Write-Host "run_pipeline.py which has a stable --precision w8a8/w8a16/... interface."
  }
  $converter = Join-Path $ConverterBinDir "qnn-onnx-converter"
  $r = Run-Step "convert_$precision" $Python (@($converter) + $convArgs)
  return @{ ok = ($r.rc -eq 0 -and (Test-Path "$outPrefix" )); prefix = $outPrefix; log = $r.log }
}

function Build-ModelLib($prefix) {
  $cpp = "$prefix.cpp"
  if (-not (Test-Path $cpp)) {
    if (Test-Path $prefix) { Copy-Item $prefix $cpp -Force }   # generator requires .cpp extension (pitfall)
    else { Fail "Converter output not found: $prefix" }
  }
  $binFile = "$prefix.bin"
  $mlibOut = "$prefix.mlib"
  $generator = Join-Path $ConverterBinDir "qnn-model-lib-generator"
  $genArgs = @("-c", $cpp, "-o", $mlibOut)
  if (Test-Path $binFile) { $genArgs = @("-c", $cpp, "-b", $binFile, "-o", $mlibOut) }
  $r = Run-Step "model_lib" $Python (@($generator) + $genArgs)
  $dllX64 = Get-ChildItem -Path $mlibOut -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "x64|x86_64" } | Select-Object -First 1
  if (-not $dllX64) { $dllX64 = Get-ChildItem -Path $mlibOut -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue | Select-Object -First 1 }
  return @{ ok = ($r.rc -eq 0 -and $dllX64); dll = if ($dllX64) { $dllX64.FullName } else { $null }; log = $r.log }
}

function Gen-Input($onnxPath, $outDir) {
  # Generate random float32 input(s) matching the ONNX declared input shape(s).
  # Real shape after conversion may differ (transpose) -- see SKILL.md pitfall #2;
  # this only creates the ORIGINAL-shape input used for the onnxruntime reference run.
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null
  $py = @"
import onnxruntime as ort
import numpy as np
import json, sys, os

sess = ort.InferenceSession(r'$onnxPath', providers=['CPUExecutionProvider'])
inputs = {}
raw_paths = []
for inp in sess.get_inputs():
    shape = [d if isinstance(d, int) and d > 0 else 1 for d in inp.shape]
    arr = np.random.randn(*shape).astype(np.float32)
    inputs[inp.name] = arr
    raw = os.path.join(r'$outDir', inp.name.replace('/', '_') + '.raw')
    arr.tofile(raw)
    raw_paths.append((inp.name, raw))

outs = sess.run(None, inputs)
for i, o in enumerate(outs):
    o.astype(np.float32).tofile(os.path.join(r'$outDir', f'ref_output_{i}.raw'))

with open(os.path.join(r'$outDir', 'input_list.txt'), 'w') as f:
    f.write(' '.join(f'{n}:={p}' for n, p in raw_paths) + '\n')

print('GEN_INPUT_OK', json.dumps({n: list(a.shape) for n, a in inputs.items()}))
"@
  $pyFile = Join-Path $outDir "_gen_input.py"
  Set-Content -Path $pyFile -Value $py -Encoding UTF8
  $r = Run-Step "gen_input" $Python @($pyFile)
  return @{ ok = ($r.rc -eq 0); inputList = Join-Path $outDir "input_list.txt"; log = $r.log }
}

function Run-NetRun($dll, $inputList, $outDir) {
  $netrun = Join-Path $X64BinDir "qnn-net-run.exe"
  $htpBackend = Join-Path $X64LibDir "QnnHtp.dll"
  if (-not (Test-Path $netrun)) { Fail "qnn-net-run.exe not found at $netrun" }
  if (-not (Test-Path $htpBackend)) { Fail "QnnHtp.dll not found at $htpBackend" }
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null
  $args = @("--model", $dll, "--backend", $htpBackend, "--input_list", $inputList, "--output_dir", $outDir)
  $r = Run-Step "net_run" $netrun $args
  # netrun.log is UTF-16LE (pitfall #7)
  $netrunLog = Get-ChildItem -Path $outDir -Filter "*.log" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
  $vtcmError = $false
  if ($netrunLog) {
    $content = Get-Content $netrunLog.FullName -Encoding Unicode -ErrorAction SilentlyContinue
    if ($content -match "flat_from_vtcm" -or $content -match "VTCM" -or $content -match "err.*1002") {
      $vtcmError = $true
    }
  }
  # also check the captured stdout/stderr log
  $stepLogContent = Get-Content $r.log -ErrorAction SilentlyContinue
  if ($stepLogContent -match "flat_from_vtcm" -or $stepLogContent -match "finalize err 1002") { $vtcmError = $true }
  return @{ ok = ($r.rc -eq 0); vtcmError = $vtcmError; outDir = $outDir; log = $r.log }
}

function Compare-Output($outDir, $refDir, $threshold) {
  $comparePy = Join-Path $ScriptDir "compare_output.py"
  $npuRaw = Get-ChildItem -Path $outDir -Recurse -Filter "*.raw" -ErrorAction SilentlyContinue | Select-Object -First 1
  $refRaw = Get-ChildItem -Path $refDir -Filter "ref_output_0.raw" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $npuRaw -or -not $refRaw) { return @{ ok = $false; reason = "raw output file(s) not found" } }
  $r = Run-Step "compare" $Python @($comparePy, $npuRaw.FullName, $refRaw.FullName, "--threshold", $threshold)
  return @{ ok = ($r.rc -eq 0); log = $r.log }
}

# ============================================================
# Main flow
# ============================================================
Write-Host "onnx-to-htp: $Onnx -> $WorkDir (Precision=$Precision)"

$refDir = Join-Path $WorkDir "ref"
$genResult = Gen-Input $Onnx $refDir
if (-not $genResult.ok) { Fail "Reference input/output generation failed. See $($genResult.log)" }

$precisionsToTry = if ($Precision -eq "auto") { @("fp32", "fp16") } else { @($Precision) }
$success = $false
$finalPrecision = $null
$finalOutDir = $null

foreach ($prec in $precisionsToTry) {
  Write-Host ""
  Write-Host "----- Attempting precision: $prec -----" -ForegroundColor Yellow

  $conv = Try-Convert $prec
  if (-not $conv.ok) {
    Write-Host "Convert step failed for $prec. See $($conv.log)."
    continue
  }

  $mlib = Build-ModelLib $conv.prefix
  if (-not $mlib.ok) {
    Write-Host "Model-lib build failed for $prec. See $($mlib.log)."
    continue
  }

  $npuOut = Join-Path $WorkDir "out_npu_$prec"
  $netrun = Run-NetRun $mlib.dll $genResult.inputList $npuOut

  if ($netrun.vtcmError) {
    Write-Host "VTCM/flat_from_vtcm error detected at precision=$prec." -ForegroundColor Yellow
    if ($Precision -eq "auto" -and $prec -eq "fp32") {
      Write-Host "Auto-fallback: retrying with FP16 (see SKILL.md pitfall #3)."
      continue
    } else {
      Fail "VTCM budget exceeded even at $prec. Consider quantization (-Quantize) or a smaller model."
    }
  }
  if (-not $netrun.ok) {
    Write-Host "qnn-net-run failed for $prec (non-VTCM error). See $($netrun.log)."
    continue
  }

  $threshold = if ($prec -eq "fp32" -and -not $Quantize) { "1e-5" } elseif ($Quantize) { "0.95" } else { "0.99" }
  $cmp = Compare-Output $npuOut $refDir $threshold
  if ($cmp.ok) {
    $success = $true
    $finalPrecision = $prec
    $finalOutDir = $npuOut
    break
  } else {
    Write-Host "Output comparison failed at precision=$prec (threshold=$threshold). See $($cmp.log)."
    if ($Precision -ne "auto") { break }
  }
}

if (-not $success) {
  Fail "All precision attempts failed. Check $WorkDir\logs\ for each step's log."
}

# --- Optional: context binary (Step 5) ---
if (-not $SkipContextBinary) {
  Write-Host ""
  Write-Host "=== Context binary (optional acceleration) ===" -ForegroundColor Cyan
  $ctxGen = Join-Path $X64BinDir "qnn-context-binary-generator.exe"
  $htpBackend = Join-Path $X64LibDir "QnnHtp.dll"
  $dllPath = (Build-ModelLib (Join-Path $WorkDir "model_$finalPrecision")).dll
  if ((Test-Path $ctxGen) -and $dllPath) {
    $binOut = Join-Path $WorkDir "model_$finalPrecision.bin"
    Run-Step "context_binary" $ctxGen @("--model", $dllPath, "--backend", $htpBackend, "--binary_file", $binOut) | Out-Null
    # generator appends .bin itself (pitfall #5) -- normalize the name
    if ((Test-Path "$binOut.bin") -and -not (Test-Path $binOut)) {
      Move-Item "$binOut.bin" $binOut -Force
    }
    if (Test-Path $binOut) { Write-Host "Context binary: $binOut" }
  } else {
    Write-Host "Skipping context binary (generator or dll not found)."
  }
}

# --- Report ---
$reportPath = Join-Path $WorkDir "REPORT.md"
@"
# onnx-to-htp deployment report

- ONNX: $Onnx
- WorkDir: $WorkDir
- Final precision: $finalPrecision
- Quantize: $(if ($Quantize) { $Quantize } else { "none" })
- NPU output dir: $finalOutDir
- Status: PASS

See logs\*.log for each step's stdout/stderr.
"@ | Set-Content -Path $reportPath -Encoding UTF8

Write-Host ""
Write-Host "PASS: NPU output validated at precision=$finalPrecision" -ForegroundColor Green
Write-Host "REPORT: $reportPath"
Write-Host "SUCCESS"
exit 0
