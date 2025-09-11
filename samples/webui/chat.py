import requests
from openai import OpenAI
from PIL import Image
import subprocess
import sys
import os
import subprocess
import json
import base64
import time
from colorama import init, Fore
from PIL import Image

import io

# 强制 stdout 使用 UTF-8 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
class Chat():
    def __init__(self) -> None:
        init(autoreset=True)
    def startservice(self, model_name = "Qwen2.0-7B-SSD"):
        """启动服务,并在新终端窗口中运行"""
        print(" 开始测试启动服务:")
        print(f"Loading model: {model_name}")
        # 使用 cmd /c start 来打开新窗口
        command = [
            "cmd", "/c", "start", "Genie API Service",  # 窗口标题为 "Genie API Service"
            "GenieAPIService.exe",
            # "-m", model_name,
            "-c", f"genie/python/models/{model_name}/config.json",
            "-l",
            "-p", "8910"
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
        url = "http://localhost:8910/servicestop"
        # 如果需要传递参数,使用 params
        params = {"text": "stop"}  # 这会变成 ?text=stop
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "stop service success\n")
        else:
            print(Fore.RED + "fail to stop sevice\n")

    def clearmessage(self):
        """清除保存的历史信息"""
        print("测试清除历史记录: ")
        url = "http://localhost:8910/clear"
        params = {"text": "clear"}  # 这会变成 ?text=stop
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "clear message success\n")
        else:
            print(Fore.RED + "fail to clear message\n")     

    def reloadmessage(self, message: list[str]):
        """加载从客户端传递的历史信息"""
        print("开始测试加载历史记录: ")
        url = "http://localhost:8910/reload"
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
            "history": [
                {"role": "user", "content": "北京旅游攻略"},
                {"role": "assistant", "content": "北京,作为中国的首都,拥有丰富的历史和文化资源.以下是一份简短的北京旅游攻略:1. **故宫**:必去的景点,了解中国悠久的历史和文化.2. **长城**:位于北京附近的长城是世界文化遗产,体验“不到长城非好汉”.3.**天安门广场**:参观国家博物馆和人民英雄纪念碑,感受国家的庄严肃穆.4. **颐和园**:感受皇家园林的美景,包括长廊、佛香阁等.5. **南锣鼓巷**:体验北京的市井文化,品尝地道小吃,购买特色手工艺品.6. **798艺术区**:欣赏现代艺术,感受北京的创意和时尚.7. **什刹海**:游览四合院,体验老北京的生活,或在湖边享受悠闲时光.别忘了,北京的四合院、胡同、老街和小吃也是体验当地文化的好去处.提前规划好行程,合理安排时间,以充分体验北京的魅力."}
            ]
        }

        response = requests.post(url, json=history_data)
        if response.status_code == 200:
            print(Fore.GREEN + "reload message success\n")
        else:
            print(Fore.RED + "fail to reload message\n")    
    
    def stopoutput(self):
        """终止当前模型输出"""
        print("开始测试终止模型输出:")
        url = "http://localhost:8910/stop"
        # 如果需要传递参数,使用 params
        params = {"text": "stop"}  # 这会变成 ?text=stop
        response = requests.post(url, json=params)
        if response.status_code == 200:
            print(Fore.GREEN + "stop output success\n")
        else:
            print(Fore.RED + "fail to stop output\n")

    def textsplit(self, text: str, max_length: int = 1024):
        """对文本进行切分"""
        print("测试文本切分")
        url = "http://localhost:8910/v1/textsplitter"

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
        return result

    def chat(self, content, system_prompt="you are a helpful assistant", model_name="Qwen2.0-7B-SSD", stream=True, max_length=4096, temp=1.5, top_k=13, top_p=0.6):
        """与模型聊天"""
        BASE_URL = "http://localhost:8910/v1"   # For Genie
        client = OpenAI(base_url=BASE_URL, api_key="123")
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6, "penalty_last_n": 64, "penalty_repeat": 1.3}

        # content = content
        # messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        # extra_body = {"size": max_length, "seed": 146, "temp": temp, "top_k": top_k, "top_p": top_p, "penalty_last_n": 64, "penalty_repeat": 1.3}
        if stream:
            response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body)

            for chunk in response:
                content = chunk.choices[0].delta.content
                if content is not None:
                    yield content
                    # print(Fore.YELLOW + content, end="", flush=True)
        else:
            response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body)
            yield response.choices[0].message.content
            # print(response)
            # print(Fore.YELLOW + response.choices[0].message.content)

    def getchatprofile(self):
        BASE_URL = "http://localhost:8910/profile"
        response = requests.get(BASE_URL)
        return response
    
    def getmodellist(self):
        BASE_URL = "http://localhost:8910/models"
        response = requests.get(BASE_URL)
        modelname = []
        datas = response.json()["data"]
        for data in datas:
            modelname.append(data["id"])
        return modelname

    def toolcall(self, content, system_prompt="You are a helpful assistant that can use tools to answer questions and perform tasks.", stream=True, model_name="Qwen2.0-7B-SSD"):
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


        # init(autoreset=True)
        BASE_URL = "http://localhost:8910/v1"
        client = OpenAI(base_url=BASE_URL, api_key="123")
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]
        extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6, "penalty_last_n": 64, "penalty_repeat": 1.3}
        params = dict(model=model_name, stream=stream, messages=[{"role": "user", "content": content}])
        if stream:
            response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body,
                                                    tools=tools, tool_choice="auto")
            process_stream_response(response, params, available_functions)
        else:
            response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body,
                                                    tools=tools, tool_choice="auto")
            process_response(response, params, available_functions)

    def imagegenerate(self, prompt, negative_prompt="", seed=42, step=20, guidance_scale=7.5, size="512x512"):

        server_url = "http://127.0.0.1:8910/images/generations"
        payload = {
            "model":"Stable Diffusion 3",
            "prompt":prompt,
            "negative_prompt":negative_prompt,
            "seed":seed,
            "step":step,
            "guidance_scale":guidance_scale,
            "size":size,
            "n":1,
            "response_format":"url",     
        }

        # 发送POST请求
        response = requests.post(server_url, json=payload)
        # 确认请求成功
        if response.status_code == 200:
            response_data = response.json()
            
            # 如果选择了"url"作为response_format，则直接获取URL
            if payload['response_format'] == 'url':
                image_url = response_data['data'][0]['url']
                # image_url = os.path.abspath("."+image_url) 
                print(f"图像已生成，可从以下URL查看: {image_url}")
                return image_url
                
            # 如果选择了"b64_json"作为response_format，则需要解码Base64字符串
            elif payload['response_format'] == 'b64_json':
                import base64
                from io import BytesIO
                from PIL import Image
                
                image_b64 = response_data['data'][0]['b64_json']
                image_data = base64.b64decode(image_b64)
                image = Image.open(BytesIO(image_data))
                
                # 展示图像
                image.show()
        elif response.status_code == 501:
            print(f"请求失败, 暂不支持")
            return ""
        else:
            print(f"请求失败，状态码: {response.status_code}, 错误信息: {response.text}")
if __name__ == "__main__":
    test = Chat()
    # test.imagegenerate(prompt="a beautiful gril")
    # for chunk in test.chat(content="how to go to beijing"):
    #     print(chunk)

    test.getmodellist()