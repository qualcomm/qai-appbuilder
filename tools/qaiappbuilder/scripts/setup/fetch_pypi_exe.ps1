# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Get a native .exe that PyPI ships inside a wheel (uv, ast-grep-cli) - no GitHub needed.

.DESCRIPTION
    uv and ast-grep publish win_arm64 / win_amd64 wheels whose only payload is the
    executable under  <name>-<ver>.data/scripts/.  A wheel is a zip, so:
      1. read the PEP 503 simple index (HTML) of each index in -Indexes, in order;
      2. pick the wheel  <pkg>-<ver>-py3-none-<platform>.whl  (exact -Version, else the
         highest final release; yanked files are skipped);
      3. download it with download_with_aria2c.ps1 (aria2c when present, retries, stall watchdog);
      4. verify the SHA-256 published in the index link fragment;
      5. extract the requested executables into -OutDir.
    The next index is tried when one fails at any stage. Exit code 0 = every requested exe
    is in -OutDir; 1 = failed (the caller falls back to the GitHub release).

    Log lines are prefixed [PYPI-EXE] so they are easy to find in the install log.

.PARAMETER Package   PyPI project name, e.g. uv or ast-grep-cli
.PARAMETER Platform  win_arm64 | win_amd64
.PARAMETER Exe       Comma list of exe names to extract, e.g. uv.exe,uvx.exe
.PARAMETER OutDir    Destination directory (created if needed)
.PARAMETER Version   Exact version to use; empty = newest final release on the index
.PARAMETER Indexes   Comma list of simple-index base URLs, tried in order
.PARAMETER Profile   official | cn. Default index order: official = pypi.org then tuna;
                     cn = tuna then pypi.org. Ignored when -Indexes is given.
.PARAMETER ProxyUrl  HTTP proxy for index and download
.PARAMETER Aria2cExe Optional aria2c.exe for the download
.PARAMETER WorkDir   Scratch directory (default: <OutDir>\..\..\downloads)
#>
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][ValidateSet('win_arm64', 'win_amd64')][string]$Platform,
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$Version = '',
    [string]$Indexes = '',
    [string]$Profile = 'official',
    [string]$ProxyUrl = '',
    [string]$Aria2cExe = '',
    [string]$WorkDir = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Say([string]$m) { [Console]::WriteLine("[PYPI-EXE] $m") }

if (-not $Indexes) {
    $Indexes = if ($Profile -eq 'cn') { 'https://pypi.tuna.tsinghua.edu.cn/simple,https://pypi.org/simple' }
               else { 'https://pypi.org/simple,https://pypi.tuna.tsinghua.edu.cn/simple' }
}
$idxList = @($Indexes -split ',' | ForEach-Object { $_.Trim().TrimEnd('/') } | Where-Object { $_ })
$exes = @($Exe -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$norm = ($Package.ToLower() -replace '[-_.]+', '_')            # wheel file name prefix
$proj = ($Package.ToLower() -replace '[_.]+', '-')             # index URL name
if (-not $WorkDir) { $WorkDir = Join-Path (Split-Path -Parent (Split-Path -Parent ([IO.Path]::GetFullPath($OutDir)))) 'downloads' }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$dl = Join-Path $PSScriptRoot 'download_with_aria2c.ps1'

# Newest wheel for this package/platform in a PEP 503 HTML page. Returns $null if none.
function Select-Wheel([string]$Html, [string]$BaseUrl) {
    $best = $null
    foreach ($m in [regex]::Matches($Html, '<a\s+([^>]*?)href="([^"]+)"([^>]*)>([^<]+)</a>')) {
        $attrs = $m.Groups[1].Value + ' ' + $m.Groups[3].Value
        $href = [Net.WebUtility]::HtmlDecode($m.Groups[2].Value)
        $name = $m.Groups[4].Value.Trim()
        if ($attrs -match 'data-yanked') { continue }
        $w = [regex]::Match($name, '^' + [regex]::Escape($norm) + '-([0-9][0-9.]*)-py3-none-' + [regex]::Escape($Platform) + '\.whl$')
        if (-not $w.Success) { continue }
        $ver = $w.Groups[1].Value
        if ($Version -and $ver -ne $Version) { continue }
        $v = $null; try { $v = [version]$ver } catch { continue }
        if (-not $best -or $v -gt $best.V) {
            $sha = ''; $hm = [regex]::Match($href, '#sha256=([0-9a-fA-F]{64})$'); if ($hm.Success) { $sha = $hm.Groups[1].Value.ToLower() }
            $url = ([Uri]::new([Uri]($BaseUrl + '/'), ($href -replace '#.*$', ''))).AbsoluteUri
            $best = [pscustomobject]@{ V = $v; Version = $ver; Name = $name; Url = $url; Sha256 = $sha }
        }
    }
    $best
}

$tmp = Join-Path $WorkDir ('_pypi_exe_' + $proj)
foreach ($idx in $idxList) {
    Say "index $idx : looking for $Package$(if ($Version) { " == $Version" } else { ' (newest)' }) [$Platform]"
    try {
        $page = "$idx/$proj/"
        $args2 = @{ Uri = $page; UseBasicParsing = $true; TimeoutSec = 25; Headers = @{ Accept = 'text/html'; 'User-Agent' = 'qai-setup' } }
        if ($ProxyUrl) { $args2.Proxy = $ProxyUrl }
        $resp = Invoke-WebRequest @args2
        # Windows PowerShell 5.1 returns byte[] for unknown content types (e.g. the PEP 691 vendor type):
        # decode defensively instead of casting.
        $html = if ($resp.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($resp.Content) } else { [string]$resp.Content }
        $w = Select-Wheel $html $page.TrimEnd('/')
        if (-not $w) { Say "no matching wheel on this index"; continue }
        Say "found $($w.Name)  sha256=$(if ($w.Sha256) { $w.Sha256.Substring(0, 12) + '...' } else { '(none published)' })"

        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $whl = Join-Path $tmp $w.Name
        $dargs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $dl, '-Url', $w.Url, '-OutFile', $whl, '-MinSize', '1000000', '-ZipTest', '-MaxRetries', '3', '-StallTimeoutSec', '60', '-AttemptTimeoutSec', '300')
        if ($Aria2cExe -and (Test-Path -LiteralPath $Aria2cExe)) { $dargs += @('-Aria2cExe', $Aria2cExe) }
        if ($ProxyUrl) { $dargs += @('-ProxyUrl', $ProxyUrl) }
        & powershell.exe @dargs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $whl)) { Say "download failed (exit $LASTEXITCODE) - next index"; continue }

        if ($w.Sha256) {
            # .NET directly: Get-FileHash is not found when Windows PowerShell 5.1 is started from a
            # PowerShell 7 terminal (it inherits the PS7 PSModulePath).
            $sha = [Security.Cryptography.SHA256]::Create()
            $fsh = [IO.File]::OpenRead($whl)
            try { $got = ([BitConverter]::ToString($sha.ComputeHash($fsh)) -replace '-', '').ToLower() } finally { $fsh.Dispose(); $sha.Dispose() }
            if ($got -ne $w.Sha256) { Say "SHA-256 MISMATCH (index $($w.Sha256), file $got) - discarding"; continue }
            Say "sha256 verified"
        }

        $zip = [IO.Compression.ZipFile]::OpenRead($whl)
        try {
            $missing = @()
            foreach ($e in $exes) {
                $ent = $zip.Entries | Where-Object { $_.FullName -match '\.data/scripts/' -and $_.Name -ieq $e } | Select-Object -First 1
                if (-not $ent) { $missing += $e; continue }
                $dest = Join-Path $OutDir $e
                $part = $dest + '.part'
                [IO.Compression.ZipFileExtensions]::ExtractToFile($ent, $part, $true)
                Move-Item -LiteralPath $part -Destination $dest -Force
                Say "extracted $e ($([math]::Round((Get-Item -LiteralPath $dest).Length / 1MB, 1)) MB) -> $OutDir"
            }
            if ($missing.Count -gt 0) { Say "wheel has no: $($missing -join ', ') - next index"; continue }
        } finally { $zip.Dispose() }

        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Say "OK $Package $($w.Version) from $idx"
        exit 0
    } catch {
        Say "index failed: $($_.Exception.Message)"
        continue
    }
}
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
Say "FAILED to get $Package from every index"
exit 1
