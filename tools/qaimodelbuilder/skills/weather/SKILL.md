---
name: Weather
description: Query weather forecasts for any city. Uses wttr.in service, no API key required.
tags: weather, forecast, wttr
use_for: Getting current weather, checking weather forecasts, querying weather by city name
homepage: "https://wttr.in/:help"
---

Use the `exec` tool to run the following command to get weather forecast:

## CRITICAL:

**Required Action**: Call exec tool to run command immediately. Replace "shanghai" with the city name from the user's request (supports both English and Chinese city names).

**Tool call**:
<tool_call>
{"name":"exec","arguments":{"command":"python ${SKILL_DIR}/scripts/get_weather.py shanghai","timeout":30}}
</tool_call>

DO NOT make up weather data. Call exec tool immediately with timeout set to 30, replacing "shanghai" with the city name from user's request.

Final Step: When you receive the output from the exec command, verify it contains weather data, and then present it to the user.

## Output Example

```
Shanghai Weather Forecast
==================================================
2026-03-30: 18~24C, Partly cloudy
2026-03-31: 17~23C, Sunny
2026-04-01: 19~25C, Cloudy
```

## Notes

- Supports both English and Chinese city names: e.g. "nanjing" or "南京"
- Script returns a concise 3-day weather forecast
- You MUST call the exec tool to get real data
