@REM ---------------------------------------------------------------------
@REM Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
@REM SPDX-License-Identifier: BSD-3-Clause
@REM ---------------------------------------------------------------------

@echo off
cd /d "%~dp0"
set "currentDir=%CD%"

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

REM Set HuggingFace mirror for tokenizer download
if not defined HF_ENDPOINT (
    set HF_ENDPOINT=https://hf-api.gitee.com
)

REM Set HF_HOME to avoid Windows path length limit (260 chars)
if not defined HF_HOME (
    set HF_HOME=%TEMP%\hf_cache
)

cd env
@REM pixi update
pixi run webui-stable-diffusion

pause
