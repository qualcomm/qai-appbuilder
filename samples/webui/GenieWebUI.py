# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import time
import sys
import os
import threading
import shutil
import re
import glob
import requests
import numpy as np
from PIL import Image
import gradio as gr
from gradio import ChatMessage
from gradio.data_classes import FileData
from chat import Chat
from Docutils import ManualDocSummarizer
import base64

os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

# ===================================================================
# 🌐 前端 UI 层（仅保留结构，后端调用将通过 HTTP）
# 所有实际逻辑用 pass 替代，留出接口接入点
# ===================================================================
service = Chat()
docx_summary = ManualDocSummarizer()

current_model = ""
HOST = "0.0.0.0"
#HOST = "127.0.0.1"
PORT = 50000
FILE_PATH = "files"
TRUSTED_OUTPUT_DIR="images"
AUDIO_TYPES = ['.wav']
FILE_TYPES = [".pdf", ".docx", ".pptx", ".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp"]
IMG_TYPES = [".png", ".jpg", "jpeg"]
FUNC_LIST = ["📐 解题答疑", "📚 文档总结", "🗛 AI 翻 译", "🌐 AI 搜 索", "✒️ 帮我写作", "🎨 图像生成", "🍸 定制功能", "✈️ 旅游规划"]
FUNC_LIST_EN = ["📐 Q & A", "📚 Doc Summary", "🗛 AI Translation", "🌐 AI Searching", "✒️ Writing Assistant", "🎨 Text To Image", "🍸 Customerized Function", "✈️ Tourism planning"]

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    END = '\033[0m'


###########################################################################

css="""
body{
	display:flex;
}

::-webkit-scrollbar {
   width:18px;
   height:18px
}
::-webkit-scrollbar-thumb {
   border-radius:10px;
   background-color:rgba(55, 109, 151, .18);
   min-height:30px;
   border:5px solid transparent;
   background-clip:content-box
}
::-webkit-scrollbar-thumb:hover {
   background-color:rgba(55, 109, 151, .3)
}

footer{display:none !important}
"""

###########################################################################

# ===================================================================
# 🧩 全局变量（仅用于前端状态管理）
# ===================================================================
_func_mode = 0
_question = ""
_sys_prompt = None
chat_history = []  # 可用于临时缓存（实际应由后端维护）
def on_model_selected(model_name):
    """
    当用户选择模型时触发
    model_name: 用户选择的模型名称
    """
    print(f"Model Selected: {model_name}")
    # 在这里保存到全局变量，或传递给其他函数
    global current_model
    current_model = model_name
    return   # 可以返回值更新其他组件
def update_max_contextsize(model_name):
    url = "http://127.0.0.1:8910/contextsize"
    params = {"modelName": model_name}  #Llama2.0-7B-SSD
    response = requests.post(url, json=params)
    if response.status_code == 200:
        result = response.json()
        print("context大小:",result["contextsize"])
        return gr.update(maximum=result["contextsize"], value=result["contextsize"])

def get_mock_profile():
    return "⏱️ 首 Token: 1.2s | 📥 输入速度: 120 tok/s | 📤 输出速度: 45 tok/s"

def stop_generation():
    """中断生成的函数（绑定到 cancel_btn）"""
    service.stopoutput()
    print("✅ The user clicked the pause button and is interrupting the generation...")

def encode_image(image_input):
    if image_input == "":
        return ""
    try:
        if not os.path.exists(image_input):
            raise FileNotFoundError(f"Local file not found: {image_input}")

        with open(image_input, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
            raise Exception(f"Failed to load local image: {e}")

def chat(chatbot, max_length, temp, top_k, top_p, system_prompt=None):
    """对话"""
    if current_model == "":
        gr.Warning("Please select a model first.", duration =5)
        yield chatbot, "", "", ""
        return

    if _question == "":
        gr.Warning("The questions can not be empty.", duration =5)
        yield chatbot, "", "", ""
        return

    answer = ""
    encoded_img = encode_image(_chat_img)
    encoded_audio = encode_image(_chat_audio)
    print('model select: ', current_model)
    for chunk in service.chat({"question": _question, "image": encoded_img, "audio": encoded_audio}, system_prompt=system_prompt, max_length=max_length, temp=temp, top_k=top_k, top_p=top_p, model_name=current_model):
        answer += chunk
        chatbot[-1].content = answer
        yield chatbot, "", "", ""

    profile = service.getchatprofile()
    if profile.status_code == 200:
        # 将响应内容解析为JSON格式
        data = profile.json()

        yield chatbot, round(float(data["time_to_first_token"]), 2), round(float(data["prompt_processing_rate"]),2), round(float(data["token_generation_rate"]), 2)

def summarize_document(chatbot, max_length, temp, top_k, top_p):
    """文档总结（上传文件后触发）"""
    yield chatbot, "", "", ""
    time.sleep(1)
    chatbot.append(ChatMessage(role="assistant", content="Parsing document ..."))
    yield chatbot, "", "", ""
    try:
        docx_summary.load_and_split_docs(file_path=FILE_PATH)
        answer = ""
        for chunk in docx_summary.summarize_map_reduce(custom_prompt="你的工作是用中文写出以下文档的摘要:"):
            answer += chunk
            chatbot[-1].content = answer
            yield chatbot, "", "", ""
    except Exception as e:
        print(f"Exception: {e}")
        yield chatbot, "", "", ""

def translate_text(chatbot, max_length, temp, top_k, top_p):
    """翻译功能"""
    if has_chinese(_question):
        system_prompt = "You are a translation expert, please translate the following sentence into English."
    else:
        system_prompt = "你是一个翻译专家，请将下面的句子翻译成中文"
    answer = ""
    print('chatbot: ', chatbot)
    for chunk in service.chat(_question, system_prompt=system_prompt, max_length=max_length, temp=temp, top_k=top_k, top_p=top_p):
        answer += chunk
        chatbot[-1].content = answer
        yield chatbot, "", "", ""
    profile = service.getchatprofile()

    if profile and profile.status_code == 200:
        # 将响应内容解析为JSON格式
        data = profile.json()

        yield chatbot, data["time_to_first_token"], data["prompt_processing_rate"], data["token_generation_rate"]

def generate_image(chatbot, max_length, temp, top_k, top_p):
    """图像生成"""
    chatbot.append(ChatMessage(role="assistant", content="正在生成图像..."))
    yield chatbot, "", "", ""

    image_path = service.imagegenerate(_question)
    if image_path != "":
        print('image path: ', image_path)

        img = Image.open(image_path)
        img.verify()  # 验证是否是有效图像
        img = Image.open(image_path)  # 重新打开用于保存
        filename = os.path.basename(image_path)

        trusted_path = os.path.join(TRUSTED_OUTPUT_DIR, filename)
        img.save(trusted_path, quality=95, optimize=True)

        chatbot[-1].content = gr.Image(trusted_path)
        yield chatbot, "", "", ""
    else:
        chatbot[-1].content = "服务暂不支持"
        yield chatbot, "", "", ""


# ===================================================================
# 🛠️ 工具函数（保留）
# ===================================================================
def has_chinese(text):
    pattern = re.compile(r'[\u4e00-\u9fff]')
    return pattern.search(text) is not None

def add_media(chatbot, chatmsg):
    """处理用户输入：文本 + 文件上传"""
    global _question, _func_mode, _chat_img, _chat_audio

    _chat_img = ""
    _chat_audio = ""
    _question = chatmsg["text"] or ""
    if _question:
        chatbot.append(ChatMessage(role="user", content=_question))

    # 处理文件上传
    if os.path.exists(FILE_PATH):
        shutil.rmtree(FILE_PATH)
    os.makedirs(FILE_PATH, exist_ok=True)

    for file in chatmsg["files"]:
        file_name = os.path.basename(file)
        shutil.copy(file, os.path.join(FILE_PATH, file_name))
        file_path = os.path.join(FILE_PATH, file_name)
        # if not _chat_img:
        #     _chat_img = file_path

        # 检查文件扩展名，如果是图片则直接显示
        file_ext = os.path.splitext(file_name)[1].lower()
        if file_ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']:
            # 图片文件：直接显示图片
            chatbot.append(ChatMessage(
                role="user",
                content=gr.Image(file_path)
            ))
            _chat_img = file_path
        elif file_ext in AUDIO_TYPES:
            # 音频文件：显示音频播放器
            chatbot.append(ChatMessage(
                role="user",
                content=gr.Audio(file_path)
            ))
            _chat_audio = file_path
        else:
            # 非图片文件：显示文件链接
            chatbot.append(ChatMessage(
                role="user",
                content=f"<small><a href='/gradio_api/file={file_path}' target='_blank'>{file_name}</a></small>"
            ))

    return chatbot, gr.MultimodalTextbox(value=None, interactive=False, submit_btn=False, stop_btn=True)

def vote(data: gr.LikeData):
    if data.liked:
        print("Upvoted:", data.value)
    else:
        print("Downvoted:", data.value)

def reset_state():
    return [], [], "", "", ""

###################
 #FR0001:Add customized prompt
def update_text(value):
    global _sys_prompt
    _sys_prompt=value
    # print("input:", _sys_prompt)
    with open("customprompt.txt", "w",encoding="utf-8" ) as file:
        file.write(value)

    return None


# ===================================================================
# 🖼️ UI 构建（完全保留原始结构）
# ===================================================================
def main():
    #FR0001:Add customized prompt
    global _sys_prompt
    file_name="customprompt.txt"
    #FR0001:Add customized prompt

    # 模拟模型列表（实际可从 /api/models 获取）
    model_list = service.getmodellist()

    with gr.Blocks(css=css, theme='davehornik/Tealy', fill_width=True, fill_height=True) as demo:
        demo.title = "Genie App"

        gr.set_static_paths(paths=["resources/", "files/"])
        gr.HTML("""<div align="center"><div style="width:500px"><font size="6" style="color:rgb(255, 255, 234);">Genie App</font></div></div>""")

        with gr.Tab("Settings"):
            with gr.Row():
                with gr.Column(scale=2):
                    with gr.Group():
                        model_select = gr.Dropdown(choices=model_list, value="", label="models", interactive=True)
                        max_length = gr.Slider(1, 4096, value=4096, step=1, label="max length", interactive=True)
                        temp = gr.Slider(0.01, 1, value=0.8, step=0.01, label="temperature", interactive=True)
                        top_k = gr.Slider(1, 100, value=40, step=1, label="Top K", interactive=True)
                        top_p = gr.Slider(0, 1, value=0.95, step=0.01, label="Top P", interactive=True)
                    with gr.Group():
                        f_latency = gr.Textbox(label="First Latency", value="N/A")
                        p_speed = gr.Textbox(label="Prompt Speed", value="N/A")
                        e_speed = gr.Textbox(label="Eval Speed", value="N/A")
                        #FR0001:Add customized prompt
                        cust_prompt = gr.Textbox(label="Customer Prompt", value="", visible=True, interactive=True)

                with gr.Column(scale=8):
                    chatbot = gr.Chatbot(scale=9, type='messages', show_copy_button=True, group_consecutive_messages=True, height="52vh",)
                    chatbot.like(vote, None, None)

                    chatmsg = gr.MultimodalTextbox(scale=1, interactive=True, file_count="multiple", placeholder="Enter message or upload file...", show_label=True, autofocus=True,
                                                   max_plain_text_length=3000, file_types=IMG_TYPES + AUDIO_TYPES, label=FUNC_LIST_EN[_func_mode])

                    with gr.Row():
                        func_1_btn = gr.Button(FUNC_LIST_EN[0], elem_classes="button_cls")
                        func_2_btn = gr.Button(FUNC_LIST_EN[1], elem_classes="button_cls")
                        func_3_btn = gr.Button(FUNC_LIST_EN[2], elem_classes="button_cls")
                        func_6_btn = gr.Button(FUNC_LIST_EN[5], elem_classes="button_cls")
                        #FR0001:Add customized prompt
                        func_7_btn = gr.Button(FUNC_LIST_EN[6], elem_classes="button_cls")

                    gr.Examples(["Summarize the document content",  "Analyze the source code and give line-by-line comments.", "Inquire about the weather in Shanghai today", "Help me check the following English grammar..."], chatmsg, label="Quick Input")

        # ===================================================================
        # 🔗 事件绑定（保留结构，逻辑由 predict 分发）
        # ===================================================================
        def predict(chatbot, max_length, temp, top_k, top_p):
            global _question, _func_mode, _sys_prompt

            if not _question.strip() and _func_mode != 1:  # 文档总结除外
                yield chatbot, "", "", ""
                return

            chatbot.append(ChatMessage(role="assistant", content=""))

            if FUNC_LIST[_func_mode] == "📐 解题答疑":
                for chunk in chat(chatbot, max_length, temp, top_k, top_p):
                    yield chunk
                chatbot.append(ChatMessage(role="assistant", content=""))

            elif FUNC_LIST[_func_mode] == "📚 文档总结":
                for chunk in summarize_document(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🗛 AI 翻 译":
                for chunk in translate_text(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🎨 图像生成":
                for chunk in generate_image(chatbot, max_length, temp, top_k, top_p):
                    yield chunk

            elif FUNC_LIST[_func_mode] == "🍸 定制功能":
                for chunk in chat(chatbot, max_length, temp, top_k, top_p, _sys_prompt):
                    yield chunk
                chatbot.append(ChatMessage(role="assistant", content=""))

            else:
                chatbot[-1].content = "该功能正在开发中..."
                yield chatbot, "", "", ""

        # 事件链：输入 → add_media → predict → 恢复输入框
        chat_run = chatmsg.submit(add_media, [chatbot, chatmsg], [chatbot, chatmsg])
        chat_run = chat_run.then(predict, [chatbot, max_length, temp, top_k, top_p], [chatbot, f_latency, p_speed, e_speed])
        chat_run.then(lambda: gr.MultimodalTextbox(interactive=True, submit_btn=True, stop_btn=False), None, chatmsg)
        chatmsg.stop(fn=stop_generation)

        #FR0001:Add customized prompt
        if not os.path.exists(file_name):
            with open(file_name, "w",encoding="utf-8") as file:
                file.write("")  # Create an empty file

        with open(file_name, "r",encoding="utf-8") as file:
            cust_prompt.value = file.read()

        cust_prompt.change(update_text, inputs=cust_prompt, outputs=None)
        _sys_prompt = cust_prompt.value
        #FR0001:Add customized prompt

        # 功能按钮切换
        def func_change(func_mode):
            global _func_mode
            global _sys_prompt

            _sys_prompt = cust_prompt.value
            # print("\nchange:sys prompt:", _sys_prompt)

            _func_mode = func_mode
            func_name = FUNC_LIST[func_mode]
            if func_name == "📚 文档总结":
                return gr.MultimodalTextbox(label=func_name, sources=["upload"], file_types=FILE_TYPES)
            elif func_name == "📐 解题答疑":
                return gr.MultimodalTextbox(label=func_name, sources=["upload"], file_types=IMG_TYPES + AUDIO_TYPES)
            else:
                return gr.MultimodalTextbox(label=func_name, sources=[])

        func_1_btn.click(lambda: func_change(0), None, chatmsg)
        func_2_btn.click(lambda: func_change(1), None, chatmsg)
        func_3_btn.click(lambda: func_change(2), None, chatmsg)
        func_6_btn.click(lambda: func_change(5), None, chatmsg)
        #FR0001:Add customized prompt
        func_7_btn.click(lambda: func_change(6), None, [chatmsg])

        # 模型切换（未来可触发 /api/load-model）
        model_select.change(
            on_model_selected,   # 原来的回调（比如加载模型）
            model_select,
            None
        ).then(
            update_max_contextsize,   # 新增：更新 max_length 的范围和值
            model_select,
            max_length
        )

    demo.queue().launch(server_name=HOST, share=False, inbrowser=True, server_port=PORT)

if __name__ == '__main__':

    main()
