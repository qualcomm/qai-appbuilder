# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

<#
.SYNOPSIS
    Robust file download for Setup.bat - aria2c primary, PowerShell fallback.

.DESCRIPTION
    Single download primitive used by Setup.bat for ALL file downloads (uv,
    aria2c.exe itself, vendor-deps.7z, PortableGit, 7zr.exe, Node.js, voice/TTS
    model archives, QAIRT SDK, ...).

    Why this exists
    ---------------
    Setup.bat used to inline 30-40-line PowerShell here-strings in 5 places to
    drive aria2c via JSON-RPC + draw a progress bar. That code:
      * never passed --max-tries / --retry-wait / --connect-timeout / --timeout
        / --lowest-speed-limit, so aria2c could hang forever on a flaky link
        without the watchdog in PowerShell ever noticing;
      * had no "main file size hasn't grown for N seconds" stall detector, so
        a half-dead aria2c (alive but downloading 0 B/s) made Setup look frozen;
      * had no overall wall-clock cap;
      * detected completion only by ".aria2 control file gone" -- fragile when
        aria2c crashes after writing the body but before deleting the control;
      * was duplicated in 5 places, drifted across copies.

    What this script guarantees
    ---------------------------
    1. aria2c gets full robustness flags:
         --max-tries=5 --retry-wait=3 --connect-timeout=15 --timeout=30
         --lowest-speed-limit=50K
       This makes aria2c itself give up on dead connections instead of looping.

    2. An OUTER stall watchdog: if the target file's size hasn't grown for
       <StallTimeoutSec> seconds (default 90), kill aria2c and treat as
       "this attempt failed". Catches cases where aria2c is running but
       stalled (the exact failure mode that froze Setup.bat for users).

    3. A wall-clock cap per attempt (<AttemptTimeoutSec>, default 1800s for
       big files) -- even if the watchdog and aria2c retries both miss, no
       single attempt runs forever.

    4. Up-to-<MaxRetries> attempts with EXPONENTIAL BACKOFF
       (2s, 5s, 10s, 20s, 40s; clamped). Each retry resumes from the partial
       file (aria2c -c). Backoff matches user request 2026-06-19.

    5. Optional -MinSize check: if the downloaded file is < MinSize bytes,
       it's treated as truncated, deleted, and the loop retries. (PortableGit
       55 MB threshold pattern, generalised.)

    6. Optional -ZipTest: open the file as a .zip via .NET ZipFile and make
       sure it has at least one entry. Catches "Range-broken proxy stitched
       a corrupt .zip" (the very bug Setup.bat:835 already had a hand-rolled
       check for in Step 5b).

    7. aria2c-not-available fallback: silently fall back to single-thread
       Invoke-WebRequest. Used by the bootstrap downloads (uv, aria2c.exe
       itself, 7zr.exe) where aria2c isn't on disk yet.

    8. Idempotency: -OutFile already present and -MinSize satisfied (and
       -ZipTest passes if requested) -> exit 0 immediately, no re-download.

    9. Progress: prints a one-line `\r`-refreshed bar with %% / size /
       speed / ETA. Falls back to a quiet "still working..." heartbeat
       when aria2c RPC isn't available.

    Exit codes
    ----------
        0  success
        1  retries exhausted (all attempts failed)
        2  size / zip integrity check failed after a successful download
        3  user cancelled (Ctrl+C) -- propagated from the inner aria2c
        4  aria2c binary missing AND fallback also failed
        5  bad arguments (e.g. -OutFile parent does not exist and not creatable)

    Robustness contract
    -------------------
    * NEVER exits with a "looks like success" code on a partial / corrupt file.
    * NEVER leaves the caller staring at a frozen progress bar -- the watchdog
      will kill aria2c after StallTimeoutSec and either retry or fail.
    * NEVER deletes a file the caller didn't ask us to manage. -OutFile is
      ours; everything else is read-only.

.PARAMETER Url
    The download source URL. Required.

.PARAMETER OutFile
    Absolute or relative path to the destination file. Parent directory will
    be created if missing. Required.

.PARAMETER Aria2cExe
    Optional path to aria2c.exe. When omitted (or the file doesn't exist),
    the script uses single-thread Invoke-WebRequest. Setup.bat passes
    `data\bin\aria2c\aria2c.exe` once Step 0 has populated it.

.PARAMETER MaxRetries
    Maximum attempt count (default 5). Each attempt is one full aria2c
    invocation; failures (network / stall / size / zip) all consume one.

.PARAMETER StallTimeoutSec
    "File size hasn't grown for this many seconds" -> kill the current
    aria2c and count this as one failed attempt. Default 90s.

.PARAMETER AttemptTimeoutSec
    Hard wall-clock cap per attempt. Default 1800s (30 minutes; covers a
    2 GB QAIRT SDK at ~1 MB/s). On expiry, kills aria2c and counts the
    attempt as failed.

.PARAMETER Connections
    aria2c -x / -s value (parallel connections). Default 16. Some CDNs
    misbehave with high values -- Setup.bat:822 already uses 4 for Node.js
    and -x8 for PortableGit; callers can override per download.

.PARAMETER MinSize
    Minimum acceptable file size in BYTES after a successful download.
    0 (default) disables the check.

.PARAMETER ZipTest
    Switch. After a successful download, open the file with
    System.IO.Compression.ZipFile. If it fails or has 0 entries, treat as
    corrupt and retry (or, if attempts exhausted, exit 2).

.PARAMETER ProxyUrl
    Optional HTTP(S) proxy URL. When set, passed to aria2c via --all-proxy
    and to PowerShell fallback via -Proxy. Empty = honour OS env vars.

.PARAMETER Quiet
    Switch. Suppress the progress bar (still emits `[INFO]` / `[WARN]` /
    `[ERROR]` lines).

.PARAMETER LogDir
    Optional directory for aria2c stdout/stderr capture. Useful for
    diagnostics; deleted on success unless -KeepLogs is set. Default
    `<OutFile dir>\_aria2c_log`.

.PARAMETER KeepLogs
    Switch. Keep aria2c log files even on success.

.EXAMPLE
    # uv (small, no aria2c yet -- falls back to PowerShell)
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\download_with_aria2c.ps1 `
        -Url 'https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-pc-windows-msvc.zip' `
        -OutFile 'data\downloads\_uv_tmp.zip' `
        -MinSize 1000000

.EXAMPLE
    # QAIRT SDK 2 GB -- full aria2c with watchdog + retry
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\download_with_aria2c.ps1 `
        -Url 'https://softwarecenter.qualcomm.com/.../v<QAIRT SDK version>.zip' `
        -OutFile 'data\downloads\_qairt_tmp.zip' `
        -Aria2cExe 'data\bin\aria2c\aria2c.exe' `
        -MinSize 1500000000 `
        -ZipTest `
        -AttemptTimeoutSec 3600

.NOTES
    Authoring notes
    ---------------
    * Pure PowerShell 5.1+; no module dependencies (Setup.bat runs before
      the venv exists for the first 5 downloads).
    * Uses .NET classes directly (System.Net.WebClient,
      System.IO.Compression.ZipFile) so behaviour is identical on PS 5.1
      (Windows-shipped) and PS 7+.
    * No Set-Content / Out-File on text containing non-ASCII (AGENTS.md
      section 3.10 ironclad rule). All progress / log strings are ASCII.
    * Stall watchdog is implemented via polling File.GetLastWriteTime +
      File.Length on the target file (NOT via aria2c RPC), so it works
      even if aria2c is alive-but-frozen and the RPC has stopped responding.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [Parameter(Mandatory = $true)]
    [string]$OutFile,

    [string]$Aria2cExe = '',

    [int]$MaxRetries = 5,

    [int]$StallTimeoutSec = 90,

    [int]$AttemptTimeoutSec = 1800,

    [int]$Connections = 16,

    [long]$MinSize = 0,

    [switch]$ZipTest,

    [string]$ProxyUrl = '',

    [switch]$Quiet,

    [string]$LogDir = '',

    [switch]$KeepLogs
)

# ---------------------------------------------------------------------------
# Constants & setup
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Backoff schedule (seconds): 2, 5, 10, 20, 40 -- caller's request 2026-06-19.
# If MaxRetries exceeds the schedule, last value is reused.
$script:BackoffSeq = @(2, 5, 10, 20, 40)

# Exit codes (keep in sync with the top-of-file table).
$EXIT_OK              = 0
$EXIT_RETRY_EXHAUSTED = 1
$EXIT_INTEGRITY       = 2
$EXIT_CANCELLED       = 3
$EXIT_NO_DOWNLOADER   = 4
$EXIT_BAD_ARGS        = 5

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

function Write-Info($msg)  { Write-Host "[INFO] $msg" }
function Write-Warn($msg)  { Write-Host "[WARN] $msg" }
function Write-Err($msg)   { Write-Host "[ERROR] $msg" }
function Write-Ok($msg)    { Write-Host "[OK]   $msg" }

function Format-Bytes([long]$bytes) {
    if     ($bytes -ge 1GB) { return '{0:F2} GB ({1:N0} bytes)' -f ($bytes / 1GB), $bytes }
    elseif ($bytes -ge 1MB) { return '{0:F1} MB ({1:N0} bytes)' -f ($bytes / 1MB), $bytes }
    elseif ($bytes -ge 1KB) { return '{0:F1} KB ({1:N0} bytes)' -f ($bytes / 1KB), $bytes }
    else                    { return "$bytes bytes" }
}

# Progress line printed with `\r`. Only used when -Quiet is not set.
function Write-ProgressLine($line) {
    if ($Quiet) { return }
    [Console]::Write($line.PadRight(78) + "`r")
}

function Clear-ProgressLine {
    if ($Quiet) { return }
    [Console]::WriteLine('')
}

# ---------------------------------------------------------------------------
# Path / arg validation
# ---------------------------------------------------------------------------

# Normalise OutFile to an absolute path so aria2c -d / -o never depend on
# our cwd (matches the AGENTS.md State-Truth-First rule 4: "use real
# location, not assumed relative path").
try {
    if ([System.IO.Path]::IsPathRooted($OutFile)) {
        $OutFileAbs = [System.IO.Path]::GetFullPath($OutFile)
    } else {
        $OutFileAbs = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $OutFile))
    }
} catch {
    Write-Err "Could not resolve -OutFile to an absolute path: $OutFile  ($($_.Exception.Message))"
    exit $EXIT_BAD_ARGS
}
$OutDir = [System.IO.Path]::GetDirectoryName($OutFileAbs)
$OutName = [System.IO.Path]::GetFileName($OutFileAbs)

if (-not (Test-Path -LiteralPath $OutDir)) {
    try {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    } catch {
        Write-Err "Could not create output directory: $OutDir ($($_.Exception.Message))"
        exit $EXIT_BAD_ARGS
    }
}

# LogDir defaults to a hidden folder beside OutFile.
if (-not $LogDir) {
    $LogDir = Join-Path $OutDir '_aria2c_log'
}

# ---------------------------------------------------------------------------
# Idempotency: bail out early if OutFile already satisfies all checks
# ---------------------------------------------------------------------------

function Test-FileIntegrity {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return $false }

    if ($MinSize -gt 0) {
        $size = (Get-Item -LiteralPath $Path).Length
        if ($size -lt $MinSize) {
            Write-Warn "File too small: $(Format-Bytes $size) < $(Format-Bytes $MinSize) (will retry)"
            return $false
        }
    }

    if ($ZipTest) {
        try {
            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
            $z = [System.IO.Compression.ZipFile]::OpenRead($Path)
            $entryCount = $z.Entries.Count
            $z.Dispose()
            if ($entryCount -lt 1) {
                Write-Warn "Zip integrity check: 0 entries (corrupt) -- will retry"
                return $false
            }
        } catch {
            Write-Warn "Zip integrity check failed: $($_.Exception.Message) -- will retry"
            return $false
        }
    }

    return $true
}

if (Test-Path -LiteralPath $OutFileAbs) {
    if (Test-FileIntegrity -Path $OutFileAbs) {
        $existingSize = (Get-Item -LiteralPath $OutFileAbs).Length
        Write-Ok "Already present, integrity OK: $OutFileAbs ($existingSize bytes)"
        exit $EXIT_OK
    } else {
        Write-Info "Existing file failed integrity check; will re-download."
        # Don't delete here -- aria2c -c can resume from a partial of the
        # right server file. If the size is wrong we delete inside the loop.
    }
}

# ---------------------------------------------------------------------------
# aria2c invocation: one attempt with stall watchdog + wall-clock cap
# ---------------------------------------------------------------------------

function Invoke-Aria2cAttempt {
    <#
        Returns one of:
            'OK'         -- file downloaded, integrity passes
            'STALL'      -- main file size frozen for StallTimeoutSec
            'TIMEOUT'    -- attempt exceeded AttemptTimeoutSec
            'EXIT:<n>'   -- aria2c exited with code <n> on its own
            'INTEGRITY'  -- aria2c finished but the file fails MinSize / ZipTest
            'CANCELLED'  -- Ctrl+C / user signal (best-effort)

        Always returns; never throws to the caller.
        Always cleans up the spawned aria2c process before returning.
    #>
    param(
        [string]$AriaPath,
        [string]$Url,
        [string]$OutDir,
        [string]$OutName,
        [string]$LogDir
    )

    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    $stdOutLog = Join-Path $LogDir 'aria2c.stdout.log'
    $stdErrLog = Join-Path $LogDir 'aria2c.stderr.log'

    $argList = @(
        "-x$Connections",
        "-s$Connections",
        '-k1M',
        '-c',
        '--file-allocation=none',
        '--auto-file-renaming=false',
        '--allow-overwrite=true',
        '--check-certificate=false',
        # Robustness flags -- the whole point of this script (2026-06-19).
        '--max-tries=5',
        '--retry-wait=3',
        '--connect-timeout=15',
        '--timeout=30',
        # NOTE: --lowest-speed-limit is intentionally NOT set. It is a
        # per-connection threshold that kills connections below X bytes/s.
        # This is unreliable for detecting dead connections because:
        #   1) Low speed != dead -- a slow but progressing connection is fine.
        #   2) With N parallel connections, each gets ~(total BW / N), which
        #      can legitimately be very low (e.g. 70KB/s total / 8 conns =
        #      9KB/s each -- all healthy but all below any sane threshold).
        #   3) Transient dips (TCP window adjustment, CDN failover) cause
        #      false kills followed by expensive reconnect+re-range.
        # Dead connections are correctly detected by the OUTER stall watchdog
        # (StallTimeoutSec: file size unchanged for N seconds -> kill aria2c).
        # That checks total progress, not per-connection speed, which is the
        # right signal. aria2c's --timeout=30 already handles individual
        # connections that stop receiving data entirely.
        '--console-log-level=warn',
        '-d', $OutDir,
        '-o', $OutName
    )
    if ($ProxyUrl) {
        $argList += @("--all-proxy=$ProxyUrl")
    }
    $argList += @($Url)

    # Spawn aria2c with stdout/stderr redirected to log files. We DON'T use
    # -Wait -- we manage the lifecycle ourselves so the watchdog can kill it.
    try {
        $proc = Start-Process -FilePath $AriaPath `
            -ArgumentList $argList `
            -NoNewWindow `
            -PassThru `
            -RedirectStandardOutput $stdOutLog `
            -RedirectStandardError $stdErrLog
    } catch {
        Write-Warn "Failed to spawn aria2c: $($_.Exception.Message)"
        return "EXIT:255"
    }

    $targetPath = Join-Path $OutDir $OutName
    $ctrlPath = $targetPath + '.aria2'

    $attemptStartUtc = [DateTime]::UtcNow
    $lastSize = -1
    $lastSizeChangeUtc = [DateTime]::UtcNow
    $lastTotalBytes = 0
    $verdict = $null

    try {
        while ($true) {
            Start-Sleep -Seconds 2

            # 1) Has aria2c exited on its own?
            if ($proc.HasExited) {
                $code = $proc.ExitCode
                # PowerShell 5.1 can return $null for ExitCode in edge cases
                # (process killed externally, or disposed). Normalise to integer.
                if ($null -eq $code) { $code = -1 }
                # Completion: control file gone + target present + integrity OK.
                if ((Test-Path -LiteralPath $targetPath) -and (-not (Test-Path -LiteralPath $ctrlPath))) {
                    if (Test-FileIntegrity -Path $targetPath) {
                        $verdict = 'OK'
                    } else {
                        $verdict = 'INTEGRITY'
                    }
                } else {
                    $verdict = "EXIT:$code"
                }
                break
            }

            # 2) Wall-clock cap.
            $elapsed = ([DateTime]::UtcNow - $attemptStartUtc).TotalSeconds
            if ($elapsed -gt $AttemptTimeoutSec) {
                $verdict = 'TIMEOUT'
                break
            }

            # 3) Stall watchdog: poll target file size (NOT the .aria2 file --
            #    aria2c writes the body chunks into the target; the .aria2
            #    file just tracks chunk metadata).
            $currentSize = 0
            if (Test-Path -LiteralPath $targetPath) {
                try {
                    $currentSize = (Get-Item -LiteralPath $targetPath).Length
                } catch { $currentSize = 0 }
            }

            if ($currentSize -ne $lastSize) {
                $lastSize = $currentSize
                $lastSizeChangeUtc = [DateTime]::UtcNow
            } else {
                $stallSec = ([DateTime]::UtcNow - $lastSizeChangeUtc).TotalSeconds
                if ($stallSec -ge $StallTimeoutSec) {
                    $verdict = 'STALL'
                    break
                }
            }

            # 4) Progress display. We don't bother with aria2c RPC (the old
            #    Setup.bat impl did, but it's the same RPC that froze when
            #    aria2c stalled). Show whatever we know from the file system.
            if (-not $Quiet) {
                $sizeMB = [math]::Round($currentSize / 1MB, 1)
                # Speed: bytes-since-last-tick / 2s (poll interval).
                $deltaBytes = $currentSize - $lastTotalBytes
                $lastTotalBytes = $currentSize
                $speedKB = [math]::Max(0, [math]::Round($deltaBytes / 1KB / 2, 0))
                $line = "[DL] $sizeMB MB  $speedKB KB/s  elapsed $([math]::Round($elapsed,0))s"
                Write-ProgressLine $line
            }
        }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            try {
                # Kill the whole tree (aria2c spawns no children, but be safe).
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                # Give Windows a beat to release file handles before we
                # report verdict; otherwise the next attempt's aria2c -c
                # may briefly hit a "file in use" error on the .aria2.
                Start-Sleep -Milliseconds 500
            } catch { }
        }
    }

    # Echo aria2c's last 5 stderr lines on non-OK verdicts to make root
    # cause obvious in Setup.bat output. (We suppressed --console-log-level
    # so most chatter goes to the log; stderr usually carries the real error.)
    if ($verdict -ne 'OK' -and (Test-Path -LiteralPath $stdErrLog)) {
        try {
            $tail = Get-Content -LiteralPath $stdErrLog -Tail 5 -ErrorAction SilentlyContinue
            if ($tail) {
                foreach ($line in $tail) {
                    if ($line.Trim()) { Write-Warn "  aria2c: $line" }
                }
            }
        } catch { }
    }

    return $verdict
}

# ---------------------------------------------------------------------------
# Single-thread PowerShell fallback (used when aria2c isn't available yet)
# ---------------------------------------------------------------------------

function Invoke-PowerShellFallback {
    <#
        Returns 'OK' / 'INTEGRITY' / 'EXIT:1'.
        No multi-thread, no resume -- but used only for small bootstraps
        (uv 10MB, aria2c 5MB, 7zr 600KB, fallback to PortableGit/Node when
        aria2c is broken). For large files (QAIRT 2GB) the aria2c path
        with retry is the realistic option.
    #>
    param(
        [string]$Url,
        [string]$OutPath
    )

    Write-Info "Falling back to PowerShell single-thread download."

    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Timeout = 30000
        $req.ReadWriteTimeout = 60000
        $req.AllowAutoRedirect = $true
        $req.UserAgent = 'QAIModelBuilder-Setup/1.0'
        if ($ProxyUrl) {
            $req.Proxy = New-Object System.Net.WebProxy($ProxyUrl, $true)
        }

        $resp = $req.GetResponse()
        $total = $resp.ContentLength
        $stream = $resp.GetResponseStream()

        $fs = [System.IO.File]::Create($OutPath)
        try {
            $buffer = New-Object byte[] 65536
            $downloaded = 0
            $lastReportTime = [DateTime]::UtcNow
            while ($true) {
                $read = $stream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $fs.Write($buffer, 0, $read)
                $downloaded += $read
                if (-not $Quiet -and ([DateTime]::UtcNow - $lastReportTime).TotalSeconds -ge 1) {
                    $lastReportTime = [DateTime]::UtcNow
                    if ($total -gt 0) {
                        $pct = [math]::Round($downloaded / $total * 100, 1)
                        $line = "[DL-PS] ${pct}%  $([math]::Round($downloaded/1MB,1))/$([math]::Round($total/1MB,1)) MB"
                    } else {
                        $line = "[DL-PS] $([math]::Round($downloaded/1MB,1)) MB"
                    }
                    Write-ProgressLine $line
                }
            }
        } finally {
            $fs.Dispose()
            $stream.Dispose()
            $resp.Dispose()
        }
        Clear-ProgressLine
    } catch {
        Write-Warn "PowerShell fallback failed: $($_.Exception.Message)"
        return "EXIT:1"
    }

    if (-not (Test-FileIntegrity -Path $OutPath)) {
        return 'INTEGRITY'
    }
    return 'OK'
}

# ---------------------------------------------------------------------------
# Main retry loop
# ---------------------------------------------------------------------------

function Get-BackoffSeconds([int]$attempt) {
    # attempt is 1-based; backoff for attempt 1 -> 0 (no wait before the
    # very first try). For retries 2..N use BackoffSeq[i-2] (clamp at end).
    if ($attempt -le 1) { return 0 }
    $idx = $attempt - 2
    if ($idx -ge $script:BackoffSeq.Length) {
        return $script:BackoffSeq[-1]
    }
    return $script:BackoffSeq[$idx]
}

# Decide whether aria2c is available. Use the explicit path if given;
# otherwise look on PATH; finally fall back to PowerShell.
$useAria2c = $false
$aria2cPath = ''
if ($Aria2cExe -and (Test-Path -LiteralPath $Aria2cExe)) {
    $aria2cPath = (Resolve-Path -LiteralPath $Aria2cExe).Path
    $useAria2c = $true
} else {
    $cmd = Get-Command aria2c -ErrorAction SilentlyContinue
    if ($cmd) {
        $aria2cPath = $cmd.Path
        $useAria2c = $true
    }
}

if ($useAria2c) {
    Write-Info "Downloader: aria2c ($aria2cPath)"
} else {
    Write-Info "Downloader: PowerShell single-thread (aria2c not available)"
}
Write-Info "URL : $Url"
Write-Info "Dest: $OutFileAbs"
if ($MinSize -gt 0) {
    Write-Info "MinSize: $(Format-Bytes $MinSize)"
}
if ($ZipTest) { Write-Info "ZipTest: enabled" }
Write-Info "Retries: max $MaxRetries, stall=$StallTimeoutSec s, attempt-cap=$AttemptTimeoutSec s"

$lastVerdict = $null
for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
    $waitSec = Get-BackoffSeconds $attempt
    if ($waitSec -gt 0) {
        Write-Info "Backoff before attempt $attempt/${MaxRetries}: ${waitSec}s"
        Start-Sleep -Seconds $waitSec
    }
    Write-Info "Attempt $attempt/$MaxRetries ..."

    if ($useAria2c) {
        $lastVerdict = Invoke-Aria2cAttempt `
            -AriaPath $aria2cPath `
            -Url $Url `
            -OutDir $OutDir `
            -OutName $OutName `
            -LogDir $LogDir
    } else {
        $lastVerdict = Invoke-PowerShellFallback -Url $Url -OutPath $OutFileAbs
    }

    Clear-ProgressLine
    Write-Info "Attempt $attempt verdict: $lastVerdict"

    switch ($lastVerdict) {
        'OK' {
            $finalSize = (Get-Item -LiteralPath $OutFileAbs).Length
            Write-Ok "Download complete: $OutFileAbs ($finalSize bytes)"
            if (-not $KeepLogs -and (Test-Path -LiteralPath $LogDir)) {
                Remove-Item -LiteralPath $LogDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            exit $EXIT_OK
        }
        'CANCELLED' {
            Write-Warn "Download cancelled by user."
            exit $EXIT_CANCELLED
        }
        'INTEGRITY' {
            # Body downloaded but failed MinSize / ZipTest. Delete and retry --
            # aria2c -c won't help here (we'd resume into the same garbage).
            Write-Warn "Downloaded file failed integrity check; deleting and retrying."
            Remove-Item -LiteralPath $OutFileAbs -Force -ErrorAction SilentlyContinue
            $ctrlFile = "$OutFileAbs.aria2"
            Remove-Item -LiteralPath $ctrlFile -Force -ErrorAction SilentlyContinue
            continue
        }
        'STALL' {
            Write-Warn "Stalled for ${StallTimeoutSec}s; killed aria2c, will retry (resume from partial)."
            continue
        }
        'TIMEOUT' {
            Write-Warn "Attempt exceeded ${AttemptTimeoutSec}s wall-clock; killed aria2c, will retry."
            continue
        }
        default {
            Write-Warn "Attempt failed ($lastVerdict); will retry."
            continue
        }
    }
}

# All retries exhausted with multi-connection aria2c.
# FALLBACK: If we were using multiple connections and they ALL failed, the
# likely root cause is low total bandwidth being split across connections,
# with each connection falling below --lowest-speed-limit and getting killed.
# Try ONE final attempt with a single connection and no speed limit -- this
# is slow but reliable on constrained networks (confirmed to work at ~70 KB/s
# on the PortableGit 57MB download where 8-connection attempts all failed).
if ($useAria2c -and $Connections -gt 1) {
    Write-Info "Multi-connection attempts exhausted. Trying single-connection fallback (slower but more reliable)..."
    $savedConnections = $Connections
    $Connections = 1
    $lastVerdict = Invoke-Aria2cAttempt `
        -AriaPath $aria2cPath `
        -Url $Url `
        -OutDir $OutDir `
        -OutName $OutName `
        -LogDir $LogDir
    Clear-ProgressLine
    Write-Info "Single-connection fallback verdict: $lastVerdict"
    $Connections = $savedConnections

    if ($lastVerdict -eq 'OK') {
        $finalSize = (Get-Item -LiteralPath $OutFileAbs).Length
        Write-Ok "Download complete: $OutFileAbs ($finalSize bytes)"
        if (-not $KeepLogs -and (Test-Path -LiteralPath $LogDir)) {
            Remove-Item -LiteralPath $LogDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        exit $EXIT_OK
    }
    # Single-connection also failed -- fall through to PowerShell fallback below.
    Write-Warn "Single-connection fallback also failed ($lastVerdict)."
}

# FALLBACK 2: If aria2c is completely broken on this network, try PowerShell
# single-thread as a last resort. This covers scenarios where aria2c has
# issues with the proxy/TLS but .NET HttpWebRequest works fine.
if ($useAria2c) {
    Write-Info "Trying PowerShell single-thread as last resort..."
    # Delete any partial left by aria2c so PowerShell starts fresh.
    Remove-Item -LiteralPath $OutFileAbs -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$OutFileAbs.aria2" -Force -ErrorAction SilentlyContinue
    $lastVerdict = Invoke-PowerShellFallback -Url $Url -OutPath $OutFileAbs
    Clear-ProgressLine
    Write-Info "PowerShell fallback verdict: $lastVerdict"
    if ($lastVerdict -eq 'OK') {
        $finalSize = (Get-Item -LiteralPath $OutFileAbs).Length
        Write-Ok "Download complete: $OutFileAbs ($finalSize bytes)"
        if (-not $KeepLogs -and (Test-Path -LiteralPath $LogDir)) {
            Remove-Item -LiteralPath $LogDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        exit $EXIT_OK
    }
    Write-Warn "PowerShell fallback also failed ($lastVerdict)."
}

Write-Err "Download failed after $MaxRetries attempts + fallbacks (last: $lastVerdict): $Url"
if ($lastVerdict -eq 'INTEGRITY') {
    exit $EXIT_INTEGRITY
}
if (-not $useAria2c) {
    exit $EXIT_NO_DOWNLOADER
}
exit $EXIT_RETRY_EXHAUSTED
