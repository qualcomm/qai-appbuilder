# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# textsplitter to split text. Only C++ version GenieAPIService support this feature.
import requests
url = "http://localhost:8910/v1/textsplitter"
text = "<your_text_here>"
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 1024, "separators": separators}
response = requests.post(url, json=body)
result = response.json()
result = result["content"]
print("result length:", len(result))
count = 0
for item in result:
    count += 1
    print("No.", count)
    print("text:", item["text"])
    print("length: Tokens", item["length"], "string", len(item["text"]))
    print()

