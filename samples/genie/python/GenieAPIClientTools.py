# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# python GenieAPIClientTools.py --prompt "The whether in Shanghai today."
# python GenieAPIClientTools.py --prompt "The whether in Shanghai today." --stream

import json
from colorama import init, Fore
import argparse
from openai import OpenAI

init(autoreset=True)

BASE_URL = "http://localhost:8910/v1"   # For Genie

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="你好", type=str)
parser.add_argument("--model", default="Qwen2.0-7B-SSD", type=str)

args = parser.parse_args()
prompt = args.prompt
stream = args.stream

client = OpenAI(base_url=BASE_URL, api_key="123")

# model_lst = client.models.list()
# print(model_lst)

model_name = args.model

messages = [{"role": "system", "content": "You are a helpful assistant that can use tools to answer questions and perform tasks."}, {"role": "user", "content": prompt}]
extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6}

def search_files(query, maxResults):
    """Find and locate files or folders by name, such as searching for README.md file location on your computer."""

    file_info = {
        "file_path": "c:\\README.md",
        "file_path2": "c:\\sample.py",
        "file_path3": "c:\\test.txt"
    }

    return json.dumps(file_info)

def get_current_weather(location, unit="fahrenheit"):
    """Get the current weather in a given location"""
    
    if not unit:
        unit = "celsius"

    weather_info = {
        "date": "today",
        "location": location,
        "temperature": "32",
        "unit": unit,
        "forecast": ["sunny", "windy"],
    }
    return json.dumps(weather_info)

def get_n_day_weather_forecast(location, unit="fahrenheit"):
    """Get an N-day weather forecast in a given location"""

    if not unit:
        unit = "celsius"

    weather_info = {
        "date": "7 days",
        "location": location,
        "temperature": "43",
        "unit": unit,
        "forecast": ["cloudy", "windy", "rain", "sunny", "cloudy", "windy", "rain"],
    }
    return json.dumps(weather_info)

available_functions = {
    "get_current_weather": get_current_weather,
    "get_n_day_weather_forecast": get_n_day_weather_forecast,
    "search_files": search_files,
}

tools = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Find and locate files or folders by name, such as searching for README.md file location on your computer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for file names",
                    },
                    "maxResults": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 110,
                        "description": "Maximum number of results to return (default: 20)",
                    },
                    "matchCase": {
                        "type": "boolean",
                        "description": "Enable case-sensitive search",
                    },
                    "matchWholeWord": {
                        "type": "boolean",
                        "description": "Match whole words only",
                    },
                    "regex": {
                        "type": "boolean",
                        "description": "Enable regular expression search",
                    },
                },
                "required": ["query"],
                "additionalProperties": False
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather in a given location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "The temperature unit to use. Infer this from the users location.",
                    },
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                },
                "required": ["location", "unit"],
                "additionalProperties": False
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_n_day_weather_forecast",
            "description": "Get an N-day weather forecast.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "The temperature unit to use. Infer this from the users location.",
                    },
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "num_days": {
                        "type": "integer",
                        "description": "The number of days to forecast",
                    }
                },
                "required": ["location", "unit", "num_days"],
                "additionalProperties": False
            },
        }
    },
]


def handle_tool_calls(tool_calls, params, available_functions):
    for tool_call in tool_calls:
        try:
            tool_call_id = tool_call.id
            function_name = tool_call.function.name
            function_to_call = available_functions[function_name]
            function_args = json.loads(tool_call.function.arguments)

            if function_name == "search_files":
                tool_response = function_to_call(
                    query=function_args.get("query"),
                    maxResults=function_args.get("maxResults"),
                )
            elif function_name == "get_n_day_weather_forecast":
                tool_response = function_to_call(
                    location=function_args.get("location"),
                    unit=function_args.get("unit"),
                )
            else:
                tool_response = function_to_call(
                    location=function_args.get("location"),
                    unit=function_args.get("unit"),
                )

            params["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_response,
                }
            )
        except Exception as e:
            print("Exception:", e)

def print_response_content(content):
    if content is not None:
        print(Fore.YELLOW + content, end="", flush=True)
        print()

def process_response(response, params, available_functions):
    content = response.choices[0].message.content
    print_response_content(content)
    tool_calls = response.choices[0].message.tool_calls
    if tool_calls:
        handle_tool_calls(tool_calls, params, available_functions)
        response = client.chat.completions.create(**params)
        content = response.choices[0].message.content or ""
        print_response_content(content)

def process_stream_response(response, params, available_functions):
    for chunk in response:
        content = chunk.choices[0].delta.content or ""
        print(Fore.YELLOW + content, end="", flush=True)
        if chunk.choices[0].finish_reason == "stop":
            print()
            return
        tool_calls = chunk.choices[0].delta.tool_calls
        if tool_calls:
            handle_tool_calls(tool_calls, params, available_functions)
            response = client.chat.completions.create(**params)
            for x in response:
                content = x.choices[0].delta.content or ""
                print(Fore.YELLOW + content, end="", flush=True)
            print()

# https://github.com/xusenlinzy/api-for-open-llm/blob/master/examples/qwen-7b-chat/get_weather.py
def main():
    params = dict(model=model_name, stream=stream, messages=[{"role": "user", "content": prompt}])

    if stream:
        response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body,
                                                  tools=tools, tool_choice="auto")
        process_stream_response(response, params, available_functions)
    else:
        response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body,
                                                  tools=tools, tool_choice="auto")
        process_response(response, params, available_functions)

if __name__ == '__main__':
    main()
