# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

<#
.SYNOPSIS
    Ensure a WDAC/Code-Integrity-usable CPython interpreter exists.

.DESCRIPTION
    uv's managed CPython (python-build-standalone, PBS) ships DLLs signed by
    Astral. On machines with an Enterprise WDAC / Smart App Control policy in
    ENFORCED user-mode code integrity (UMCI=2), loading pbs's python3XX.dll is
    blocked by the kernel: python.exe exits 0xC0E90002 and uv reports
    "Querying Python ... failed with exit code: 0xc0e90002". No application-
    level workaround (copying DLLs, VC++ redist, PATH tweaks) can make a
    CI-rejected DLL load -- the block is in the kernel.

    The official python.org Windows installer, by contrast, is Authenticode-
    signed by the "Python Software Foundation", which enterprise WDAC policies
    commonly trust. This script:

      1. Checks whether user-mode code integrity is ENFORCED (Win32_DeviceGuard
         UsermodeCodeIntegrityPolicyEnforcementStatus == 2). This is the GATE:
         the block is non-deterministic (Smart App Control in Evaluation can
         permit the pbs DLL one moment and block it the next) and a runtime
         probe is masked on dev boxes that happen to have a system Python, so
         we do NOT gamble on "does it run right now".
      2. If UMCI is NOT enforced -> prints RESULT=managed and exits 0 with no
         side effects. The uv-managed interpreter is used as-is (untouched main
         path); Setup.bat's existing VC++ redist + venv-create retries handle
         any genuine non-WDAC breakage. This path never aborts.
      3. If UMCI IS enforced -> provisions a PSF-signed interpreter: reuse the
         one already in our private dir; else install the official python.org
         build (SHA-256 when pinned + PSF Authenticode signature verified)
         SILENTLY into that private dir; else, if the same major.minor is
         already installed on the machine, reuse that. Then verify the chosen
         python.exe both RUNS and is PSF-signed, and print RESULT=signed.
         (A recent CodeIntegrity Event 3033/3077 is logged as confirmation but
         is NOT a gate -- the block is intermittent.)
      4. If a signed interpreter cannot be obtained / verified / installed,
         exits non-zero with a precise English diagnosis. It NEVER falls back
         to a Python found on the user's PATH -- silently adopting an unknown
         system Python is not a controlled build.

    Contract (stdout + a side file):
        RESULT=managed  (and -OutPathFile written EMPTY)   -> use uv-managed spec
        RESULT=signed   (and -OutPathFile = abs python.exe) -> seed venvs from it
    On failure: no RESULT line, non-zero exit, English [ERROR] lines.

.NOTES
    ASCII-only per AGENTS.md 10.1 (.ps1 must not contain non-ASCII).
#>

[CmdletBinding()]
param(
    # uv interpreter spec, e.g. cpython-3.13-windows-x86_64 (matches Setup.bat PY_SPEC).
    [Parameter(Mandatory = $true)] [string]$UvSpec,
    # Full dotted version of the python.org installer to fetch when CI-blocked, e.g. 3.13.14.
    [Parameter(Mandatory = $true)] [string]$FullVersion,
    # amd64 | arm64 (python.org installer arch tag).
    [Parameter(Mandatory = $true)] [ValidateSet('amd64', 'arm64')] [string]$InstallerArch,
    # Expected SHA-256 of the python.org installer (from python.org release
    # page). Optional: empty -> integrity relies on the Authenticode signature.
    [string]$Sha256 = '',
    # Directory to install the signed Python into (we own it).
    [Parameter(Mandatory = $true)] [string]$InstallDir,
    # Path to uv.exe (reserved: passed by Setup.bat; retained for diagnostics
    # and possible future interpreter probing. Not required by current logic).
    [string]$UvExe = '',
    # Path to aria2c.exe for the downloader (may be absent; downloader falls back).
    [string]$Aria2cExe = '',
    # Directory for the downloaded installer + logs.
    [string]$DownloadDir = '',
    # Path to download_with_aria2c.ps1 (reused as the download primitive).
    [string]$DownloaderPs1 = '',
    # File to write the resolved SIGNED interpreter path into (empty/absent =
    # managed interpreter is fine and should be used as-is). Setup.bat reads it.
    [string]$OutPathFile = ''
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Info($m) { Write-Host "[INFO] $m" }
function Write-Warn($m) { Write-Host "[WARN] $m" }
function Write-Err($m)  { Write-Host "[ERROR] $m" }
function Write-Ok($m)   { Write-Host "[OK]   $m" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $DownloaderPs1) { $DownloaderPs1 = Join-Path $ScriptDir 'download_with_aria2c.ps1' }
if (-not $DownloadDir)   { $DownloadDir = Join-Path (Split-Path -Parent (Split-Path -Parent $ScriptDir)) 'data\downloads' }

# ---------------------------------------------------------------------------
# 1. Is user-mode code integrity enforced at all?
# ---------------------------------------------------------------------------
function Test-UmciEnforced {
    try {
        $g = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' `
            -ClassName Win32_DeviceGuard -ErrorAction Stop
        # UsermodeCodeIntegrityPolicyEnforcementStatus: 0=Off 1=Audit 2=Enforced
        return ([int]$g.UsermodeCodeIntegrityPolicyEnforcementStatus -eq 2)
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# 3. Confirm the failure is a Code-Integrity block on THIS pbs interpreter,
#    not a generic runtime failure. We look at the CodeIntegrity/Operational
#    log for a recent 3033/3077 event naming a pbs python*.dll for this arch.
# ---------------------------------------------------------------------------
function Test-CiBlockedRecently {
    try {
        $since = (Get-Date).AddMinutes(-5)
        $ev = Get-WinEvent -FilterHashtable @{
            LogName   = 'Microsoft-Windows-CodeIntegrity/Operational'
            Id        = 3033, 3077
            StartTime = $since
        } -ErrorAction Stop
    } catch {
        # No matching events in the window -> cannot confirm a CI block.
        return $false
    }
    foreach ($e in $ev) {
        $m = $e.Message
        if ($m -and ($m -match 'uv[\\/]python') -and ($m -match 'python3\d+\.dll|python\.exe') `
                -and ($m -match 'signing level|code integrity')) {
            return $true
        }
    }
    return $false
}

# ---------------------------------------------------------------------------
# 4. Download + verify + silently install the official python.org interpreter.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Resolve an already-installed python.org interpreter for major.minor (e.g.
# "3.13") from the standard CPython registry keys, checking both per-user
# (HKCU) and per-machine (HKLM) hives. Returns the InstallPath dir or $null.
# ---------------------------------------------------------------------------
function Get-RegisteredPythonCore([string]$MajorMinor) {
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        $key = "$hive\Software\Python\PythonCore\$MajorMinor\InstallPath"
        try {
            $p = (Get-ItemProperty -Path $key -ErrorAction Stop).'(default)'
            if (-not $p) { $p = (Get-ItemProperty -Path $key -ErrorAction Stop).ExecutablePath }
        } catch { $p = $null }
        if ($p) {
            # ExecutablePath points at python.exe; InstallPath default is the dir.
            if ($p -match '(?i)python\.exe$') { $p = Split-Path $p -Parent }
            if (Test-Path -LiteralPath (Join-Path $p 'python.exe')) { return $p }
        }
    }
    return $null
}

function Install-SignedPython {
    $installer = "python-$FullVersion-$InstallerArch.exe"
    $url = "https://www.python.org/ftp/python/$FullVersion/$installer"
    $out = Join-Path $DownloadDir $installer

    # REUSE-FIRST (before any network I/O): if a usable signed interpreter is
    # already present -- in our private dir from a prior Setup, or a same
    # major.minor python.org build already registered on the machine -- use it
    # and skip the download entirely. This makes a re-run cheap AND lets an
    # OFFLINE machine that already has the interpreter still succeed (a
    # download-first ordering would fail such a host even though it is ready).
    $mmEarly = ($FullVersion -split '\.')[0..1] -join '.'
    $tgtEarly = Join-Path $InstallDir 'python.exe'
    if (Test-Path -LiteralPath $tgtEarly) {
        Write-Info "Reusing signed Python already at $InstallDir (no download needed)."
        return $tgtEarly
    }
    $regEarly = Get-RegisteredPythonCore $mmEarly
    if ($regEarly -and (Test-Path -LiteralPath (Join-Path $regEarly 'python.exe'))) {
        Write-Info "python.org $mmEarly already installed at $regEarly; using it (no download needed)."
        return (Join-Path $regEarly 'python.exe')
    }

    Write-Info "Downloading official python.org installer (PSF-signed): $url"
    $dlArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $DownloaderPs1,
        '-Url', $url, '-OutFile', $out, '-MinSize', '10000000',
        '-MaxRetries', '4', '-StallTimeoutSec', '60', '-AttemptTimeoutSec', '600'
    )
    if ($Aria2cExe -and (Test-Path -LiteralPath $Aria2cExe)) { $dlArgs += @('-Aria2cExe', $Aria2cExe) }
    # Pipe to Out-Host so the downloader's stdout does NOT leak into this
    # function's return value (PowerShell returns ALL uncaptured output).
    & powershell.exe @dlArgs 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $out)) {
        Write-Err "Failed to download the official python.org installer: $url"
        return $null
    }

    if ($Sha256) {
        Write-Info "Verifying SHA-256 ..."
        $actual = (Get-FileHash -LiteralPath $out -Algorithm SHA256).Hash
        if ($actual -ne $Sha256.ToUpper()) {
            Write-Err "SHA-256 mismatch for $installer"
            Write-Err "  expected: $($Sha256.ToUpper())"
            Write-Err "  actual  : $actual"
            Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
            return $null
        }
    } else {
        Write-Info "No pinned SHA-256 supplied; relying on Authenticode signature for integrity."
    }

    Write-Info "Verifying Authenticode signature (Python Software Foundation) ..."
    $sig = Get-AuthenticodeSignature -LiteralPath $out
    $signer = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '<none>' }
    if ($sig.Status -ne 'Valid' -or ($signer -notmatch 'Python Software Foundation')) {
        Write-Err "Installer is not validly signed by the Python Software Foundation."
        Write-Err "  status: $($sig.Status)  signer: $signer"
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
        return $null
    }
    Write-Ok "Installer verified (signature: PSF; $(if ($Sha256) {'SHA-256 pinned'} else {'signature-only'}))."
    # Strip Mark-of-the-Web so the installer does not raise a MOTW prompt. This
    # does NOT bypass WDAC / Smart App Control -- those evaluate the signature.
    Unblock-File -LiteralPath $out -ErrorAction SilentlyContinue

    # Fresh install into our private TargetDir. (Reuse of an existing install
    # -- our dir or a registered same major.minor -- was already handled at the
    # top of this function, before the download.) The python.org bundle is a
    # per-(major.minor) singleton keyed by ProviderKey "CPython-3.x": if the
    # same line somehow got registered between the early check and here, a
    # /quiet run plans action=Modify and will NOT populate TargetDir (exit 0,
    # dir empty); the post-install fallback below reuses that registered copy.
    $mm = ($FullVersion -split '\.')[0..1] -join '.'
    Write-Info "Installing silently into $InstallDir (per-user, no PATH/registry changes) ..."
    # python.org installer (WiX bundle): silent, custom target, minimal, isolated.
    #   InstallAllUsers=0 / InstallLauncherAllUsers=0 -> per-user, no admin needed
    #   TargetDir              -> our private dir (we own it)
    #   PrependPath=0/AppendPath=0/AssociateFiles=0/Shortcuts=0 -> touch nothing global
    #   Include_launcher=0     -> do NOT replace/upgrade the shared py.exe launcher
    #   Include_test/doc/tcltk=0 -> runtime footprint only (no tkinter/tests/docs)
    #   Include_pip=1          -> keep pip in the interpreter
    $piArgs = @(
        '/quiet', 'InstallAllUsers=0', "TargetDir=$InstallDir",
        'PrependPath=0', 'AppendPath=0', 'AssociateFiles=0', 'Shortcuts=0',
        'Include_launcher=0', 'InstallLauncherAllUsers=0',
        'Include_pip=1', 'Include_test=0', 'Include_doc=0', 'Include_tcltk=0'
    )
    $tgt = Join-Path $InstallDir 'python.exe'
    $p = Start-Process -FilePath $out -ArgumentList $piArgs -Wait -PassThru -NoNewWindow
    # 0 = ok; 3010/1641 = ok but restart initiated/required (benign for our use).
    if ($p.ExitCode -notin @(0, 3010, 1641)) {
        Write-Err "python.org installer returned exit code $($p.ExitCode)."
        return $null
    }
    # /quiet can return just before the MSI finishes flushing files; poll briefly.
    for ($i = 0; $i -lt 30 -and -not (Test-Path -LiteralPath $tgt); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path -LiteralPath $tgt) {
        Write-Ok "Installed signed Python into $InstallDir"
        return $tgt
    }
    # No-op MAINTENANCE pass: the same python.org major.minor is already present.
    # Use that existing install rather than failing.
    $reg = Get-RegisteredPythonCore $mm
    if ($reg -and (Test-Path -LiteralPath (Join-Path $reg 'python.exe'))) {
        Write-Info "python.org $mm is already installed at $reg; using the existing install."
        return (Join-Path $reg 'python.exe')
    }
    Write-Err "Installer did not populate $InstallDir and no existing python.org $mm was found (exit $($p.ExitCode))."
    return $null
}

# ---------------------------------------------------------------------------
# 5. Confirm an interpreter both RUNS (exit 0) and is PSF-signed.
# ---------------------------------------------------------------------------
function Confirm-SignedPythonUsable([string]$PyExe, [string]$MajorMinor) {
    # THE decisive WDAC test: actually RUN the interpreter. If the enforced CI
    # policy blocks its python3XX.dll, this exits 0xC0E90002 (nonzero) and we
    # reject it. Also assert version line + 64-bit arch to catch a mis-resolved
    # interpreter.
    # Native stderr must not become a terminating error under Stop; a WDAC
    # block emits stderr and we want a clean $false, not a thrown exception.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $out = & $PyExe -c "import platform,sys; print(sys.version.split()[0]); print(platform.architecture()[0])" 2>&1
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($rc -ne 0) {
        Write-Err "Signed Python failed to run (exit $rc): $PyExe"
        return $false
    }
    $lines = @($out | Where-Object { $_ -ne '' })
    $ver = [string]$lines[0]
    $arch = [string]$lines[1]
    if ($MajorMinor -and ($ver -notlike "$MajorMinor.*") -and ($ver -ne $MajorMinor)) {
        Write-Err "Version mismatch at ${PyExe}: expected $MajorMinor.x, found $ver."
        return $false
    }
    if ($arch -and $arch -ne '64bit') {
        Write-Err "Architecture mismatch at ${PyExe}: expected 64bit, found $arch."
        return $false
    }
    $dll = Get-ChildItem -LiteralPath (Split-Path $PyExe -Parent) -Filter 'python3*.dll' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($dll) {
        $sig = Get-AuthenticodeSignature -LiteralPath $dll.FullName
        $dsigner = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '<none>' }
        if ($sig.Status -ne 'Valid' -or ($dsigner -notmatch 'Python Software Foundation')) {
            Write-Err "python3*.dll beside the interpreter is not PSF-signed (status $($sig.Status)); WDAC would block it."
            return $false
        }
    }
    Write-Ok "Signed Python usable: $PyExe (version $ver, $arch)"
    return $true
}

# Persist the resolved interpreter path (signed path, or empty for managed) to
# the file Setup.bat reads. UTF-8 without BOM, LF; ASCII content in practice.
function Write-Interp([string]$Path) {
    if (-not $OutPathFile) { return }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($OutPathFile, [string]$Path, $enc)
}

# ===========================================================================
# Main
# ===========================================================================
if (-not (Test-Path -LiteralPath $DownloadDir)) {
    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
}

# Decision policy (per project owner, 2026-07-26):
#   The block is REAL but NON-DETERMINISTIC on this class of machine. UMCI is
#   Enforced (UMCI=2) while Smart App Control runs in Evaluation, so the very
#   same pbs python3XX.dll that the kernel blocked at one moment (confirmed:
#   python.exe -> 0xC0E90002, CodeIntegrity Event 3033/3077) can be permitted a
#   few hours later once reputation/ISG re-evaluates. A runtime probe is also
#   MASKED on a dev box that happens to have a system PSF Python (uv silently
#   resolves the spec to it) -- but real user machines have no system Python,
#   so uv resolves to the blockable pbs build. Relying on "does it run right
#   now" would therefore pass on this dev box and on a lucky reputation window,
#   yet fail on the user's next boot or on a clean machine.
#
$umci = Test-UmciEnforced
if (-not $umci) {
    # UMCI is NOT enforced -> the signed-Python fallback is IRRELEVANT here.
    # Stay on the untouched uv main path with ZERO behaviour change: the
    # existing Step 2b VC++ redist logic + Step 3's own venv-create retries
    # already handle a genuinely-broken pbs on non-WDAC hosts. We deliberately
    # do NOT run an extra runtime probe or ever abort on this path -- doing so
    # could hard-fail a common-case clean machine over a transient hiccup that
    # the normal retry path would have absorbed.
    Write-Ok "UMCI not enforced; using the uv-managed Python (no signed-Python fallback needed)."
    Write-Interp ''
    Write-Host "RESULT=managed"
    exit 0
}

Write-Warn "User-mode code integrity is ENFORCED (UMCI=2) on this machine."
$ciBlocked = Test-CiBlockedRecently
if ($ciBlocked) {
    Write-Warn "Confirmed: recent CodeIntegrity Event 3033/3077 blocking uv's unsigned"
    Write-Warn "python-build-standalone interpreter."
} else {
    Write-Info "No recent CI block event captured, but under an enforced policy the unsigned"
    Write-Info "pbs interpreter is not deterministically usable; provisioning a signed Python."
}
Write-Info "Provisioning the PSF-signed official python.org $FullVersion ($InstallerArch)..."

$py = Install-SignedPython
if (-not $py) {
    Write-Err "Could not provision a WDAC-usable signed Python. Setup cannot continue."
    Write-Err "The uv-managed Python is blocked by this machine's Enterprise code integrity"
    Write-Err "policy, and the official python.org installer could not be downloaded, verified,"
    Write-Err "or installed. Options:"
    Write-Err "  * Ensure this machine can reach https://www.python.org/ftp/python/ , or"
    Write-Err "  * Manually install python.org $FullVersion ($InstallerArch) into:"
    Write-Err "        $InstallDir"
    Write-Err "    then re-run Setup.bat, or"
    Write-Err "  * Ask IT to allow the interpreter under the WDAC policy."
    exit 4
}

if (-not (Confirm-SignedPythonUsable $py (($FullVersion -split '\.')[0..1] -join '.'))) {
    Write-Err "The installed python.org Python is still not usable under this machine's policy."
    Write-Err "Setup cannot continue. Contact IT: the WDAC policy may not trust the PSF"
    Write-Err "certificate on this machine."
    exit 5
}

Write-Interp $py
Write-Host "RESULT=signed"
exit 0
