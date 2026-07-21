# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询城市天气信息（简化版）
使用 wttr.in API 获取指定城市的天气信息，并以简洁格式输出未来3天的天气预报

用法:
    python get_weather.py shanghai
    python get_weather.py 上海
"""

import sys
import io
import ssl
import urllib.request
import urllib.error
import urllib.parse
import json

# 强制 stdout/stderr 使用 UTF-8，避免 Windows cp1252 编码导致中文 UnicodeEncodeError
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def get_weather(city: str) -> None:
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"

    # 创建忽略 SSL 证书验证的 context，兼容企业代理/自签名证书环境
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"获取天气信息失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"获取天气信息失败: {e}")
        sys.exit(1)

    city_name = data["nearest_area"][0]["areaName"][0]["value"]
    print(f"{city_name} 天气预报")
    print("=" * 50)

    for day in data["weather"][:3]:
        date = day["date"]
        min_temp = day["mintempC"]
        max_temp = day["maxtempC"]
        desc = day["hourly"][4]["weatherDesc"][0]["value"]
        print(f"{date}: {min_temp}~{max_temp}C, {desc}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python get_weather.py <城市名>")
        print("示例: python get_weather.py shanghai")
        sys.exit(1)
    get_weather(sys.argv[1])
