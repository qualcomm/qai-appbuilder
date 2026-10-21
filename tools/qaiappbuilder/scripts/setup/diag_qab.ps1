# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Diagnose why `import qai_appbuilder` fails (e.g. WinError 1114 "DLL initialization routine failed").

.DESCRIPTION
    Collects, in one report (printed and saved to -OutFile):
      * machine: OS, CPU model / architecture, virtual machine or not, CPU feature flags
      * the venv python: architecture, which vcruntime140.dll it ships
      * a load test, one native file at a time, with the exact Windows error code
        (vcruntime140 / msvcp140 / msvcp140_atomic_wait / libappbuilder / Genie / import)
      * the DLLs really mapped into the python process after a failed load, with versions
        (a vcruntime140.dll in the Python folder wins over System32 - a mismatch with the
        newer msvcp140.dll in System32 is one classic cause of WinError 1114)
      * signature / Mark-of-the-Web state of the package files
      * Code Integrity, AppLocker and Defender events (full text, last 24 h)
      * WDAC / Smart App Control state, installed VC++ runtimes, antivirus products

    Read-only: nothing is installed or changed.

.PARAMETER Python   python.exe of the venv to test (default: x64 venv, else arm64 venv)
.PARAMETER OutFile  report path (default: data\logs\qab_diag_<time>.txt)
#>
param(
    [string]$Python = '',
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

if (-not $Python) {
    foreach ($n in '.venv_x64_313', '.venv_arm64_313') {
        $c = Join-Path $env:LOCALAPPDATA "QAIModelBuilder\envs\$n\Scripts\python.exe"
        if (Test-Path -LiteralPath $c) { $Python = $c; break }
    }
}
if (-not $OutFile) { $OutFile = Join-Path $Root ('data\logs\qab_diag_{0}.txt' -f (Get-Date -Format 'yyyyMMdd_HHmmss')) }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null
[IO.File]::WriteAllText($OutFile, '', (New-Object Text.UTF8Encoding($false)))

function Out-Rep([string]$s = '') {
    [Console]::WriteLine($s)
    [IO.File]::AppendAllText($OutFile, $s + "`r`n", (New-Object Text.UTF8Encoding($false)))
}
function Section([string]$t) { Out-Rep ''; Out-Rep ('==== ' + $t + ' ' + ('=' * [Math]::Max(3, 70 - $t.Length))) }
function Safe([scriptblock]$b) { try { & $b } catch { Out-Rep ('  (failed: ' + $_.Exception.Message + ')') } }

Out-Rep ('qai_appbuilder import diagnosis   ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'))
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    Out-Rep "No venv python found under $env:LOCALAPPDATA\QAIModelBuilder\envs. Run setup_env.bat first."
    exit 2
}
Out-Rep "python: $Python"

# ---------------------------------------------------------------- machine
Section 'Machine'
Safe {
    $os = Get-CimInstance Win32_OperatingSystem
    Out-Rep ("OS            : {0}  build {1}  {2}" -f $os.Caption, $os.BuildNumber, $os.OSArchitecture)
    Out-Rep ("Host arch env : PROCESSOR_ARCHITECTURE={0}  PROCESSOR_ARCHITEW6432={1}" -f $env:PROCESSOR_ARCHITECTURE, $env:PROCESSOR_ARCHITEW6432)
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Out-Rep ("CPU           : {0}  ({1} cores / {2} threads)  arch code {3}" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors, $cpu.Architecture)
    $cs = Get-CimInstance Win32_ComputerSystem
    Out-Rep ("Computer      : {0} {1}   hypervisor present: {2}" -f $cs.Manufacturer, $cs.Model, $cs.HypervisorPresent)
}

# ---------------------------------------------------------------- python side
# Every probe runs in its OWN python process with -u (unbuffered) and a timeout: a native DLL that
# kills the process (STATUS_DLL_INIT_FAILED 0xC0000142 ...) would otherwise take all buffered output
# with it and leave this section empty - which is exactly what happened on the first report.
Section 'Python and the native files (each test in its own process)'

# Windows PowerShell 5.1 types 0xFFFFFFFF as Int32 (-1), so "-band 0xFFFFFFFF" does not mask: go through bytes.
function ConvertTo-U32([long]$code) { [BitConverter]::ToUInt32([BitConverter]::GetBytes([long]$code), 0) }   # low 32 bits of the 64-bit value
function Get-ExitMeaning([long]$code) {
    # Match on the 8-digit hex STRING: in Windows PowerShell 5.1 a literal such as 0xC0000142 is a negative
    # Int32 and never equals the unsigned exit code.
    $h = '{0:X8}' -f (ConvertTo-U32 $code)
    switch ($h) {
        '00000000' { 'ok' }
        '00000001' { 'python error (see output)' }
        'C0000142' { 'STATUS_DLL_INIT_FAILED: a DLL start-up routine failed - the process was terminated by Windows' }
        'C0000135' { 'STATUS_DLL_NOT_FOUND' }
        'C000007B' { 'STATUS_INVALID_IMAGE_FORMAT: 32/64-bit or ARM64/x64 mismatch' }
        'C0000005' { 'ACCESS_VIOLATION: the native code crashed' }
        'C0000409' { 'STACK_BUFFER_OVERRUN / fail-fast' }
        'C0000374' { 'HEAP_CORRUPTION' }
        'C000001D' { 'ILLEGAL_INSTRUCTION: the DLL uses a CPU instruction this CPU/emulation lacks' }
        'C0000096' { 'PRIVILEGED_INSTRUCTION' }
        'C06D007E' { 'delay-load: module not found' }
        'C06D007F' { 'delay-load: procedure not found' }
        default    { 'unknown status 0x' + $h }
    }
}

# Run python code in a fresh unbuffered process. Returns @{ Code; Timeout; Lines }.
function Invoke-Py([string]$Code, [int]$TimeoutSec = 60) {
    $f = Join-Path $env:TEMP ('qab_diag_{0}_{1}.py' -f $PID, [Guid]::NewGuid().ToString('N').Substring(0, 8))
    [IO.File]::WriteAllText($f, $Code, (New-Object Text.UTF8Encoding($false)))
    $so = $f + '.out'; $se = $f + '.err'
    $res = @{ Code = -1; Timeout = $false; Lines = @() }
    try {
        $proc = Start-Process -FilePath $Python -ArgumentList @('-u', $f) -NoNewWindow -PassThru -RedirectStandardOutput $so -RedirectStandardError $se
        # Windows PowerShell 5.1 quirk: with -PassThru -NoNewWindow, ExitCode stays $null (read as 0 =
        # "success") unless the process handle was touched while the process was running.
        $null = $proc.Handle
        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            $res.Timeout = $true
            try { $proc.Kill() } catch { }
            [void]$proc.WaitForExit(3000)
        }
        $proc.WaitForExit()
        $res.Code = if ($res.Timeout) { -1 } elseif ($null -eq $proc.ExitCode) { -2 } else { [long]$proc.ExitCode }
        $l = @()
        foreach ($x in $so, $se) {
            if (Test-Path -LiteralPath $x) { $l += @(Get-Content -LiteralPath $x -ErrorAction SilentlyContinue | ForEach-Object { [string]$_ }) }
        }
        $res.Lines = $l
    } catch {
        $res.Lines = @('(could not run python: ' + $_.Exception.Message + ')')
    } finally {
        Remove-Item -LiteralPath $f, $so, $se -Force -ErrorAction SilentlyContinue
    }
    $res
}

# ---- 1. facts about the interpreter ----
$infoCode = @'
import sys, os, platform, sysconfig, ctypes, importlib.util
print("python        :", sys.version.split()[0], platform.machine(), sysconfig.get_platform(), "|", sys.executable)
home = sys.base_prefix
print("python home   :", home)
for n in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
    p = os.path.join(home, n)
    print("  in python dir:", n, "->", p if os.path.exists(p) else "(absent: Windows uses System32)")
k32 = ctypes.WinDLL("kernel32")
feat = {10: "SSE2", 13: "SSE3", 36: "SSSE3", 37: "SSE4.1", 38: "SSE4.2", 39: "AVX", 40: "AVX2", 41: "AVX512F", 43: "AVX512 (PF 43)"}
print("cpu features  :", ", ".join("%s=%s" % (v, "yes" if k32.IsProcessorFeaturePresent(k) else "NO") for k, v in sorted(feat.items())))
sp = importlib.util.find_spec("qai_appbuilder")
print("package dir   :", os.path.dirname(sp.origin) if sp and sp.origin else "")
'@
$info = Invoke-Py $infoCode
$pkgDir = $null
foreach ($l in $info.Lines) {
    Out-Rep $l
    if ($l -like 'package dir*') { $pkgDir = ($l -replace '^package dir\s*:\s*', '').Trim() }
}
if ($info.Code -ne 0) { Out-Rep ('  (interpreter probe exit code 0x{0:X8})' -f (ConvertTo-U32 $info.Code)) }

# ---- 2. one load test per process ----
Out-Rep ''
Out-Rep 'load tests (full paths, one process each; a crash only kills that test):'
$sysdir = Join-Path $env:SystemRoot 'System32'
$tests = @()
foreach ($n in 'vcruntime140.dll', 'msvcp140.dll', 'msvcp140_atomic_wait.dll') { $tests += ,@("$n (System32)", (Join-Path $sysdir $n)) }
if ($pkgDir) {
    $tests += ,@('libappbuilder.dll', (Join-Path $pkgDir 'libappbuilder.dll'))
    $tests += ,@('Genie.dll', (Join-Path $pkgDir 'Genie.dll'))
}
foreach ($t in $tests) {
    $path = $t[1] -replace "'", "''"
    $loadCode = "import ctypes, sys`ntry:`n    ctypes.WinDLL(r'$path')`n    print('LOADED')`nexcept OSError as e:`n    print('OSERROR winerror=%s | %s' % (e.winerror, e))`n    sys.exit(3)`n"
    $r = Invoke-Py $loadCode
    $tail = (@($r.Lines) | Where-Object { $_ } | Select-Object -Last 1)
    if ($r.Timeout) {
        Out-Rep ('  TIMEOUT     : {0} (no answer in 60 s)' -f $t[0])
    } elseif ($r.Code -eq 0 -and (@($r.Lines) -contains 'LOADED')) {
        Out-Rep ('  LOAD OK     : {0}' -f $t[0])
    } elseif ($r.Code -eq 0 -or $r.Code -eq -2) {
        Out-Rep ('  UNKNOWN     : {0} | the test did not report a result (exit code unavailable)' -f $t[0])
    } elseif ($r.Code -eq 3) {
        Out-Rep ('  LOAD FAILED : {0} | {1}' -f $t[0], ($tail -replace '^OSERROR\s*', ''))
    } else {
        Out-Rep ('  PROCESS DIED: {0} | exit code 0x{1:X8} = {2}' -f $t[0], (ConvertTo-U32 $r.Code), (Get-ExitMeaning $r.Code))
        if ($tail) { Out-Rep ('                  last output: ' + $tail) }
    }
}

# ---- 3. the real import, then (same process) the runtime DLLs it mapped ----
Out-Rep ''
Out-Rep 'import qai_appbuilder (own process):'
$impCode = @'
import ctypes, sys, os
from ctypes import wintypes
print("STEP start", flush=True)
ok = False
try:
    import qai_appbuilder as q
    print("IMPORT OK: qai_appbuilder", getattr(q, "__version__", "?"), flush=True)
    ok = True
except BaseException as e:
    print("IMPORT FAILED: %s | %s" % (type(e).__name__, e), flush=True)
k32 = ctypes.WinDLL("kernel32"); psapi = ctypes.WinDLL("psapi")
k32.GetCurrentProcess.restype = wintypes.HANDLE
hproc = k32.GetCurrentProcess()
arr = (wintypes.HMODULE * 1024)(); need = wintypes.DWORD()
psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
psapi.EnumProcessModules(hproc, arr, ctypes.sizeof(arr), ctypes.byref(need))
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
buf = ctypes.create_unicode_buffer(1024)
for i in range(need.value // ctypes.sizeof(wintypes.HMODULE)):
    psapi.GetModuleFileNameExW(hproc, arr[i], buf, 1024)
    low = buf.value.lower()
    if any(k in low for k in ("vcruntime", "msvcp", "ucrtbase", "python3", "appbuilder", "genie", "qnn", "d3d12")):
        print("MODULE", buf.value, flush=True)
sys.exit(0 if ok else 3)
'@
$imp = Invoke-Py $impCode
foreach ($l in $imp.Lines) {
    if ($l -like 'MODULE *') {
        $p = $l.Substring(7); $v = ''
        try { $v = (Get-Item -LiteralPath $p).VersionInfo.FileVersion } catch { }
        Out-Rep ('  {0,-22} {1}' -f $v, $p)
    } elseif ($l -ne 'STEP start') {
        Out-Rep ('  ' + $l)
    }
}
if ($imp.Timeout) { Out-Rep '  TIMEOUT: the import did not finish in 60 s' }
elseif ($imp.Code -eq -2 -or ($imp.Code -eq 0 -and -not (@($imp.Lines) | Where-Object { $_ -like 'IMPORT *' }))) { Out-Rep '  UNKNOWN: the import test did not report a result' }
elseif ($imp.Code -ne 0 -and $imp.Code -ne 3) { Out-Rep ('  PROCESS DIED: exit code 0x{0:X8} = {1}' -f (ConvertTo-U32 $imp.Code), (Get-ExitMeaning $imp.Code)) }

# ---------------------------------------------------------------- package files
Section 'Package files: signature and Mark-of-the-Web'
if ($pkgDir -and (Test-Path -LiteralPath $pkgDir)) {
    foreach ($f in Get-ChildItem -LiteralPath $pkgDir -File | Where-Object { $_.Extension -in '.dll', '.pyd' }) {
        $sig = 'n/a'; Safe { $script:sig = (Get-AuthenticodeSignature -LiteralPath $f.FullName).Status.ToString() }
        $motw = 'no'; Safe { if (Get-Item -LiteralPath $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue) { $script:motw = 'YES (downloaded-file mark)' } }
        Out-Rep ('  {0,-34} {1,8:N1} MB  signature={2,-12} MOTW={3}' -f $f.Name, ($f.Length / 1MB), $sig, $motw)
    }
} else { Out-Rep '  (package directory not found)' }

# ---------------------------------------------------------------- policy
Section 'Application control state'
Safe {
    $dg = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -ErrorAction Stop
    Out-Rep ('  WDAC kernel-mode policy status : {0}   (0=off 1=audit 2=enforced)' -f $dg.CodeIntegrityPolicyEnforcementStatus)
    Out-Rep ('  WDAC user-mode policy status   : {0}' -f $dg.UsermodeCodeIntegrityPolicyEnforcementStatus)
}
Safe {
    $sac = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState
    Out-Rep ('  Smart App Control state        : {0}   (0=off 1=on 2=evaluation)' -f $sac)
}
Safe {
    $al = Get-AppLockerPolicy -Effective -ErrorAction Stop
    $n = @($al.RuleCollections | ForEach-Object { $_ }).Count
    Out-Rep ('  AppLocker effective rules      : {0}' -f $n)
}
Safe {
    $av = Get-CimInstance -Namespace 'root\SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction Stop
    foreach ($a in $av) { Out-Rep ('  Antivirus product              : {0}' -f $a.displayName) }
}

Section 'Installed Visual C++ runtimes'
Safe {
    foreach ($k in 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\arm64', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64') {
        if (Test-Path $k) { $r = Get-ItemProperty $k; Out-Rep ('  {0,-70} {1} installed={2}' -f $k.Replace('HKLM:\SOFTWARE\', ''), $r.Version, $r.Installed) }
    }
    foreach ($n in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140_atomic_wait.dll') {
        $p = Join-Path $env:SystemRoot "System32\$n"
        if (Test-Path $p) { Out-Rep ('  System32\{0,-28} {1}' -f $n, (Get-Item $p).VersionInfo.FileVersion) } else { Out-Rep ('  System32\{0,-28} MISSING' -f $n) }
    }
}

# ---------------------------------------------------------------- event logs
Section 'Event logs, last 24 h (full text)'
$since = (Get-Date).AddHours(-24)
Out-Rep ('  now (this machine): ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
foreach ($log in 'Microsoft-Windows-CodeIntegrity/Operational', 'Microsoft-Windows-AppLocker/EXE and DLL', 'Microsoft-Windows-AppLocker/MSI and Script', 'Microsoft-Windows-Windows Defender/Operational') {
    Out-Rep ''
    Out-Rep ('-- ' + $log)
    Safe {
        # Get-WinEvent throws when nothing matches: that is "no events", not a failure.
        $all = @()
        try { $all = @(Get-WinEvent -FilterHashtable @{ LogName = $log; StartTime = $since } -ErrorAction Stop) }
        catch {
            if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') { throw }
        }
        # Keep errors/warnings, blocks, detections, and anything naming our files. Defender audit
        # events 1122 (Exploit Guard audit-mode) fire for every python.exe start and would bury the
        # useful lines, so they are only counted.
        $audit = @($all | Where-Object { $_.Id -eq 1122 })
        $ev = @($all | Where-Object { $_.Id -ne 1122 -and ($_.Level -le 3 -or $_.Message -match 'appbuilder|Genie|qai|QAIModelBuilder|\.venv_') } | Select-Object -First 15)
        if ($audit.Count -gt 0) { Out-Rep ('  ({0} Defender Exploit-Guard AUDIT events id=1122 omitted: audit mode only logs, it does not block)' -f $audit.Count) }
        if ($ev.Count -eq 0) { Out-Rep '  (no matching events)' }
        foreach ($e in $ev) {
            Out-Rep ('  [{0}] id={1} level={2}' -f $e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $e.Id, $e.LevelDisplayName)
            Out-Rep ('      ' + (($e.Message -replace "\r?\n", ' ') -replace '\s{2,}', ' '))
        }
    }
}
Out-Rep ''
Out-Rep ('Report saved to: ' + $OutFile)
exit 0
