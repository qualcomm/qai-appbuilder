# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# =============================================================================
# install_vs.ps1
# QAIModelBuilder - Visual Studio 2022 Silent Installer
# =============================================================================
#
# Installs or updates Visual Studio 2022 Community with the components required
# for QAIRT model compilation on BOTH ARM64 (WoS) and x64 hosts. The component
# list below is a superset that satisfies either target arch, so a single
# invocation covers both -- no -Arch parameter is needed (installing an extra
# tool set is safe and idempotent, and lets a WoS host cross-compile x64 tools
# and vice versa without a second run).
#
# Required components:
#   Microsoft.Component.MSBuild                              - MSBuild
#   Microsoft.VisualStudio.Component.VC.Tools.x86.x64        - MSVC x64/x86
#   Microsoft.VisualStudio.Component.VC.Tools.ARM64          - MSVC ARM64
#   Microsoft.VisualStudio.Component.VC.CMake.Project        - CMake
#   Microsoft.VisualStudio.ComponentGroup.NativeDesktop.Llvm.Clang - clang-cl
#   Microsoft.VisualStudio.Component.Windows11SDK.22621      - Windows SDK
#
# Usage:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\install_vs.ps1
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\install_vs.ps1 -LogDir "data\downloads"
# =============================================================================

param(
    [string]$LogDir = "data\downloads"
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$VS_COMPONENTS = @(
    'Microsoft.Component.MSBuild',
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
    'Microsoft.VisualStudio.Component.VC.Tools.ARM64',
    'Microsoft.VisualStudio.Component.VC.CMake.Project',
    'Microsoft.VisualStudio.ComponentGroup.NativeDesktop.Llvm.Clang',
    'Microsoft.VisualStudio.Component.Windows11SDK.22621'
)

$VSWHERE    = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$INSTALLER  = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe'
$VS_DL_URL  = 'https://aka.ms/vs/17/release/vs_community.exe'

$vsOut = Join-Path $LogDir 'vs_install.log'
$vsErr = Join-Path $LogDir 'vs_install.err.log'

# Ensure log directory exists
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

# Build --add argument list
$addArgs = ($VS_COMPONENTS | ForEach-Object { "--add $_" }) -join ' '

# ---------------------------------------------------------------------------
# Helper: real-time progress watcher
# ---------------------------------------------------------------------------
function Watch-VSInstall {
    param(
        [System.Diagnostics.Process]$Proc,
        [string]$LogFile
    )

    $startTime   = Get-Date
    $lastLine    = ''
    $lastSize    = 0
    $dots        = 0
    $spinner     = @('|', '/', '-', '\')
    $spinIdx     = 0

    # Phase labels based on elapsed time (approximate VS install stages)
    function Get-PhaseLabel([int]$ElapsedSec, [long]$CurSize, [long]$LastSize) {
        if ($ElapsedSec -lt 15)  { return 'Initializing' }
        if ($CurSize -gt $LastSize) { return 'Downloading packages' }
        if ($ElapsedSec -lt 120) { return 'Resolving dependencies' }
        if ($ElapsedSec -lt 600) { return 'Installing components' }
        return 'Finalizing installation'
    }

    while (-not $Proc.HasExited) {
        Start-Sleep -Seconds 5

        $elapsed = [int](New-TimeSpan -Start $startTime -End (Get-Date)).TotalSeconds
        $em      = [math]::Floor($elapsed / 60)
        $es      = $elapsed % 60
        $timeStr = '{0}m{1:D2}s' -f $em, $es
        $spin    = $spinner[$spinIdx % 4]; $spinIdx++

        $curSize = 0

        if (Test-Path $LogFile) {
            $curSize = (Get-Item $LogFile).Length
            $lines   = Get-Content $LogFile -ErrorAction SilentlyContinue
            $cur     = $lines | Where-Object {
                $_ -match 'Preparing:|Applying:|Downloading:|Installing:|Configuring:|Verifying:|Acquiring:'
            } | Select-Object -Last 1

            if ($cur -and $cur -ne $lastLine) {
                $lastLine = $cur
                $short    = $cur -replace '^.*?(Preparing:|Applying:|Downloading:|Installing:|Configuring:|Verifying:|Acquiring:)', '$1'
                if ($short.Length -gt 52) { $short = $short.Substring(0, 49) + '...' }
                [Console]::Write((' {0} [VS] [{1}] {2}' -f $spin, $timeStr, $short).PadRight(78) + "`r")
            }
            else {
                $phase = Get-PhaseLabel -ElapsedSec $elapsed -CurSize $curSize -LastSize $lastSize
                $dots  = ($dots + 1) % 4
                [Console]::Write((' {0} [VS] [{1}] {2}{3}' -f $spin, $timeStr, $phase, ('.' * ($dots + 1))).PadRight(78) + "`r")
            }
        }
        else {
            $dots = ($dots + 1) % 4
            [Console]::Write((' {0} [VS] [{1}] Initializing{2}' -f $spin, $timeStr, ('.' * ($dots + 1))).PadRight(78) + "`r")
        }

        $lastSize = $curSize
    }

    # Clear the progress line and print completion
    [Console]::Write(''.PadRight(78) + "`r")
    [Console]::WriteLine('')
}

# ---------------------------------------------------------------------------
# Helper: run VS Installer and watch progress
# NOTE: bootstrapper exit code is unreliable (it spawns the real installer and
#       may exit before it finishes). We verify success by checking vswhere
#       after the process exits.
# ---------------------------------------------------------------------------
function Invoke-VSInstaller {
    param(
        [string]$Executable,
        [string]$Arguments
    )

    $p = Start-Process -FilePath $Executable `
        -ArgumentList $Arguments `
        -PassThru `
        -RedirectStandardOutput $vsOut `
        -RedirectStandardError  $vsErr

    Watch-VSInstall -Proc $p -LogFile $vsOut
    $p.WaitForExit()

    # Verify success by checking whether VS is now detectable via vswhere.
    # This is more reliable than exit codes (bootstrapper may return $null).
    $vsDetected = $false
    if (Test-Path $VSWHERE) {
        $detected = & $VSWHERE -latest -property productId 2>$null
        $vsDetected = (-not [string]::IsNullOrEmpty($detected))
    }

    if ($vsDetected) {
        Remove-Item $vsOut, $vsErr -Force -ErrorAction SilentlyContinue
        return $true
    }
    else {
        # VS not detected — show friendly failure message
        Write-Host ''
        Write-Host '  [!] VS installation could not be verified.' -ForegroundColor Red
        Write-Host "  [!] Log file: $vsOut" -ForegroundColor Red
        Write-Host '  [!] To fix manually: open Visual Studio Installer and click Repair or Modify.' -ForegroundColor Yellow
        Write-Host ''
        return $false
    }
}

# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

# Detect existing VS installation
$productId = $null
if (Test-Path $VSWHERE) {
    $productId = & $VSWHERE -latest -property productId 2>$null
}

if ($productId) {
    # VS already installed. Fast-path: if every required component is already
    # present, skip the (slow) `modify` round-trip entirely — re-running the
    # installer to add nothing still spins up the VS Installer and costs time.
    # Use `vswhere -requires <comp> -property productId`: it returns the
    # productId only when that component IS installed, empty otherwise. This is
    # the documented, version-stable way to probe per-component presence.
    $missing = @()
    foreach ($comp in $VS_COMPONENTS) {
        $hit = & $VSWHERE -latest -products * -requires $comp -property productId 2>$null
        if ([string]::IsNullOrEmpty($hit)) { $missing += $comp }
    }

    if ($missing.Count -eq 0) {
        Write-Host "[SKIP] VS 2022 found ($productId) - all required components already installed."
        exit 0
    }

    # VS already installed but some components missing — add them silently.
    Write-Host "[INFO] VS 2022 found ($productId) - adding $($missing.Count) missing component(s) silently..."
    $installArgs = "modify --productId $productId --channelId VisualStudio.17.Release $addArgs --quiet --norestart"
    $ok = Invoke-VSInstaller -Executable $INSTALLER -Arguments $installArgs

    if (-not $ok) {
        # modify failed — VS state may be corrupted from a previous interrupted install.
        # Attempt repair first, then retry modify.
        Write-Host '[INFO] modify failed - attempting repair to recover VS state...' -ForegroundColor Yellow
        $repairArgs = "repair --productId $productId --channelId VisualStudio.17.Release --quiet --norestart"
        $repairOk = Invoke-VSInstaller -Executable $INSTALLER -Arguments $repairArgs
        if ($repairOk) {
            Write-Host '[INFO] Repair succeeded - retrying component installation...'
            $ok = Invoke-VSInstaller -Executable $INSTALLER -Arguments $installArgs
        }
    }

    if ($ok) {
        Write-Host '[OK]   VS components installed/verified.'
    }
    else {
        Write-Host '[WARN] VS component installation failed. Try running Visual Studio Installer manually to repair.' -ForegroundColor Yellow
    }
}
else {
    # VS not installed — download bootstrapper and install with all components
    Write-Host '[INFO] VS 2022 not found - downloading bootstrapper and installing with required components...'

    $guid        = [System.Guid]::NewGuid().ToString('N').Substring(0, 8)
    $bootstrapper = Join-Path $env:TEMP "vs_community_$guid.exe"

    Write-Host '[INFO] Downloading VS bootstrapper (~4MB)...'
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $VS_DL_URL -OutFile $bootstrapper -UseBasicParsing -ErrorAction Stop

    Write-Host ''
    Write-Host '[INFO] Running VS installer with required components (this may take 10-20 minutes)...' -ForegroundColor Yellow
    Write-Host ''

    $installArgs = "--quiet --norestart --wait $addArgs"
    $ok = Invoke-VSInstaller -Executable $bootstrapper -Arguments $installArgs

    Remove-Item $bootstrapper -Force -ErrorAction SilentlyContinue

    if ($ok) {
        Write-Host '[OK]   VS 2022 Community installed with all required components.'
    }
}
