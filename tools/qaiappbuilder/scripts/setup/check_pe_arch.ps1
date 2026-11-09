# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Check the CPU architecture of a Windows PE file (exe / dll).
.DESCRIPTION
    Reads the COFF "Machine" field. Exit codes:
      0  the file matches -Arch
      1  the file exists but is another architecture (or is not a valid PE file)
      2  the file does not exist
    Used by Setup.bat / installer\_setup_env.bat so that a downloaded tool (aria2c.exe) of the wrong architecture is
    replaced instead of being reused forever.
.EXAMPLE
    powershell -NoProfile -File scripts\setup\check_pe_arch.ps1 -Path data\bin\aria2c\aria2c.exe -Arch arm64
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][ValidateSet('arm64', 'x64')][string]$Arch,
    [switch]$Quiet
)
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { exit 2 }
$want = @{ arm64 = 0xAA64; x64 = 0x8664 }[$Arch]
$names = @{ 0xAA64 = 'arm64'; 0x8664 = 'x64'; 0x014C = 'x86'; 0xA641 = 'arm64ec' }
try {
    $fs = [IO.File]::OpenRead((Resolve-Path -LiteralPath $Path).Path)
    try {
        $br = New-Object IO.BinaryReader($fs)
        if ($br.ReadUInt16() -ne 0x5A4D) { throw 'no MZ header' }
        $fs.Position = 0x3C
        $fs.Position = $br.ReadInt32()
        if ($br.ReadUInt32() -ne 0x00004550) { throw 'no PE signature' }
        $machine = [int]$br.ReadUInt16()
    } finally { $fs.Dispose() }
} catch {
    if (-not $Quiet) { Write-Host "[WARN] $Path is not a valid PE file ($($_.Exception.Message))." }
    exit 1
}
if ($machine -eq $want) { exit 0 }
if (-not $Quiet) {
    $got = $names[$machine]; if (-not $got) { $got = '0x{0:X4}' -f $machine }
    Write-Host "[WARN] $Path is $got, expected $Arch."
}
exit 1
