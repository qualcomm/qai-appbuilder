# gen_mlib.ps1 — standalone helper: ONNX-converter output -> QNN model lib (.dll)
# (onnx-to-htp skill — Step 2 of the six-step flow, isolated for debugging)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File gen_mlib.ps1 `
#       -QairtRoot C:\Qualcomm\AIStack\QAIRT\2.48.40.260702 `
#       -Python C:\venv310x\Scripts\python.exe `
#       -ConverterOutPrefix C:\work\model_workdir\model_fp32   # no extension; expects <prefix> (.cpp source) + <prefix>.bin
#
# run_qnn_deploy.ps1 already calls the equivalent of this internally --
# use this standalone script only when you need to re-run just Step 2
# (e.g. after manually editing the converter's .cpp output, or to retry
# with different cmake/compiler settings without re-running the converter).

param(
  [Parameter(Mandatory=$true)][string]$ConverterOutPrefix,
  [string]$QairtRoot = "C:\Qualcomm\AIStack\QAIRT\2.48.40.260702",
  [string]$Python = "",
  [string]$Cmake = ""
)

$ErrorActionPreference = "Stop"

if (-not $Python) {
  $cand = Get-Command python -ErrorAction SilentlyContinue
  if ($cand) { $Python = $cand.Source } else { throw "No -Python given and none on PATH." }
}
if ($Cmake -and (Test-Path $Cmake)) { $env:PATH = "$Cmake;$env:PATH" }
$env:PYTHONPATH = Join-Path $QairtRoot "lib\python"
$env:QNN_SDK_ROOT = $QairtRoot
$env:QAIRT_SDK_ROOT = $QairtRoot

function Resolve-ArchDir($base, $candidates) {
  foreach ($c in $candidates) {
    $p = Join-Path $QairtRoot (Join-Path $base $c)
    if (Test-Path $p) { return $p }
  }
  return $null
}
$ConverterBinDir = Resolve-ArchDir "bin" @("arm64x-windows-msvc","aarch64-windows-msvc","x86_64-windows-msvc")
if (-not $ConverterBinDir) { throw "Could not find converter bin dir under $QairtRoot\bin\<arch>." }

$cpp = "$ConverterOutPrefix.cpp"
if (-not (Test-Path $cpp)) {
  if (Test-Path $ConverterOutPrefix) {
    Copy-Item $ConverterOutPrefix $cpp -Force   # generator requires .cpp extension (SKILL.md pitfall)
    Write-Host "Copied $ConverterOutPrefix -> $cpp (generator requires .cpp extension)"
  } else {
    throw "Converter output not found: $ConverterOutPrefix (nor $cpp)"
  }
}
$binFile = "$ConverterOutPrefix.bin"
$mlibOut = "$ConverterOutPrefix.mlib"

$generator = Join-Path $ConverterBinDir "qnn-model-lib-generator"
$genArgs = @("-c", $cpp, "-o", $mlibOut)
if (Test-Path $binFile) { $genArgs = @("-c", $cpp, "-b", $binFile, "-o", $mlibOut) }

Write-Host "Running: $Python $generator $($genArgs -join ' ')"
& $Python $generator @genArgs
if ($LASTEXITCODE -ne 0) { throw "qnn-model-lib-generator failed with exit code $LASTEXITCODE" }

$dll = Get-ChildItem -Path $mlibOut -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Produced model libs:"
$dll | ForEach-Object { Write-Host "  $($_.FullName)" }
Write-Host ""
Write-Host "Remember: qnn-net-run MUST use the x64 (x86_64) .dll, not the ARM64 one"
Write-Host "(ARM64 toolchain crashes at prepare -- SKILL.md pitfall #1)."
