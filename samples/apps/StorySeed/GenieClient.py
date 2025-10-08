# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from openai import OpenAI
import time
HOST = "127.0.0.1"
PORT = "8910"
TIMEOUT_SECONDS = 240
client = OpenAI(base_url=f"http://{HOST}:{PORT}/v1", api_key="123", timeout=TIMEOUT_SECONDS)

def generate_response(prompt: str, model: str = "Qwen2.0-7B-SSD", stream: bool = False, is_debug: bool = False) -> str:
    messages = [
        {"role": "system", "content": "You are an English writer."},
        {"role": "user", "content": prompt}
    ]

    extra_body = {
        "size": 4096,
        "seed": 146,
        "temp": 1.5,
        "top_k": 13,
        "top_p": 0.6,
        "penalty_last_n": 64,
        "penalty_repeat": 1.3
    }
    
    timeout_sec = TIMEOUT_SECONDS
    if stream:
        if is_debug:
            print("Streaming response:")
        output = ""
        start_time = time.time()
        try:
            response = client.chat.completions.create(model=model, messages=messages, stream=True, extra_body=extra_body)
            for chunk in response:
                if time.time() - start_time > timeout_sec:
                    raise TimeoutError(f"❌ Streaming response exceeded {TIMEOUT_SECONDS} timeout. Maybe the service is abnormal")
                content = chunk.choices[0].delta.content
                if content:
                    print(content, end="", flush=True)
                    output += content
            if is_debug:
                print()
            return output
        except TimeoutError as e:
            print(str(e))
            return "ERROR_TIMEOUT"
    else:
        try:
            response = client.chat.completions.create(model=model, messages=messages, extra_body=extra_body)
            return response.choices[0].message.content.strip()
        except Exception as e:
            if isinstance(e, TimeoutError) or "timeout" in str(e).lower():
                print("❌ Request timed out.")
                return "ERROR_TIMEOUT"
            else:
                print(f"❌ Other error occurred: {e}, but treated as timeout")
                return "ERROR_TIMEOUT"