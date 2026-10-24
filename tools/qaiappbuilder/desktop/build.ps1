<#
.SYNOPSIS
  Build the QAI ModelBuilder desktop shell (Tauri 2 / WebView2) for Windows ARM64 and x64.

.DESCRIPTION
  The shell is only a window around the page the backend serves, so the frontend is not touched. It needs:
    * Rust (rustup) with the targets aarch64-pc-windows-msvc and x86_64-pc-windows-msvc
    * Visual Studio 2022 Build Tools (C++ workload; ARM64 + x64 tools) and a Windows SDK
  Output:  dist\desktop\<arch>\QAIModelBuilder.exe     (arch = arm64 | x64, ~3 MB each)
  Cargo's target dir is .build\desktop-target (see src-tauri\.cargo\config.toml), so desktop\ stays source-only.

.PARAMETER Arch   arm64 | x64 | both (default both)
.PARAMETER SkipTests   skip `cargo test` (unit tests run on the host architecture only)
#>
param(
    [ValidateSet('arm64', 'x64', 'both')][string]$Arch = 'both',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Root = (Resolve-Path (Join-Path $Here '..')).Path
function Info($m) { Write-Host "[INFO] $m" }
function Fail($m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }

$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
if (Test-Path $cargoBin) { $env:Path = "$cargoBin;$env:Path" }
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Fail 'cargo not found. Install Rust:  winget install Rustlang.Rustup   (then re-open the shell)'
}
# Corporate proxies often block the certificate-revocation lookup that cargo does by default.
if (-not $env:CARGO_HTTP_CHECK_REVOKE) { $env:CARGO_HTTP_CHECK_REVOKE = 'false' }

$targets = @{ arm64 = 'aarch64-pc-windows-msvc'; x64 = 'x86_64-pc-windows-msvc' }
$arches = if ($Arch -eq 'both') { @('arm64', 'x64') } else { @($Arch) }

$installed = (& rustup target list --installed) -join ' '
foreach ($a in $arches) {
    if ($installed -notmatch [regex]::Escape($targets[$a])) {
        Info "adding Rust target $($targets[$a])"
        & rustup target add $targets[$a]
        if ($LASTEXITCODE -ne 0) { Fail "rustup target add $($targets[$a]) failed" }
    }
}

# the app icon (icons\icon.ico) is generated from the front-end favicon geometry; keep the committed copy
if (-not (Test-Path (Join-Path $Here 'src-tauri\icons\icon.ico'))) { Fail 'src-tauri\icons\icon.ico missing - run src-tauri\icons\render_icon.py' }

Push-Location (Join-Path $Here 'src-tauri')
try {
    if (-not $SkipTests) {
        $hostArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
        Info "cargo test ($hostArch)"
        & cargo test --release --target $targets[$hostArch] --lib
        if ($LASTEXITCODE -ne 0) { Fail 'cargo test failed' }
    }
    foreach ($a in $arches) {
        Info "cargo build --release ($a)"
        & cargo build --release --target $targets[$a]
        if ($LASTEXITCODE -ne 0) { Fail "cargo build failed for $a" }
        $src = Join-Path $Root ".build\desktop-target\$($targets[$a])\release\QAIModelBuilder.exe"
        if (-not (Test-Path $src)) { Fail "build output missing: $src" }

        # PE machine type must match the architecture we asked for (0xAA64 = ARM64, 0x8664 = x64)
        $b = [IO.File]::ReadAllBytes($src)
        $m = [BitConverter]::ToUInt16($b, [BitConverter]::ToInt32($b, 0x3C) + 4)
        $want = if ($a -eq 'arm64') { 0xAA64 } else { 0x8664 }
        if ($m -ne $want) { Fail ("{0}: PE machine 0x{1:X}, expected 0x{2:X}" -f $a, $m, $want) }

        $out = Join-Path $Root "dist\desktop\$a"
        New-Item -ItemType Directory -Force $out | Out-Null
        Copy-Item $src (Join-Path $out 'QAIModelBuilder.exe') -Force
        Info ("{0}: {1}  ({2:N2} MB)" -f $a, (Join-Path $out 'QAIModelBuilder.exe'), ((Get-Item (Join-Path $out 'QAIModelBuilder.exe')).Length / 1MB))
    }
}
finally { Pop-Location }
Info 'done'
