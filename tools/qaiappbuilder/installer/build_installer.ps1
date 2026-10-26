# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Build the QAI ModelBuilder Windows installer(s) (Inno Setup), one per architecture.

.DESCRIPTION
    1. (optional) refresh frontend\dist via Build.bat
    2. (optional) build the desktop window exe for the requested architecture(s) via desktop\build.ps1
    3. stage a WHITELIST of files per arch under .build\installer\stage_<arch>\
         - src, apps, factory, scripts, interfaces, skills, tools, frontend\dist
         - pyproject.toml, build_info.json, launcher .bat files, READMEs
         - data\bin\uv\uv.exe (fetched from PyPI for the TARGET arch), data\bin\aria2c\aria2c.exe
       Tests, docs, samples, models, .qai, desktop sources, vendor, node_modules are NOT staged (the built desktop shell exe is).
    4. run ISCC.exe  ->  dist\installer\QAIModelBuilder-<version>-qairt<A.B.C>-<arch>-setup.exe
       (A.B.C = first three parts of the QAIRT SDK version, e.g. qairt2.48.40)

    The installer copies the files, then runs setup_env.bat (no --builder: QAIRT is NOT installed).

.PARAMETER Arch          x64 | arm64 | both (default both)
.PARAMETER SkipFrontend  do not run Build.bat; use the existing frontend\dist
.PARAMETER SkipDesktop   do not run desktop\build.ps1; use the existing dist\desktop\<arch>\QAIModelBuilder.exe
                         (needed on a machine without Rust / the VS C++ tools)
.PARAMETER Version       override the version (default: build_info.json "version")
.PARAMETER KeepStage     keep .build\installer\stage_<arch> after compiling
.PARAMETER IsccPath      path to ISCC.exe (auto-detected by default)
.PARAMETER GuardZip      a local guard.zip to take guard64.dll from (default: Downloads\guard.zip, else the release URL)
.PARAMETER QairtVersion  QAIRT SDK version to build the installer for, e.g. 2.51.40.260930 (default: scripts\qairt_release.json).
                         qai_appbuilder follows it (first three parts -> release tag). Only the STAGED copy of
                         qairt_release.json is changed; the file in the working tree is left alone.
.PARAMETER Help          print the usage and exit
.EXAMPLE
    .\installer\build_installer.ps1
    Build the desktop window and both installers (x64 + arm64).
.EXAMPLE
    .\installer\build_installer.ps1 -Arch x64 -SkipFrontend
    Only x64, reuse the existing frontend\dist.
#>
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64', 'both')][string]$Arch = 'both',
    [switch]$SkipFrontend,
    [switch]$SkipDesktop,
    [string]$Version = '',
    [switch]$KeepStage,
    [string]$IsccPath = '',
    [string]$GuardZip = '',
    [string]$QairtVersion = '',
    [switch]$Help
)

function Show-Usage {
    @'
QAI ModelBuilder installer build

Usage:
  .\installer\build_installer.ps1 [options]

Builds, in order: the frontend (Build.bat), the desktop window exe (desktop\build.ps1), then the Inno Setup installer(s).
Output: dist\installer\QAIModelBuilder-<version>-qairt<A.B.C>-<arch>-setup.exe   (A.B.C from the QAIRT version)

Options:
  -Arch <x64|arm64|both>  architecture(s) to build                       (default: both)
  -SkipFrontend           do not run Build.bat; use the existing frontend\dist
  -SkipDesktop            do not run desktop\build.ps1; use the existing dist\desktop\<arch>\QAIModelBuilder.exe
                          (for machines without Rust / the VS C++ tools)
  -Version <x.y.z>        override the version                           (default: build_info.json "version")
  -KeepStage              keep .build\installer\stage_<arch> after compiling
  -IsccPath <path>        path to ISCC.exe                               (default: auto-detected)
  -GuardZip <path>        local guard.zip to take guard64.dll from       (default: Downloads\guard.zip, else the release URL)
  -QairtVersion <A.B.C[.build]>
                          QAIRT SDK version for this installer, e.g. 2.51.40.260930. qai_appbuilder follows it
                          (2.51.40 -> release v2.51.40). Checked online before building; only the staged copy of
                          scripts\qairt_release.json changes.   (default: the value in scripts\qairt_release.json)
  -Help                   show this text and exit

Examples:
  .\installer\build_installer.ps1                          # everything, both architectures
  .\installer\build_installer.ps1 -Arch x64 -SkipFrontend  # x64 only, reuse frontend\dist
  .\installer\build_installer.ps1 -SkipDesktop             # package the exes already in dist\desktop
  .\installer\build_installer.ps1 -QairtVersion 2.51.40.260930   # build for a specific QAIRT SDK version

Needs: Inno Setup 6, Rust + VS 2022 C++ tools + Windows SDK (unless -SkipDesktop), Node + pnpm (unless -SkipFrontend),
and network access to pypi.org. Details: installer\README.md, desktop\README.md
'@ | Write-Host
}

# Help first: before any other work (and before the checks below that need tools to be installed).
if ($Help) { Show-Usage; exit 0 }

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

# ---- QAIRT version --------------------------------------------------------
# qairt_release.json is the single source of truth; qai_appbuilder is released with the SDK, so its tag is the first three
# numeric parts of the QAIRT version (2.51.40.260930 -> v2.51.40). -QairtVersion overrides it for THIS build only: the
# staged copy is patched, never the working tree.
$ReleaseJson = Join-Path $Root 'scripts\qairt_release.json'
$QairtDefault = [string](Get-Content $ReleaseJson -Raw | ConvertFrom-Json).qairt_version
if ($QairtVersion) {
    if ($QairtVersion -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { Fail "bad -QairtVersion '$QairtVersion' (expected A.B.C or A.B.C.build, e.g. 2.51.40.260930)" }
} else { $QairtVersion = $QairtDefault }
if ($QairtVersion -notmatch '^(\d+\.\d+\.\d+)') { Fail "bad qairt_version '$QairtVersion' in scripts\qairt_release.json" }
$AbTag = $Matches[1]
Info "QAIRT SDK: $QairtVersion   qai_appbuilder release: v$AbTag$(if ($QairtVersion -ne $QairtDefault) { "   (overrides $QairtDefault from qairt_release.json)" })"

# Fail now, not on the user's machine at install time: both wheels of that release must exist.
$AbBase = "https://github.com/qualcomm/qai-appbuilder/releases/download/v$AbTag"
foreach ($w in "qai_appbuilder-$AbTag.1-cp313-cp313-win_amd64.whl", "qai_appbuilder-$AbTag-cp313-cp313-win_arm64.whl") {
    if ($Arch -eq 'x64' -and $w -like '*arm64*') { continue }
    if ($Arch -eq 'arm64' -and $w -like '*amd64*') { continue }
    try { $null = Invoke-WebRequest -Uri "$AbBase/$w" -Method Head -UseBasicParsing -TimeoutSec 60 -MaximumRedirection 5 }
    catch { Fail "qai_appbuilder wheel not found online: $AbBase/$w  ($($_.Exception.Message.Split("`n")[0]))  - wrong -QairtVersion, or no access to github.com." }
    Info "wheel ok: $w"
}

# ---- frontend -------------------------------------------------------------
if (-not $SkipFrontend) {
    Info 'building frontend via Build.bat ...'
    & cmd.exe /c "`"$Root\Build.bat`""
    if ($LASTEXITCODE -ne 0) { Fail "Build.bat failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path (Join-Path $Root 'frontend\dist\index.html'))) {
    Fail 'frontend\dist\index.html missing - run Build.bat first (or drop -SkipFrontend).'
}

# ---- desktop shell --------------------------------------------------------
# Built for exactly the architecture(s) being packaged. desktop\build.ps1 also runs the Rust unit tests and checks
# each exe's PE machine type. Use -SkipDesktop to package the exes already in dist\desktop (no Rust needed).
$desktopArches = if ($Arch -eq 'both') { @('x64', 'arm64') } else { @($Arch) }
if (-not $SkipDesktop) {
    Info "building the desktop window via desktop\build.ps1 -Arch $Arch ..."
    # run in a child PowerShell: build.ps1 calls `exit` on failure, which would otherwise end this script silently
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'desktop\build.ps1') -Arch $Arch
    if ($LASTEXITCODE -ne 0) { Fail "desktop\build.ps1 failed (exit $LASTEXITCODE). Fix it, or pass -SkipDesktop to package the exes already in dist\desktop." }
}
foreach ($da in $desktopArches) {
    if (-not (Test-Path (Join-Path $Root "dist\desktop\$da\QAIModelBuilder.exe"))) {
        Fail "dist\desktop\$da\QAIModelBuilder.exe is missing - run without -SkipDesktop (needs Rust + VS C++ tools), or run desktop\build.ps1 -Arch $da first."
    }
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

    # The desktop shell (Tauri/WebView2 window around the local backend), built per architecture by desktop\build.ps1.
    $shellExe = Join-Path $Root "dist\desktop\$a\QAIModelBuilder.exe"
    if (-not (Test-Path $shellExe)) { Fail "desktop shell missing: $shellExe - run desktop\build.ps1 -Arch $a first" }
    New-Item -ItemType Directory -Force (Join-Path $stage 'desktop') | Out-Null
    Copy-Item $shellExe (Join-Path $stage 'desktop\QAIModelBuilder.exe') -Force

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

    # guard64.dll (+ the ARM64 injector) from the release's guard.zip, ONLY this arch's files: ~0.7 MB per package
    # (the whole zip is 30 MB because it also carries both ast-grep builds). setup_env.bat skips its own download when
    # vendor\bin\guard.source holds exactly the URL it would fetch, so write that URL. guard.zip is pinned to its own
    # release (QAI_GUARD_RELEASE_TAG in setup_env.bat), NOT to the qai_appbuilder tag: newer releases do not carry it.
    $guardTag = (Select-String -Path (Join-Path $Root 'setup_env.bat') -Pattern 'set "QAI_GUARD_RELEASE_TAG=([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
    if (-not $guardTag) { Fail 'QAI_GUARD_RELEASE_TAG not found in setup_env.bat' }
    $guardUrl = "https://github.com/qualcomm/qai-appbuilder/releases/download/v$guardTag/guard.zip"
    $guardZip = Join-Path $Root '.build\installer\_dl\guard.zip'
    if (-not (Test-Path $guardZip)) {
        $local = @($GuardZip, (Join-Path $env:USERPROFILE 'Downloads\guard.zip')) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if ($local) { Copy-Item $local $guardZip -Force; Info "guard.zip: using $local" }
        else {
            try { Invoke-WebRequest -Uri $guardUrl -OutFile $guardZip -UseBasicParsing -TimeoutSec 300; Info "guard.zip: downloaded $guardUrl" }
            catch { Remove-Item $guardZip -Force -ErrorAction SilentlyContinue }
        }
    }
    $guardOk = $false
    if (Test-Path $guardZip) {
        $gt = Join-Path $Root ".build\installer\_guard_$a"
        Remove-Item $gt -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive -Path $guardZip -DestinationPath $gt -Force
        $gOut = Join-Path $stage "vendor\bin\$a"
        New-Item -ItemType Directory -Force $gOut | Out-Null
        foreach ($n in 'guard64.dll', 'guard-injector.exe') {
            $src = Join-Path $gt "$a\$n"
            if (Test-Path $src) { Copy-Item $src (Join-Path $gOut $n) -Force }
        }
        Remove-Item $gt -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path (Join-Path $gOut 'guard64.dll')) {
            [IO.File]::WriteAllText((Join-Path $stage 'vendor\bin\guard.source'), "$guardUrl$nl".Replace("`n", "`r`n").Replace("`r`r`n", "`r`n"), (New-Object Text.ASCIIEncoding))
            $guardOk = $true
        }
    }
    if (-not $guardOk) { Info 'guard.zip not bundled - setup_env.bat will download it at install time.' }
    # vendor\tiktoken: the two BPE vocab files the token estimator reads offline (~5.3 MB). They are NOT in the
    # tiktoken wheel. Source order: cache -> official URL -> Downloads\vendor-deps.7z (tar.exe). tiktoken names the cache
    # file sha1(url); download_bpe_files.py documents sha256(url), so both names are written (same bytes).
    $tkCache = Join-Path $Root '.build\installer\_dl\tiktoken'
    New-Item -ItemType Directory -Force $tkCache | Out-Null
    $tkOut = Join-Path $stage 'vendor\tiktoken'
    New-Item -ItemType Directory -Force $tkOut | Out-Null
    $tkOk = 0
    foreach ($enc in 'cl100k_base', 'o200k_base') {
        $u = "https://openaipublic.blob.core.windows.net/encodings/$enc.tiktoken"
        $ub = [Text.Encoding]::UTF8.GetBytes($u)
        $n1 = [BitConverter]::ToString([Security.Cryptography.SHA1]::Create().ComputeHash($ub)).Replace('-', '').ToLower()
        $n2 = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($ub)).Replace('-', '').ToLower()
        $c = Join-Path $tkCache "$enc.tiktoken"
        if (-not (Test-Path $c)) {
            try { Invoke-WebRequest -Uri $u -OutFile $c -UseBasicParsing -TimeoutSec 120 }
            catch { Remove-Item $c -Force -ErrorAction SilentlyContinue }
        }
        if (-not (Test-Path $c)) {
            $vd = Join-Path $env:USERPROFILE 'Downloads\vendor-deps.7z'
            $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
            if ((Test-Path $vd) -and (Test-Path $tar)) {
                $tmp = Join-Path $Root '.build\installer\_vd_tk'
                Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
                New-Item -ItemType Directory -Force $tmp | Out-Null
                & $tar -xf $vd -C $tmp "tiktoken/$n1" 2>$null
                $hit = Join-Path $tmp "tiktoken\$n1"
                if (Test-Path $hit) { Copy-Item $hit $c -Force; Info "tiktoken ${enc}: taken from vendor-deps.7z" }
                Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        # a truncated/error page must not ship: BPE files are "<base64 token> <rank>" lines
        if ((Test-Path $c) -and (Get-Item $c).Length -gt 1000000 -and ((Get-Content $c -TotalCount 1) -match '^\S+ \d+$')) {
            Copy-Item $c (Join-Path $tkOut $n1) -Force
            Copy-Item $c (Join-Path $tkOut $n2) -Force
            $tkOk++
        } else { Remove-Item $c -Force -ErrorAction SilentlyContinue }
    }
    if ($tkOk -eq 2) { Info "tiktoken vocab bundled ($tkOk/2)" }
    else { Remove-Item $tkOut -Recurse -Force -ErrorAction SilentlyContinue; Info "tiktoken vocab not bundled ($tkOk/2) - token counting falls back to a download at first use, or to the length//2 estimate." }
    # Pin the QAIRT version for THIS package: patch only the staged copy of qairt_release.json (text replace keeps the
    # file's formatting and the other fields), then read it back so a silent no-op cannot ship the wrong version.
    $stRel = Join-Path $stage 'scripts\qairt_release.json'
    if ($QairtVersion -ne $QairtDefault) {
        $txt = [IO.File]::ReadAllText($stRel)
        $new = [regex]::Replace($txt, '("qairt_version"\s*:\s*")[^"]*(")', "`${1}$QairtVersion`${2}")
        [IO.File]::WriteAllText($stRel, $new, (New-Object Text.UTF8Encoding($false)))
    }
    $stGot = [string](Get-Content $stRel -Raw | ConvertFrom-Json).qairt_version
    if ($stGot -ne $QairtVersion) { Fail "staged qairt_release.json says '$stGot', expected '$QairtVersion'" }
    Info "staged qairt_version: $stGot"
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
    & $IsccPath /Q "/DAppVersion=$Version" "/DArch=$a" "/DQairtTag=$AbTag" "/DStageDir=$stage" "/DOutDir=$OutDir" $iss
    if ($LASTEXITCODE -ne 0) { Fail "ISCC failed for $a (exit $LASTEXITCODE)" }
    if (-not $KeepStage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
}

Info 'done:'
Get-ChildItem $OutDir -Filter "QAIModelBuilder-$Version-qairt$AbTag-*-setup.exe" |
    ForEach-Object { '  {0}  {1:N1} MB' -f $_.FullName, ($_.Length / 1MB) }

