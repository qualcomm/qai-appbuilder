# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    Small Windows GUI around setup_env.bat / build_frontend.bat.

.DESCRIPTION
    Lets the user pick the optional components, proxy and mirror, shows the
    exact command that will run, streams its log with a step progress bar, and
    offers "Start server" when done. All real work is still done by the .bat
    scripts; this window only builds their command line and tails the log.

    Uses Windows PowerShell 5.1 + WinForms (present on every Windows 10/11,
    x64 and ARM64) because no Python exists yet when setup runs.

    The command runs as:  cmd /c "(call setup_env.bat ... [&& call build_frontend.bat ...]) > log 2>&1 < nul"
    Log file: data\logs\setup_gui_<kind>_<timestamp>.log

.PARAMETER Preset
    Test hook / pre-selection. Comma list of: browser, astgrep, apps, frontend,
    nofrontend, builder, noprobe, mirror=<auto|official|cn>, arch=<arm64|x64>,
    proxy=<url>.
.PARAMETER AutoRun
    Test hook: press "Install" as soon as the window is shown.
.PARAMETER AutoClose
    Test hook: close the window when the job ends; the script exit code is the
    job exit code.
.PARAMETER PrintCommand
    Test hook: print the command for -Preset and exit without a window.
#>
param(
    [string]$Preset = '',
    [switch]$AutoRun,
    [switch]$AutoClose,
    [switch]$PrintCommand
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
    GrpGeneral = 'General'; Arch = 'Architecture'; Mirror = 'Mirror'; Proxy = 'Proxy (optional)'
    ProxyTip = 'Leave empty to use the HTTPS_PROXY environment variable, then the Windows system proxy. Format: http://host:port'
    MirrorTip = 'auto: switch to China mirrors only when the official source is unreachable; official: official sources only; cn: always use China mirrors'
    NoProbe = 'Skip connectivity probe'; TestNet = 'Test network'
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
    Cmd = 'Command to run'
    Install = 'Install'; Stop = 'Stop'; OpenLog = 'Open log'; StartSrv = 'Start server'; Close = 'Close'
    Ready = 'Ready. Choose the components, then click "Install".'
    Running = 'Running'; NetRunning = 'Testing network'
    Done = 'Finished successfully.'; NetDone = 'Network test finished.'
    Failed = 'Failed (exit code {0}). See the log.'; Stopped = 'Stopped by user.'
    ConfirmStop = 'A job is still running. Stop it and close?'
    ProxyBad = 'The proxy must look like http://host:port (no spaces, quotes or % & | < > ^ ( ) characters).'
    AutoArch = 'auto ({0})'; Elapsed = 'elapsed {0}'; LogAt = 'Full log: {0}'
    SlowestIs = 'slowest step: {0}'
}
$S_zh = @{
    Title = 'QAI ModelBuilder 安装'
    GrpGeneral = '基本设置'; Arch = '架构'; Mirror = '镜像源'; Proxy = '代理(可选)'
    ProxyTip = '留空则依次使用 HTTPS_PROXY 环境变量、Windows 系统代理。格式:http://host:port'
    MirrorTip = 'auto:官方源不通时自动切换国内镜像;official:只用官方源;cn:强制使用国内镜像'
    NoProbe = '跳过网络检测'; TestNet = '检测网络'
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
    Cmd = '将执行的命令'
    Install = '开始安装'; Stop = '停止'; OpenLog = '打开日志'; StartSrv = '启动服务'; Close = '关闭'
    Ready = '就绪。选择组件后点击“开始安装”。'
    Running = '正在执行'; NetRunning = '正在检测网络'
    Done = '安装完成。'; NetDone = '网络检测完成。'
    Failed = '执行失败(退出码 {0}),请查看日志。'; Stopped = '已被用户停止。'
    ConfirmStop = '任务仍在执行，确定要停止并关闭吗?'
    ProxyBad = '代理格式应为 http://host:port(不能包含空格、引号及 % & | < > ^ ( ) 等字符)。'
    AutoArch = '自动({0})'; Elapsed = '用时 {0}'; LogAt = '完整日志:{0}'
    SlowestIs = '最慢步骤:{0}'
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

function Get-NetCommand($o) {
    $need = 'core'
    if ($o.Browser)  { $need += ',browser' }
    if ($o.Builder)  { $need += ',builder' }
    if ($o.Frontend) { $need += ',frontend' }
    $mirror = if ($o.Mirror) { $o.Mirror } else { 'auto' }
    $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup\net_config.ps1 -Mirror ' + $mirror + ' -Need ' + $need + ' -Emit "data\downloads\_net_env_gui.bat"'
    if ($o.Proxy) { $cmd += ' -Proxy "' + $o.Proxy + '"' }
    $cmd
}

function Get-TotalSteps($o) {
    $n = 7
    if ($o.Builder)  { $n += 1 }
    if ($o.Frontend) { $n += 4 }
    $n
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

$uiFont  = if ($lang -eq 'zh') { New-Object System.Drawing.Font('Microsoft YaHei UI', 9) } else { New-Object System.Drawing.Font('Segoe UI', 9) }
$logFont = New-Object System.Drawing.Font('Consolas', 9)

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

$form = New-Object System.Windows.Forms.Form
$form.Text = $S.Title
$form.Font = $uiFont
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object System.Drawing.Size(780, 660)
$form.MinimumSize = New-Object System.Drawing.Size(720, 560)

$tip = New-Object System.Windows.Forms.ToolTip
$tip.AutoPopDelay = 15000

# --- General group --------------------------------------------------------
$grpGen = New-Ctl 'GroupBox' 10 8 760 92 $S.GrpGeneral
$grpGen.Anchor = $AnchorTLR
$form.Controls.Add($grpGen)

$hostArch = Get-HostArch
$grpGen.Controls.Add((New-Ctl 'Label' 12 27 0 0 $S.Arch))
$cbArch = New-Ctl 'ComboBox' 120 23 150 24 ''
$cbArch.DropDownStyle = 'DropDownList'
[void]$cbArch.Items.Add(($S.AutoArch -f $hostArch)); [void]$cbArch.Items.Add('arm64'); [void]$cbArch.Items.Add('x64')
$cbArch.SelectedIndex = 0
$grpGen.Controls.Add($cbArch)

$grpGen.Controls.Add((New-Ctl 'Label' 300 27 0 0 $S.Mirror))
$cbMirror = New-Ctl 'ComboBox' 380 23 110 24 ''
$cbMirror.DropDownStyle = 'DropDownList'
foreach ($m in 'auto', 'official', 'cn') { [void]$cbMirror.Items.Add($m) }
$cbMirror.SelectedIndex = 0
$tip.SetToolTip($cbMirror, $S.MirrorTip)
$grpGen.Controls.Add($cbMirror)

$chkNoProbe = New-Ctl 'CheckBox' 515 24 230 22 $S.NoProbe
$grpGen.Controls.Add($chkNoProbe)

$grpGen.Controls.Add((New-Ctl 'Label' 12 60 0 0 $S.Proxy))
$txtProxy = New-Ctl 'TextBox' 120 56 495 24 ''
$txtProxy.Anchor = $AnchorTLR
$tip.SetToolTip($txtProxy, $S.ProxyTip)
$grpGen.Controls.Add($txtProxy)

$btnNet = New-Ctl 'Button' 630 55 118 26 $S.TestNet
$btnNet.Anchor = $AnchorTR
$grpGen.Controls.Add($btnNet)

# --- Components group ----------------------------------------------------
$grpComp = New-Ctl 'GroupBox' 10 106 760 166 $S.GrpComp
$grpComp.Anchor = $AnchorTLR
$form.Controls.Add($grpComp)

function New-Check([int]$row, [string]$text) {
    $c = New-Ctl 'CheckBox' 14 (22 + 23 * $row) 735 22 $text
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

# --- Command preview / buttons / progress / log ---------------------------
$form.Controls.Add((New-Ctl 'Label' 12 280 0 0 $S.Cmd))
$txtCmd = New-Ctl 'TextBox' 10 299 760 24 ''
$txtCmd.ReadOnly = $true
$txtCmd.Font = $logFont
$txtCmd.Anchor = $AnchorTLR
$form.Controls.Add($txtCmd)

$btnInstall = New-Ctl 'Button' 10 332 110 30 $S.Install
$btnStop    = New-Ctl 'Button' 126 332 90 30 $S.Stop
$btnLog     = New-Ctl 'Button' 222 332 100 30 $S.OpenLog
$btnStart   = New-Ctl 'Button' 328 332 110 30 $S.StartSrv
$btnClose   = New-Ctl 'Button' 680 332 90 30 $S.Close
$btnClose.Anchor = $AnchorTR
$btnStop.Enabled = $false
foreach ($b in $btnInstall, $btnStop, $btnLog, $btnStart, $btnClose) { $form.Controls.Add($b) }
$btnInstall.Font = New-Object System.Drawing.Font($uiFont, [System.Drawing.FontStyle]::Bold)

$progress = New-Ctl 'ProgressBar' 10 370 760 16 ''
$progress.Anchor = $AnchorTLR
$form.Controls.Add($progress)

$lblStatus = New-Ctl 'Label' 10 390 760 20 $S.Ready
$lblStatus.AutoSize = $false
$lblStatus.Anchor = $AnchorTLR
$form.Controls.Add($lblStatus)

$txtLog = New-Ctl 'TextBox' 10 412 760 238 ''
$txtLog.Multiline = $true
$txtLog.ReadOnly = $true
$txtLog.ScrollBars = 'Both'
$txtLog.WordWrap = $false
$txtLog.Font = $logFont
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(24, 24, 24)
$txtLog.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 220)
$txtLog.Anchor = $AnchorAll
$form.Controls.Add($txtLog)

# ---------------------------------------------------------------------------
# UI <-> options
# ---------------------------------------------------------------------------
function Get-UiOptions {
    $o = New-Options
    $o.Arch     = if ($cbArch.SelectedIndex -gt 0) { [string]$cbArch.SelectedItem } else { '' }
    $o.Mirror   = [string]$cbMirror.SelectedItem
    $o.Proxy    = $txtProxy.Text.Trim()
    $o.NoProbe  = $chkNoProbe.Checked
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
    $i = $cbMirror.Items.IndexOf([string]$o.Mirror); if ($i -ge 0) { $cbMirror.SelectedIndex = $i }
    $txtProxy.Text      = [string]$o.Proxy
    $chkNoProbe.Checked = [bool]$o.NoProbe
    $chkBrowser.Checked = [bool]$o.Browser
    $chkAstGrep.Checked = [bool]$o.AstGrep
    $chkApps.Checked    = [bool]$o.Apps
    $chkFrontend.Checked = [bool]$o.Frontend
    $chkBuilder.Checked = [bool]$o.Builder
    $script:Applying = $false
}

function Update-Preview {
    $txtCmd.Text = (Get-RunCommand (Get-UiOptions))[1]
}

function Get-VenvPython {
    $arch = if ($cbArch.SelectedIndex -gt 0) { [string]$cbArch.SelectedItem } else { Get-HostArch }
    Join-Path $env:LOCALAPPDATA ("QAIModelBuilder\envs\.venv_{0}_313\Scripts\python.exe" -f $arch)
}

function Update-StartButton {
    $btnStart.Enabled = (-not (Test-Running)) -and (Test-Path -LiteralPath (Get-VenvPython))
}

# ---------------------------------------------------------------------------
# Job runner: cmd.exe child writes to a log file, a UI timer tails it.
# (Process output events would need a free PowerShell pipeline, which
#  ShowDialog() blocks - tailing a file avoids all cross-thread issues.)
# ---------------------------------------------------------------------------
$script:Proc = $null
$script:Kind = ''
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

function Set-Busy([bool]$busy) {
    foreach ($c in $cbArch, $cbMirror, $txtProxy, $chkNoProbe, $chkBrowser, $chkAstGrep, $chkApps, $chkFrontend, $chkBuilder, $btnInstall, $btnNet) { $c.Enabled = -not $busy }
    $btnStop.Enabled = $busy
    Update-StartButton
}

function Start-Run([string]$cmdline, [string]$kind, [int]$total) {
    $logDir = Join-Path $Root 'data\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data\downloads') | Out-Null
    $script:LogPath = Join-Path $logDir ('setup_gui_{0}_{1}.log' -f $kind, (Get-Date -Format 'yyyyMMdd_HHmmss'))
    [IO.File]::WriteAllText($script:LogPath, '')
    $script:LogPos = 0; $script:Partial = ''; $script:Offset = 0
    $script:StepText = ''; $script:DlText = ''; $script:Totals = @(); $script:Slowest = @()
    $script:UserStopped = $false; $script:Kind = $kind

    $txtLog.Clear()
    $txtLog.AppendText('> ' + $cmdline.Replace('call ', '') + "`r`n`r`n")
    if ($kind -eq 'net') {
        $progress.Style = 'Marquee'
    } else {
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
    Set-Busy $true
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
    if ($script:Kind -eq 'setup') {
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
    }
    [void]$sb.Append($l).Append("`r`n")
}

function Read-Log {
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
    if ($script:Partial) { $sb = New-Object System.Text.StringBuilder; Add-LogLine $sb $script:Partial; $txtLog.AppendText($sb.ToString()); $script:Partial = '' }
    $script:Proc = $null
    $script:ExitCode = $code
    $progress.Style = 'Continuous'
    $el = $S.Elapsed -f (Format-Elapsed)
    if ($script:UserStopped) {
        $lblStatus.Text = $S.Stopped + '  ' + $el
    } elseif ($code -eq 0) {
        if ($script:Kind -eq 'net') {
            $lblStatus.Text = $S.NetDone
        } else {
            $progress.Value = $progress.Maximum
            $msg = $S.Done + '  ' + $el
            if ($script:Slowest.Count -gt 0) { $msg += '  |  ' + ($S.SlowestIs -f ($script:Slowest -join ' ; ')) }
            $lblStatus.Text = $msg
        }
    } else {
        $lblStatus.Text = ($S.Failed -f $code) + '  ' + $el
    }
    $txtLog.AppendText("`r`n" + ($S.LogAt -f $script:LogPath) + "`r`n")
    Update-FrontendLabel
    Set-Busy $false
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
        $label = if ($script:Kind -eq 'net') { $S.NetRunning } else { $S.Running }
        $t = $label + '  ' + (Format-Elapsed)
        if ($script:StepText) { $t += '  |  ' + $script:StepText }
        if ($script:DlText)   { $t += '  |  ' + $script:DlText }
        $lblStatus.Text = $t
    }
})

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
$onChange = { if (-not $script:Applying) { Update-Preview } }
foreach ($c in $chkNoProbe, $chkBrowser, $chkAstGrep, $chkApps, $chkFrontend) { $c.Add_CheckedChanged($onChange) }
$cbMirror.Add_SelectedIndexChanged($onChange)
$cbArch.Add_SelectedIndexChanged({ if (-not $script:Applying) { Update-Preview; Update-StartButton } })
$txtProxy.Add_TextChanged($onChange)

$chkBuilder.Add_CheckedChanged({
    if (-not $script:Applying -and $chkBuilder.Checked -and -not $AutoRun) {
        $r = [System.Windows.Forms.MessageBox]::Show($S.BuilderWarn, $S.Title, 'OKCancel', 'Warning')
        if ($r -ne 'OK') { $chkBuilder.Checked = $false }
    }
    if (-not $script:Applying) { Update-Preview }
})

function Confirm-Proxy($o) {
    if ($o.Proxy -and -not (Test-ProxyOk $o.Proxy)) {
        [void][System.Windows.Forms.MessageBox]::Show($S.ProxyBad, $S.Title, 'OK', 'Error')
        $txtProxy.Focus()
        return $false
    }
    return $true
}

$btnInstall.Add_Click({
    $o = Get-UiOptions
    if (-not (Confirm-Proxy $o)) { return }
    Start-Run (Get-RunCommand $o)[0] 'setup' (Get-TotalSteps $o)
})
$btnNet.Add_Click({
    $o = Get-UiOptions
    if (-not (Confirm-Proxy $o)) { return }
    Start-Run (Get-NetCommand $o) 'net' 1
})
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
    if ($AutoRun) { $btnInstall.PerformClick() }
})

# ---------------------------------------------------------------------------
Set-UiOptions $(if ($Preset) { ConvertFrom-Preset $Preset } else { New-Options })
Update-Preview
Update-StartButton
[void]$form.ShowDialog()
$form.Dispose()
exit $script:ExitCode
