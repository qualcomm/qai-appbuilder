# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Build the QAI AppBuilder Linux package (tar.gz) on Windows.

.DESCRIPTION
    1. (optional) refresh frontend\dist via Build.bat
    2. export the COMMITTED sources of HEAD with `git archive` (LF line endings,
       exactly what is in the repository - not the working tree)
    3. add the built frontend\dist plus frontend\dist\.build_info
    4. pack everything under one top-level directory:
         dist\linux\QAIAppBuilder-<version>-qairt<A.B.C>-linux.tar.gz

    On the Linux machine:  tar -xzf <file>  &&  cd QAIAppBuilder-<version>-qairt<A.B.C>-linux  &&  bash setup.sh  &&  bash start.sh
    setup.sh finds frontend\dist already built and skips Node/pnpm and the frontend build.

    Not packed: desktop\ (Tauri, Windows only) and installer\ (this script, Inno Setup).
    The package is architecture independent; Python/QAIRT environments are created by setup.sh.

.PARAMETER SkipFrontend  do not run Build.bat; use the existing frontend\dist
.PARAMETER QairtVersion  QAIRT version shown in the file name (default: scripts\qairt_release.json); only the A.B.C part is used
.PARAMETER Version       version used in the file name (default: build_info.json "version"); no version file is changed
.PARAMETER OutDir        output directory (default: dist\linux)
.PARAMETER Help          show this help
#>
param(
    [switch]$SkipFrontend,
    [string]$Version = '',
    [string]$QairtVersion = '',
    [string]$OutDir = '',
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
function Info($m) { Write-Host "[INFO] $m" }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }

if ($Help) { Get-Help $MyInvocation.MyCommand.Path -Detailed; exit 0 }

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

foreach ($tool in 'git', 'tar') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool not found on PATH" }
}
& git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Fail "$Root is not inside a git work tree" }

# --- version (only used in the file name) -----------------------------------
if (-not $Version) {
    $bi = Get-Content (Join-Path $Root 'build_info.json') -Raw | ConvertFrom-Json
    $Version = [string]$bi.version
}
if ($Version -notmatch '^\d+(\.\d+){1,3}') { Fail "bad version '$Version'" }
Info "version: $Version"

# QAIRT version in the file name, aligned with the Windows installer (read from scripts\qairt_release.json)
if (-not $QairtVersion) {
    $QairtVersion = [string](Get-Content (Join-Path $Root 'scripts\qairt_release.json') -Raw | ConvertFrom-Json).qairt_version
}
if ($QairtVersion -notmatch '^(\d+\.\d+\.\d+)') { Fail "bad qairt_version '$QairtVersion'" }
$QairtShort = $Matches[1]
Info "QAIRT: $QairtVersion (file name uses $QairtShort)"

# --- 1. frontend --------------------------------------------------------------
if (-not $SkipFrontend) {
    Info 'building frontend via Build.bat ...'
    & cmd.exe /c "`"$Root\Build.bat`""
    if ($LASTEXITCODE -ne 0) { Fail "Build.bat failed (exit $LASTEXITCODE)" }
}
$dist = Join-Path $Root 'frontend\dist'
if (-not (Test-Path (Join-Path $dist 'index.html'))) {
    Fail 'frontend\dist\index.html missing - run Build.bat first (or drop -SkipFrontend).'
}
if (-not (Get-ChildItem (Join-Path $dist 'assets') -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    Fail 'frontend\dist\assets is empty - frontend build failed?'
}

# --- 2. commit info -------------------------------------------------------------
$commit = (& git rev-parse --short HEAD).Trim()
$dirty = [bool](& git status --porcelain)
if ($dirty) {
    Warn "working tree has uncommitted changes - the package contains the COMMITTED sources of HEAD ($commit), not those changes."
    $commit = "$commit-dirty"
}
$srcDirty = [bool](& git status --porcelain -- frontend/src frontend/package.json frontend/index.html)
if ($srcDirty) { Warn 'frontend sources have uncommitted changes - frontend\dist may not match HEAD.' }

# --- 3. stage: git archive + frontend\dist ---------------------------------------
$Name = "QAIAppBuilder-$Version-qairt$QairtShort-linux"
if (-not $OutDir) { $OutDir = Join-Path $Root 'dist\linux' }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$Archive = Join-Path $OutDir "$Name.tar.gz"

$work = Join-Path $Root '.build\linux_package'
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
$stage = Join-Path $work $Name
New-Item -ItemType Directory -Force $stage | Out-Null

Info "exporting HEAD ($commit) with git archive ..."
$srcTar = Join-Path $work 'src.tar'
# core.autocrlf=false: with autocrlf=true (the Windows default) git archive would convert every text file to CRLF.
& git -c core.autocrlf=false -c core.eol=lf archive --format=tar -o $srcTar HEAD -- . ':(exclude)desktop' ':(exclude)installer'
if ($LASTEXITCODE -ne 0) { Fail "git archive failed ($LASTEXITCODE)" }
& tar -xf $srcTar -C $stage
if ($LASTEXITCODE -ne 0) { Fail "extracting the git archive failed ($LASTEXITCODE)" }
Remove-Item $srcTar -Force

$null = robocopy $dist (Join-Path $stage 'frontend\dist') /E /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { Fail "robocopy frontend\dist failed ($LASTEXITCODE)" }

$buildInfo = "version=$Version`nqairt=$QairtVersion`ncommit=$commit`nbuilt_at=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))`n"
[IO.File]::WriteAllText((Join-Path $stage 'frontend\dist\.build_info'), $buildInfo, (New-Object Text.UTF8Encoding $false))

# --- 4. safety checks, then pack -------------------------------------------------
foreach ($must in 'setup.sh', 'start.sh', 'build.sh', 'pyproject.toml', 'frontend\dist\index.html') {
    if (-not (Test-Path (Join-Path $stage $must))) { Fail "package is missing $must" }
}
# CRLF in a shell script breaks `#!/usr/bin/env bash` on Linux; check every shell/python/text file we ship.
$crlf = Get-ChildItem $stage -Recurse -File -Include *.sh,*.py,*.toml,*.json,*.md,*.ts,*.vue,*.yaml,*.yml |
    Where-Object { $_.FullName -notlike "$stage\frontend\dist\*" } |
    Where-Object { [IO.File]::ReadAllText($_.FullName).Contains("`r") }
if ($crlf) { Fail "files with CRLF line endings, e.g. $(($crlf | Select-Object -First 3 | ForEach-Object { $_.FullName.Substring($stage.Length + 1) }) -join ', ')" }

if (Test-Path $Archive) { Remove-Item $Archive -Force }
Info "packing -> $Archive"
& tar -czf $Archive -C $work $Name
if ($LASTEXITCODE -ne 0) { Fail "tar failed ($LASTEXITCODE)" }
Remove-Item $work -Recurse -Force

$list = & tar -tzf $Archive
if ($LASTEXITCODE -ne 0) { Fail 'cannot read the archive back' }
if ($list -notcontains "$Name/frontend/dist/index.html") { Fail 'archive does not contain frontend/dist/index.html' }
if ($list -notcontains "$Name/setup.sh") { Fail 'archive does not contain setup.sh' }

$size = [math]::Round((Get-Item $Archive).Length / 1MB, 1)
$sha = (Get-FileHash $Archive -Algorithm SHA256).Hash
Write-Host ''
Info 'done:'
Write-Host "  $Archive  ($size MB, $($list.Count) entries)"
Write-Host "  commit : $commit"
Write-Host "  sha256 : $sha"
Write-Host ''
Write-Host '  On the Linux machine:'
Write-Host "    tar -xzf $Name.tar.gz"
Write-Host "    cd $Name && bash setup.sh && bash start.sh"
