# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Build the QAI ModelBuilder Windows installer(s) (Inno Setup), one per architecture.

.DESCRIPTION
    1. (optional) refresh frontend\dist via Build.bat
    2. stage a WHITELIST of files per arch under .build\installer\stage_<arch>\
         - src, apps, factory, scripts, interfaces, skills, tools, frontend\dist
         - pyproject.toml, build_info.json, launcher .bat files, READMEs
         - data\bin\uv\uv.exe (fetched from PyPI for the TARGET arch), data\bin\aria2c\aria2c.exe
       Tests, docs, samples, models, .qai, desktop, vendor, node_modules are NOT staged.
    3. run ISCC.exe  ->  dist\installer\QAIModelBuilder-<version>-<arch>-setup.exe

    The installer copies the files, then runs setup_env.bat (no --builder: QAIRT is NOT installed).

.PARAMETER Arch          x64 | arm64 | both (default both)
.PARAMETER SkipFrontend  do not run Build.bat; use the existing frontend\dist
.PARAMETER Version       override the version (default: build_info.json "version")
.PARAMETER KeepStage     keep .build\installer\stage_<arch> after compiling
.PARAMETER IsccPath      path to ISCC.exe (auto-detected by default)
#>
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64', 'both')][string]$Arch = 'both',
    [switch]$SkipFrontend,
    [string]$Version = '',
    [switch]$KeepStage,
    [string]$IsccPath = ''
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Info($m) { Write-Host "[INFO] $m" }
function Fail($m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }

# ---- locate ISCC ----------------------------------------------------------
if (-not $IsccPath) {
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $IsccPath = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $IsccPath) {
        $c = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($c) { $IsccPath = $c.Source }
    }
}
if (-not $IsccPath -or -not (Test-Path $IsccPath)) {
    Fail 'ISCC.exe not found. Install it:  winget install JRSoftware.InnoSetup --scope user'
}
Info "ISCC: $IsccPath"

# ---- version --------------------------------------------------------------
if (-not $Version) {
    $bi = Get-Content (Join-Path $Root 'build_info.json') -Raw | ConvertFrom-Json
    $Version = [string]$bi.version
}
if ($Version -notmatch '^\d+(\.\d+){1,3}') { Fail "bad version '$Version'" }
Info "version: $Version"

# ---- frontend -------------------------------------------------------------
if (-not $SkipFrontend) {
    Info 'building frontend via Build.bat ...'
    & cmd.exe /c "`"$Root\Build.bat`""
    if ($LASTEXITCODE -ne 0) { Fail "Build.bat failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path (Join-Path $Root 'frontend\dist\index.html'))) {
    Fail 'frontend\dist\index.html missing - run Build.bat first (or drop -SkipFrontend).'
}

# ---- whitelist ------------------------------------------------------------
$Dirs = @('src', 'apps', 'factory', 'scripts', 'interfaces', 'skills', 'tools')
$Files = @(
    'pyproject.toml', 'build_info.json',
    'setup_env.bat', 'Start.bat', 'qai.bat', 'Console.bat', 'Uninstall.bat', 'clean_env.bat',
    'README.md', 'README.zh-CN.md', 'QUICK-START.md', 'QUICK-START.zh-CN.md'
)
$ExcludeDirs = @('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules', 'tests', '.git')
$ExcludeFiles = @('*.pyc', '*.pyo', '*.log')

function Stage-Arch([string]$a) {
    $stage = Join-Path $Root ".build\installer\stage_$a"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Force $stage | Out-Null
    Info "staging $a -> $stage"

    foreach ($d in $Dirs) {
        $src = Join-Path $Root $d
        if (-not (Test-Path $src)) { Fail "required dir missing: $d" }
        $null = robocopy $src (Join-Path $stage $d) /E /NFL /NDL /NJH /NJS /NP /XD $ExcludeDirs /XF $ExcludeFiles
        if ($LASTEXITCODE -ge 8) { Fail "robocopy $d failed ($LASTEXITCODE)" }
    }
    $global:LASTEXITCODE = 0

    $null = robocopy (Join-Path $Root 'frontend\dist') (Join-Path $stage 'frontend\dist') /E /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { Fail "robocopy frontend\dist failed ($LASTEXITCODE)" }
    $global:LASTEXITCODE = 0

    foreach ($f in $Files) {
        $p = Join-Path $Root $f
        if (Test-Path $p) { Copy-Item $p (Join-Path $stage $f) -Force }
        elseif ($f -in @('pyproject.toml', 'setup_env.bat', 'Start.bat')) { Fail "required file missing: $f" }
    }

    # aria2c is x64; it also runs on Windows-on-ARM, so the same copy serves both packages.
    $aria = Join-Path $Root 'data\bin\aria2c\aria2c.exe'
    if (Test-Path $aria) {
        New-Item -ItemType Directory -Force (Join-Path $stage 'data\bin\aria2c') | Out-Null
        Copy-Item $aria (Join-Path $stage 'data\bin\aria2c\aria2c.exe') -Force
    } else {
        Info 'aria2c.exe not in data\bin - setup_env.bat will download it at install time.'
    }

    # uv must match the TARGET arch, so fetch it from PyPI instead of copying the local one.
    $plat = if ($a -eq 'x64') { 'win_amd64' } else { 'win_arm64' }
    $uvDir = Join-Path $stage 'data\bin\uv'
    $work = Join-Path $Root '.build\installer\_dl'
    New-Item -ItemType Directory -Force $uvDir, $work | Out-Null
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\setup\fetch_pypi_exe.ps1') `
        -Package uv -Platform $plat -Exe 'uv.exe,uvx.exe' -OutDir $uvDir -WorkDir $work *>&1 | Out-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $uvDir 'uv.exe'))) {
        Info 'uv not bundled (PyPI fetch failed); setup_env.bat will fetch it at install time.'
        $global:LASTEXITCODE = 0
    }

    $files = Get-ChildItem $stage -Recurse -File
    $mb = ($files | Measure-Object Length -Sum).Sum / 1MB
    Info ("staged {0} files, {1:N1} MB uncompressed" -f $files.Count, $mb)
    Write-Output $stage
}

$OutDir = Join-Path $Root 'dist\installer'
New-Item -ItemType Directory -Force $OutDir | Out-Null
$arches = if ($Arch -eq 'both') { @('x64', 'arm64') } else { @($Arch) }
$iss = Join-Path $PSScriptRoot 'qaimodelbuilder.iss'

foreach ($a in $arches) {
    $stage = Stage-Arch $a | Select-Object -Last 1
    Info "compiling installer ($a) ..."
    & $IsccPath /Q "/DAppVersion=$Version" "/DArch=$a" "/DStageDir=$stage" "/DOutDir=$OutDir" $iss
    if ($LASTEXITCODE -ne 0) { Fail "ISCC failed for $a (exit $LASTEXITCODE)" }
    if (-not $KeepStage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
}

Info 'done:'
Get-ChildItem $OutDir -Filter 'QAIModelBuilder-*-setup.exe' |
    ForEach-Object { '  {0}  {1:N1} MB' -f $_.FullName, ($_.Length / 1MB) }
