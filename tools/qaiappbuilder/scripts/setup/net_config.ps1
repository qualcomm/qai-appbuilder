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
      NPM_CONFIG_REGISTRY / COREPACK_NPM_REGISTRY   npm registry mirror (pnpm, corepack)
      QAI_NODE_MIRROR                        Node.js dist mirror (build_frontend.bat)
      QAI_NET_PROFILE                        official | cn

.PARAMETER Proxy
    Explicit proxy URL. Default: HTTPS_PROXY/HTTP_PROXY env, else the system proxy.
.PARAMETER Mirror
    auto (default) | official | cn.  auto = use cn mirrors only when the official
    source of the probed groups (pypi.org, or nodejs.org/npmjs for the frontend
    group) is unreachable but the matching cn mirror is reachable.
.PARAMETER Emit
    Path of the .bat fragment to write.
.PARAMETER NoProbe
    Skip the connectivity probe (auto then behaves like official).
.PARAMETER Need
    Comma list of probe groups: core,browser,builder,frontend. Default core.
.PARAMETER ProbeAll
    GUI mode (setup_gui.ps1 step 1): probe EVERY source, official AND cn mirror,
    in parallel, print a table and write the result as JSON to -Json. Then exit;
    -Emit / -Mirror / -Need are ignored. JSON shape:
      { proxy, proxySource, recommend: official|cn|blocked, reason,
        items: [ { id, name, need, official:{label,url,ok,ms,note}, cn:{...}|null } ] }
.PARAMETER Json
    Output file for -ProbeAll.
.PARAMETER Only
    With -ProbeAll: comma list of item ids to (re)probe, e.g. pypi,github.
#>
param(
    [string]$Proxy = '',
    [ValidateSet('auto', 'official', 'cn')][string]$Mirror = 'auto',
    [string]$Emit = '',
    [switch]$NoProbe,
    [string]$Need = 'core',
    [switch]$ProbeAll,
    [string]$Json = '',
    [string]$Only = ''
)
if (-not $ProbeAll -and -not $Emit) { [Console]::Error.WriteLine('[NET] -Emit is required (or use -ProbeAll -Json <file>)'); exit 2 }
if ($ProbeAll -and -not $Json) { [Console]::Error.WriteLine('[NET] -ProbeAll needs -Json <file>'); exit 2 }

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }

$CN = @{
    PyPI       = 'https://pypi.tuna.tsinghua.edu.cn/simple'
    PyStandalone = 'https://registry.npmmirror.com/-/binary/python-build-standalone/'
    Playwright = 'https://cdn.npmmirror.com/binaries/playwright'
    GitHub     = 'https://ghfast.top/'
    Npm        = 'https://registry.npmmirror.com'
    Node       = 'https://registry.npmmirror.com/-/binary/node'
}

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

function Test-Url([string]$Url, [int]$TimeoutSec = 8) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $req = [Net.HttpWebRequest]::Create($Url)
        $req.Method = 'HEAD'; $req.Timeout = $TimeoutSec * 1000; $req.AllowAutoRedirect = $true
        $req.UserAgent = 'qai-setup-preflight'
        if ($proxyUrl) { $req.Proxy = New-Object Net.WebProxy($proxyUrl, $true) }
        $res = $req.GetResponse(); $res.Close()
        return @{ Ok = $true; Ms = $sw.ElapsedMilliseconds; Note = 'ok' }
    } catch [Net.WebException] {
        # An HTTP error status (403/405/404) still proves the host is reachable.
        if ($_.Exception.Response) { return @{ Ok = $true; Ms = $sw.ElapsedMilliseconds; Note = [int]$_.Exception.Response.StatusCode } }
        return @{ Ok = $false; Ms = $sw.ElapsedMilliseconds; Note = $_.Exception.Status.ToString() }
    } catch { return @{ Ok = $false; Ms = $sw.ElapsedMilliseconds; Note = $_.Exception.Message } }
}

# ---- -ProbeAll: every source, official + cn, in parallel (GUI step 1) ----------
if ($ProbeAll) {
    # id, display name, which option needs it, official url, cn url ('' = no mirror)
    $items = @(
        @('pypi',     'PyPI (Python packages)',          'core',     'https://pypi.org/simple/pip/',                       'https://pypi.tuna.tsinghua.edu.cn/simple/pip/'),
        @('pyfiles',  'files.pythonhosted.org',          'core',     'https://files.pythonhosted.org/',                    ''),
        @('python',   'Python 3.13 interpreter',         'core',     'https://github.com/astral-sh/python-build-standalone/releases/latest', 'https://registry.npmmirror.com/-/binary/python-build-standalone/'),
        @('github',   'GitHub releases (uv, aria2c ...)', 'core',    'https://github.com/astral-sh/uv/releases/latest',    ($CN.GitHub + 'https://github.com/astral-sh/uv/releases/latest')),
        @('vendor',   'ARM64 vendor bundle',             'arm64',    'https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z', ''),
        @('chromium', 'Chromium (browser tools)',        'browser',  'https://cdn.playwright.dev/',                        ($CN.Playwright + '/')),
        @('node',     'Node.js (WebUI build)',           'frontend', 'https://nodejs.org/dist/index.json',                 ($CN.Node + '/')),
        @('npm',      'npm registry (pnpm packages)',    'frontend', 'https://registry.npmjs.org/pnpm',                    ($CN.Npm + '/pnpm')),
        @('qairt',    'Qualcomm software center (QAIRT)', 'builder', 'https://softwarecenter.qualcomm.com/',               '')
    )
    if ($Only) { $want = @($Only -split ',' | ForEach-Object { $_.Trim() }); $items = @($items | Where-Object { $want -contains $_[0] }) }
    $jobs = @()
    foreach ($it in $items) {
        foreach ($side in 'official', 'cn') {
            $u = if ($side -eq 'official') { $it[3] } else { $it[4] }
            if ($u) { $jobs += [pscustomobject]@{ Id = $it[0]; Side = $side; Url = $u } }
        }
    }
    $probeScript = {
        param($Url, $ProxyUrl)
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            $req = [Net.HttpWebRequest]::Create($Url)
            $req.Method = 'HEAD'; $req.Timeout = 8000; $req.ReadWriteTimeout = 8000; $req.AllowAutoRedirect = $true
            $req.UserAgent = 'qai-setup-preflight'
            if ($ProxyUrl) { $req.Proxy = New-Object Net.WebProxy($ProxyUrl, $true) }
            $res = $req.GetResponse(); $res.Close()
            return @{ ok = $true; ms = $sw.ElapsedMilliseconds; note = 'ok' }
        } catch [Net.WebException] {
            if ($_.Exception.Response) { return @{ ok = $true; ms = $sw.ElapsedMilliseconds; note = [string][int]$_.Exception.Response.StatusCode } }
            return @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = $_.Exception.Status.ToString() }
        } catch { return @{ ok = $false; ms = $sw.ElapsedMilliseconds; note = $_.Exception.Message } }
    }
    Write-Host "[NET] proxy : $(if ($proxyUrl) { "$proxyUrl ($proxySrc)" } else { '(none)' })"
    Write-Host "[NET] probing $($jobs.Count) sources in parallel (HEAD, 8s timeout) ..."
    $pool = [RunspaceFactory]::CreateRunspacePool(1, 12); $pool.Open()
    foreach ($j in $jobs) {
        $ps = [PowerShell]::Create(); $ps.RunspacePool = $pool
        [void]$ps.AddScript($probeScript).AddArgument($j.Url).AddArgument($proxyUrl)
        $j | Add-Member -NotePropertyName PS -NotePropertyValue $ps
        $j | Add-Member -NotePropertyName H -NotePropertyValue $ps.BeginInvoke()
    }
    $res = @{}
    foreach ($j in $jobs) {
        $r = $null
        try { $out = $j.PS.EndInvoke($j.H); if ($out.Count -gt 0) { $r = $out[0] } } catch { }
        if (-not $r) { $r = @{ ok = $false; ms = 0; note = 'error' } }
        $j.PS.Dispose()
        $res["$($j.Id)|$($j.Side)"] = [ordered]@{ url = $j.Url; ok = [bool]$r.ok; ms = [long]$r.ms; note = [string]$r.note }
    }
    $pool.Close()

    $outItems = @()
    Write-Host ('  {0,-34} {1,-20} {2}' -f 'source', 'official', 'cn mirror')
    foreach ($it in $items) {
        $o = $res["$($it[0])|official"]; $c = $res["$($it[0])|cn"]
        $fmt = { param($x) if (-not $x) { '-' } elseif ($x.ok) { "OK $($x.ms) ms" } else { "FAIL ($($x.note))" } }
        Write-Host ('  {0,-34} {1,-20} {2}' -f $it[1], (& $fmt $o), (& $fmt $c))
        $outItems += [ordered]@{ id = $it[0]; name = $it[1]; need = $it[2]; official = $o; cn = $c }
    }

    # Recommendation: the core path decides (PyPI + GitHub + python). Official wins
    # when it works; cn when official fails but every cn core mirror works.
    $offOk = $res['pypi|official'].ok -and $res['pyfiles|official'].ok -and $res['github|official'].ok -and $res['python|official'].ok
    $cnOk  = $res['pypi|cn'].ok -and $res['github|cn'].ok -and $res['python|cn'].ok
    if ($Only) { $rec = 'partial'; $why = 'subset probed (-Only); caller decides' }
    elseif ($offOk) {
        $rec = 'official'; $why = 'official sources reachable'
        # Much slower than the mirror (e.g. 3x and > 1.5 s) -> suggest cn.
        if ($cnOk -and $res['pypi|official'].ms -gt 1500 -and $res['pypi|official'].ms -gt 3 * $res['pypi|cn'].ms) { $rec = 'cn'; $why = 'official sources reachable but much slower than the cn mirrors' }
    } elseif ($cnOk) { $rec = 'cn'; $why = 'official sources unreachable, cn mirrors reachable' }
    else { $rec = 'blocked'; $why = 'neither official sources nor cn mirrors fully reachable - set a proxy' }
    Write-Host "[NET] recommend: $rec ($why)"

    $doc = [ordered]@{ proxy = [string]$proxyUrl; proxySource = $(if ($proxyUrl) { $proxySrc } else { '' }); recommend = $rec; reason = $why; items = $outItems }
    $dir = Split-Path -Parent $Json
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($Json, ($doc | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
    exit 0
}

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
function Run-Probe($label, $url) {
    $r = Test-Url $url
    $results[$label] = $r
    $mark = if ($r.Ok) { '[ OK ]' } else { '[FAIL]' }
    Write-Host ("  {0} {1,-40} {2,6} ms  {3}" -f $mark, $label, $r.Ms, $r.Note)
    return $r
}

Write-Host "[NET] proxy : $(if ($proxyUrl) { "$proxyUrl ($proxySrc)" } else { '(none)' })"
$pypiOk = $true
if (-not $NoProbe) {
    Write-Host '[NET] connectivity probe (HEAD, 8s timeout):'
    foreach ($g in ($Need -split ',')) {
        if ($groups.ContainsKey($g.Trim())) { foreach ($e in $groups[$g.Trim()]) { [void](Run-Probe $e[0] $e[1]) } }
    }
    # Only judge a group that was actually probed (build_frontend probes frontend only).
    if ($results.ContainsKey('pypi.org (PyPI)')) { $pypiOk = $results['pypi.org (PyPI)'].Ok -and $results['files.pythonhosted.org'].Ok }
    if ($results.ContainsKey('registry.npmjs.org (npm)')) { $pypiOk = $pypiOk -and $results['registry.npmjs.org (npm)'].Ok -and $results['nodejs.org (Node.js zip)'].Ok }
}

# ---- profile decision ------------------------------------------------------
$profile = $Mirror
if ($Mirror -eq 'auto') {
    $profile = 'official'
    if (-not $NoProbe -and -not $pypiOk) {
        $probe = if ($results.ContainsKey('pypi.org (PyPI)')) { $cnProbe[0] } else { $cnProbe[1] }
        $t = Run-Probe $probe[0] $probe[1]
        if ($t.Ok) { $profile = 'cn'; Write-Host '[NET] official source unreachable but cn mirror reachable -> using cn mirrors' }
        else { Write-Host '[NET] WARN: neither the official source nor the cn mirror is reachable. Set a proxy: --proxy http://host:port' }
    }
}
Write-Host "[NET] profile: $profile"

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
    $lines.Add("set `"QAI_GITHUB_MIRROR=$($CN.GitHub)`"")
    # npm / pnpm / corepack read these; QAI_NODE_MIRROR is used by build_frontend.bat
    $lines.Add("set `"NPM_CONFIG_REGISTRY=$($CN.Npm)`"")
    $lines.Add("set `"COREPACK_NPM_REGISTRY=$($CN.Npm)`"")
    $lines.Add("set `"QAI_NODE_MIRROR=$($CN.Node)`"")
}
# % must be doubled inside a .bat file
$text = ($lines -join "`r`n") -replace '%', '%%'
[IO.File]::WriteAllText($Emit, $text + "`r`n", (New-Object Text.UTF8Encoding($false)))
exit 0
