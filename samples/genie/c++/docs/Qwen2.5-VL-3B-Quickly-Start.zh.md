# Qwen2.5-VL-3B 模型端侧部署

## 第一部分：在 Windows 平台上使用

本节说明如何在 Windows 环境中配置并运行 Qwen2.5-VL-3B 模型。

### 1.1 资源下载与准备

- **下载模型文件**：<br>
  从 [Qwen2.5-VL-3B](https://www.aidevhome.com/?id=51) 下载对应平台的模型文件，并放置到
  `qai-appbuilder\samples\genie\python\models` 目录中。

- **下载 Genie 服务程序**：<br>
  前往 GitHub Releases 页面，下载与您的 NPU 架构相匹配的 `GenieAPIService_<version>_QAIRT_<qairt_version>_<stub>.zip` 软件包
  （确切的版本号会随每次发布而变化，因此本指南特意不写死具体版本号 — 请查看 [Releases](https://github.com/qualcomm/qai-appbuilder/releases) 页面获取
  最新的构建版本）。

- **解压文件**：<br>
  将下载的压缩包解压到项目代码目录 `qai-appbuilder\samples` 中。

### 1.2 启动服务并运行示例

打开终端，进入 samples 目录，分别运行服务和客户端命令。

```
# 1. Entry the directory
cd qai-appbuilder\samples

# 2. Start the GenieAPI Service (loading the config file)
GenieAPIService\GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l
 [W] load successfully! use second: 4.56947
 [W] Model load successfully: qwen2.5vl3b
 [W] GenieService::setupHttpServer start
 [W] GenieService::setupHttpServer end
 [A] [OK] Genie API Service IS Running.
 [A] [OK] Genie API Service -> http://0.0.0.0:8910

# 3. running the client for test(ensure test.png is existed in the current directory)
GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

## 第二部分：在 Android 平台上使用

### 2.1 资源下载与准备

- **下载模型文件**：<br>
  与 Windows 平台相同，从 [Qwen2.5-VL-3B](https://www.aidevhome.com/?id=51) 下载对应平台的模型，
  并放置到设备的 `/sdcard/GenieModels/` 目录中。


- **下载并安装 APK**：<br>
  从 [GitHub Releases](https://github.com/qualcomm/qai-appbuilder/releases) 下载最新的 `GenieAPIService.apk`
  并安装到 Android 设备上。

### 2.2 示例应用的编译与运行

Android 示例应用源码位于项目目录中，需自行编译。

- **源代码路径**：
  `samples\android\GenieChat`


- **操作说明**：<br>
  用 Android Studio 打开此目录，编译并安装到设备上，然后配合已安装的
  `GenieAPIService` 使用。

### 2.3 示例应用截图

**Geniechat** 中的示例

![img.png](img/6.png)

![img.png](img/5.png)

## 第三部分：Python 调用指南

无论 GenieAPIService.exe 运行在 Windows 上，还是 GenieAPIService.apk 运行在 Android 上，服务启动成功后都会显示
一个 IP 地址和端口（例如 127.0.0.1:8910，或手机的 IP）。通过 OpenAI 兼容接口，用 Python 调用该服务。

### 3.1 资源下载与准备

安装 `openai` 库：

```pip install openai```

### 3.2 Python 调用代码（vl_client.py）

创建 Python 脚本（例如 vl_client.py），复制以下代码，并根据实际环境修改 IP 地址。

```
import argparse
import base64
import requests # 新增：用于从网络下载图片
import os
from openai import OpenAI

IP_ADDR = "127.0.0.1:8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Describe this image", type=str)
parser.add_argument("--image", required=True, type=str, help="Path to local image file or Image URL")
args = parser.parse_args()

# 1. 修改后的辅助函数：支持本地路径和 URL
def encode_image(image_input):
    # 判断是否为 URL
    if image_input.startswith(('http://', 'https://')):
        try:
            print(f"Downloading image from URL: {image_input}...")
            response = requests.get(image_input, timeout=10)
            response.raise_for_status() # 检查请求是否成功
            # 直接将下载的二进制内容转换为 Base64
            return base64.b64encode(response.content).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to download image from URL: {e}")
   
    # 否则将其视为本地文件
    else:
        try:
            if not os.path.exists(image_input):
                raise FileNotFoundError(f"Local file not found: {image_input}")
           
            with open(image_input, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to load local image: {e}")

# 获取 Base64 编码后的图片
try:
    base64_image = encode_image(args.image)
except Exception as e:
    print(f"Error: {e}")
    exit(1)

client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

# 构造 Genie 服务（VL 模型）所需的特殊消息结构
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": {
            "question": args.prompt,  
            "image": base64_image     # 无论来源是 URL 还是本地文件，此处都统一以 Base64 形式发送给服务器
        }
    }
]

extra_body = {
    "size": 4096,
    "temp": 1.5,
    "top_k": 13,
    "top_p": 0.6,
    "messages": custom_messages
}

model_name = "qwen2.5vl3b-8380-2.42"  # 替换为您实际部署的模型名称（即加载/注册时使用的
                                       # "name"）；此处的字符串仅为示例，
                                       # 不保证与您自己部署环境中的某个模型目录相匹配。
placeholder_msgs = [{"role": "user", "content": "placeholder"}]

# 发送请求
try:
    if args.stream:
        response = client.chat.completions.create(
            model=model_name,
            stream=True,
            messages=placeholder_msgs,
            extra_body=extra_body
        )

        for chunk in response:
            if chunk.choices:
                content = chunk.choices[0].delta.content
                if content is not None:
                    print(content, end="", flush=True)
        print() # 换行
    else:
        response = client.chat.completions.create(
            model=model_name,
            messages=placeholder_msgs,
            extra_body=extra_body
        )
        if response.choices:
            print(response.choices[0].message.content)

except Exception as e:
    print(f"\n请求失败: {e}")

```

### 3.3 运行脚本

在命令行中运行该脚本，指定图片路径和（可选的）提示词：

```
python vl_client.py --image test.png --prompt "what is image descript?" --stream
python vl_client.py --image "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png" --prompt "What implies in this logo?"
```

### 3.4 两种支持的消息格式

上面的脚本使用了扁平化的非标准 `content` 对象 — `{"question": ..., "image": base64_image}`。服务器仍支持
此格式（通过 `ModelInputBuilder::ProcessObject` 解析），这是为向后兼容保留的 GenieAPIClient 风格简写。

服务器还支持（并且对于新的集成方式，推荐使用）**标准的 OpenAI `content` 数组格式**
（通过 `ModelInputBuilder::ProcessArray` 解析），该格式与现成的 OpenAI 客户端
库和工具具有更好的兼容性：

```python
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": args.prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64_image}}
        ]
    }
]
```

这两种格式服务器都同时支持 — 您可以根据自己的集成需求选择更方便的一种。
完整的格式参考（包括音频 (`input_audio`) 支持）请参见 [API.md](API.zh.md) 中的 "Chat Completions" 章节。
