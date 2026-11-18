; ---------------------------------------------------------------------
; Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
; SPDX-License-Identifier: BSD-3-Clause
; ---------------------------------------------------------------------
; QAI AppBuilder installer (browser edition), one per architecture.
; Build with installer\build_installer.ps1, which passes:
;   /DAppVersion=3.1.0  /DArch=x64|arm64  /DQairtTag=2.48.40  /DStageDir=<staged tree>  /DOutDir=<output dir>
;
; What it does: copies the staged tree (pre-built frontend, backend source, uv, aria2c),
; then runs  _setup_env.bat --arch <arch> --no-pause --with-browser --with-astgrep  (no --builder: QAIRT is NOT
; installed here). --with-browser = the search extra (playwright/selectolax/lxml/browserforge) + Chromium;
; --with-astgrep = ast-grep.exe/sg.exe from the ast-grep-cli PyPI wheel. Both are non-fatal in _setup_env.bat.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif
#ifndef Arch
  #error Pass /DArch=x64 or /DArch=arm64
#endif
#ifndef QairtTag
  #error Pass /DQairtTag=A.B.C (first three parts of the QAIRT SDK version; it goes into the file name)
#endif
#ifndef StageDir
  #error Pass /DStageDir=<staged tree>
#endif
#ifndef OutDir
  #define OutDir "..\dist\installer"
#endif
; The desktop shell's Tauri "identifier" (desktop\src-tauri\tauri.conf.json). WebView2 keeps the window's profile
; (localStorage with the open-tab layout, cookies, cache) in %LOCALAPPDATA%\<identifier>. build_installer.ps1 passes it.
#ifndef DesktopIdentifier
  #error Pass /DDesktopIdentifier=<identifier from desktop\src-tauri\tauri.conf.json>
#endif

#define AppName "QAI AppBuilder"
; AppId GUID without braces; also names the uninstall registry key {<guid>}_is1 that a cancelled setup removes.
; Never change it for a release (upgrades find the earlier install by it); /DAppGuid=... is only for test builds.
#ifndef AppGuid
  #define AppGuid "6E3B1F52-8C0A-4D57-9B1E-3A7C5D2F8E14"
#endif
; Optional components the installer ALWAYS asks _setup_env.bat for (setup and the Start-menu repair shortcut use the
; same list, so a repair completes them too). Both come from PyPI (uv pip / the ast-grep-cli wheel); Chromium from
; `python -m playwright install chromium` (cdn.playwright.dev, or npmmirror with the cn profile). Non-fatal.
#define SetupComponents "--with-browser --with-astgrep"

[Setup]
; Inno 6.7 turns RedirectionGuard on by default and child processes inherit it. That blocks traversal of NTFS junctions
; made by non-elevated processes, and `uv python install` creates exactly such a junction (cpython-3.13-...-none ->
; cpython-3.13.17-...), failing with os error 448 "untrusted mount point". _setup_env.bat must run unrestricted.
RedirectionGuard=no
; Same AppId for both architectures: one install location, a later install upgrades the earlier one.
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Qualcomm Technologies, Inc.
VersionInfoVersion={#AppVersion}
; Layout: the wizard picks the PARENT folder ({app}, default %LOCALAPPDATA%\QAIAppBuilder); every program file goes
; one level down into {app}\app. So {app}\app only ever holds what this product put there (installed files plus what
; it writes at run time: data\, models\, vendor\ ...), and the uninstaller can simply delete {app}\app as a whole
; (keeping data\ when the user asks to). The uninstaller itself lives in {app}\app too (UninstallFilesDir).
; NOTE: not upgrade-compatible with builds that installed straight into ...\QAIModelBuilder\app - uninstall those first.
DefaultDirName={localappdata}\QAIAppBuilder
UninstallFilesDir={app}\app
; The wizard shows the folder it will create; we create the app\ child ourselves, so do not warn about an existing parent.
DirExistsWarning=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutDir}
OutputBaseFilename=QAIAppBuilder-{#AppVersion}-qairt{#QairtTag}-{#Arch}-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; _setup_env.bat (without --builder) creates the runtime venv in {app}\app\envs (~0.75 GB, measured) plus download caches
; (+ ~0.2 GB vendor wheels on ARM64), + Chromium (~0.7 GB unpacked, in {app}\app\envs\ms-playwright) and
; ast-grep (~0.1 GB). The converter venv (.venv_x64_310) and the QAIRT SDK are NOT installed here.
ExtraDiskSpaceRequired=3000000000
UninstallDisplayName={#AppName} ({#Arch})
#if Arch == "arm64"
ArchitecturesAllowed=arm64
#else
; x64compatible also admits Windows-on-ARM (x64 emulation); the x64 package is then an explicit choice.
ArchitecturesAllowed=x64compatible
#endif
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\app\desktop\QAIAppBuilder.exe"; WorkingDir: "{app}\app"; Comment: "Open QAI AppBuilder (desktop window)"
; The browser edition stays available through Start.bat in the install folder; it is no longer a shortcut.
; Repair goes through cmd.exe for the same reason as Uninstall below: a shortcut straight to the .bat with arguments is not listed.
Name: "{group}\{cm:RepairName}"; Filename: "{sys}\cmd.exe"; Parameters: "/c """"{app}\app\installer\_setup_env.bat"" --arch {#Arch} {#SetupComponents}"""; WorkingDir: "{app}\app"
; Not a direct {uninstallexe} link: the shell hides a Start-menu shortcut that points straight at the registered uninstaller
; from the apps list. Going through cmd.exe keeps it listed (verified in an isolated install).
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{sys}\cmd.exe"; Parameters: "/c start """" ""{uninstallexe}"""; Flags: runminimized
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\app\desktop\QAIAppBuilder.exe"; WorkingDir: "{app}\app"

[Run]
Filename: "{app}\app\desktop\QAIAppBuilder.exe"; Description: "{cm:LaunchNow}"; WorkingDir: "{app}\app"; Flags: postinstall nowait skipifsilent; Check: EnvOk

[UninstallDelete]
; Everything under {app}\app except data\ is removed by the [Code] uninstall step (it deletes the folder as a
; whole, including what the app wrote at run time). Only these temp files inside data\ need a rule: they are not
; user data, so they go even when data\ is kept. ~2.3 GB. Exact names, no wildcard (`_qairt_tmp.zip*` would also
; catch unrelated files that merely start with that name).
Type: files; Name: "{app}\app\data\downloads\_qairt_tmp.zip"
Type: files; Name: "{app}\app\data\downloads\_qairt_tmp.zip.aria2"

[CustomMessages]
english.NetTitle=Network
english.NetSub=How should the installer download Python and dependencies?
english.NetDesc=Choose the package source. "Auto" probes the network and picks one.
english.MirrorAuto=Auto-detect (recommended)
english.MirrorOfficial=Official sources (pypi.org, github.com)
english.MirrorCn=China mirrors (use if github.com / pypi.org are slow or blocked)
english.PathWarn=The install path contains spaces or non-ASCII characters, which can break some setup scripts.%n%nContinue anyway?
english.OldLayout=An older version of QAI AppBuilder is installed in:%n%1%n%nThis version uses a different folder layout and cannot upgrade it. Uninstall the old version first (Settings > Apps, or its Start-menu uninstall shortcut), then run this installer again.
english.AppDirTaken=The folder%n%1%nalready contains files that were not installed by QAI AppBuilder.%n%nQAI AppBuilder installs into an "app" subfolder of the folder you choose and deletes that subfolder when uninstalled. Choose a different folder.
english.SettingUp=Setting up the Python environment and the browser (downloads about 1.2 GB, this can take 10-20 minutes)...
english.StepFmt=Step %1 of 7: %2
english.ElapsedFmt=Elapsed %1
english.LogHint=Detailed log: %1
english.DlFileFmt=Downloading: %1
english.DlSrcFmt=From: %1
english.CancelConfirm=Stop the installation?%n%nThe environment setup is stopped and everything this installer has put on the computer is removed.
english.CancelConfirmUpgrade=Stop the environment setup?%n%nQAI AppBuilder was already installed here, so nothing is removed: the setup stops where it is. Run "Repair or complete environment setup" from the Start menu later to finish it.
english.CancelledUpgrade=The environment setup was stopped. The existing installation was left in place.%n%nRun "Repair or complete environment setup" from the Start menu to finish it.%n%nClick Finish to close the installer.
english.Cancelling=Cancelling: stopping the setup and removing the installed files...
english.CancelledTitle=Installation cancelled
english.CancelledText=The installation was cancelled and the installed files were removed.%n%nClick Finish to close the installer.
english.CancelledKeptData=The installation was cancelled and the program files were removed. Your data folder from an earlier installation was kept:%n%1%n%nClick Finish to close the installer.
english.Step1=Checking the network
english.Step2=Preparing download tools (aria2c, uv)
english.Step3=Installing Python 3.13 and creating the environment
english.Step4=Downloading platform components (incl. ast-grep)
english.Step5=Installing Python dependencies and the Chromium browser (the longest step)
english.Step6=Initializing application data
english.Step7=Verifying the installation
english.StepDone=Finishing
english.SetupFailed=Environment setup did not finish (exit code %1).%n%nThe files are installed. Check your network, then run "Repair or complete environment setup" from the Start menu. It resumes where it stopped.%n%nLog: %2
english.RepairName=Repair or complete environment setup
english.LaunchNow=Start QAI AppBuilder now
english.UninstTitle=Choose what else to remove
english.UninstIntro=Untick what you want to keep for a later reinstall.
english.UninstData=Your data (chat history, settings, secrets)
english.UninstSdk=QAIRT SDK %1 (model conversion, several GB; other tools may use it too)
english.UninstOk=Uninstall
english.UninstCancel=Cancel
english.SdkRemoveFailed=Could not fully delete the QAIRT SDK folder:%n%1%n%nA file in it may be in use. Close programs that use it and delete the folder by hand.
chinesesimp.NetTitle=网络
chinesesimp.NetSub=安装程序应从哪里下载 Python 和依赖?
chinesesimp.NetDesc=选择软件包来源。"自动"会探测网络并自行选择。
chinesesimp.MirrorAuto=自动检测(推荐)
chinesesimp.MirrorOfficial=官方源(pypi.org、github.com)
chinesesimp.MirrorCn=国内镜像(github.com / pypi.org 慢或无法访问时选择)
chinesesimp.PathWarn=安装路径包含空格或非 ASCII 字符,可能导致部分安装脚本出错。%n%n仍要继续吗?
chinesesimp.OldLayout=以下位置已安装旧版本的 QAI AppBuilder:%n%1%n%n新版本使用不同的目录结构,无法直接升级。请先卸载旧版本(“设置 > 应用”,或开始菜单中的卸载快捷方式),然后重新运行本安装程序。
chinesesimp.AppDirTaken=文件夹%n%1%n中已有不是 QAI AppBuilder 安装的文件。%n%nQAI AppBuilder 会安装到你所选文件夹下的 "app" 子文件夹,卸载时会删除该子文件夹。请选择其他文件夹。
chinesesimp.SettingUp=正在配置 Python 环境和浏览器(需下载约 1.2 GB,可能需要 10-20 分钟)...
chinesesimp.StepFmt=第 %1 步，共 7 步：%2
chinesesimp.ElapsedFmt=已用时 %1
chinesesimp.LogHint=详细日志：%1
chinesesimp.DlFileFmt=正在下载：%1
chinesesimp.DlSrcFmt=下载来源：%1
chinesesimp.CancelConfirm=要停止安装吗?%n%n环境配置将被停止,本安装程序已安装到电脑上的内容会被全部删除。
chinesesimp.CancelConfirmUpgrade=要停止环境配置吗?%n%n此处已安装过 QAI AppBuilder,因此不会删除任何内容,配置会停在当前位置。之后可从开始菜单运行“修复或完成环境配置”继续完成。
chinesesimp.CancelledUpgrade=环境配置已停止,原有安装保持不变。%n%n请从开始菜单运行“修复或完成环境配置”完成配置。%n%n点击“完成”关闭安装程序。
chinesesimp.Cancelling=正在取消:停止环境配置并删除已安装的内容...
chinesesimp.CancelledTitle=安装已取消
chinesesimp.CancelledText=安装已取消,已安装的内容已删除。%n%n点击“完成”关闭安装程序。
chinesesimp.CancelledKeptData=安装已取消,程序文件已删除。之前安装留下的数据文件夹已保留:%n%1%n%n点击“完成”关闭安装程序。
chinesesimp.Step1=检测网络
chinesesimp.Step2=准备下载工具(aria2c、uv)
chinesesimp.Step3=安装 Python 3.13 并创建环境
chinesesimp.Step4=下载平台组件(含 ast-grep)
chinesesimp.Step5=安装 Python 依赖和 Chromium 浏览器(耗时最长的一步)
chinesesimp.Step6=初始化应用数据
chinesesimp.Step7=验证安装
chinesesimp.StepDone=正在收尾
chinesesimp.SetupFailed=环境配置未完成(退出码 %1)。%n%n文件已安装。请检查网络后,从开始菜单运行"修复或完成环境配置",它会从中断处继续。%n%n日志：%2
chinesesimp.RepairName=修复或完成环境配置
chinesesimp.LaunchNow=立即启动 QAI AppBuilder
chinesesimp.UninstTitle=选择还要删除的内容
chinesesimp.UninstIntro=取消勾选即可保留,供以后重新安装使用。
chinesesimp.UninstData=你的数据(聊天记录、设置、密钥)
chinesesimp.UninstSdk=QAIRT SDK %1(模型转换用,占用数 GB;其他工具也可能在使用)
chinesesimp.UninstOk=卸载
chinesesimp.UninstCancel=取消
chinesesimp.SdkRemoveFailed=未能完全删除 QAIRT SDK 文件夹:%n%1%n%n其中可能有文件正被占用。请关闭使用它的程序后手动删除该文件夹。

[Code]
// kernel32 tick counter in ms (wraps after ~49 days; differences are computed in Cardinal-safe range below).
function GetTickCount: DWORD;
  external 'GetTickCount@kernel32.dll stdcall';
// A WM_TIMER callback keeps the wizard's UI thread free (a polling loop here froze the window: Sleep() does not
// pump messages, so the bar never moved and the window was "Not responding" until _setup_env.bat finished).
function SetTimer(hWnd, nIDEvent, uElapse, lpTimerFunc: Longword): Longword;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(hWnd, nIDEvent: Longword): Longword;
  external 'KillTimer@user32.dll stdcall';
function EnableWindow(hWnd: Longword; bEnable: LongBool): LongBool;
  external 'EnableWindow@user32.dll stdcall';

var
  NetPage: TInputOptionWizardPage;
  SetupExit: Integer;
  DetailLabel: TNewStaticText;
  DlFileLabel: TNewStaticText;
  DlSrcLabel: TNewStaticText;
  ElapsedLabel: TNewStaticText;
  CancelRequested: Boolean;   // the user confirmed Cancel while _setup_env.bat was running
  InCancelPrompt: Boolean;    // the Cancel confirmation box is open
  HadDataBefore: Boolean;     // {app}\app\data existed before this install (kept by an earlier uninstall)
  HadInstallBefore: Boolean;  // {app}\app held a complete earlier install (upgrade / reinstall): a cancel removes nothing
  OldCancelClick: TNotifyEvent;
  SetupTimer: Longword;
  SetupRunning: Boolean;
  SetupLog, SetupBat: String;
  SetupStartTick, StepStartTick: DWORD;
  LastStepSeen: Integer;

procedure InitializeWizard;
begin
  SetupExit := -1;
  NetPage := CreateInputOptionPage(wpSelectDir, CustomMessage('NetTitle'), CustomMessage('NetSub'),
    CustomMessage('NetDesc'), True, False);
  NetPage.Add(CustomMessage('MirrorAuto'));
  NetPage.Add(CustomMessage('MirrorOfficial'));
  NetPage.Add(CustomMessage('MirrorCn'));
  NetPage.SelectedValueIndex := 0;
  CancelRequested := False;
  InCancelPrompt := False;
  HadDataBefore := False;
  HadInstallBefore := False;
end;

function HasSpaceOrNonAscii(const S: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to Length(S) do
    if (S[I] = ' ') or (Ord(S[I]) > 127) then
    begin
      Result := True;
      Exit;
    end;
end;

// Why the chosen folder cannot be used, or '' when it is fine. {app} is the PARENT; our files go to {app}\app,
// which the uninstaller deletes as a whole - so app\ must not already hold someone else's files.
function DirProblem(const Dir: String): String;
var
  App: String;
  FR: TFindRec;
  Foreign: Boolean;
begin
  Result := '';
  // An older build installed straight into this folder (it holds that build's uninstaller): not upgradable.
  if FileExists(AddBackslash(Dir) + 'unins000.dat') or
     (FileExists(AddBackslash(Dir) + 'installer\_setup_env.bat') and FileExists(AddBackslash(Dir) + 'Start.bat')) then
  begin
    Result := FmtMessage(CustomMessage('OldLayout'), [Dir]);
    Exit;
  end;
  App := AddBackslash(Dir) + 'app';
  if not DirExists(App) then Exit;
  if FileExists(AddBackslash(App) + 'unins000.dat') then Exit;  // our own earlier install: upgrade in place
  // Left over by an uninstall that kept data: only data\ remains. Anything else is not ours.
  Foreign := False;
  if FindFirst(AddBackslash(App) + '*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') and (CompareText(FR.Name, 'data') <> 0) then Foreign := True;
    until Foreign or (not FindNext(FR));
  finally
    FindClose(FR);
  end;
  if Foreign then Result := FmtMessage(CustomMessage('AppDirTaken'), [App]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  P: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    P := DirProblem(WizardDirValue);
    if P <> '' then
    begin
      SuppressibleMsgBox(P, mbError, MB_OK, IDOK);
      Result := False;  // on a silent install this aborts Setup
      Exit;
    end;
    if HasSpaceOrNonAscii(WizardDirValue) then
      Result := MsgBox(CustomMessage('PathWarn'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
end;

// No proxy page: _setup_env.bat takes the proxy from HTTPS_PROXY / HTTP_PROXY or the Windows system proxy.
function SetupArgs: String;
begin
  Result := '--arch {#Arch} --no-pause {#SetupComponents}';
  case NetPage.SelectedValueIndex of
    1: Result := Result + ' --mirror official';
    2: Result := Result + ' --mirror cn';
  end;
end;

function EnvOk: Boolean;
begin
  Result := SetupExit = 0;
end;

// Weight (share of the bar, sum 100) and expected seconds per step, from measured runs:
// step 5 (dependencies + the ~150 MB Chromium download) dominates; step 4 downloads the ~210 MB vendor bundle on
// ARM64 only, plus the ~15 MB ast-grep wheel.
function StepWeight(N: Integer): Integer;
begin
  case N of
    1: Result := 2;
    2: Result := 3;
    3: Result := 10;
    4: Result := 20;
    5: Result := 50;
    6: Result := 5;
    7: Result := 10;
  else
    Result := 0;
  end;
end;

function StepExpectedSec(N: Integer): Integer;
begin
  case N of
    1: Result := 5;
    2: Result := 10;
    3: Result := 30;
    4: Result := 100;
    5: Result := 240;
    6: Result := 10;
    7: Result := 15;
  else
    Result := 10;
  end;
end;

function StepName(N: Integer): String;
begin
  if (N >= 1) and (N <= 7) then
    Result := CustomMessage('Step' + IntToStr(N))
  else
    Result := CustomMessage('StepDone');
end;

// Last "-- Step N/7" header in the log; 0 when none yet. Reads the whole (small, ~35 KB) file each tick.
function CurrentStep(const LogFile: String): Integer;
var
  Lines: TArrayOfString;
  I, P: Integer;
  S: String;
begin
  Result := 0;
  if not LoadStringsFromLockedFile(LogFile, Lines) then Exit;
  for I := GetArrayLength(Lines) - 1 downto 0 do
  begin
    S := Lines[I];
    if Copy(S, 1, 8) = '-- Step ' then
    begin
      P := StrToIntDef(Copy(S, 9, 1), 0);
      if (P >= 1) and (P <= 7) and (Copy(S, 10, 2) = '/7') then
      begin
        Result := P;
        Exit;
      end;
    end;
  end;
end;

// Last meaningful output line, trimmed for the wizard (skips blank lines and the [TIME] bookkeeping).
function LastCharPos(C: Char; const S: String): Integer;
var
  K: Integer;
begin
  Result := 0;
  for K := Length(S) downto 1 do
    if S[K] = C then
    begin
      Result := K;
      Exit;
    end;
end;

function LastOutputLine(const LogFile: String): String;
var
  Lines: TArrayOfString;
  I, N: Integer;
  S: String;
begin
  Result := '';
  if not LoadStringsFromLockedFile(LogFile, Lines) then Exit;
  N := GetArrayLength(Lines);
  for I := N - 1 downto 0 do
  begin
    S := Trim(Lines[I]);
    // [DLFILE] / [DLEND] are download markers for the labels below, not output for the user.
    if (S <> '') and (Copy(S, 1, 6) <> '[TIME]') and (Copy(S, 1, 3) <> '-- ') and
       (Copy(S, 1, 8) <> '[DLFILE]') and (Copy(S, 1, 7) <> '[DLEND]') then
    begin
      // aria2c progress lines are CR-overwritten; keep only the last segment.
      if Pos(#13, S) > 0 then S := Trim(Copy(S, LastCharPos(#13, S) + 1, Length(S)));
      if Length(S) > 90 then S := Copy(S, 1, 87) + '...';
      Result := S;
      Exit;
    end;
  end;
end;

// Latest download announced by download_with_aria2c.ps1 / _setup_env.bat in the current step:
//   [DLFILE] <file> | <source> | <url>     ...     [DLEND]
// Returns False (nothing is downloading) when the newest marker is [DLEND] or a step header comes first.
function CurrentDownload(const LogFile: String; var FileName, Source: String): Boolean;
var
  Lines: TArrayOfString;
  I, P: Integer;
  S: String;
begin
  Result := False;
  FileName := '';
  Source := '';
  if not LoadStringsFromLockedFile(LogFile, Lines) then Exit;
  for I := GetArrayLength(Lines) - 1 downto 0 do
  begin
    S := Trim(Lines[I]);
    if Copy(S, 1, 8) = '-- Step ' then Exit;
    if Pos('[DLEND]', S) > 0 then Exit;
    P := Pos('[DLFILE]', S);
    if P > 0 then
    begin
      S := Trim(Copy(S, P + 8, Length(S)));
      P := Pos('|', S);
      if P = 0 then
        FileName := S
      else
      begin
        FileName := Trim(Copy(S, 1, P - 1));
        S := Trim(Copy(S, P + 1, Length(S)));
        // "<source> | <url>": show both, the host first (the URL may be cut off by the label width).
        P := Pos('|', S);
        if P = 0 then
          Source := S
        else
          Source := Trim(Copy(S, 1, P - 1)) + '   ' + Trim(Copy(S, P + 1, Length(S)));
      end;
      if Length(FileName) > 100 then FileName := Copy(FileName, 1, 97) + '...';
      if Length(Source) > 130 then Source := Copy(Source, 1, 127) + '...';
      Result := FileName <> '';
      Exit;
    end;
  end;
end;

// ---- Process stop / folder removal (cancel and uninstall) ------------------------------------------------------
// A string as a single-quoted PowerShell literal ('' inside doubles a quote, e.g. a user named O'Brien).
function PsQuote(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '''', '''''', True);
  Result := '''' + Result + '''';
end;

// Stop the processes that keep files under Dir busy, so they can be deleted: every process whose EXECUTABLE lives
// under Dir (backend server, desktop window, aria2c, uv, venv python), every cmd/powershell whose command line names
// Dir (scripts run from there), and every process whose command line contains Marker (the hidden wrapper of
// _setup_env.bat; '' = none). taskkill /T takes the child processes too. Editors or terminals that merely have the
// folder open are not matched; Setup's and the uninstaller's own processes never are.
// Wait=False returns at once (used from the Cancel click, which runs while Exec() of the setup is still waiting).
procedure StopProcessesUnder(const Dir, Marker: String; Wait: Boolean);
var
  Rc: Integer;
  Script: String;
  How: TExecWait;
begin
  if Dir = '' then Exit;
  Script := '$d=' + PsQuote(RemoveBackslashUnlessRoot(Dir) + '\') + '; $m=' + PsQuote(Marker) + '; ' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ' +
    '$_.Name -notmatch ''^(_unins|unins\d+)'' -and $_.Name -notmatch ''\.tmp$'' -and (' +
    '($_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, ''OrdinalIgnoreCase'')) -or ' +
    '($_.CommandLine -and $_.Name -match ''^(cmd|powershell|pwsh)\.exe$'' -and ' +
    '$_.CommandLine.IndexOf($d, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or ' +
    '($m -and $_.CommandLine -and $_.CommandLine.IndexOf($m, [StringComparison]::OrdinalIgnoreCase) -ge 0)) } | ' +
    'ForEach-Object { & taskkill.exe /F /T /PID $_.ProcessId 2>$null | Out-Null }';
  if Wait then How := ewWaitUntilTerminated else How := ewNoWait;
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"', '', SW_HIDE, How, Rc);
end;

// Delete every entry of Dir except the one named Keep ('' = keep nothing, then Dir itself goes too).
procedure DeleteDirContentsExcept(const Dir, Keep: String);
var
  FR: TFindRec;
  P: String;
begin
  if FindFirst(AddBackslash(Dir) + '*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') and ((Keep = '') or (CompareText(FR.Name, Keep) <> 0)) then
      begin
        P := AddBackslash(Dir) + FR.Name;
        if FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          DelTree(P, True, True, True)
        else
          DeleteFile(P);
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
  if Keep = '' then RemoveDir(Dir);
end;

function FmtElapsed(Sec: Integer): String;
begin
  if Sec >= 60 then
    Result := IntToStr(Sec div 60) + ' min ' + IntToStr(Sec mod 60) + ' s'
  else
    Result := IntToStr(Sec) + ' s';
end;

procedure CreateProgressLabels;
begin
  DetailLabel := TNewStaticText.Create(WizardForm);
  DetailLabel.Parent := WizardForm.InstallingPage;
  DetailLabel.Left := WizardForm.ProgressGauge.Left;
  DetailLabel.Top := WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height + ScaleY(10);
  DetailLabel.Width := WizardForm.ProgressGauge.Width;
  DetailLabel.AutoSize := False;
  DetailLabel.Height := ScaleY(16);
  // What is being downloaded right now, and from where (empty between downloads).
  DlFileLabel := TNewStaticText.Create(WizardForm);
  DlFileLabel.Parent := WizardForm.InstallingPage;
  DlFileLabel.Left := WizardForm.ProgressGauge.Left;
  DlFileLabel.Top := DetailLabel.Top + DetailLabel.Height + ScaleY(4);
  DlFileLabel.Width := WizardForm.ProgressGauge.Width;
  DlFileLabel.AutoSize := False;
  DlFileLabel.Height := ScaleY(16);
  DlSrcLabel := TNewStaticText.Create(WizardForm);
  DlSrcLabel.Parent := WizardForm.InstallingPage;
  DlSrcLabel.Left := WizardForm.ProgressGauge.Left;
  DlSrcLabel.Top := DlFileLabel.Top + DlFileLabel.Height + ScaleY(2);
  DlSrcLabel.Width := WizardForm.ProgressGauge.Width;
  DlSrcLabel.AutoSize := False;
  DlSrcLabel.Height := ScaleY(16);
  ElapsedLabel := TNewStaticText.Create(WizardForm);
  ElapsedLabel.Parent := WizardForm.InstallingPage;
  ElapsedLabel.Left := WizardForm.ProgressGauge.Left;
  ElapsedLabel.Top := DlSrcLabel.Top + DlSrcLabel.Height + ScaleY(4);
  ElapsedLabel.Width := WizardForm.ProgressGauge.Width;
  ElapsedLabel.AutoSize := False;
  ElapsedLabel.Height := ScaleY(16);
end;

// Percent of the bar for step N, T seconds into it: finished steps count fully; inside a step the bar
// creeps toward (but never past) 90% of that step's share, so a slow step does not look frozen or "done".
function BarPercent(N, StepSec: Integer): Integer;
var
  I, Done, Frac: Integer;
begin
  Done := 0;
  for I := 1 to N - 1 do Done := Done + StepWeight(I);
  Frac := (StepSec * 90) div StepExpectedSec(N);
  if Frac > 90 then Frac := 90;
  Result := Done + (StepWeight(N) * Frac) div 100;
  if Result > 99 then Result := 99;
end;

// Called by the WM_TIMER every 500 ms while Exec() below waits: refreshes the bar/labels from the log.
// Must stay quick and must never raise.
procedure SetupTimerTick(A1, A2, A3, A4: Longword);
var
  Step, Pct: Integer;
  Now: DWORD;
  DlFile, DlSrc: String;
begin
  if not SetupRunning then Exit;
  // While the Cancel confirmation box is open the wizard must stay disabled (modal), or Cancel could be clicked again.
  if InCancelPrompt then Exit;
  // Exec(ewWaitUntilTerminated) leaves the wizard window WS_DISABLED while it waits. A disabled window ignores the
  // restore command sent by a taskbar click, so after being minimized/covered it could not be brought back.
  // This timer runs on the UI thread, so re-enable the form here. Next stays greyed out; Cancel is wired to
  // SetupCancelClick for the duration of the setup.
  EnableWindow(WizardForm.Handle, True);
  if CancelRequested then Exit;
  WizardForm.CancelButton.Enabled := True;
  try
    Now := GetTickCount;
    Step := CurrentStep(SetupLog);
    if Step <> LastStepSeen then
    begin
      LastStepSeen := Step;
      StepStartTick := Now;
    end;
    if Step = 0 then Pct := 0
    else Pct := BarPercent(Step, (Now - StepStartTick) div 1000);
    WizardForm.ProgressGauge.Position := Pct;
    if Step = 0 then
      DetailLabel.Caption := CustomMessage('StepDone')
    else
      DetailLabel.Caption := FmtMessage(CustomMessage('StepFmt'), [IntToStr(Step), StepName(Step)]);
    WizardForm.FilenameLabel.Caption := LastOutputLine(SetupLog);
    if CurrentDownload(SetupLog, DlFile, DlSrc) then
    begin
      DlFileLabel.Caption := FmtMessage(CustomMessage('DlFileFmt'), [DlFile]);
      if DlSrc <> '' then
        DlSrcLabel.Caption := FmtMessage(CustomMessage('DlSrcFmt'), [DlSrc])
      else
        DlSrcLabel.Caption := '';
    end
    else
    begin
      DlFileLabel.Caption := '';
      DlSrcLabel.Caption := '';
    end;
    ElapsedLabel.Caption := FmtMessage(CustomMessage('ElapsedFmt'), [FmtElapsed((Now - SetupStartTick) div 1000)]) +
                            '    ' + FmtMessage(CustomMessage('LogHint'), [SetupLog]);
  except
    // A transient read error (log being written) must not kill the timer; the next tick retries.
  end;
end;

// Stop _setup_env.bat: kill the hidden wrapper cmd.exe (matched by its .bat path) with its whole tree (powershell,
// aria2c, uv, python), plus anything still running from {app}\app. Wait=False is used from the Cancel click.
procedure StopEnvironmentSetup(Wait: Boolean);
begin
  StopProcessesUnder(ExpandConstant('{app}\app'), SetupBat, Wait);
end;

// Asks, then stops the environment setup. Used by the Cancel button (OnClick replaced while the setup runs) and by
// CancelButtonClick (the window's close box).
procedure RequestSetupCancel;
var
  Msg: String;
begin
  if (not SetupRunning) or CancelRequested or InCancelPrompt then Exit;
  if HadInstallBefore then Msg := CustomMessage('CancelConfirmUpgrade') else Msg := CustomMessage('CancelConfirm');
  InCancelPrompt := True;
  try
    if MsgBox(Msg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then Exit;
  finally
    InCancelPrompt := False;
  end;
  // The setup may have finished while the box was open: then there is nothing to stop, keep the result.
  if not SetupRunning then Exit;
  CancelRequested := True;
  WizardForm.CancelButton.Enabled := False;
  WizardForm.StatusLabel.Caption := CustomMessage('Cancelling');
  WizardForm.FilenameLabel.Caption := '';
  DetailLabel.Caption := '';
  DlFileLabel.Caption := '';
  DlSrcLabel.Caption := '';
  // No wait here: this handler runs inside the Exec() wait of RunEnvironmentSetup. Once the wrapper cmd.exe is gone
  // Exec() returns and CurStepChanged cleans up.
  StopEnvironmentSetup(False);
end;

procedure SetupCancelClick(Sender: TObject);
begin
  RequestSetupCancel;
end;

// Runs _setup_env.bat hidden and returns only when it has finished (so Setup cannot jump to the Finished page
// early). A generated wrapper .bat redirects the output to the log. Exec(ewWaitUntilTerminated) keeps Inno's
// message loop running while it waits, so the WM_TIMER callback above repaints the wizard.
procedure RunEnvironmentSetup;
var
  Cmd: String;
  Rc: Integer;
begin
  ForceDirectories(ExpandConstant('{app}\app\data\logs'));
  SetupLog := ExpandConstant('{app}\app\data\logs\install_setup_env.log');
  SetupBat := ExpandConstant('{app}\app\data\logs\_run_setup_env.bat');
  DeleteFile(SetupLog);

  Cmd := '@echo off' + #13#10 +
         'cd /d "' + ExpandConstant('{app}\app') + '"' + #13#10 +
         'call "' + ExpandConstant('{app}\app\installer\_setup_env.bat') + '" ' + SetupArgs + ' > "' + SetupLog + '" 2>&1' + #13#10 +
         'exit /b %ERRORLEVEL%' + #13#10;
  SaveStringToFile(SetupBat, Cmd, False);

  WizardForm.StatusLabel.Caption := CustomMessage('SettingUp');
  WizardForm.ProgressGauge.Min := 0;
  WizardForm.ProgressGauge.Max := 100;
  WizardForm.ProgressGauge.Position := 0;
  CreateProgressLabels;

  SetupStartTick := GetTickCount;
  StepStartTick := SetupStartTick;
  LastStepSeen := 0;
  SetupRunning := True;
  // Inno does not offer Cancel after the files are copied (ssPostInstall): take the button over while the setup runs.
  OldCancelClick := WizardForm.CancelButton.OnClick;
  WizardForm.CancelButton.OnClick := @SetupCancelClick;
  WizardForm.CancelButton.Visible := True;
  WizardForm.CancelButton.Enabled := True;
  SetupTimer := SetTimer(0, 0, 500, CreateCallback(@SetupTimerTick));
  try
    // SW_HIDE: no console window.
    if Exec(ExpandConstant('{cmd}'), '/c ""' + SetupBat + '""', ExpandConstant('{app}\app'), SW_HIDE,
            ewWaitUntilTerminated, Rc) then
      SetupExit := Rc
    else
      SetupExit := -2;
  finally
    SetupRunning := False;
    KillTimer(0, SetupTimer);
    WizardForm.CancelButton.OnClick := OldCancelClick;
    WizardForm.CancelButton.Enabled := False;
  end;
  if SetupExit = 0 then WizardForm.ProgressGauge.Position := 100;
  DeleteFile(SetupBat);
end;

// The uninstall entry Inno registered for this AppId (HKA = HKLM for an administrative install, else HKCU).
procedure RemoveUninstallEntry;
begin
  RegDeleteKeyIncludingSubkeys(HKA, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + '{' + '{#AppGuid}' + '}_is1');
end;

// Delete {app}\app (keeping data\ when KeepData) with a helper .bat run through Exec(): Exec keeps the wizard's message
// loop going, so the window does not freeze while ~1.5 GB (venvs, Chromium) is removed.
procedure DeleteAppFolder(const App: String; KeepData: Boolean);
var
  Bat, S: String;
  Rc: Integer;
begin
  Bat := ExpandConstant('{tmp}\_cancel_cleanup.bat');
  S := '@echo off' + #13#10 +
       'rem aria2c / uv may need a moment to release their files after taskkill' + #13#10 +
       'ping -n 3 127.0.0.1 >nul' + #13#10;
  if KeepData then
    S := S + 'for /d %%D in ("' + App + '\*") do if /i not "%%~nxD"=="data" rmdir /s /q "%%D"' + #13#10 +
             'del /f /q /a "' + App + '\*" >nul 2>&1' + #13#10
  else
    S := S + 'rmdir /s /q "' + App + '"' + #13#10;
  S := S + 'exit /b 0' + #13#10;
  SaveStringToFile(Bat, S, False);
  Exec(ExpandConstant('{cmd}'), '/c ""' + Bat + '""', ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Rc);
  DeleteFile(Bat);
  // Whatever the .bat could not remove (a file still locked): one more pass in-process.
  if DirExists(App) then
  begin
    if KeepData then DeleteDirContentsExcept(App, 'data') else DeleteDirContentsExcept(App, '');
  end;
end;

// Cancel during the environment setup of a NEW install: everything this installer put on the computer goes again -
// the program folder {app}\app (files, venvs, Chromium, downloads, the uninstaller), the shortcuts and the uninstall
// entry. data\ is kept only when it was already there before (kept by an earlier uninstall).
// uv's shared per-user folders (%APPDATA%\uv, %LOCALAPPDATA%\uv) are left alone, as on uninstall.
// Not used for an upgrade/reinstall over an existing install: that one is only stopped (see CurStepChanged).
procedure RemoveInstalledContent;
var
  App: String;
begin
  App := ExpandConstant('{app}\app');
  StopEnvironmentSetup(True);
  if DirExists(App) then
    DeleteAppFolder(App, HadDataBefore and DirExists(AddBackslash(App) + 'data'));
  DeleteFile(ExpandConstant('{autodesktop}\{#AppName}.lnk'));
  DelTree(ExpandConstant('{group}'), True, True, True);
  RemoveUninstallEntry;
  RemoveDir(ExpandConstant('{app}'));
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpInstalling then
    WizardForm.NextButton.Enabled := False;
  if (CurPageID = wpFinished) and CancelRequested then
  begin
    WizardForm.FinishedHeadingLabel.Caption := CustomMessage('CancelledTitle');
    if HadInstallBefore then
      WizardForm.FinishedLabel.Caption := CustomMessage('CancelledUpgrade')
    else if HadDataBefore and DirExists(ExpandConstant('{app}\app\data')) then
      WizardForm.FinishedLabel.Caption := FmtMessage(CustomMessage('CancelledKeptData'), [ExpandConstant('{app}\app\data')])
    else
      WizardForm.FinishedLabel.Caption := CustomMessage('CancelledText');
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  // While Inno copies the files, its own Cancel handling applies (it rolls the copied files back).
  // While _setup_env.bat runs (window close box; the button itself goes to SetupCancelClick), never let Inno exit:
  // that would orphan the hidden setup. Stop it and clean up instead.
  if not SetupRunning then Exit;
  Cancel := False;
  RequestSetupCancel;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  App: String;
begin
  App := ExpandConstant('{app}\app');
  // Before any file is copied: what was here already? A cancel must not remove an earlier install or its data.
  // (DirProblem() only lets an {app}\app through that is our own install or holds nothing but data\.)
  if CurStep = ssInstall then
  begin
    HadInstallBefore := FileExists(AddBackslash(App) + 'unins000.dat');
    HadDataBefore := DirExists(AddBackslash(App) + 'data');
  end;
  if CurStep = ssPostInstall then
  begin
    RunEnvironmentSetup;
    if CancelRequested then
    begin
      SetupExit := -3;
      DetailLabel.Caption := '';
      ElapsedLabel.Caption := '';
      WizardForm.FilenameLabel.Caption := '';
      if HadInstallBefore then
        // Upgrade / reinstall: the earlier install stays; only the setup was stopped. Repair finishes it later.
        StopEnvironmentSetup(True)
      else
      begin
        WizardForm.StatusLabel.Caption := CustomMessage('Cancelling');
        RemoveInstalledContent;
      end;
      WizardForm.ProgressGauge.Position := 0;
      Exit;
    end;
    if (SetupExit <> 0) and (not WizardSilent) then
      MsgBox(FmtMessage(CustomMessage('SetupFailed'), [IntToStr(SetupExit), SetupLog]), mbError, MB_OK);
  end;
end;

// ---- QAIRT SDK removal (uninstall) ----------------------------------------------------------------------------
// The SDK lives OUTSIDE {app} (default C:\Qualcomm\AIStack\QAIRT\<version>, several GB). Never deleted silently: when
// data\config\qairt_env.json names it (qairt_sdk_root) and that folder still exists, it is listed in the uninstall
// options window (ticked by default, the user can untick it).
// No ownership check -- the user decides. The path must still be absolute, local and at least 3 levels deep, so a
// bad value can never point the delete at a drive root or C:\Qualcomm.
// Read BEFORE data\ may be deleted below.
function JsonStringValue(const Json, Key: String): String;
var
  P, Q: Integer;
  S: String;
begin
  Result := '';
  P := Pos('"' + Key + '"', Json);
  if P = 0 then Exit;
  S := Copy(Json, P + Length(Key) + 2, MaxInt);
  P := Pos(':', S);
  if P = 0 then Exit;
  S := TrimLeft(Copy(S, P + 1, MaxInt));
  if (Length(S) = 0) or (S[1] <> '"') then Exit;
  S := Copy(S, 2, MaxInt);
  Q := Pos('"', S);
  if Q = 0 then Exit;
  Result := Copy(S, 1, Q - 1);
  StringChangeEx(Result, '\\', '\', True);
end;

function PathDepth(const Path: String): Integer;
var
  I: Integer;
begin
  Result := 0;
  for I := 1 to Length(Path) do
    if Path[I] = '\' then Result := Result + 1;
end;

// The desktop window's WebView2 profile folder (see DesktopIdentifier at the top).
function WebViewDataDir: String;
begin
  Result := ExpandConstant('{localappdata}\{#DesktopIdentifier}');
end;

// '' when there is no SDK to offer: qairt_env.json missing / no qairt_sdk_root / not a safe path / folder gone.
function SdkToOffer: String;
var
  Cfg: AnsiString;
  Root: String;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{app}\app\data\config\qairt_env.json'), Cfg) then Exit;
  Root := RemoveBackslashUnlessRoot(Trim(JsonStringValue(String(Cfg), 'qairt_sdk_root')));
  // absolute local path (X:\...), at least X:\a\b\c, no relative parts
  if (Length(Root) < 4) or (Root[2] <> ':') or (Root[3] <> '\') then Exit;
  if (PathDepth(Root) < 3) or (Pos('..', Root) > 0) then Exit;
  if not DirExists(Root) then Exit;
  Result := Root;
end;

var
  RemoveAllData: Boolean;   // user ticked "your data"
  DelSdk: Boolean;          // user ticked the QAIRT SDK
  SdkPath: String;
// Not offered, on purpose:
//  * Chromium for the browser tools: _setup_env.bat installs it into {app}\app\envs\ms-playwright, so it goes with the
//    program. Playwright's shared per-user cache (%LOCALAPPDATA%\ms-playwright) is not ours and is left alone.
//  * uv's per-user folders (%APPDATA%\uv managed Pythons, %LOCALAPPDATA%\uv cache): shared with every other uv user
//    on the machine, and a reinstall reuses them. Always kept.

// A window of its own, shown when the uninstall starts. It is NOT put inside "Uninstall Status": Inno disables that
// window while it uninstalls, so controls placed in it cannot be clicked (measured). The window is sized and
// centred like Uninstall Status. One tick box per optional item, all ticked; an item whose folder is not there is
// not listed. "Uninstall" goes on with what is ticked; "Cancel" (or closing the window) returns False and the caller
// stops the uninstall with nothing removed. The SDK path comes from qairt_env.json, which lives in data\, so this
// must run BEFORE data\ is deleted.
function AskUninstallChoices: Boolean;
var
  Form: TSetupForm;
  List: TNewCheckListBox;
  OkBtn, KeepBtn: TNewButton;
  IxData, IxSdk, W, H, Pct: Integer;
  DataDir: String;
begin
  Result := True;
  RemoveAllData := False;
  DelSdk := False;
  DataDir := ExpandConstant('{app}\app\data');
  SdkPath := SdkToOffer;
  if not (DirExists(DataDir) or (SdkPath <> '')) then Exit;

  // CreateCustomForm scales its size arguments to the screen DPI itself, so give it 96-dpi numbers: take the
  // Uninstall Status window's pixel size and divide the DPI factor back out (ScaleX(100) px = 100 units).
  Pct := ScaleX(100);
  Form := CreateCustomForm(UninstallProgressForm.ClientWidth * 100 div Pct,
    UninstallProgressForm.ClientHeight * 100 div Pct, False, True);
  try
    // The window frame is added on top of the client size, so match the OUTER size of Uninstall Status exactly.
    Form.Width := UninstallProgressForm.Width;
    Form.Height := UninstallProgressForm.Height;
    Form.Caption := CustomMessage('UninstTitle');

    // buttons first (bottom right), so the list can fill what is left
    OkBtn := TNewButton.Create(Form);
    OkBtn.Parent := Form;
    OkBtn.Caption := CustomMessage('UninstOk');
    OkBtn.ModalResult := mrOk;
    OkBtn.Default := True;
    KeepBtn := TNewButton.Create(Form);
    KeepBtn.Parent := Form;
    KeepBtn.Caption := CustomMessage('UninstCancel');
    KeepBtn.ModalResult := mrCancel;
    KeepBtn.Cancel := True;
    W := Form.CalculateButtonWidth([OkBtn.Caption, KeepBtn.Caption]);
    OkBtn.Width := W;
    KeepBtn.Width := W;
    OkBtn.Height := ScaleY(23);
    KeepBtn.Height := ScaleY(23);
    KeepBtn.Left := Form.ClientWidth - W - ScaleX(12);
    OkBtn.Left := KeepBtn.Left - W - ScaleX(8);
    KeepBtn.Top := Form.ClientHeight - ScaleY(23 + 12);
    OkBtn.Top := KeepBtn.Top;

    List := TNewCheckListBox.Create(Form);
    List.Parent := Form;
    List.Left := ScaleX(12);
    List.Top := ScaleY(12);
    List.Width := Form.ClientWidth - ScaleX(24);
    List.Height := KeepBtn.Top - List.Top - ScaleY(12);
    IxData := -1;
    IxSdk := -1;
    // The intro text is the first row, then each item with its folder on an indented line below it.
    List.AddGroup(CustomMessage('UninstIntro'), '', 0, nil);
    // The desktop window's WebView2 profile is not offered here: it is window state only and is always removed
    // (see CurUninstallStepChanged).
    if DirExists(DataDir) then
    begin
      IxData := List.AddCheckBox(CustomMessage('UninstData'), '', 0, True, True, False, False, nil);
      List.AddGroup(DataDir, '', 1, nil);
    end;
    if SdkPath <> '' then
    begin
      IxSdk := List.AddCheckBox(FmtMessage(CustomMessage('UninstSdk'), [ExtractFileName(SdkPath)]), '', 0, True, True, False, False, nil);
      List.AddGroup(SdkPath, '', 1, nil);
    end;
    // shrink the frame to its rows (about 18 px each at 96 dpi) instead of stretching it down to the buttons
    H := List.Items.Count * ScaleY(18) + ScaleY(8);
    if H < List.Height then List.Height := H;

    Form.FlipAndCenterIfNeeded(True, UninstallProgressForm, False);

    Result := Form.ShowModal = mrOk;
    if Result then
    begin
      RemoveAllData := (IxData >= 0) and List.Checked[IxData];
      DelSdk := (IxSdk >= 0) and List.Checked[IxSdk];
    end;
  finally
    Form.Free;
  end;
end;
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  App := ExpandConstant('{app}\app');  // everything of ours; {app} itself is only the parent the user picked
  if CurUninstallStep = usUninstall then
  begin
    RemoveAllData := False;
    // Ask FIRST, before anything is stopped or deleted: "Cancel" must leave the app exactly as it was. Abort in
    // usUninstall ends the uninstaller without removing anything (Inno: Abort is honoured in this step).
    // Silent: no dialog; keep data and the SDK (envs\ still goes in usPostUninstall).
    if not UninstallSilent then
      if not AskUninstallChoices then Abort;
    // The backend server / a conversion / a download may still be running from here: stop them first, or their
    // open files survive the delete. This includes the venvs: the server runs from {app}\app\envs\.venv_*.
    StopProcessesUnder(App, '', True);
    // The desktop window's WebView2 profile (open-tab layout, cookies, cache) is window state, not user data: always
    // remove it, also on a silent uninstall. The window was stopped just above (it runs from {app}\app\desktop), so the
    // folder is no longer locked. Keeping it would restore tabs for conversations a fresh database does not have.
    DelTree(WebViewDataDir, True, True, True);
    if RemoveAllData then
    begin
      DelTree(ExpandConstant('{app}\app\data'), True, True, True);
    end;
    if DelSdk and (SdkPath <> '') then
    begin
      if not DelTree(SdkPath, True, True, True) then
        MsgBox(FmtMessage(CustomMessage('SdkRemoveFailed'), [SdkPath]), mbError, MB_OK);
    end;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    // Inno has removed the files it installed. Whatever is left in {app}\app was created at run time (envs\ with
    // the venvs, models\, vendor\, logs, caches ...) and is ours too: remove it all, keeping only data\ unless the
    // user chose to delete their data.
    if DirExists(App) then
    begin
      if RemoveAllData or (not DirExists(AddBackslash(App) + 'data')) then
        DeleteDirContentsExcept(App, '')
      else
        DeleteDirContentsExcept(App, 'data');
    end;
    // The parent the user picked: removed only when nothing is left in it (RemoveDir fails on a non-empty dir),
    // e.g. %LOCALAPPDATA%\QAIAppBuilder once app\ is gone.
    RemoveDir(ExpandConstant('{app}'));
  end;
end;

