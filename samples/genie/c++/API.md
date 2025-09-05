# GenieAPIService API <br>


## Parameters: <br>
-c, --config_file: Path to the config file.<br>
-m, --model_name: Name of the model to use.<br>
--adapter: if using lora model, set the adapter of lora.<br>
--lora_aplha: if using lora model, set lora value weight.<br>
-l, --load_model: if load model.<br>
-a, --all_text: Output all text includes tool calls text. Disabled by default.<br>
-t, --enable_thinking: Enable thinking model. Disabled by default.<br>
-v, --version: Print version info and exit.<br>
-n, --num_response: The number of dialogue turns saved in the history record. If you do not need to enable the historical context feature, please set this value to 0.<br>
-o, --min_output_num: The number of tokens reserved for output.<br>
-d, --loglevel: log level setting for record.<br>
-f, --logfile: log file path, it's a option.<br>
-p, --port: Port used for running.<br>

Note: Please note that the input length must not exceed the maximum number of tokens reserved for the input, which means it cannot exceed the model's maximum context length minus the value set for '--min_output_num'. You can invoke the Text Splitter to send the input text to the server for segmentation, and then sequentially pass the split segments to the LLM to complete the question-answering process.<br>
Note: It is recommended to disable thinking mode when using the tools call function.<br>
Note: You can refer to [GenieAPIClientTools.py](../python/GenieAPIClientTools.py) on how to use tools call.<br>

```
GenieAPIService\GenieAPIService.exe -c "genie\python\models\Qwen3.0-8B-v31\config.json" -l --all_text --enable_thinking --num_response 10 --min_output_num 1024 -p 8096
```

## Text Splitter
This function can divide a long text into multiple paragraphs according to the priority order of the specified delimiter and the maximum length of each paragraph. Length is counted by token number instead of text length. You can also use this function to calculate the token number of text. <br>
You can get the sample code on how to use Text Splitter 

```
import argparse
from openai import OpenAI
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="Qwen2.0-7B-SSD", type=str)  
args = parser.parse_args()

url = "http://localhost:8910/v1/textsplitter"
text = ""   # Please enter the text to be split.
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 128, "separators": separators}
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
```

## Terminate output
This function is used to terminate the model's current output.<br>
You can get the sample code on how to use this function.
```
import requests
url = "http://localhost:8910/stop"
params = {"text": "stop"}  
response = requests.post(url, json=params)
```

## Clear the history of conversation records.
When you use the --num_response parameter to enable the record history dialogue function, you can call this function when you need to clear the historical dialogue.<br>
You can get the sample code on how to use this function.
```
import requests

url = "http://localhost:8910/clear"
params = {"text": "clear"}
response = requests.post(url, json=params)

```

## Reload the history of conversation records.
When you enable the record history conversation feature using the --num_response parameter, you can call this function to upload the history from local to the server. You must put the historical conversation records into 'history', and for each conversation, there needs to be a 'role' and 'content.' For multiple records, they should be placed sequentially in the message array.
You can get the sample code on how to reload the history of conversation records.<br>
```
import requests
url = "http://localhost:8910/reload"
history_data = {
    "action": "import_history",
    "history": [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
    ]
}

response = requests.post(url, json=history_data)

```

## Get modelname list
get the name of the model that can be loaded.<br>
```
import requests
BASE_URL = "http://localhost:8910/models"
response = requests.get(BASE_URL)
modelname = []
datas = response.json()["data"]
for data in datas:
    modelname.append(data["id"])
return modelname
```

## Get model profile
Obtain the performance information of the model.<br>
```
import requests
BASE_URL = "http://localhost:8910/profile"
response = requests.get(BASE_URL)
return response
```

## Stop service
Terminate the server process.<br>
```
import requests
print("开始测试终止服务:")
url = "http://localhost:8910/servicestop"
# 如果需要传递参数,使用 params
params = {"text": "stop"}  # 这会变成 ?text=stop
response = requests.post(url, json=params)
if response.status_code == 200:
    print(Fore.GREEN + "stop service success\n")
else:
    print(Fore.RED + "fail to stop sevice\n")
```

## Get model context size
Enter the model name to obtain the maximum context length of that model.<br>
```
url = "http://localhost:8910/contextsize"
params = {"modelName": model_name}  #Llama2.0-7B-SSD
response = requests.post(url, json=params)
if response.status_code == 200:
    result = response.json()
    print("context大小:",result["contextsize"])  
    return gr.update(maximum=result["contextsize"], value=result["contextsize"])
```
