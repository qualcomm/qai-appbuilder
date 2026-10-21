# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Step-by-step Windows setup wizard around setup_env.bat / build_frontend.bat.

.DESCRIPTION
    Three pages:
      1. Network  - proxy box + "Test network": probes every source, official
                    AND China mirror, in parallel (net_config.ps1 -ProbeAll) and
                    recommends official / cn. The test may be skipped.
      2. Options  - architecture + optional components + command preview.
      3. Install  - runs the command, progress bar, live log, Stop / Open log /
                    Start server; on failure "Back to network".
    All real work is still done by the .bat scripts; the network decision of
    page 1 is passed down as --mirror / --proxy (+ --no-probe), so the scripts do
    not probe again.

    Uses Windows PowerShell 5.1 + WinForms (present on every Windows 10/11,
    x64 and ARM64) because no Python exists yet when setup runs.

    Install runs as:  cmd /c "(call setup_env.bat ... [&& call build_frontend.bat ...]) > log 2>&1 < nul"
    Logs: data\logs\setup_gui_<net|setup>_<timestamp>.log

.PARAMETER Preset
    Test hook / pre-selection. Comma list of: browser, astgrep, apps, frontend,
    nofrontend, builder, noprobe, mirror=<auto|official|cn>, arch=<arm64|x64>,
    proxy=<url>.
.PARAMETER AutoRun
    Test hook: jump to page 3 and start the install as soon as the window shows.
.PARAMETER AutoNet
    Test hook: start the network test as soon as the window shows.
.PARAMETER AutoClose
    Test hook: close the window when the job ends; the script exit code is the
    job exit code.
.PARAMETER PrintCommand
    Test hook: print the command for -Preset and exit without a window.
.PARAMETER SelfTest
    Test hook: build the window, walk through the pages programmatically, print
    the checks and exit (no ShowDialog; exit code = number of failed checks).
#>
param(
    [string]$Preset = '',
    [switch]$AutoRun,
    [switch]$AutoNet,
    [switch]$AutoClose,
    [switch]$PrintCommand,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

# ---------------------------------------------------------------------------
# Strings (zh when the UI culture is Chinese, else en; QAI_GUI_LANG=en|zh overrides)
# ---------------------------------------------------------------------------
$lang = $env:QAI_GUI_LANG
if (-not $lang) { $lang = if ((Get-UICulture).Name -like 'zh*' -or (Get-Culture).Name -like 'zh*') { 'zh' } else { 'en' } }

$S_en = @{
    Title = 'QAI ModelBuilder - Setup'
    Step1 = '1. Network'; Step2 = '2. Options'; Step3 = '3. Install'
    Intro1 = 'Step 1: test the network and decide whether a proxy or the China mirrors are needed.'
    Intro2 = 'Step 2: choose what to install.'
    Intro3 = 'Step 3: download and install.'
    Proxy = 'Proxy'
    ProxyTip = 'Leave empty to use the HTTPS_PROXY environment variable, then the Windows system proxy. Format: http://host:port'
    ProxyDetected = 'detected: {0}'
    TestNet = 'Test network'; Retest = 'Retest failed'
    ColSource = 'Source'; ColNeed = 'Needed for'; ColOfficial = 'Official'; ColCn = 'China mirror'
    NeedCore = 'core'; NeedArm64 = 'ARM64 only'; NeedBrowser = 'browser tools'; NeedFrontend = 'WebUI build'; NeedBuilder = 'model builder'
    CellOk = 'OK  {0} ms'; CellFail = 'FAIL ({0})'
    Mirror = 'Use source'
    MirrorTip = 'auto: the installer probes again and switches to China mirrors only when the official source is unreachable; official: official sources only; cn: China mirrors'
    MirrorAuto = 'auto (decide during install)'; MirrorOfficial = 'official sources'; MirrorCn = 'China mirrors (cn)'
    Skip = 'Skip the network test (use the source selected above)'
    NotTested = 'Not tested yet. Click "Test network" (about 10 s), or tick "Skip".'
    Testing = 'Testing {0} sources in parallel ...'
    RecOfficial = 'Recommended: official sources (reachable).'
    RecCnSlow = 'Recommended: China mirrors (official sources reachable but much slower).'
    RecCn = 'Recommended: China mirrors (official sources unreachable, mirrors reachable).'
    RecBlocked = 'Neither the official sources nor the China mirrors are fully reachable. Enter a proxy and test again.'
    ProxyChanged = 'Proxy changed - test the network again.'
    BlockedConfirm = "The network test found the required sources unreachable; the install will most likely fail.`r`nContinue anyway?"
    NetSummary = 'Network: source = {0}, proxy = {1}   (go back to step 1 to change)'
    ProxyNone = '(none)'; ProxyAuto = '(HTTPS_PROXY / system proxy if any)'
    Arch = 'Architecture'; AutoArch = 'auto ({0})'
    GrpComp = 'Components'
    Core = 'Core runtime: Python 3.13 venv + server dependencies (always installed)'
    Browser = 'Browser tool + Google browser search (Playwright + Chromium, ~150 MB)'
    AstGrep = 'ast-grep CLI for the ast_grep / ast_edit tools (~50 MB)'
    Apps = 'App Builder voice / OCR model dependencies (whisper, melotts, ...)'
    Frontend = 'Build the WebUI frontend\dist (installs Node.js + pnpm, ~1-2 min)'
    FrontendHave = '  [already built - optional]'
    FrontendMissing = '  [missing - without it the page returns 404]'
    Builder = 'Model conversion env: QAIRT SDK (~2 GB) + x64 converter venv + VS Build Tools'
    BuilderWarn = "The model conversion environment downloads the ~2 GB QAIRT SDK and may install Visual Studio Build Tools; it takes a long time.`r`nIt is NOT needed to run pre-built models. Install it anyway?"
    Estimate = 'Estimated download: up to ~{0} (parts already present are skipped)'
    Cmd = 'Command to run'
    Back = '< Back'; Next = 'Next >'; Install = 'Install'
    Stop = 'Stop'; OpenLog = 'Open log'; StartSrv = 'Start server'; Close = 'Close'; BackNet = 'Back to network step'
    Ready = 'Click "Install" on step 2 to start.'
    Running = 'Running'
    Done = 'Finished successfully.'
    Failed = 'Failed (exit code {0}). See the log; network problems: go back to step 1.'; Stopped = 'Stopped by user.'
    ConfirmStop = 'A job is still running. Stop it and close?'
    ProxyBad = 'The proxy must look like http://host:port (no spaces, quotes or % & | < > ^ ( ) characters).'
    Elapsed = 'elapsed {0}'; LogAt = 'Full log: {0}'
    SlowestIs = 'slowest step: {0}'
    NetFailed = 'Network test failed to run (exit code {0}). See {1}'
}
$S_zh = @{
    Title = 'QAI ModelBuilder 安装'
    Step1 = '1. 网络'; Step2 = '2. 选项'; Step3 = '3. 安装'
    Intro1 = '第一步：检测网络，确定是否需要代理或国内镜像。'
    Intro2 = '第二步：选择要安装的内容。'
    Intro3 = '第三步：下载并安装。'
    Proxy = '代理'
    ProxyTip = '留空则依次使用 HTTPS_PROXY 环境变量、Windows 系统代理。格式:http://host:port'
    ProxyDetected = '检测到:{0}'
    TestNet = '检测网络'; Retest = '重测失败项'
    ColSource = '下载源'; ColNeed = '用途'; ColOfficial = '官方源'; ColCn = '国内镜像'
    NeedCore = '核心'; NeedArm64 = '仅 ARM64'; NeedBrowser = '浏览器工具'; NeedFrontend = 'WebUI 构建'; NeedBuilder = '模型转换'
    CellOk = 'OK  {0} ms'; CellFail = '失败({0})'
    Mirror = '使用源'
    MirrorTip = 'auto:安装时再检测一次，官方源不通才切换国内镜像;official:只用官方源;cn:使用国内镜像'
    MirrorAuto = 'auto(安装时自动判断)'; MirrorOfficial = '官方源(official)'; MirrorCn = '国内镜像(cn)'
    Skip = '跳过网络检测(直接使用上面选择的源)'
    NotTested = '尚未检测。点击“检测网络”(约 10 秒),或勾选“跳过”。'
    Testing = '正在并行检测 {0} 个下载源...'
    RecOfficial = '推荐：官方源(可正常访问)。'
    RecCnSlow = '推荐：国内镜像(官方源可访问，但明显慢于镜像)。'
    RecCn = '推荐：国内镜像(官方源无法访问，镜像可访问)。'
    RecBlocked = '官方源和国内镜像都无法完整访问。请填写代理后重新检测。'
    ProxyChanged = '代理已修改，请重新检测网络。'
    BlockedConfirm = "网络检测显示必需的下载源无法访问，安装很可能失败。`r`n仍要继续吗?"
    NetSummary = '网络：源 = {0},代理 = {1}   (返回第 1 步可修改)'
    ProxyNone = '(无)'; ProxyAuto = '(如有则用 HTTPS_PROXY / 系统代理)'
    Arch = '架构'; AutoArch = '自动({0})'
    GrpComp = '安装组件'
    Core = '核心运行环境:Python 3.13 虚拟环境 + 服务依赖(必装)'
    Browser = '浏览器工具和 Google 浏览器搜索(Playwright + Chromium,约 150 MB)'
    AstGrep = 'ast-grep 命令行(ast_grep / ast_edit 工具，约 50 MB)'
    Apps = 'App Builder 语音 / OCR 模型依赖(whisper、melotts 等)'
    Frontend = '构建 Web 界面 frontend\dist(自动安装 Node.js + pnpm,约 1~2 分钟)'
    FrontendHave = '  [已构建，可不选]'
    FrontendMissing = '  [缺失，不构建则页面返回 404]'
    Builder = '模型转换环境:QAIRT SDK(约 2 GB)+ x64 转换 venv + VS 构建工具'
    BuilderWarn = "模型转换环境需要下载约 2 GB 的 QAIRT SDK,并可能安装 Visual Studio 构建工具，耗时较长。`r`n只运行预构建模型不需要它。确定要安装吗?"
    Estimate = '预计下载：最多约 {0}(已存在的部分会跳过)'
    Cmd = '将执行的命令'
    Back = '< 上一步'; Next = '下一步 >'; Install = '开始安装'
    Stop = '停止'; OpenLog = '打开日志'; StartSrv = '启动服务'; Close = '关闭'; BackNet = '返回网络设置'
    Ready = '在第 2 步点击“开始安装”。'
    Running = '正在执行'
    Done = '安装完成。'
    Failed = '执行失败(退出码 {0}),请查看日志;如为网络问题请返回第 1 步。'; Stopped = '已被用户停止。'
    ConfirmStop = '任务仍在执行，确定要停止并关闭吗?'
    ProxyBad = '代理格式应为 http://host:port(不能包含空格、引号及 % & | < > ^ ( ) 等字符)。'
    Elapsed = '用时 {0}'; LogAt = '完整日志:{0}'
    SlowestIs = '最慢步骤:{0}'
    NetFailed = '网络检测未能运行(退出码 {0}),见 {1}'
}
$S = if ($lang -eq 'zh') { $S_zh } else { $S_en }

# ---------------------------------------------------------------------------
# Option model -> command line (no UI dependency, so -PrintCommand can test it)
# ---------------------------------------------------------------------------
function Get-HostArch {
    $f = Join-Path $Root 'data\config\host_arch'
    if (Test-Path -LiteralPath $f) {
        $v = ([string](Get-Content -LiteralPath $f -TotalCount 1)).Trim().ToLower()
        if ($v -eq 'arm64' -or $v -eq 'x64') { return $v }
    }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'AMD64') { return 'x64' }
    return 'arm64'
}

function Test-DistPresent { Test-Path -LiteralPath (Join-Path $Root 'frontend\dist\index.html') }

function New-Options {
    @{ Arch = ''; Mirror = 'auto'; Proxy = ''; NoProbe = $false
       Browser = $false; AstGrep = $false; Apps = $false; Builder = $false
       Frontend = -not (Test-DistPresent) }
}

function ConvertFrom-Preset([string]$p) {
    $o = New-Options
    foreach ($t in ($p -split ',')) {
        $t = $t.Trim()
        if (-not $t) { continue }
        if ($t -match '^(\w+)=(.*)$') { $o[$matches[1]] = $matches[2].Trim() }
        elseif ($t -eq 'nofrontend') { $o.Frontend = $false }
        else { $o[$t] = $true }
    }
    $o
}

function Test-ProxyOk([string]$p) {
    # Everything that would break the cmd /c line or a %VAR% expansion is refused.
    return ($p -match '^(https?|socks5h?)://[^\s"''%&|<>^()]+$')
}

function Get-CommonArgs($o) {
    $a = @('--no-pause')
    if ($o.Arch) { $a += @('--arch', $o.Arch) }
    if ($o.Mirror -and $o.Mirror -ne 'auto') { $a += @('--mirror', $o.Mirror) }
    if ($o.Proxy) { $a += @('--proxy', $o.Proxy) }
    if ($o.NoProbe) { $a += '--no-probe' }
    , $a
}

# Returns @(cmdline-for-cmd, display-text)
function Get-RunCommand($o) {
    $c = Get-CommonArgs $o
    $s = @('setup_env.bat') + $c
    if ($o.Browser) { $s += '--with-browser' }
    if ($o.AstGrep) { $s += '--with-astgrep' }
    if ($o.Apps)    { $s += '--with-apps' }
    if ($o.Builder) { $s += '--builder' }
    $run  = 'call ' + ($s -join ' ')
    $show = $s -join ' '
    if ($o.Frontend) {
        $f = (@('build_frontend.bat') + $c) -join ' '
        $run  += ' && call ' + $f
        $show += '  &&  ' + $f
    }
    , @($run, $show)
}

$NetJson = 'data\downloads\_net_probe.json'
function Get-NetCommand([string]$proxy, [string]$only) {
    $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\net_config.ps1 -ProbeAll -Json "' + $NetJson + '"'
    if ($proxy) { $cmd += ' -Proxy "' + $proxy + '"' }
    if ($only)  { $cmd += ' -Only ' + $only }
    $cmd
}

function Get-TotalSteps($o) {
    $n = 7
    if ($o.Builder)  { $n += 1 }
    if ($o.Frontend) { $n += 4 }
    $n
}

# Upper-bound download estimate in MB (things already on disk are skipped by the scripts).
function Get-EstimateMB($o) {
    $arch = if ($o.Arch) { $o.Arch } else { Get-HostArch }
    $mb = 120                                   # uv + Python + PyPI deps + aria2c
    if ($arch -eq 'arm64' -and -not (Get-ChildItem -Path (Join-Path $Root 'vendor\whl') -Filter 'qai_appbuilder-*win_arm64.whl' -ErrorAction SilentlyContinue)) { $mb += 211 }
    if ($arch -eq 'x64') { $mb += 92 }          # qai_appbuilder wheel
    if ($o.Browser)  { $mb += 150 }
    if ($o.AstGrep)  { $mb += 50 }
    if ($o.Apps)     { $mb += 300 }
    if ($o.Frontend) { $mb += 180 }             # Node zip + pnpm packages
    if ($o.Builder)  { $mb += 2200 }
    $mb
}
function Format-MB([int]$mb) { if ($mb -ge 1024) { '{0:F1} GB' -f ($mb / 1024) } else { "$mb MB" } }

# Recommendation from the probe items (same rule as net_config.ps1; recomputed
# here because "Retest failed" merges a partial result into the table).
function Get-NetRecommend($items) {
    $m = @{}; foreach ($i in $items) { $m[$i.id] = $i }
    function Ok($id, $side) { $x = $m[$id]; if (-not $x) { return $false }; $y = $x.$side; return ($null -ne $y -and [bool]$y.ok) }
    $offOk = (Ok 'pypi' 'official') -and (Ok 'pyfiles' 'official') -and (Ok 'github' 'official') -and (Ok 'python' 'official')
    $cnOk  = (Ok 'pypi' 'cn') -and (Ok 'github' 'cn') -and (Ok 'python' 'cn')
    if ($offOk) {
        $po = [long]$m['pypi'].official.ms
        if ($cnOk -and $po -gt 1500 -and $po -gt 3 * [long]$m['pypi'].cn.ms) { return 'cnslow' }
        return 'official'
    }
    if ($cnOk) { return 'cn' }
    return 'blocked'
}

if ($PrintCommand) {
    $o = ConvertFrom-Preset $Preset
    if ($o.Proxy -and -not (Test-ProxyOk $o.Proxy)) { Write-Output 'INVALID_PROXY'; exit 2 }
    $rc = Get-RunCommand $o
    Write-Output $rc[1]
    Write-Output ('total_steps=' + (Get-TotalSteps $o))
    exit 0
}

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    [Console]::Error.WriteLine('[ERROR] PowerShell runs in ' + $ExecutionContext.SessionState.LanguageMode + ' (WDAC / AppLocker policy); the setup GUI cannot start. Run setup_env.bat directly.')
    exit 3
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$uiFont   = if ($lang -eq 'zh') { New-Object System.Drawing.Font('Microsoft YaHei UI', 9) } else { New-Object System.Drawing.Font('Segoe UI', 9) }
$boldFont = New-Object System.Drawing.Font($uiFont, [System.Drawing.FontStyle]::Bold)
$stepFont = New-Object System.Drawing.Font($uiFont.FontFamily, 11, [System.Drawing.FontStyle]::Bold)
$logFont  = New-Object System.Drawing.Font('Consolas', 9)
$cOk   = [System.Drawing.Color]::FromArgb(0, 128, 0)
$cFail = [System.Drawing.Color]::FromArgb(200, 0, 0)
$cDim  = [System.Drawing.Color]::FromArgb(150, 150, 150)
$cCur  = [System.Drawing.Color]::FromArgb(0, 90, 200)

function New-Ctl([string]$type, [int]$x, [int]$y, [int]$w, [int]$h, [string]$text) {
    $c = New-Object ("System.Windows.Forms." + $type)
    $c.Location = New-Object System.Drawing.Point($x, $y)
    if ($w -gt 0) { $c.Size = New-Object System.Drawing.Size($w, $h) } else { $c.AutoSize = $true }
    if ($text) { $c.Text = $text }
    $c
}
$AnchorTLR = [System.Windows.Forms.AnchorStyles]'Top, Left, Right'
$AnchorAll = [System.Windows.Forms.AnchorStyles]'Top, Bottom, Left, Right'
$AnchorTR  = [System.Windows.Forms.AnchorStyles]'Top, Right'
$AnchorBL  = [System.Windows.Forms.AnchorStyles]'Bottom, Left'
$AnchorBR  = [System.Windows.Forms.AnchorStyles]'Bottom, Right'
$AnchorBLR = [System.Windows.Forms.AnchorStyles]'Bottom, Left, Right'

$form = New-Object System.Windows.Forms.Form
$form.Text = $S.Title
$form.Font = $uiFont
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object System.Drawing.Size(800, 640)
$form.MinimumSize = New-Object System.Drawing.Size(740, 600)

$tip = New-Object System.Windows.Forms.ToolTip
$tip.AutoPopDelay = 15000

# --- step header -----------------------------------------------------------
$hdr = New-Object System.Windows.Forms.Panel
$hdr.Location = New-Object System.Drawing.Point(0, 0)
$hdr.Size = New-Object System.Drawing.Size(800, 44)
$hdr.Anchor = $AnchorTLR
$hdr.BackColor = [System.Drawing.Color]::FromArgb(242, 245, 250)
$form.Controls.Add($hdr)
$stepLbls = @()
$sx = 16
foreach ($t in $S.Step1, $S.Step2, $S.Step3) {
    $l = New-Ctl 'Label' $sx 12 0 0 $t
    $l.Font = $stepFont
    $hdr.Controls.Add($l)
    $stepLbls += $l
    $sx += 150
    if ($stepLbls.Count -lt 3) {
        $a = New-Ctl 'Label' ($sx - 34) 12 0 0 '>'
        $a.Font = $stepFont; $a.ForeColor = $cDim
        $hdr.Controls.Add($a)
    }
}

function New-Page {
    $p = New-Object System.Windows.Forms.Panel
    $p.Location = New-Object System.Drawing.Point(0, 48)
    $p.Size = New-Object System.Drawing.Size(800, 542)
    $p.Anchor = $AnchorAll
    $p.Visible = $false
    $form.Controls.Add($p)
    $p
}

# --- page 1: network -------------------------------------------------------
$pg1 = New-Page
$l = New-Ctl 'Label' 12 6 0 0 $S.Intro1; $l.Font = $boldFont; $pg1.Controls.Add($l)

$pg1.Controls.Add((New-Ctl 'Label' 12 40 0 0 $S.Proxy))
$txtProxy = New-Ctl 'TextBox' 90 36 420 24 ''
$txtProxy.Anchor = $AnchorTLR
$tip.SetToolTip($txtProxy, $S.ProxyTip)
$pg1.Controls.Add($txtProxy)
$btnTest = New-Ctl 'Button' 520 34 125 28 $S.TestNet
$btnTest.Anchor = $AnchorTR; $btnTest.Font = $boldFont
$pg1.Controls.Add($btnTest)
$btnRetest = New-Ctl 'Button' 652 34 136 28 $S.Retest
$btnRetest.Anchor = $AnchorTR; $btnRetest.Enabled = $false
$pg1.Controls.Add($btnRetest)
$lblProxyHint = New-Ctl 'Label' 90 64 600 18 ''
$lblProxyHint.AutoSize = $false; $lblProxyHint.ForeColor = $cDim; $lblProxyHint.Anchor = $AnchorTLR
$pg1.Controls.Add($lblProxyHint)

$lv = New-Object System.Windows.Forms.ListView
$lv.Location = New-Object System.Drawing.Point(12, 86)
$lv.Size = New-Object System.Drawing.Size(776, 262)
$lv.Anchor = $AnchorAll
$lv.View = 'Details'; $lv.FullRowSelect = $true; $lv.GridLines = $true; $lv.HeaderStyle = 'Nonclickable'
[void]$lv.Columns.Add($S.ColSource, 250)
[void]$lv.Columns.Add($S.ColNeed, 110)
[void]$lv.Columns.Add($S.ColOfficial, 190)
[void]$lv.Columns.Add($S.ColCn, 200)
$pg1.Controls.Add($lv)

$lblRec = New-Ctl 'Label' 12 356 776 20 $S.NotTested
$lblRec.AutoSize = $false; $lblRec.Anchor = $AnchorBLR; $lblRec.Font = $boldFont
$pg1.Controls.Add($lblRec)

$l = New-Ctl 'Label' 12 390 0 0 $S.Mirror; $l.Anchor = $AnchorBL; $pg1.Controls.Add($l)
$cbMirror = New-Ctl 'ComboBox' 90 386 260 24 ''
$cbMirror.DropDownStyle = 'DropDownList'; $cbMirror.Anchor = $AnchorBL
[void]$cbMirror.Items.Add($S.MirrorAuto); [void]$cbMirror.Items.Add($S.MirrorOfficial); [void]$cbMirror.Items.Add($S.MirrorCn)
$cbMirror.SelectedIndex = 0
$tip.SetToolTip($cbMirror, $S.MirrorTip)
$pg1.Controls.Add($cbMirror)
$MirrorKeys = @('auto', 'official', 'cn')

$chkSkip = New-Ctl 'CheckBox' 12 420 700 22 $S.Skip
$chkSkip.Anchor = $AnchorBL
$pg1.Controls.Add($chkSkip)

$lblNet = New-Ctl 'Label' 12 450 776 40 ''
$lblNet.AutoSize = $false; $lblNet.Anchor = $AnchorBLR; $lblNet.ForeColor = $cDim
$pg1.Controls.Add($lblNet)

# --- page 2: options -------------------------------------------------------
$pg2 = New-Page
$l = New-Ctl 'Label' 12 6 0 0 $S.Intro2; $l.Font = $boldFont; $pg2.Controls.Add($l)
$lblNetSum = New-Ctl 'Label' 12 32 776 20 ''
$lblNetSum.AutoSize = $false; $lblNetSum.Anchor = $AnchorTLR; $lblNetSum.ForeColor = $cCur
$pg2.Controls.Add($lblNetSum)

$hostArch = Get-HostArch
$pg2.Controls.Add((New-Ctl 'Label' 12 64 0 0 $S.Arch))
$cbArch = New-Ctl 'ComboBox' 120 60 160 24 ''
$cbArch.DropDownStyle = 'DropDownList'
[void]$cbArch.Items.Add(($S.AutoArch -f $hostArch)); [void]$cbArch.Items.Add('arm64'); [void]$cbArch.Items.Add('x64')
$cbArch.SelectedIndex = 0
$pg2.Controls.Add($cbArch)

$grpComp = New-Ctl 'GroupBox' 12 96 776 172 $S.GrpComp
$grpComp.Anchor = $AnchorTLR
$pg2.Controls.Add($grpComp)
function New-Check([int]$row, [string]$text) {
    $c = New-Ctl 'CheckBox' 14 (22 + 24 * $row) 750 22 $text
    $c.Anchor = $AnchorTLR
    $grpComp.Controls.Add($c)
    $c
}
$chkCore     = New-Check 0 $S.Core
$chkCore.Checked = $true; $chkCore.Enabled = $false
$chkBrowser  = New-Check 1 $S.Browser
$chkAstGrep  = New-Check 2 $S.AstGrep
$chkApps     = New-Check 3 $S.Apps
$chkFrontend = New-Check 4 $S.Frontend
$chkBuilder  = New-Check 5 $S.Builder
function Update-FrontendLabel {
    $chkFrontend.Text = $S.Frontend + $(if (Test-DistPresent) { $S.FrontendHave } else { $S.FrontendMissing })
}
Update-FrontendLabel

$lblEst = New-Ctl 'Label' 12 278 776 20 ''
$lblEst.AutoSize = $false; $lblEst.Anchor = $AnchorTLR
$pg2.Controls.Add($lblEst)
$pg2.Controls.Add((New-Ctl 'Label' 12 308 0 0 $S.Cmd))
$txtCmd = New-Ctl 'TextBox' 12 328 776 60 ''
$txtCmd.ReadOnly = $true; $txtCmd.Multiline = $true; $txtCmd.WordWrap = $true
$txtCmd.Font = $logFont; $txtCmd.Anchor = $AnchorTLR
$pg2.Controls.Add($txtCmd)

# --- page 3: install -------------------------------------------------------
$pg3 = New-Page
$l = New-Ctl 'Label' 12 6 0 0 $S.Intro3; $l.Font = $boldFont; $pg3.Controls.Add($l)
$btnStop  = New-Ctl 'Button' 12 32 90 28 $S.Stop
$btnLog   = New-Ctl 'Button' 108 32 100 28 $S.OpenLog
$btnStart = New-Ctl 'Button' 214 32 110 28 $S.StartSrv
$btnBackNet = New-Ctl 'Button' 330 32 150 28 $S.BackNet
$btnStop.Enabled = $false; $btnBackNet.Visible = $false
foreach ($b in $btnStop, $btnLog, $btnStart, $btnBackNet) { $pg3.Controls.Add($b) }

$progress = New-Ctl 'ProgressBar' 12 68 776 16 ''
$progress.Anchor = $AnchorTLR
$pg3.Controls.Add($progress)
$lblStatus = New-Ctl 'Label' 12 88 776 20 $S.Ready
$lblStatus.AutoSize = $false; $lblStatus.Anchor = $AnchorTLR
$pg3.Controls.Add($lblStatus)
$txtLog = New-Ctl 'TextBox' 12 110 776 428 ''
$txtLog.Multiline = $true; $txtLog.ReadOnly = $true; $txtLog.ScrollBars = 'Both'; $txtLog.WordWrap = $false
$txtLog.Font = $logFont
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(24, 24, 24)
$txtLog.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 220)
$txtLog.Anchor = $AnchorAll
$pg3.Controls.Add($txtLog)

# --- navigation bar --------------------------------------------------------
$sep = New-Ctl 'Label' 0 594 800 2 ''
$sep.AutoSize = $false; $sep.BorderStyle = 'Fixed3D'; $sep.Anchor = $AnchorBLR
$form.Controls.Add($sep)
$btnBack  = New-Ctl 'Button' 476 604 100 30 $S.Back
$btnNext  = New-Ctl 'Button' 582 604 110 30 $S.Next
$btnClose = New-Ctl 'Button' 698 604 90 30 $S.Close
$btnBack.Anchor = $AnchorBR; $btnNext.Anchor = $AnchorBR; $btnClose.Anchor = $AnchorBR
$btnNext.Font = $boldFont
foreach ($b in $btnBack, $btnNext, $btnClose) { $form.Controls.Add($b) }

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
$script:Page = 1
$script:NetItems = @()       # probe items (PSCustomObject from JSON)
$script:NetTested = $false
$script:NetRec = ''
$script:NetProxy = ''        # proxy text the last test ran with
$script:Applying = $false

function Get-MirrorKey { $MirrorKeys[[Math]::Max(0, $cbMirror.SelectedIndex)] }

function Get-UiOptions {
    $o = New-Options
    $o.Arch     = if ($cbArch.SelectedIndex -gt 0) { [string]$cbArch.SelectedItem } else { '' }
    $o.Mirror   = Get-MirrorKey
    $o.Proxy    = $txtProxy.Text.Trim()
    # An explicit source was decided on page 1 -> the scripts need not probe again.
    $o.NoProbe  = ($o.Mirror -ne 'auto') -or [bool]$script:PresetNoProbe
    $o.Browser  = $chkBrowser.Checked
    $o.AstGrep  = $chkAstGrep.Checked
    $o.Apps     = $chkApps.Checked
    $o.Frontend = $chkFrontend.Checked
    $o.Builder  = $chkBuilder.Checked
    $o
}

function Set-UiOptions($o) {
    $script:Applying = $true
    if ($o.Arch -eq 'arm64') { $cbArch.SelectedIndex = 1 } elseif ($o.Arch -eq 'x64') { $cbArch.SelectedIndex = 2 } else { $cbArch.SelectedIndex = 0 }
    $i = [Array]::IndexOf($MirrorKeys, [string]$o.Mirror); if ($i -ge 0) { $cbMirror.SelectedIndex = $i }
    $txtProxy.Text       = [string]$o.Proxy
    $script:PresetNoProbe = [bool]$o.NoProbe
    $chkBrowser.Checked  = [bool]$o.Browser
    $chkAstGrep.Checked  = [bool]$o.AstGrep
    $chkApps.Checked     = [bool]$o.Apps
    $chkFrontend.Checked = [bool]$o.Frontend
    $chkBuilder.Checked  = [bool]$o.Builder
    $script:Applying = $false
}

function Update-Preview {
    $o = Get-UiOptions
    $txtCmd.Text = (Get-RunCommand $o)[1]
    $lblEst.Text = $S.Estimate -f (Format-MB (Get-EstimateMB $o))
    $mirrorText = [string]$cbMirror.SelectedItem
    $px = if ($o.Proxy) { $o.Proxy } else { $S.ProxyAuto }
    $lblNetSum.Text = $S.NetSummary -f $mirrorText, $px
}

function Get-VenvPython {
    $arch = if ($cbArch.SelectedIndex -gt 0) { [string]$cbArch.SelectedItem } else { Get-HostArch }
    Join-Path $env:LOCALAPPDATA ("QAIModelBuilder\envs\.venv_{0}_313\Scripts\python.exe" -f $arch)
}

# Detected proxy (same order as net_config.ps1) shown as a hint under the box.
function Get-DetectedProxy {
    foreach ($n in 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY') {
        $v = [Environment]::GetEnvironmentVariable($n); if ($v) { return "$v (env $n)" }
    }
    try {
        $u = [Net.WebRequest]::GetSystemWebProxy().GetProxy([Uri]'https://github.com')
        if ($u -and $u.Host -ne 'github.com') { return $u.AbsoluteUri.TrimEnd('/') + ' (system proxy)' }
    } catch { }
    return ''
}
$det = Get-DetectedProxy
$lblProxyHint.Text = if ($det) { $S.ProxyDetected -f $det } else { $S.ProxyTip }

# ---------------------------------------------------------------------------
# Page switching
# ---------------------------------------------------------------------------
function Update-Nav {
    $busy = Test-Running
    switch ($script:Page) {
        1 {
            $btnBack.Visible = $false
            $btnNext.Visible = $true; $btnNext.Text = $S.Next
            $btnNext.Enabled = (-not $busy) -and ($script:NetTested -or $chkSkip.Checked)
        }
        2 {
            $btnBack.Visible = $true; $btnBack.Enabled = $true
            $btnNext.Visible = $true; $btnNext.Text = $S.Install; $btnNext.Enabled = $true
        }
        3 {
            $btnBack.Visible = $true; $btnBack.Enabled = -not $busy
            $btnNext.Visible = $false
        }
    }
    foreach ($c in $txtProxy, $btnTest, $cbMirror, $chkSkip) { $c.Enabled = -not $busy }
    $btnRetest.Enabled = (-not $busy) -and $script:NetTested -and (@(Get-FailedIds).Count -gt 0)
    $btnStop.Enabled = $busy -and $script:Kind -eq 'setup'
    $btnStart.Enabled = (-not $busy) -and (Test-Path -LiteralPath (Get-VenvPython))
}

function Set-Page([int]$n) {
    $script:Page = $n
    $pg1.Visible = ($n -eq 1); $pg2.Visible = ($n -eq 2); $pg3.Visible = ($n -eq 3)
    $pg1.Tag = ($n -eq 1); $pg2.Tag = ($n -eq 2); $pg3.Tag = ($n -eq 3)
    for ($i = 0; $i -lt 3; $i++) {
        if ($i + 1 -eq $n) { $stepLbls[$i].ForeColor = $cCur }
        elseif ($i + 1 -lt $n) { $stepLbls[$i].ForeColor = $cOk }
        else { $stepLbls[$i].ForeColor = $cDim }
    }
    if ($n -eq 2) { Update-FrontendLabel; Update-Preview }
    if ($n -eq 3) { $btnBackNet.Visible = $false }
    Update-Nav
}

# ---------------------------------------------------------------------------
# Network test results
# ---------------------------------------------------------------------------
function Get-NeedText([string]$need) {
    switch ($need) {
        'core' { $S.NeedCore } 'arm64' { $S.NeedArm64 } 'browser' { $S.NeedBrowser }
        'frontend' { $S.NeedFrontend } 'builder' { $S.NeedBuilder } default { $need }
    }
}

function Get-FailedIds {
    $ids = @()
    foreach ($i in $script:NetItems) {
        if (($i.official -and -not $i.official.ok) -or ($i.cn -and -not $i.cn.ok)) { $ids += $i.id }
    }
    , $ids
}

function Show-NetItems {
    $lv.BeginUpdate()
    $lv.Items.Clear()
    foreach ($i in $script:NetItems) {
        $it = New-Object System.Windows.Forms.ListViewItem([string]$i.name)
        $it.UseItemStyleForSubItems = $false
        [void]$it.SubItems.Add((Get-NeedText $i.need))
        foreach ($side in 'official', 'cn') {
            $x = $i.$side
            if ($null -eq $x) { $sub = $it.SubItems.Add('-'); $sub.ForeColor = $cDim }
            elseif ($x.ok)    { $sub = $it.SubItems.Add(($S.CellOk -f $x.ms)); $sub.ForeColor = $cOk }
            else              { $sub = $it.SubItems.Add(($S.CellFail -f $x.note)); $sub.ForeColor = $cFail }
        }
        [void]$lv.Items.Add($it)
    }
    $lv.EndUpdate()
}

function Import-NetResult([bool]$merge) {
    $p = Join-Path $Root $NetJson
    if (-not (Test-Path -LiteralPath $p)) { return $false }
    try { $doc = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8) | ConvertFrom-Json } catch { return $false }
    $new = @($doc.items)
    if ($merge -and $script:NetItems.Count -gt 0) {
        $byId = @{}; foreach ($n in $new) { $byId[$n.id] = $n }
        $script:NetItems = @($script:NetItems | ForEach-Object { if ($byId.ContainsKey($_.id)) { $byId[$_.id] } else { $_ } })
    } else {
        $script:NetItems = $new
    }
    $script:NetTested = $true
    $script:NetProxy = $txtProxy.Text.Trim()
    Show-NetItems
    $rec = Get-NetRecommend $script:NetItems
    $script:NetRec = $rec
    switch ($rec) {
        'official' { $lblRec.Text = $S.RecOfficial; $lblRec.ForeColor = $cOk;  $cbMirror.SelectedIndex = 1 }
        'cnslow'   { $lblRec.Text = $S.RecCnSlow;   $lblRec.ForeColor = $cOk;  $cbMirror.SelectedIndex = 2 }
        'cn'       { $lblRec.Text = $S.RecCn;       $lblRec.ForeColor = $cOk;  $cbMirror.SelectedIndex = 2 }
        default    { $lblRec.Text = $S.RecBlocked;  $lblRec.ForeColor = $cFail; $txtProxy.Focus() | Out-Null }
    }
    $lblNet.Text = $S.LogAt -f $script:LogPath
    return $true
}

# ---------------------------------------------------------------------------
# Job runner: cmd.exe child writes to a log file, a UI timer tails it.
# (Process output events would need a free PowerShell pipeline, which
#  ShowDialog() blocks - tailing a file avoids all cross-thread issues.)
# ---------------------------------------------------------------------------
$script:Proc = $null
$script:Kind = ''
$script:NetMerge = $false
$script:LogPath = ''
$script:LogPos = 0
$script:Partial = ''
$script:Offset = 0
$script:StepText = ''
$script:DlText = ''
$script:Totals = @()
$script:Slowest = @()
$script:UserStopped = $false
$script:ExitCode = 0
$script:Watch = New-Object System.Diagnostics.Stopwatch
$oemEnc = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)

function Test-Running { return ($null -ne $script:Proc -and -not $script:Proc.HasExited) }

function Start-Run([string]$cmdline, [string]$kind, [int]$total) {
    $logDir = Join-Path $Root 'data\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data\downloads') | Out-Null
    $script:LogPath = Join-Path $logDir ('setup_gui_{0}_{1}.log' -f $kind, (Get-Date -Format 'yyyyMMdd_HHmmss'))
    [IO.File]::WriteAllText($script:LogPath, '')
    $script:LogPos = 0; $script:Partial = ''; $script:Offset = 0
    $script:StepText = ''; $script:DlText = ''; $script:Totals = @(); $script:Slowest = @()
    $script:UserStopped = $false; $script:Kind = $kind

    if ($kind -eq 'setup') {
        $txtLog.Clear()
        $txtLog.AppendText('> ' + $cmdline.Replace('call ', '') + "`r`n`r`n")
        $progress.Style = 'Continuous'; $progress.Minimum = 0; $progress.Maximum = $total; $progress.Value = 0
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $env:ComSpec
    # /s: strip only the outer quotes; ( ... ) groups the && chain so the redirect covers all of it.
    $psi.Arguments = '/d /s /c "(' + $cmdline + ') > "' + $script:LogPath + '" 2>&1 < nul"'
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $script:Watch.Reset(); $script:Watch.Start()
    $script:Proc = [System.Diagnostics.Process]::Start($psi)
    Update-Nav
    $timer.Start()
}

function Format-Elapsed { '{0:mm\:ss}' -f $script:Watch.Elapsed }

function Add-LogLine([System.Text.StringBuilder]$sb, [string]$line) {
    $l = $line -replace "\x1b\[[0-9;?]*[A-Za-z]", ''
    if ($l.Trim().Length -eq 0) { return }
    # Download progress bars are \r-refreshed: show them in the status line only.
    if ($l -match '^\[(DL|DL-PS)\]') { $script:DlText = $l.Trim(); return }
    $script:DlText = ''
    if ($l -match 'QAI ModelBuilder\s+-\s+build_frontend') {
        $script:Offset = 7 + $(if ($chkBuilder.Checked) { 1 } else { 0 })
    }
    if ($l -match '^-- Step (\d+)/(\d+):\s*(.+?)\s*-*\s*$') {
        $v = $script:Offset + [int]$matches[1] - 1
        if ($v -le $progress.Maximum) { $progress.Value = [Math]::Max($progress.Value, $v) }
        $script:StepText = $matches[3]
    } elseif ($l -match '^-- Builder:') {
        $progress.Value = [Math]::Min($progress.Maximum, [Math]::Max($progress.Value, 7))
        $script:StepText = 'QAIRT model builder'
    } elseif ($l -match '^\s*(\S.*?)\s+TOTAL\s*$') {
        $script:Totals += $matches[1].Trim()
    } elseif ($l -match '^\s*slowest:\s*(.+)$') {
        $script:Slowest += ($matches[1].Trim() -replace '\s{2,}', '  ')
    }
    [void]$sb.Append($l).Append("`r`n")
}

function Read-Log {
    if ($script:Kind -ne 'setup') { return }
    if (-not $script:LogPath -or -not (Test-Path -LiteralPath $script:LogPath)) { return }
    $fs = $null
    try {
        $fs = [IO.File]::Open($script:LogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete')
        if ($fs.Length -lt $script:LogPos) { $script:LogPos = 0 }
        $n = [int]($fs.Length - $script:LogPos)
        if ($n -le 0) { return }
        [void]$fs.Seek($script:LogPos, [IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] $n
        $r = $fs.Read($buf, 0, $n)
        $script:LogPos += $r
    } catch { return } finally { if ($fs) { $fs.Dispose() } }
    # cmd's own echo uses the OEM code page, uv / pip / vite write UTF-8: prefer
    # UTF-8, fall back to OEM for a chunk that is not valid UTF-8.
    $text = [Text.Encoding]::UTF8.GetString($buf, 0, $r)
    if ($text.IndexOf([char]0xFFFD) -ge 0) { $text = $oemEnc.GetString($buf, 0, $r) }
    $text = $script:Partial + $text
    $parts = [regex]::Split($text, "\r\n|\n|\r")
    $script:Partial = $parts[$parts.Length - 1]
    $sb = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $parts.Length - 1; $i++) { Add-LogLine $sb $parts[$i] }
    if ($sb.Length -gt 0) {
        if ($txtLog.TextLength -gt 600000) { $txtLog.Text = $txtLog.Text.Substring($txtLog.TextLength - 300000) }
        $txtLog.AppendText($sb.ToString())
    }
}

function Complete-Run([int]$code) {
    $timer.Stop()
    $script:Watch.Stop()
    $script:Proc = $null
    $script:ExitCode = $code
    if ($script:Kind -eq 'net') {
        if ($code -ne 0 -or -not (Import-NetResult $script:NetMerge)) {
            $lblNet.Text = $S.NetFailed -f $code, $script:LogPath
            if ($code -eq 0) { $script:ExitCode = 1 }
        }
        Update-Nav
        if ($AutoClose) { $script:Closing = $true; $form.Close() }
        return
    }
    if ($script:Partial) { $sb = New-Object System.Text.StringBuilder; Add-LogLine $sb $script:Partial; $txtLog.AppendText($sb.ToString()); $script:Partial = '' }
    $el = $S.Elapsed -f (Format-Elapsed)
    if ($script:UserStopped) {
        $lblStatus.Text = $S.Stopped + '  ' + $el
        $btnBackNet.Visible = $true
    } elseif ($code -eq 0) {
        $progress.Value = $progress.Maximum
        $msg = $S.Done + '  ' + $el
        if ($script:Slowest.Count -gt 0) { $msg += '  |  ' + ($S.SlowestIs -f ($script:Slowest -join ' ; ')) }
        $lblStatus.Text = $msg
        $stepLbls[2].ForeColor = $cOk
    } else {
        $lblStatus.Text = ($S.Failed -f $code) + '  ' + $el
        $btnBackNet.Visible = $true
    }
    $txtLog.AppendText("`r`n" + ($S.LogAt -f $script:LogPath) + "`r`n")
    Update-FrontendLabel
    Update-Nav
    if ($AutoClose) { $script:Closing = $true; $form.Close() }
}

function Stop-Run {
    if (Test-Running) {
        $script:UserStopped = $true
        & "$env:SystemRoot\System32\taskkill.exe" /PID $script:Proc.Id /T /F 2>&1 | Out-Null
    }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300
$timer.Add_Tick({
    Read-Log
    if ($null -ne $script:Proc) {
        if ($script:Proc.HasExited) {
            Read-Log
            Complete-Run $script:Proc.ExitCode
            return
        }
        if ($script:Kind -eq 'net') {
            $lblNet.Text = ($S.Testing -f 15) + '  ' + (Format-Elapsed)
        } else {
            $t = $S.Running + '  ' + (Format-Elapsed)
            if ($script:StepText) { $t += '  |  ' + $script:StepText }
            if ($script:DlText)   { $t += '  |  ' + $script:DlText }
            $lblStatus.Text = $t
        }
    }
})

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
function Confirm-Proxy {
    $p = $txtProxy.Text.Trim()
    if ($p -and -not (Test-ProxyOk $p)) {
        if (-not $SelfTest) { [void][System.Windows.Forms.MessageBox]::Show($S.ProxyBad, $S.Title, 'OK', 'Error') }
        $txtProxy.Focus() | Out-Null
        return $false
    }
    return $true
}

function Start-NetTest([bool]$failedOnly) {
    if (-not (Confirm-Proxy)) { return }
    $only = ''
    if ($failedOnly) { $only = (Get-FailedIds) -join ',' ; if (-not $only) { return } }
    $script:NetMerge = $failedOnly
    if (-not $failedOnly) { $lv.Items.Clear(); $lblRec.Text = ''; }
    $lblNet.Text = $S.Testing -f 15
    Start-Run (Get-NetCommand $txtProxy.Text.Trim() $only) 'net' 1
}

$btnTest.Add_Click({ Start-NetTest $false })
$btnRetest.Add_Click({ Start-NetTest $true })
$chkSkip.Add_CheckedChanged({ Update-Nav })
$txtProxy.Add_TextChanged({
    if ($script:Applying) { return }
    if ($script:NetTested -and $txtProxy.Text.Trim() -ne $script:NetProxy) {
        $script:NetTested = $false
        $lblRec.Text = $S.ProxyChanged; $lblRec.ForeColor = $cFail
    }
    Update-Nav
})

$onChange = { if (-not $script:Applying) { Update-Preview } }
foreach ($c in $chkBrowser, $chkAstGrep, $chkApps, $chkFrontend) { $c.Add_CheckedChanged($onChange) }
$cbMirror.Add_SelectedIndexChanged($onChange)
$cbArch.Add_SelectedIndexChanged({ if (-not $script:Applying) { Update-Preview; Update-Nav } })
$chkBuilder.Add_CheckedChanged({
    if (-not $script:Applying -and $chkBuilder.Checked -and -not $AutoRun -and -not $SelfTest) {
        $r = [System.Windows.Forms.MessageBox]::Show($S.BuilderWarn, $S.Title, 'OKCancel', 'Warning')
        if ($r -ne 'OK') { $chkBuilder.Checked = $false }
    }
    if (-not $script:Applying) { Update-Preview }
})

function Start-Install {
    $o = Get-UiOptions
    Set-Page 3
    Start-Run (Get-RunCommand $o)[0] 'setup' (Get-TotalSteps $o)
}

function Invoke-Next {
    if ($script:Page -eq 1) {
        if (-not (Confirm-Proxy)) { return }
        if ($script:NetTested -and $script:NetRec -eq 'blocked' -and -not $chkSkip.Checked -and -not $SelfTest) {
            $r = [System.Windows.Forms.MessageBox]::Show($S.BlockedConfirm, $S.Title, 'YesNo', 'Warning')
            if ($r -ne 'Yes') { return }
        }
        Set-Page 2
    } elseif ($script:Page -eq 2) {
        Start-Install
    }
}
function Invoke-Back { if ($script:Page -gt 1 -and -not (Test-Running)) { Set-Page ($script:Page - 1) } }
$btnNext.Add_Click({ Invoke-Next })
$btnBack.Add_Click({ Invoke-Back })
$btnBackNet.Add_Click({ if (-not (Test-Running)) { Set-Page 1 } })
$btnStop.Add_Click({ Stop-Run })
$btnLog.Add_Click({
    if ($script:LogPath -and (Test-Path -LiteralPath $script:LogPath)) { Start-Process notepad.exe -ArgumentList ('"' + $script:LogPath + '"') }
    else {
        $d = Join-Path $Root 'data\logs'
        if (Test-Path -LiteralPath $d) { Start-Process explorer.exe -ArgumentList ('"' + $d + '"') }
    }
})
$btnStart.Add_Click({
    Start-Process -FilePath $env:ComSpec -ArgumentList '/k', 'Start.bat' -WorkingDirectory $Root
})
$btnClose.Add_Click({ $form.Close() })

$form.Add_FormClosing({
    param($sender, $e)
    if (Test-Running) {
        if (-not $script:Closing) {
            $r = [System.Windows.Forms.MessageBox]::Show($S.ConfirmStop, $S.Title, 'YesNo', 'Warning')
            if ($r -ne 'Yes') { $e.Cancel = $true; return }
        }
        Stop-Run
    }
    $timer.Stop()
})

$form.Add_Shown({
    $form.Activate()
    if ($AutoRun) { Start-Install }
    elseif ($AutoNet) { Start-NetTest $false }
})

# ---------------------------------------------------------------------------
Set-UiOptions $(if ($Preset) { ConvertFrom-Preset $Preset } else { New-Options })
Set-Page 1

if ($SelfTest) {
    # Headless walk-through of the wizard logic (no ShowDialog, no child process).
    $fails = 0
    function Check([string]$name, [bool]$cond) {
        if ($cond) { Write-Output "[PASS] $name" } else { Write-Output "[FAIL] $name"; $script:fails++ }
    }
    # (The form is never shown here, so Control.Visible reads false: check the
    #  page state and the pages' intended visibility flags instead.)
    function PageIs([int]$n) { ($script:Page -eq $n) -and ($pg1.Tag -eq ($n -eq 1)) -and ($pg2.Tag -eq ($n -eq 2)) -and ($pg3.Tag -eq ($n -eq 3)) }
    Check 'page1 visible at start' (PageIs 1)
    Check 'next disabled before test/skip' (-not $btnNext.Enabled)
    $chkSkip.Checked = $true
    Check 'next enabled after skip' $btnNext.Enabled
    $chkSkip.Checked = $false
    # Fake a probe result: official blocked, cn ok -> recommend cn.
    $fake = @{ items = @(
        @{ id = 'pypi'; name = 'PyPI'; need = 'core'; official = @{ ok = $false; ms = 8000; note = 'Timeout' }; cn = @{ ok = $true; ms = 300; note = 'ok' } },
        @{ id = 'pyfiles'; name = 'files'; need = 'core'; official = @{ ok = $false; ms = 8000; note = 'Timeout' }; cn = $null },
        @{ id = 'python'; name = 'Python'; need = 'core'; official = @{ ok = $false; ms = 8000; note = 'Timeout' }; cn = @{ ok = $true; ms = 400; note = 'ok' } },
        @{ id = 'github'; name = 'GitHub'; need = 'core'; official = @{ ok = $false; ms = 8000; note = 'Timeout' }; cn = @{ ok = $true; ms = 500; note = 'ok' } }
    ) }
    $jp = Join-Path $Root $NetJson
    $bak = if (Test-Path -LiteralPath $jp) { [IO.File]::ReadAllText($jp) } else { $null }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $jp) | Out-Null
    [IO.File]::WriteAllText($jp, ($fake | ConvertTo-Json -Depth 6))
    $ok = Import-NetResult $false
    Update-Nav
    Check 'probe json imported' $ok
    Check 'recommend cn when official blocked' ($script:NetRec -eq 'cn' -and (Get-MirrorKey) -eq 'cn')
    Check 'table filled (4 rows)' ($lv.Items.Count -eq 4)
    Check 'retest enabled with failures' $btnRetest.Enabled
    Check 'next enabled after test' $btnNext.Enabled
    $txtProxy.Text = 'http://127.0.0.1:7890'
    Check 'proxy change invalidates test' (-not $script:NetTested -and -not $btnNext.Enabled)
    [void](Import-NetResult $false); Update-Nav
    Invoke-Next
    Check 'page2 after next' (PageIs 2)
    $o = Get-UiOptions
    $cmd = (Get-RunCommand $o)[1]
    Check 'command has --mirror cn --proxy --no-probe' ($cmd -match '--mirror cn' -and $cmd -match '--proxy http://127\.0\.0\.1:7890' -and $cmd -match '--no-probe')
    Check 'preview shows command' ($txtCmd.Text -eq $cmd)
    Check 'estimate label set' ($lblEst.Text.Length -gt 0)
    $cbMirror.SelectedIndex = 0
    $cmd2 = (Get-RunCommand (Get-UiOptions))[1]
    Check 'auto mirror -> scripts probe themselves' ($cmd2 -notmatch '--no-probe' -and $cmd2 -notmatch '--mirror')
    Invoke-Back
    Check 'back to page1' (PageIs 1)
    $txtProxy.Text = 'bad proxy'
    Invoke-Next
    Check 'invalid proxy blocks next' ($script:Page -eq 1)
    if ($null -ne $bak) { [IO.File]::WriteAllText($jp, $bak) } else { Remove-Item -LiteralPath $jp -Force -ErrorAction SilentlyContinue }
    $form.Dispose()
    Write-Output "selftest failures=$fails"
    exit $fails
}

[void]$form.ShowDialog()
$form.Dispose()
exit $script:ExitCode
