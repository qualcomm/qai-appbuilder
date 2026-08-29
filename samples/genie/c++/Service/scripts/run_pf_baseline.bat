@echo off
REM One-off helper: run --suite prompt_fidelity baseline on the remote ARM64 box (no C++ change).
REM ASCII-only on purpose: non-ASCII bytes in a .bat get mangled by the remote console codepage
REM and cmd then tries to execute the mangled comment text as a command (see windows-remote-test skill).
REM Uses the existing build_sdk24800 artifacts (read-only run, no build there) + QAIModelBuilder data models.
REM Read exitcode.txt and run.log when it finishes.
setlocal
set PF_DIR=C:\Users\HCKTest\pf_baseline
set SVC_ROOT=C:\Users\HCKTest\Desktop\GenieEnv\Tmp\qai-appbuilder\samples\genie\c++\Service
cd /d %PF_DIR%
python -u test_service.py --exe_dir %SVC_ROOT%\build_sdk24800\GenieService-win-arm64 --models C:\Users\HCKTest\Desktop\GenieEnv\QAIModelBuilder\data\models --suite prompt_fidelity --model_name qwen3-8b --out_dir %PF_DIR%\out > %PF_DIR%\run.log 2>&1
echo %ERRORLEVEL%> %PF_DIR%\exitcode.txt
endlocal
