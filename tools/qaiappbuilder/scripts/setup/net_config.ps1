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
      QAI_NET_PROFILE                        official | cn

.PARAMETER Proxy
    Explicit proxy URL. Default: HTTPS_PROXY/HTTP_PROXY env, else the system proxy.
.PARAMETER Mirror
    auto (default) | official | cn.  auto = use cn mirrors only when pypi.org
    is unreachable but the cn PyPI mirror is reachable.
.PARAMETER Emit
    Path of the .bat fragment to write.
.PARAMETER NoProbe
    Skip the connectivity probe (auto then behaves like official).
.PARAMETER Need
    Comma list of probe groups: core,browser,builder. Default core.
#>
param(
    [string]$Proxy = '',
    [ValidateSet('auto', 'official', 'cn')][string]$Mirror = 'auto',
    [Parameter(Mandatory = $true)][string]$Emit,
    [switch]$NoProbe,
    [string]$Need = 'core'
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch { }

$CN = @{
    PyPI       = 'https://pypi.tuna.tsinghua.edu.cn/simple'
    PyStandalone = 'https://registry.npmmirror.com/-/binary/python-build-standalone/'
    Playwright = 'https://cdn.npmmirror.com/binaries/playwright'
    GitHub     = 'https://ghfast.top/'
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
    $pypiOk = $results['pypi.org (PyPI)'].Ok -and $results['files.pythonhosted.org'].Ok
}

# ---- profile decision ------------------------------------------------------
$profile = $Mirror
if ($Mirror -eq 'auto') {
    $profile = 'official'
    if (-not $NoProbe -and -not $pypiOk) {
        $t = Run-Probe $cnProbe[0][0] $cnProbe[0][1]
        if ($t.Ok) { $profile = 'cn'; Write-Host '[NET] pypi.org unreachable but cn mirror reachable -> using cn mirrors' }
        else { Write-Host '[NET] WARN: neither pypi.org nor the cn mirror is reachable. Set a proxy: setup_env.bat --proxy http://host:port' }
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
}
# % must be doubled inside a .bat file
$text = ($lines -join "`r`n") -replace '%', '%%'
[IO.File]::WriteAllText($Emit, $text + "`r`n", (New-Object Text.UTF8Encoding($false)))
exit 0
