# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$username = $env:USERNAME

$filePath = "C:\Users\$username\.pixi\config.toml"

$content = @"
tls-no-verify = true
[pypi-config]
allow-insecure-host = ["*"]
"@

$directory = Split-Path -Path $filePath -Parent
if (-not (Test-Path -Path $directory)) {
    New-Item -Path $directory -ItemType Directory -Force | Out-Null
}

Set-Content -Path $filePath -Value $content -Force

if (Test-Path -Path $filePath) {
    Write-Host "The Pixi config file is ready: $filePath"
} else {
    Write-Host "The Pixi config file is not ready: $filePath"
}