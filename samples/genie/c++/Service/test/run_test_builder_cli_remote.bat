@echo off
cd /d "C:\Users\HCKTest\Desktop\GenieEnv\Tmp\qai-appbuilder\samples\genie\c++\Service\test"
set QAI_TEST_OUT=C:\Users\HCKTest\Desktop\GenieEnv\Tmp\test_builder_cli_out
"C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_arm64_313\Scripts\python.exe" test_builder_cli.py --builder_dir "C:\Users\HCKTest\Desktop\GenieEnv\Tmp\qai-appbuilder\tools\qaimodelbuilder" --out_dir "%QAI_TEST_OUT%" > "%QAI_TEST_OUT%_run.out.log" 2> "%QAI_TEST_OUT%_run.err.log"
echo %ERRORLEVEL% > "%QAI_TEST_OUT%_run.exitcode.txt"
