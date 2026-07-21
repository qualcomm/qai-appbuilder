#!/usr/bin/env pwsh
# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
<#
.SYNOPSIS
    查询城市天气信息（简化版）
.DESCRIPTION
    使用 wttr.in API 获取指定城市的天气信息，并以简洁格式输出未来3天的天气预报
.PARAMETER City
    城市名称，支持中文或拼音（如：shanghai 或 上海）
.EXAMPLE
    .\get_weather.ps1 shanghai
    .\get_weather.ps1 上海
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$City
)

# 设置输出编码为UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

try {
    # 获取天气数据（JSON格式）
    $weatherData = Invoke-RestMethod "https://wttr.in/${City}?format=j1" -ErrorAction Stop
    
    # 获取城市名称
    $cityName = $weatherData.nearest_area[0].areaName[0].value
    
    # 输出城市名称（使用ASCII字符避免乱码）
    Write-Output "$cityName 天气预报"
    Write-Output ("=" * 50)
    
    # 输出未来3天天气
    $weatherData.weather | Select-Object -First 3 | ForEach-Object {
        $date = $_.date
        $minTemp = $_.mintempC
        $maxTemp = $_.maxtempC
        $desc = $_.hourly[4].weatherDesc[0].value
        
        # 使用C代替°C避免乱码
        Write-Output ("{0}: {1}~{2}C, {3}" -f $date, $minTemp, $maxTemp, $desc)
    }
    
} catch {
    Write-Output "获取天气信息失败: $_"
    exit 1
}
