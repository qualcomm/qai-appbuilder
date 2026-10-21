# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Network preflight + proxy/mirror resolution for setup_env.bat.

.DESCRIPTION
    Probes the hosts the installer needs, decides proxy + mirror settings and
    writes them as `set "NAME=value"` lines into a .bat fragment that the
    caller runs with `call`. Nothing is installed here.

    Variables emitted (only when a value applies):
      HTTP_PROXY / HTTPS_PROXY / ALL_PROXY   generic proxy (uv, pip, playwright, urllib)
      UV_DEFAULT_INDEX / PIP_INDEX_URL       PyPI mirror
      UV_PYTHON_INSTALL_MIRROR               python-build-standalone mirror
      PLAYWRIGHT_DOWNLOAD_HOST               Chromium mirror
      QAI_GITHUB_MIRROR                      URL prefix for github.com release assets
                                             (first candidate that really serves a file;
                                             an existing QAI_GITHUB_MIRROR env wins)
      NPM_CONFIG_REGISTRY / COREPACK_NPM_REGISTRY   npm registry mirror (pnpm, corepack)
      QAI_NODE_MIRROR                        Node.js dist mirror (build_frontend.bat)
      QAI_NET_PROFILE                        official | cn

    Every probe is retried (DNS failures and timeouts are often transient on home
    routers) and runs with a small concurrency limit so a burst of lookups does not
    overwhelm a weak DNS forwarder.

    LOG FILE  data\logs\net_<mode>_<timestamp>.log  (last 20 kept). It records the
    environment (OS, PowerShell, proxy env vars, WinINET proxy / PAC, per-host system
    proxy decision, DNS servers, hosts-file overrides, DNS control lookups) and, for
    every probe, each attempt with the full exception chain, the socket error code,
    and - on failure - a DNS lookup and a TCP connect test (also to the proxy).

.PARAMETER Proxy
    Explicit proxy URL. Default: HTTPS_PROXY/HTTP_PROXY env, else the system proxy.
.PARAMETER Mirror
    auto (default) | official | cn.  auto = use cn mirrors only when the official
    source of the probed groups (pypi.org, or nodejs.org/npmjs for the frontend
    group) is unreachable but the matching cn mirror is reachable.
.PARAMETER Emit
    Path of the .bat fragment to write.
.PARAMETER NoProbe
    Skip the connectivity probe (auto then behaves like official). The GitHub
    mirror candidates are still tried when the profile is cn.
.PARAMETER Need
    Comma list of probe groups: core,browser,builder,frontend. Default core.
.PARAMETER ProbeAll
    GUI mode (setup_gui.ps1 step 1): probe EVERY source, official AND cn mirror,
    print a table and write the result as JSON to -Json. Then exit;
    -Emit / -Mirror / -Need are ignored. JSON shape:
      { proxy, proxySource, recommend: official|cn|blocked, reason, log,
        items: [ { id, name, need, official:{url,ok,ms,note,tries,detail},
                   cn:{...,mirror}|null } ] }
.PARAMETER Json
    Output file for -ProbeAll.
.PARAMETER Only
    With -ProbeAll: comma list of item ids to (re)probe, e.g. pypi,github.
.PARAMETER LogFile
    Detailed log path. Default data\logs\net_<mode>_<timestamp>.log
#>
param(
    [string]$Proxy = '',
    [ValidateSet('auto', 'official', 'cn')][string]$Mirror = 'auto',
    [string]$Emit = '',
    [switch]$NoProbe,
    [string]$Need = 'core',
    [switch]$ProbeAll,
    [string]$Json = '',
    [string]$Only = '',
    [string]$LogFile = ''
)
if (-not $ProbeAll -and -not $Emit) { [Console]::Error.WriteLine('[NET] -Emit is required (or use -ProbeAll -Json <file>)'); exit 2 }
if ($ProbeAll -and -not $Json) { [Console]::Error.WriteLine('[NET] -ProbeAll needs -Json <file>'); exit 2 }

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }

$CN = @{
    PyPI         = 'https://pypi.tuna.tsinghua.edu.cn/simple'
    PyStandalone = 'https://registry.npmmirror.com/-/binary/python-build-standalone/'
    Playwright   = 'https://cdn.npmmirror.com/binaries/playwright'
    Npm          = 'https://registry.npmmirror.com'
    Node         = 'https://registry.npmmirror.com/-/binary/node'
}
# GitHub release proxies (URL prefix + the original https://github.com/... URL).
# Free proxies come and go, so several are tried and the first one that really
# serves bytes wins. Override with the QAI_GITHUB_MIRROR environment variable.
$GhCandidates = @('https://gh.llkk.cc/', 'https://ghfast.top/', 'https://gh-proxy.com/', 'https://ghproxy.net/', 'https://mirror.ghproxy.com/', 'https://github.moeyy.xyz/')
$GhTestAsset  = 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip'

# ---- log file ------------------------------------------------------------------
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$script:T0 = [Diagnostics.Stopwatch]::StartNew()
$LogMode = if ($ProbeAll) { 'probe' } else { 'config' }
if (-not $LogFile) { $LogFile = Join-Path $Root ('data\logs\net_{0}_{1}.log' -f $LogMode, (Get-Date -Format 'yyyyMMdd_HHmmss')) }
$script:LogOk = $true
try {
    $ld = Split-Path -Parent $LogFile
    if ($ld -and -not (Test-Path -LiteralPath $ld)) { New-Item -ItemType Directory -Force -Path $ld | Out-Null }
    [IO.File]::WriteAllText($LogFile, '', (New-Object Text.UTF8Encoding($false)))
    # keep the 20 newest net_*.log files
    Get-ChildItem -LiteralPath $ld -Filter 'net_*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 | Remove-Item -Force -ErrorAction SilentlyContinue
} catch { $script:LogOk = $false }

function Write-Log([string]$Text) {
    if (-not $script:LogOk) { return }
    try { [IO.File]::AppendAllText($LogFile, $Text + "`r`n", (New-Object Text.UTF8Encoding($false))) } catch { }
}
function Hide-Secret([string]$s) { if ($s) { $s -replace '://[^/@\s]+@', '://***@' } else { $s } }

# ---- proxy resolution ------------------------------------------------------
$proxyUrl = $Proxy
$proxySrc = 'argument'
if (-not $proxyUrl) { foreach ($n in 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY') { $v = [Environment]::GetEnvironmentVariable($n); if ($v) { $proxyUrl = $v; $proxySrc = "env $n"; break } } }
if (-not $proxyUrl) {
    try {
        $sp = [Net.WebRequest]::GetSystemWebProxy()
        $u = $sp.GetProxy([Uri]'https://github.com')
        if ($u -and $u.Host -ne 'github.com') { $proxyUrl = $u.AbsoluteUri.TrimEnd('/'); $proxySrc = 'system proxy' }
    } catch { }
}
if ($proxyUrl -and $proxyUrl -notmatch '^[a-z]+://') { $proxyUrl = "http://$proxyUrl" }

# ---- environment dump (written once, before the probes) ---------------------------
function Write-EnvDiag([string[]]$Urls) {
    Write-Log ('=== net_config {0}  {1} ===' -f $LogMode, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    Write-Log ('OS         : {0}  64bit={1}  arch={2}' -f [Environment]::OSVersion.VersionString, [Environment]::Is64BitOperatingSystem, $env:PROCESSOR_ARCHITECTURE)
    Write-Log ('PowerShell : {0}  CLR {1}  user={2}' -f $PSVersionTable.PSVersion, $PSVersionTable.CLRVersion, [Environment]::UserName)
    Write-Log ('TLS        : {0}' -f [Net.ServicePointManager]::SecurityProtocol)
    Write-Log ('Proxy used : {0}  (source: {1})' -f $(if ($proxyUrl) { Hide-Secret $proxyUrl } else { '(none)' }), $(if ($proxyUrl) { $proxySrc } else { '-' }))
    foreach ($n in 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'NO_PROXY', 'QAI_GITHUB_MIRROR') {
        $v = [Environment]::GetEnvironmentVariable($n); if ($v) { Write-Log ('env {0} = {1}' -f $n, (Hide-Secret $v)) }
    }
    try {
        $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        Write-Log ('WinINET    : ProxyEnable={0} ProxyServer={1} AutoConfigURL={2} AutoDetect={3} ProxyOverride={4}' -f $ie.ProxyEnable, (Hide-Secret ([string]$ie.ProxyServer)), $ie.AutoConfigURL, $ie.AutoDetect, $ie.ProxyOverride)
    } catch { Write-Log ('WinINET    : (unreadable) ' + $_.Exception.Message) }
    $hosts = @(); foreach ($u in $Urls) { try { $h = ([Uri]$u).Host; if ($hosts -notcontains $h) { $hosts += $h } } catch { } }
    try {
        $sp = [Net.WebRequest]::GetSystemWebProxy()
        foreach ($h in $hosts) {
            $d = $sp.GetProxy([Uri]("https://$h/"))
            Write-Log ('system proxy decision  {0,-40} -> {1}' -f $h, $(if ($d.Host -eq $h) { 'DIRECT' } else { Hide-Secret $d.AbsoluteUri }))
        }
    } catch { Write-Log ('system proxy decision failed: ' + $_.Exception.Message) }
    try {
        foreach ($ni in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($ni.OperationalStatus -eq 'Up' -and $ni.NetworkInterfaceType -ne 'Loopback') {
                $dns = @($ni.GetIPProperties().DnsAddresses | ForEach-Object { $_.IPAddressToString })
                Write-Log ('adapter    : {0} [{1}] dns={2}' -f $ni.Name, $ni.NetworkInterfaceType, ($dns -join ', '))
            }
        }
    } catch { Write-Log ('adapter list failed: ' + $_.Exception.Message) }
    try {
        $hf = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
        $lines = @(Get-Content -LiteralPath $hf -ErrorAction Stop | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') })
        Write-Log ('hosts file : {0} active entries' -f $lines.Count)
        foreach ($l in $lines) { foreach ($h in $hosts) { if ($l -match [regex]::Escape($h)) { Write-Log ('hosts file : OVERRIDE ' + $l.Trim()); break } } }
    } catch { Write-Log ('hosts file : (unreadable) ' + $_.Exception.Message) }
    # control lookups: tell "all DNS is broken" from "only some domains fail"
    foreach ($h in (@('www.baidu.com', 'www.microsoft.com') + $hosts)) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try { $a = @([Net.Dns]::GetHostAddresses($h) | ForEach-Object { $_.IPAddressToString }); Write-Log ('DNS lookup : {0,-40} {1,5} ms  {2}' -f $h, $sw.ElapsedMilliseconds, ($a -join ', ')) }
        catch { Write-Log ('DNS lookup : {0,-40} {1,5} ms  FAILED {2}' -f $h, $sw.ElapsedMilliseconds, $_.Exception.Message) }
    }
    Write-Log ''
}

# ---- one probe (runs inside a runspace) ----------------------------------------
# Mode 'head'  : any HTTP answer (even 403/404) proves the host is reachable.
# Mode 'range' : GET bytes=0-0 must come back 206 (or a non-HTML 200) - proves a
#                download proxy really serves files (many answer 403 / an HTML page).
# DNS errors, timeouts and resets are retried: on home networks they are often transient.
# Returns @{ ok; ms; note; tries; detail; log = <lines for the log file> }.
$probeScript = {
    param($Url, $ProxyUrl, $Mode)
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }
    function Get-ExDesc($ex) {
        $parts = @(); $e = $ex
        while ($e) {
            $s = $e.GetType().Name + ': ' + $e.Message
            if ($e -is [Net.Sockets.SocketException]) { $s += ' (SocketError=' + $e.SocketErrorCode + ', native=' + $e.NativeErrorCode + ')' }
            $parts += $s; $e = $e.InnerException
        }
        $parts -join ' <- '
    }
    function Test-Tcp([string]$HostName, [int]$Port, $Addr) {
        $sw = [Diagnostics.Stopwatch]::StartNew(); $tc = New-Object Net.Sockets.TcpClient
        try {
            $iar = $tc.BeginConnect($Addr, $Port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(5000)) { try { $tc.EndConnect($iar); return "TCP connect $Addr`:$Port OK in $($sw.ElapsedMilliseconds) ms" } catch { return "TCP connect $Addr`:$Port FAILED in $($sw.ElapsedMilliseconds) ms: $(Get-ExDesc $_.Exception)" } }
            return "TCP connect $Addr`:$Port TIMED OUT after 5000 ms"
        } catch { return "TCP connect $Addr`:$Port error: $(Get-ExDesc $_.Exception)" }
        finally { $tc.Close() }
    }
    $log = New-Object Collections.Generic.List[string]
    $total = [Diagnostics.Stopwatch]::StartNew()
    $retryable = 'NameResolutionFailure', 'Timeout', 'ConnectFailure', 'ReceiveFailure', 'SendFailure', 'ConnectionClosed', 'KeepAliveFailure', 'ProxyNameResolutionFailure'
    $last = @{ ok = $false; ms = 0; note = 'error'; tries = 0; detail = 'no attempt ran' }
    $done = $null
    for ($try = 1; $try -le 3 -and -not $done; $try++) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            $req = [Net.HttpWebRequest]::Create($Url)
            $req.Timeout = 7000; $req.ReadWriteTimeout = 7000; $req.AllowAutoRedirect = $true
            $req.UserAgent = 'qai-setup-preflight'
            if ($Mode -eq 'range') { $req.Method = 'GET'; $req.AddRange(0, 0) } else { $req.Method = 'HEAD' }
            if ($ProxyUrl) { $req.Proxy = New-Object Net.WebProxy($ProxyUrl, $true) }
            $res = $req.GetResponse(); $code = [int]$res.StatusCode; $ct = [string]$res.ContentType; $final = [string]$res.ResponseUri; $res.Close()
            if ($Mode -eq 'range' -and -not ($code -eq 206 -or ($code -eq 200 -and $ct -notmatch 'text/html'))) {
                $log.Add("  try ${try}: HTTP $code content-type=$ct in $($sw.ElapsedMilliseconds) ms -> does not serve files (FAIL)")
                $done = @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = "HTTP $code"; tries = $try; detail = "HTTP $code, content-type $ct (not a file download)" }
            } else {
                $log.Add("  try ${try}: HTTP $code in $($sw.ElapsedMilliseconds) ms -> OK" + $(if ($final -and $final -ne $Url) { "  (final url $final)" } else { '' }))
                $done = @{ ok = $true; ms = $sw.ElapsedMilliseconds; note = 'ok'; tries = $try; detail = '' }
            }
        } catch [Net.WebException] {
            $resp = $_.Exception.Response
            if ($resp) {
                $code = [int]$resp.StatusCode
                if ($Mode -eq 'range') {
                    $log.Add("  try ${try}: HTTP $code $($resp.StatusDescription) in $($sw.ElapsedMilliseconds) ms -> does not serve files (FAIL)")
                    $done = @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = "HTTP $code"; tries = $try; detail = "HTTP $code $($resp.StatusDescription)" }
                } else {
                    $log.Add("  try ${try}: HTTP $code $($resp.StatusDescription) in $($sw.ElapsedMilliseconds) ms -> host reachable (OK)")
                    $done = @{ ok = $true; ms = $sw.ElapsedMilliseconds; note = [string]$code; tries = $try; detail = '' }
                }
            } else {
                $st = $_.Exception.Status.ToString()
                $desc = Get-ExDesc $_.Exception
                $log.Add("  try ${try}: FAIL status=$st in $($sw.ElapsedMilliseconds) ms; $desc")
                $last = @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = $st; tries = $try; detail = $desc }
                if ($retryable -notcontains $st) { $done = $last }
            }
        } catch {
            $desc = Get-ExDesc $_.Exception
            $log.Add("  try ${try}: ERROR $desc")
            $last = @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = $_.Exception.GetType().Name; tries = $try; detail = $desc }
            $done = $last
        }
        if (-not $done) {
            if ($total.Elapsed.TotalSeconds -gt 12) { $log.Add('  (giving up: more than 12 s spent on this URL)'); $done = $last }
            elseif ($try -lt 3) { Start-Sleep -Milliseconds 500 }
        }
    }
    if (-not $done) { $done = $last }

    # Diagnosis for a failed probe: where exactly does it break - DNS, TCP, proxy?
    if (-not $done.ok -and $done.note -notmatch '^HTTP ') {
        $diag = @()
        try {
            $uri = [Uri]$Url
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $addrs = $null
            try {
                $addrs = @([Net.Dns]::GetHostAddresses($uri.Host))
                $ips = ($addrs | ForEach-Object { $_.IPAddressToString }) -join ', '
                $log.Add("  diag: DNS $($uri.Host) -> $ips ($($sw.ElapsedMilliseconds) ms)")
                $diag += "local DNS ok ($ips)"
            } catch {
                $log.Add("  diag: DNS $($uri.Host) FAILED after $($sw.ElapsedMilliseconds) ms: $(Get-ExDesc $_.Exception)")
                $diag += "local DNS lookup of $($uri.Host) failed"
                # Ask every DNS server of every connected adapter on its own: shows WHICH server fails
                # (e.g. a VPN adapter's DNS answering badly while the Wi-Fi DNS is fine).
                try {
                    $servers = @()
                    foreach ($ni in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
                        if ($ni.OperationalStatus -eq 'Up' -and $ni.NetworkInterfaceType -ne 'Loopback') {
                            foreach ($d in $ni.GetIPProperties().DnsAddresses) { if ($d.AddressFamily -eq 'InterNetwork' -and $servers -notcontains $d.IPAddressToString) { $servers += $d.IPAddressToString } }
                        }
                    }
                    foreach ($srv in $servers) {
                        $s2 = [Diagnostics.Stopwatch]::StartNew()
                        try {
                            $rr = @(Resolve-DnsName -Name $uri.Host -Server $srv -Type A -DnsOnly -QuickTimeout -ErrorAction Stop | Where-Object { $_.IPAddress } | ForEach-Object { $_.IPAddress })
                            $log.Add("  diag: DNS server $srv answers $($uri.Host) -> $($rr -join ', ') ($($s2.ElapsedMilliseconds) ms)")
                        } catch { $log.Add("  diag: DNS server $srv FAILED for $($uri.Host) after $($s2.ElapsedMilliseconds) ms: $($_.Exception.Message)") }
                    }
                } catch { $log.Add("  diag: per-server DNS check error: $($_.Exception.Message)") }
            }
            if ($ProxyUrl) {
                try {
                    $pu = [Uri]$ProxyUrl
                    $pa = @([Net.Dns]::GetHostAddresses($pu.Host) | Where-Object { $_.AddressFamily -eq 'InterNetwork' })
                    if ($pa.Count -gt 0) { $t = Test-Tcp $pu.Host $pu.Port $pa[0]; $log.Add("  diag: proxy $($pu.Host):$($pu.Port) -> $t"); $diag += "proxy: $t" }
                } catch { $log.Add("  diag: proxy check error: $(Get-ExDesc $_.Exception)") }
            } elseif ($addrs) {
                $v4 = @($addrs | Where-Object { $_.AddressFamily -eq 'InterNetwork' })
                if ($v4.Count -gt 0) { $t = Test-Tcp $uri.Host $uri.Port $v4[0]; $log.Add("  diag: $t"); $diag += $t }
            }
        } catch { $log.Add("  diag: error $(Get-ExDesc $_.Exception)") }
        if ($diag.Count -gt 0) { $done.detail = $done.detail + ' | ' + ($diag -join '; ') }
    }
    $done.log = $log
    return $done
}

# Run probe jobs ({Key,Url,Mode}) with a concurrency limit; returns @{ Key -> result }.
# Every attempt of every job is appended to the log file (in job order).
function Invoke-Probes($jobs, [int]$Throttle = 6) {
    $pool = [RunspaceFactory]::CreateRunspacePool(1, $Throttle); $pool.Open()
    $running = @()
    foreach ($j in $jobs) {
        $ps = [PowerShell]::Create(); $ps.RunspacePool = $pool
        [void]$ps.AddScript($probeScript).AddArgument($j.Url).AddArgument($proxyUrl).AddArgument($j.Mode)
        $running += [pscustomobject]@{ Job = $j; PS = $ps; H = $ps.BeginInvoke() }
    }
    $res = @{}
    foreach ($x in $running) {
        $r = $null
        try { $out = $x.PS.EndInvoke($x.H); if ($out.Count -gt 0) { $r = $out[$out.Count - 1] } } catch { Write-Log ('  runspace error: ' + $_.Exception.Message) }
        if (-not $r) { $r = @{ ok = $false; ms = 0; note = 'error'; tries = 0; detail = 'probe returned nothing'; log = @() } }
        $x.PS.Dispose()
        Write-Log ('[{0}] {1} {2}' -f $x.Job.Key, $x.Job.Mode.ToUpper(), $x.Job.Url)
        foreach ($l in @($r.log)) { Write-Log $l }
        Write-Log ('  => {0}{1}' -f $(if ($r.ok) { 'OK' } else { 'FAIL' }), $(if ($r.ok) { '' } else { " ($($r.note))" }))
        $res[$x.Job.Key] = [ordered]@{ url = $x.Job.Url; ok = [bool]$r.ok; ms = [long]$r.ms; note = [string]$r.note; tries = [int]$r.tries; detail = [string]$r.detail }
    }
    $pool.Close()
    Write-Log ''
    $res
}

# Fastest GitHub proxy that really serves a file. $null when none.
function Select-GitHubMirror {
    if ($env:QAI_GITHUB_MIRROR) {
        Write-Host "[NET] GitHub mirror (from QAI_GITHUB_MIRROR): $env:QAI_GITHUB_MIRROR"
        return [ordered]@{ prefix = $env:QAI_GITHUB_MIRROR; result = $null }
    }
    $jobs = @(); $i = 0
    foreach ($c in $GhCandidates) { $jobs += [pscustomobject]@{ Key = "gh$i"; Url = ($c + $GhTestAsset); Mode = 'range' }; $i++ }
    $r = Invoke-Probes $jobs
    for ($k = 0; $k -lt $GhCandidates.Count; $k++) {
        $x = $r["gh$k"]
        Write-Host ('  {0} GitHub proxy {1,-30} {2}' -f $(if ($x.ok) { '[ OK ]' } else { '[FAIL]' }), $GhCandidates[$k], $(if ($x.ok) { "$($x.ms) ms" } else { $x.note }))
    }
    $best = $null
    for ($k = 0; $k -lt $GhCandidates.Count; $k++) { if ($r["gh$k"].ok -and (-not $best -or $r["gh$k"].ms -lt $best.result.ms)) { $best = [ordered]@{ prefix = $GhCandidates[$k]; result = $r["gh$k"] } } }
    return $best
}

# ---- -ProbeAll: every source, official + cn, in parallel (GUI step 1) ----------
if ($ProbeAll) {
    # id, display name, which option needs it, official url, cn url ('' = no mirror, '@github' = proxy candidates)
    $items = @(
        @('pypi',     'PyPI (Python packages)',           'core',     'https://pypi.org/simple/pip/',                       'https://pypi.tuna.tsinghua.edu.cn/simple/pip/'),
        @('pyfiles',  'files.pythonhosted.org',           'core',     'https://files.pythonhosted.org/',                    ''),
        @('python',   'Python 3.13 interpreter',          'core',     'https://github.com/astral-sh/python-build-standalone/releases/latest', 'https://registry.npmmirror.com/-/binary/python-build-standalone/'),
        @('github',   'GitHub releases (aria2c, guard...)', 'optional', 'https://github.com/astral-sh/uv/releases/latest',  '@github'),
        @('vendor',   'ARM64 vendor bundle',              'arm64',    'https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z', ''),
        @('chromium', 'Chromium (browser tools)',         'browser',  'https://cdn.playwright.dev/',                        ($CN.Playwright + '/')),
        @('node',     'Node.js (WebUI build)',            'frontend', 'https://nodejs.org/dist/index.json',                 ($CN.Node + '/')),
        @('npm',      'npm registry (pnpm packages)',     'frontend', 'https://registry.npmjs.org/pnpm',                    ($CN.Npm + '/pnpm')),
        @('qairt',    'Qualcomm software center (QAIRT)', 'builder',  'https://softwarecenter.qualcomm.com/',               '')
    )
    if ($Only) { $want = @($Only -split ',' | ForEach-Object { $_.Trim() }); $items = @($items | Where-Object { $want -contains $_[0] }) }
    $jobs = @()
    foreach ($it in $items) {
        $jobs += [pscustomobject]@{ Key = "$($it[0])|official"; Url = $it[3]; Mode = 'head' }
        if ($it[4] -eq '@github') {
            $k = 0
            if ($env:QAI_GITHUB_MIRROR) { $jobs += [pscustomobject]@{ Key = 'github|cn|0'; Url = ($env:QAI_GITHUB_MIRROR + $GhTestAsset); Mode = 'range' } }
            else { foreach ($c in $GhCandidates) { $jobs += [pscustomobject]@{ Key = "github|cn|$k"; Url = ($c + $GhTestAsset); Mode = 'range' }; $k++ } }
        } elseif ($it[4]) {
            $jobs += [pscustomobject]@{ Key = "$($it[0])|cn"; Url = $it[4]; Mode = 'head' }
        }
    }
    Write-Host "[NET] proxy : $(if ($proxyUrl) { "$(Hide-Secret $proxyUrl) ($proxySrc)" } else { '(none)' })"
    Write-Host "[NET] log   : $LogFile"
    Write-EnvDiag @($jobs | ForEach-Object { $_.Url })
    Write-Host "[NET] probing $($jobs.Count) URLs (max 6 at a time, DNS/timeout errors retried) ..."
    $res = Invoke-Probes $jobs

    # GitHub proxy: the FASTEST candidate that really served bytes.
    $ghPick = $null
    if ($res.Keys | Where-Object { $_ -like 'github|cn|*' }) {
        $cands = if ($env:QAI_GITHUB_MIRROR) { @($env:QAI_GITHUB_MIRROR) } else { @($GhCandidates) }
        $fails = @()
        for ($k = 0; $k -lt $cands.Count; $k++) {
            $x = $res["github|cn|$k"]
            if ($x -and $x.ok -and (-not $ghPick -or $x.ms -lt $ghPick.result.ms)) { $ghPick = [ordered]@{ prefix = $cands[$k]; result = $x } }
            if ($x -and -not $x.ok) { $fails += ('{0}: {1}' -f $cands[$k], $x.detail) }
        }
        if ($ghPick) { $r = $ghPick.result; $res['github|cn'] = [ordered]@{ url = $r.url; ok = $true; ms = $r.ms; note = 'ok'; tries = $r.tries; detail = ''; mirror = $ghPick.prefix } }
        else { $res['github|cn'] = [ordered]@{ url = ''; ok = $false; ms = 0; note = 'no working GitHub proxy'; tries = 0; detail = ('tried ' + $cands.Count + ' proxies - ' + ($fails -join ' || ')); mirror = '' } }
    }

    $outItems = @()
    Write-Host ('  {0,-36} {1,-26} {2}' -f 'source', 'official', 'cn mirror')
    foreach ($it in $items) {
        $o = $res["$($it[0])|official"]; $c = $res["$($it[0])|cn"]
        $fmt = { param($x) if (-not $x) { '-' } elseif ($x.ok) { $s = "OK $($x.ms) ms"; if ($x.mirror) { $s += " via $($x.mirror)" }; $s } else { $t = "FAIL ($($x.note)"; if ($x.tries -gt 1) { $t += " x$($x.tries)" }; $t + ')' } }
        Write-Host ('  {0,-36} {1,-26} {2}' -f $it[1], (& $fmt $o), (& $fmt $c))
        foreach ($p in @(@('official', $o), @('cn', $c))) { if ($p[1] -and -not $p[1].ok -and $p[1].detail) { Write-Host ('      {0} {1}: {2}' -f $it[0], $p[0], $p[1].detail) } }
        $outItems += [ordered]@{ id = $it[0]; name = $it[1]; need = $it[2]; official = $o; cn = $c }
    }

    # Recommendation. GitHub is optional (aria2c / guard / ast-grep only): uv comes from a
    # PyPI wheel, Python from the mirror in cn mode, the app wheel from PyPI or the vendor host.
    $offOk = $res['pypi|official'].ok -and $res['pyfiles|official'].ok -and $res['python|official'].ok
    $cnOk  = $res['pypi|cn'].ok -and $res['python|cn'].ok
    if ($Only) { $rec = 'partial'; $why = 'subset probed (-Only); caller decides' }
    elseif ($offOk) {
        $rec = 'official'; $why = 'official sources reachable'
        # Much slower than the mirror (e.g. 3x and > 1.5 s) -> suggest cn.
        if ($cnOk -and $res['pypi|official'].ms -gt 1500 -and $res['pypi|official'].ms -gt 3 * $res['pypi|cn'].ms) { $rec = 'cn'; $why = 'official sources reachable but much slower than the cn mirrors' }
    } elseif ($cnOk) { $rec = 'cn'; $why = 'official sources unreachable, cn mirrors reachable' }
    else { $rec = 'blocked'; $why = 'neither official sources nor cn mirrors fully reachable - set a proxy' }
    Write-Host "[NET] recommend: $rec ($why)"
    Write-Log "RECOMMEND: $rec ($why)"
    Write-Log ("TOTAL TIME: {0:N1} s" -f $script:T0.Elapsed.TotalSeconds)

    $doc = [ordered]@{ proxy = [string](Hide-Secret $proxyUrl); proxySource = $(if ($proxyUrl) { $proxySrc } else { '' }); recommend = $rec; reason = $why; log = $LogFile; items = $outItems }
    $dir = Split-Path -Parent $Json
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($Json, ($doc | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
    exit 0
}

# ---- classic mode: probe groups, decide profile, emit .bat -------------------------
$groups = @{
    core    = @(
        @('pypi.org (PyPI)',            'https://pypi.org/simple/pip/'),
        @('files.pythonhosted.org',     'https://files.pythonhosted.org/'),
        @('github.com (uv/releases)',   'https://github.com/astral-sh/uv/releases/latest')
    )
    browser = @(@('cdn.playwright.dev (Chromium)', 'https://cdn.playwright.dev/'))
    builder = @(
        @('softwarecenter.qualcomm.com (QAIRT)', 'https://softwarecenter.qualcomm.com/'),
        @('python.org (signed Python)',          'https://www.python.org/')
    )
    frontend = @(
        @('nodejs.org (Node.js zip)',   'https://nodejs.org/dist/index.json'),
        @('registry.npmjs.org (npm)',   'https://registry.npmjs.org/pnpm')
    )
}
$cnProbe = @(
    @('tuna PyPI mirror',     'https://pypi.tuna.tsinghua.edu.cn/simple/pip/'),
    @('npmmirror (python/playwright)', 'https://registry.npmmirror.com/')
)

$results = @{}
function Show-Probes($list) {
    $jobs = @(); foreach ($e in $list) { $jobs += [pscustomobject]@{ Key = $e[0]; Url = $e[1]; Mode = 'head' } }
    $r = Invoke-Probes $jobs
    foreach ($e in $list) {
        $x = $r[$e[0]]; $results[$e[0]] = $x
        Write-Host ("  {0} {1,-40} {2,6} ms  {3}" -f $(if ($x.ok) { '[ OK ]' } else { '[FAIL]' }), $e[0], $x.ms, $x.note)
        if (-not $x.ok -and $x.detail) { Write-Host ('         ' + $x.detail) }
    }
}

Write-Host "[NET] proxy : $(if ($proxyUrl) { "$(Hide-Secret $proxyUrl) ($proxySrc)" } else { '(none)' })"
Write-Host "[NET] log   : $LogFile"
$pypiOk = $true
if (-not $NoProbe) {
    Write-Host '[NET] connectivity probe (HEAD, retried on DNS/timeout errors):'
    $list = @()
    foreach ($g in ($Need -split ',')) { if ($groups.ContainsKey($g.Trim())) { $list += $groups[$g.Trim()] } }
    Write-EnvDiag @($list | ForEach-Object { $_[1] } | Where-Object { $_ }) 
    Show-Probes $list
    # Only judge a group that was actually probed (build_frontend probes frontend only).
    # github.com is NOT required: uv comes from a PyPI wheel, and aria2c/guard are optional.
    if ($results.ContainsKey('pypi.org (PyPI)')) { $pypiOk = $results['pypi.org (PyPI)'].ok -and $results['files.pythonhosted.org'].ok }
    if ($results.ContainsKey('registry.npmjs.org (npm)')) { $pypiOk = $pypiOk -and $results['registry.npmjs.org (npm)'].ok -and $results['nodejs.org (Node.js zip)'].ok }
} else {
    Write-EnvDiag @()
}

# ---- profile decision ------------------------------------------------------
$profile = $Mirror
if ($Mirror -eq 'auto') {
    $profile = 'official'
    if (-not $NoProbe -and -not $pypiOk) {
        $probe = if ($results.ContainsKey('pypi.org (PyPI)')) { $cnProbe[0] } else { $cnProbe[1] }
        Show-Probes @(, $probe)
        if ($results[$probe[0]].ok) { $profile = 'cn'; Write-Host '[NET] official source unreachable but cn mirror reachable -> using cn mirrors' }
        else { Write-Host '[NET] WARN: neither the official source nor the cn mirror is reachable. Set a proxy: --proxy http://host:port' }
    }
}
Write-Host "[NET] profile: $profile"
Write-Log "PROFILE: $profile"

# ---- GitHub proxy (cn profile only; works even with -NoProbe) ---------------------
$ghPrefix = ''
if ($profile -eq 'cn') {
    Write-Host '[NET] selecting a GitHub download proxy ...'
    $pick = Select-GitHubMirror
    if ($pick) { $ghPrefix = $pick.prefix; Write-Host "[NET] GitHub proxy: $ghPrefix"; Write-Log "GITHUB PROXY: $ghPrefix" }
    else { Write-Host '[NET] WARN: no GitHub proxy works here - GitHub downloads (aria2c, guard, ast-grep) will be skipped or slow. PyPI / npm mirrors are unaffected.'; Write-Log 'GITHUB PROXY: none working' }
}

# ---- emit ------------------------------------------------------------------
$lines = New-Object Collections.Generic.List[string]
$lines.Add('@echo off')
$lines.Add("set `"QAI_NET_PROFILE=$profile`"")
if ($proxyUrl) {
    foreach ($n in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') { $lines.Add("set `"$n=$proxyUrl`"") }
    $lines.Add('set "NO_PROXY=localhost,127.0.0.1,::1"')
}
if ($profile -eq 'cn') {
    $lines.Add("set `"UV_DEFAULT_INDEX=$($CN.PyPI)`"")
    $lines.Add("set `"PIP_INDEX_URL=$($CN.PyPI)`"")
    $lines.Add("set `"UV_PYTHON_INSTALL_MIRROR=$($CN.PyStandalone)`"")
    $lines.Add("set `"PLAYWRIGHT_DOWNLOAD_HOST=$($CN.Playwright)`"")
    if ($ghPrefix) { $lines.Add("set `"QAI_GITHUB_MIRROR=$ghPrefix`"") }
    # npm / pnpm / corepack read these; QAI_NODE_MIRROR is used by build_frontend.bat
    $lines.Add("set `"NPM_CONFIG_REGISTRY=$($CN.Npm)`"")
    $lines.Add("set `"COREPACK_NPM_REGISTRY=$($CN.Npm)`"")
    $lines.Add("set `"QAI_NODE_MIRROR=$($CN.Node)`"")
}
Write-Log ("TOTAL TIME: {0:N1} s" -f $script:T0.Elapsed.TotalSeconds)
# % must be doubled inside a .bat file
$text = ($lines -join "`r`n") -replace '%', '%%'
[IO.File]::WriteAllText($Emit, $text + "`r`n", (New-Object Text.UTF8Encoding($false)))
exit 0
