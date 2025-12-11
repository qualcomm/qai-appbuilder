#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import requests
from openai import OpenAI
import subprocess
import sys
import os
import subprocess
import json
import time
from colorama import init, Fore
from pathlib import Path
import io

# 强制 stdout 使用 UTF-8 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


class Test():
    def __init__(self) -> None:
        init(autoreset=True)

    def startservice(self, model_name="Qwen2.0-7B-SSD"):
        """启动服务,并在新终端窗口中运行"""
        (" 开始测试启动服务:")
        print(f"Loading model: {model_name}")
        # 使用 cmd /c start 来打开新窗口
        command = [
            "conhost.exe", "GenieAPIService.exe",
            "-c", f"models/{model_name}/config.json",
            "-l"
        ]

        try:
            # shell=True 是必须的,以便支持 'start' 命令
            subprocess.Popen(command, shell=True, cwd=os.path.dirname(__file__))  # 设置工作目录
            print(Fore.GREEN + "服务已在新窗口中启动,监听端口 8910\n")
            time.sleep(10)
        except FileNotFoundError:
            print(Fore.RED + "错误:找不到 GenieAPIService.exe,请确认路径是否正确.\n")
        except Exception as e:
            print(Fore.RED + f"启动服务时出错:{e}\n")

    def stopservice(self):
        """终止服务"""
        print("开始测试终止服务:")
        url = "http://10.92.142.185:8910/servicestop"

        params = {"text": "stop"}
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "stop service success\n")
        else:
            print(Fore.RED + "fail to stop sevice\n")

    def clearmessage(self):
        """清除保存的历史信息"""
        print("测试清除历史记录: ")
        url = "http://10.92.142.185:8910/clear"
        params = {"text": "clear"}
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "clear message success\n")
        else:
            print(Fore.RED + "fail to clear message\n")

    def reloadmessage(self, message: list[str]):
        """加载从客户端传递的历史信息"""
        print("开始测试加载历史记录: ")
        url = "http://10.92.142.185:8910/reload"
        user_content = dict()
        user_content['role'] = "user"
        user_content['content'] = ""
        assistant_content = dict()
        assistant_content["role"] = "assistant"
        assistant_content['content'] = ""
        history = []
        for i in range(len(message)):
            if i % 2 == 0:
                user_content['content'] = message[i]
                history.append(user_content)
            else:
                assistant_content['content'] = message[i]
                history.append(assistant_content)

        history_data = {
            "action": "import_history",
            "history": history
        }

        response = requests.post(url, json=history_data)
        if response.status_code == 200:
            print(Fore.GREEN + "reload message success\n")
        else:
            print(Fore.RED + "fail to reload message\n")

    def stopoutput(self):
        """终止当前模型输出"""
        print("开始测试终止模型输出:")
        url = "http://10.92.142.185:8910/stop"

        params = {"text": "stop"}
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "stop output success\n")
        else:
            print(Fore.RED + "fail to stop output\n")

    def getcontextsize(self):
        """获得模型上下文长度"""
        url = "http://10.92.142.185:8910/contextsize"
        params = {"modelName": "Qwen2.0-7B-SSD"}  # Llama2.0-7B-SSD, Qwen2.0-7B-SSD
        response = requests.post(url, json=params)
        if response.status_code == 200:
            result = response.json()
            print("context大小:", result["contextsize"])

    def textsplit(self, text: str, max_length: int = 1024):
        """对文本进行切分"""
        print("测试文本切分")
        url = "http://10.92.142.185:8910/v1/textsplitter"

        separators = ["\n\n", "\n", ".", "！", "？", ",", ".", "?", "!", ",", " ", ""]
        body = {"text": text, "max_length": max_length, "separators": separators}
        response = requests.post(url, json=body)
        if response.status_code == 200:
            print(Fore.GREEN + "split text success")
        else:
            print(Fore.RED + "fail to split text")

        result = response.json()
        result = result["content"]
        print("切分长度:", max_length)
        print(Fore.YELLOW + "result length:", len(result))
        count = 0
        for item in result:
            count += 1
            print(Fore.YELLOW + "No.", count)
            print(Fore.YELLOW + "text:", item["text"])
            print(Fore.YELLOW + "length: Tokens", item["length"], "string", len(item["text"]))
            print()

    def chat(self, content,  model_name, system_prompt="you are a helpful assistant", stream=True):
        """与模型聊天"""
        print(f"开始测试聊天:\n{content}")
        BASE_URL = "http://10.92.142.185:8910/v1"  # For Genie
        client = OpenAI(base_url=BASE_URL, api_key="123")
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6, "penalty_last_n": 64,
                      "penalty_repeat": 1.3}

        if stream:
            response = client.chat.completions.create(model=model_name, stream=True, messages=messages,
                                                      extra_body=extra_body)

            for chunk in response:
                content = chunk.choices[0].delta.content
                if content is not None:
                    print(Fore.YELLOW + content, end="", flush=True)
        else:
            response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body)
            # print(response)
            print(Fore.YELLOW + response.choices[0].message.content)

    def toolcall(self, content,
                 system_prompt="You are a helpful assistant that can use tools to answer questions and perform tasks.",
                 stream=True, model_name="Qwen2.0-7B-SSD"):
        """工具调用"""
        print(f"开始测试工具调用:\n{content}")

        def search_files(query, maxResults):
            """Find and locate files or folders by name, such as searching for README.md file location on your computer."""

            file_info = {
                "file_path": "c:\\aaa.txt",
                "file_path2": "c:\\McpServer.py",
                "file_path3": "c:\\ccc.txt"
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
                    print("Exception", e)

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

        # init(autoreset=True)
        BASE_URL = "http://10.92.142.185:8910/v1"
        client = OpenAI(base_url=BASE_URL, api_key="123")
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6, "penalty_last_n": 64,
                      "penalty_repeat": 1.3}
        params = dict(model=model_name, stream=stream, messages=[{"role": "user", "content": content}])
        if stream:
            response = client.chat.completions.create(model=model_name, stream=True, messages=messages,
                                                      extra_body=extra_body,
                                                      tools=tools, tool_choice="auto")
            process_stream_response(response, params, available_functions)
        else:
            response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body,
                                                      tools=tools, tool_choice="auto")
            process_response(response, params, available_functions)


if __name__ == "__main__":
    test = Test()

    model_root = Path("./models")
    models = []

    for entry in model_root.iterdir():
        if not entry.is_dir():
            continue
        models.append(entry.name)
        print("get model name:", entry.name)

    text = """你是一位小说创作者,请根据以下故事内容和设定,生成接下来的剧情走向（50-100字）.
    
        #     要求:
        #     - 保持故事风格为「童话」
        #     - 剧情要自然衔接已有内容,并引入新的冲突、事件或角色发展
        #     - 语言生动,富有画面感
        #     - 输出内容应为小说中的下一段剧情,不要添加总结或评论
    
        #     【世界设定】:
        #     世界名称:幽暗星域
        #     时代背景:远古未来
        #     地理环境:由星系构成的宇宙,包含多种星云、星系和异次元裂缝
        #     社会结构:星际联邦,由多种文明组成,包括人类、机械生命体、能量生物等
        #     当前局势:星际联邦正面临未知星系的威胁,需要联合各方力量来保护宇宙和平
    
        #     【角色设定】:
        #     角色1:名字:艾瑞斯,身份:星际探险家,性格:勇敢、聪明、好奇心强,背景:从小在星际探险家家庭长大,对未知世界充满无限好奇,目标:寻找能平衡宇宙力量的神秘星系,保护宇宙和平
        #     角色2:名字:维拉,身份:机械生命体工程师,性格:冷静、有条理、技术精湛,背景:来自一个由机械生命体构成的星球,致力于研发能够抵御未知威胁的防御系统,目标:为星际联邦提供先进的防御技术,确保联盟安全
    
        # """

    split_lengths = [16, 64, 128, 512, 1024]

    queries = [
        "上海有哪些美食", "北京有哪些美食", "天津有哪些美食", "西安有哪些美食",
        "我已经问了几个问题", "深圳有哪些美食", "成都有哪些美食", "云南有哪些美食",
        "南昌有哪些美食", "长沙有哪些美食", "武汉有哪些美食", "合肥有哪些美食",
        "杭州有哪些美食", "我已经问了几个问题", "无锡有哪些美食", "苏州有哪些美食",
        "南通有哪些美食", "宁波有哪些美食", "内蒙古有哪些美食", "新疆有哪些美食",
    ]

    for m in models:
        # test.startservice(model_name=m)
        # for L in split_lengths:
        #     test.textsplit(text=text, max_length=L)
        for q in queries:
            test.chat(q, m)
        # test.reloadmessage(["北京旅游攻略","北京,作为中国的首都,拥有丰富的历史和文化资源.以下是一份简短的北京旅游攻略:1. **故宫**:必去的景点,了解中国悠久的历史和文化.2. **长城**:位于北京附近的长城是世界文化遗产,体验“不到长城非好汉”.3.**天安门广场**:参观国家博物馆和人民英雄纪念碑,感受国家的庄严肃穆.4. **颐和园**:感受皇家园林的美景,包括长廊、佛香阁等.5. **南锣鼓巷**:体验北京的市井文化,品尝地道小吃,购买特色手工艺品.6. **798艺术区**:欣赏现代艺术,感受北京的创意和时尚.7. **什刹海**:游览四合院,体验老北京的生活,或在湖边享受悠闲时光.别忘了,北京的四合院、胡同、老街和小吃也是体验当地文化的好去处.提前规划好行程,合理安排时间,以充分体验北京的魅力."])
        # test.chat("当地有哪些美食")
        # test.chat("当地面积多大")
        # test.chat("当地气候如何")
        # test.chat("当地是否宜居")
        # test.chat("当地人均收入")
        # test.chat("当地有哪些景点")
        # test.clearmessage()
        # test.getcontextsize()
        # test.stopoutput()
        test.stopservice()
