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
Section 'Python and the native files (load test, one at a time)'
$py = @'
import sys, os, platform, sysconfig, ctypes, glob, json
from ctypes import wintypes
print("python        :", sys.version.split()[0], platform.machine(), sysconfig.get_platform(), "|", sys.executable)
home = os.path.dirname(os.path.realpath(sys.base_prefix + os.sep + "python.exe")) if False else sys.base_prefix
print("python home   :", home)
for n in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
    p = os.path.join(home, n)
    print("  in python dir:", n, "->", p if os.path.exists(p) else "(absent: Windows uses System32)")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
feat = {10: "SSE2", 13: "SSE3", 36: "SSSE3", 37: "SSE4.1", 38: "SSE4.2", 39: "AVX", 40: "AVX2", 41: "AVX512F", 43: "AVX512 (PF 43)"}
print("cpu features  :", ", ".join("%s=%s" % (v, "yes" if k32.IsProcessorFeaturePresent(k) else "NO") for k, v in sorted(feat.items())))
try:
    import qai_appbuilder  # noqa
    spec = None
except Exception:
    pass
import importlib.util
sp = importlib.util.find_spec("qai_appbuilder")
pk = os.path.dirname(sp.origin) if sp and sp.origin else None
print("package dir   :", pk)
def load(label, path):
    try:
        ctypes.WinDLL(path)
        print("  LOAD OK     :", label)
        return True
    except OSError as e:
        print("  LOAD FAILED :", label, "| winerror=%s | %s" % (e.winerror, e))
        return False
sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
print("load test (full paths):")
for n in ("vcruntime140.dll", "msvcp140.dll", "msvcp140_atomic_wait.dll"):
    load(n + " (System32)", os.path.join(sysdir, n))
if pk:
    load("libappbuilder.dll", os.path.join(pk, "libappbuilder.dll"))
    load("Genie.dll", os.path.join(pk, "Genie.dll"))
try:
    import qai_appbuilder as q
    print("  IMPORT OK   : qai_appbuilder", getattr(q, "__version__", "?"))
except BaseException as e:
    print("  IMPORT FAILED:", type(e).__name__, "|", e)
# modules mapped into THIS process
psapi = ctypes.WinDLL("psapi", use_last_error=True)
hproc = k32.GetCurrentProcess()
arr = (wintypes.HMODULE * 1024)(); need = wintypes.DWORD()
psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
psapi.EnumProcessModules(hproc, arr, ctypes.sizeof(arr), ctypes.byref(need))
buf = ctypes.create_unicode_buffer(1024)
print("modules mapped in the process (runtime / QNN related):")
for i in range(need.value // ctypes.sizeof(wintypes.HMODULE)):
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    psapi.GetModuleFileNameExW(hproc, arr[i], buf, 1024)
    low = buf.value.lower()
    if any(k in low for k in ("vcruntime", "msvcp", "ucrtbase", "python3", "appbuilder", "genie", "qnn", "d3d12")):
        print("MODULE", buf.value)
'@
$tmp = Join-Path $env:TEMP ('qab_diag_{0}.py' -f $PID)
[IO.File]::WriteAllText($tmp, $py, (New-Object Text.UTF8Encoding($false)))
$lines = @()
Safe { $script:lines = @(& $Python $tmp 2>&1 | ForEach-Object { [string]$_ }) }
Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
$pkgDir = $null
foreach ($l in $lines) {
    if ($l -like 'MODULE *') {
        $p = $l.Substring(7)
        $v = ''
        try { $v = (Get-Item -LiteralPath $p).VersionInfo.FileVersion } catch { }
        Out-Rep ('  {0,-22} {1}' -f $v, $p)
    } else {
        Out-Rep $l
        if ($l -like 'package dir*') { $pkgDir = ($l -replace '^package dir\s*:\s*', '').Trim() }
    }
}

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
