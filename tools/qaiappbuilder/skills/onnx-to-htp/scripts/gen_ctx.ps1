# gen_ctx.ps1 — standalone helper: model lib (.dll) -> QNN context binary (.bin)
# (onnx-to-htp skill — Step 5 of the six-step flow, isolated for debugging)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File gen_ctx.ps1 `
#       -QairtRoot C:\Qualcomm\AIStack\QAIRT\2.48.40.260702 `
#       -Dll C:\work\model_workdir\model_fp32.mlib\x64\model_fp32.dll `
#       -Output C:\work\model_workdir\model_fp32.bin
#
# run_qnn_deploy.ps1 already calls the equivalent of this internally (unless
# -SkipContextBinary is passed) -- use this standalone script only when you
# need to regenerate the context binary alone (e.g. different VTCM config)
# without re-running conversion + model-lib generation.

param(
  [Parameter(Mandatory=$true)][string]$Dll,
  [Parameter(Mandatory=$true)][string]$Output,
  [string]$QairtRoot = "C:\Qualcomm\AIStack\QAIRT\2.48.40.260702"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Dll)) { throw "Model lib .dll not found: $Dll" }

$env:QNN_SDK_ROOT = $QairtRoot
$env:QAIRT_SDK_ROOT = $QairtRoot

$X64BinDir = Join-Path $QairtRoot "bin\x86_64-windows-msvc"
$X64LibDir = Join-Path $QairtRoot "lib\x86_64-windows-msvc"
$ctxGen = Join-Path $X64BinDir "qnn-context-binary-generator.exe"
$htpBackend = Join-Path $X64LibDir "QnnHtp.dll"

if (-not (Test-Path $ctxGen)) { throw "qnn-context-binary-generator.exe not found: $ctxGen" }
if (-not (Test-Path $htpBackend)) { throw "QnnHtp.dll not found: $htpBackend" }

Write-Host "Running: $ctxGen --model $Dll --backend $htpBackend --binary_file $Output"
& $ctxGen --model $Dll --backend $htpBackend --binary_file $Output

# The generator appends .bin to whatever you pass -- normalize the name
# (SKILL.md pitfall #5: "X.bin" -> actually produces "X.bin.bin").
if ((Test-Path "$Output.bin") -and -not (Test-Path $Output)) {
  Move-Item "$Output.bin" $Output -Force
  Write-Host "Renamed $Output.bin -> $Output (generator auto-appends .bin)"
}

if (Test-Path $Output) {
  Write-Host "Context binary produced: $Output"
} else {
  throw "Context binary generation appears to have failed -- neither $Output nor $Output.bin exists."
}
