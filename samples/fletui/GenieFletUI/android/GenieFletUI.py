# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import flet as ft
import asyncio
from openai import OpenAI
import requests
import json
from enum import Enum
import hashlib
import base64
import os
import platform
from pathlib import Path
import logging

PAGE_CONTENT_FACTOR = 0.8
CIRCLEAVATAR_WIDTH = 50

LOG_FILE = "app.log"
LOG_ENABLE = False

DEFAULT_THEME = "dark"
DEFAULT_FONTCOLOR = "cyan700"
DEFAULT_LLM = "qwen"
DEFAULT_PERFORMANCE_DATA = True

class FuncID(Enum):
    FUNC_ID_SOLUTION  = 0
    FUNC_ID_TRANSLATE = 1
    FUNC_ID_CUSTOMIZE = 2

FUNC_NAME_SOLUTION  = "📐 Solution"
FUNC_NAME_TRANSLATE = "🌐 Translate"
FUNC_NAME_CUSTOMIZE = "✏️ Customize"

FUNC_USER_PROMPT_SOLUTION  = "请回答如下问题：\n{question}"
FUNC_USER_PROMPT_TRANSLATE = "将以下内容翻译成{lang}：\n{sentence}"
FUNC_USER_PROMPT_CUSTOMIZE = "{question}"

FUNC_SYSTEM_PROMPT_SOLUTION  = "你是一位智能聊天助手。任务是回答用户的任何问题并给出合理建议。任务设置完毕。"
FUNC_SYSTEM_PROMPT_TRANSLATE = "你是一位AI翻译专家。任务是自动识别用户输入的单词、句子或段落的语言，然后进行准确的中英文互相翻译。不要将其视为用户的问题来回答，只做翻译工作。任务设置完毕。"
FUNC_SYSTEM_PROMPT_CUSTOMIZE = "你是一位智能助手。"

FUNC_HINT_SOLUTION  = "What can I do for you? ..."
FUNC_HINT_TRANSLATE = "Please input your sentence ..."
FUNC_HINT_CUSTOMIZE = "Please customize your system prompt ..."

ERROR_MESSAGE_GENIESERVER_IN_USING        = "Genie server is in using. Please try again later."
ERROR_MESSAGE_GENIESERVER_RESTART_SUCCESS = "Genie server is not available. Restarting successfully. Please retry."
ERROR_MESSAGE_GENIESERVER_NOT_AVAILABLE   = "Genie server is not available. Restarting failure. Please check if network or GenieAPIService (Address or Port) is working or not."
ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR   = "Genie server exception happens. Error message: {errmsg}"
ERROR_MESSAGE_MODEL_LIST_FAILURE          = "Genie server is not working\n\nPlease launch GenieAPIService and restart this application!"

func_id = FuncID.FUNC_ID_SOLUTION
current_theme = DEFAULT_THEME
current_fontcolor = DEFAULT_FONTCOLOR
performance_data = DEFAULT_PERFORMANCE_DATA
customized_system_prompt = FUNC_SYSTEM_PROMPT_CUSTOMIZE

server_host = "127.0.0.1"
server_port = "8910"
api_key = "123"
n_predict = 4096
temperature = 0.9
top_k = 13
top_p = 0.6
running_llm = ""
client = None
base_path = ""

def get_sys_prompt_file():
    global base_path
    if platform.system() == "Android":
        base_path = Path.home()
    else:
        base_path = Path(__file__).resolve().parent

    return os.path.join(str(base_path), 'data', 'sys_prompt.json')

SYS_PROMPT_FILE = get_sys_prompt_file()
os.makedirs(os.path.dirname(SYS_PROMPT_FILE), exist_ok=True)

if LOG_ENABLE is True:
    log_path = str(base_path / LOG_FILE)
    logging.basicConfig(filename=log_path, level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8')
else:
    logging.disable(logging.CRITICAL)

extrabody = {
    "n_predict": n_predict, "seed": 146, "temp": temperature,
    "top_k": top_k, "top_p": top_p, "penalty_last_n": 64,
    "penalty_repeat": 1.3
}

def has_chinese(string):
    for c in string:
        if '\u4e00' <= c <= '\u9fa5':
            return True
    return False

class Message:
    def __init__(self, user: str, text: str):
        self.user = user
        self.text = text

class ChatMessage(ft.Row):
    def __init__(self, message: Message, page_width: int = 1000, font_color: str = DEFAULT_FONTCOLOR):
        super().__init__()        
        self.vertical_alignment = ft.CrossAxisAlignment.START
        self.auto_scroll = True

        # ✅ 根据角色决定对齐方式和图标分布
        if message.user == "User":
            self.alignment = ft.MainAxisAlignment.END   # 整个Row主轴靠右对齐
            self.controls = [
                ft.Text(
                    message.text, 
                    selectable=True, 
                    no_wrap=False,    # 自动换行
                    color=font_color,
                    width=(page_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR,   # 根据窗口宽度动态调整Text宽度，结合no_wrap自动换行
                    text_align=ft.TextAlign.RIGHT   # 文字靠右对齐
                ),
                ft.CircleAvatar(
                    content=ft.Text(message.user, weight="bold"),
                    color=ft.Colors.WHITE,
                    bgcolor=ft.Colors.BLUE,
                    width=CIRCLEAVATAR_WIDTH
                )
            ]
        elif message.user == "AI":
            self.alignment = ft.MainAxisAlignment.START
            self.controls = [
                ft.CircleAvatar(
                    content=ft.Text(message.user, weight="bold"),
                    color=ft.Colors.WHITE,
                    bgcolor=ft.Colors.GREEN,
                    width=CIRCLEAVATAR_WIDTH
                ),
                ft.Markdown(
                    value=message.text,
                    selectable=True,
                    width=(page_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR,
                    on_tap_link=lambda e: ft.Page.launch_url(e.data),     # 自动打开链接
                    auto_follow_links=True,
                    auto_follow_links_target=ft.UrlTarget.BLANK,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB
                )
            ]
        else:   # performance data shown
            self.alignment = ft.MainAxisAlignment.START
            self.controls = [
                ft.Container(width=CIRCLEAVATAR_WIDTH, bgcolor="transparent"),
                ft.Text(message.text, selectable=True, no_wrap=False, width=(page_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR, text_align=ft.TextAlign.LEFT, italic=True, size=11, color=ft.Colors.ON_SURFACE_VARIANT)
            ]

    def update_width(self, new_width):
        """动态调整消息框的宽度"""
        if new_width - CIRCLEAVATAR_WIDTH > 200:
            if isinstance(self.controls[0], ft.Text):  # this is "User" ChatMessage
                self.controls[0].width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            else:   # this is "AI" ChatMessage
                self.controls[1].width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            self.update()

def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def encode_prompt(prompt: str) -> str:
    return base64.b64encode(prompt.encode()).decode()

def decode_prompt(encoded: str) -> str:
    return base64.b64decode(encoded.encode()).decode()

def save_prompt(prompt: str):
    encoded = encode_prompt(prompt)
    hash_value = get_hash(prompt)
    with open(SYS_PROMPT_FILE, "w") as f:
        json.dump({"data": encoded, "hash": hash_value}, f)

def load_prompt():
    try:
        with open(SYS_PROMPT_FILE, "r") as f:
            obj = json.load(f)
            decoded = decode_prompt(obj["data"])
            if get_hash(decoded) != obj["hash"]:
                raise ValueError("customized system prompt is tampered!")
            return decoded

    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"load customized system prompt failure. Use default system prompt. Error code: {e}")
        logging.debug("load customized system prompt failure. Use default system prompt. Error code: {error}".format(error=e))
        return FUNC_SYSTEM_PROMPT_CUSTOMIZE

def get_llm_list(host:str, port:str, key:str) -> tuple[list[str], str]:
    global client

    try:
        response = requests.get(f"http://{host}:{port}/v1/models", timeout=1)   # 1 second timeout
        if response.status_code == 200: 
            try:
                client = OpenAI(base_url=f"http://{host}:{port}/v1", api_key=key)
                model_lst = client.models.list()
                modelname_lst = [model.id for model in model_lst.data]
                default_model = next((m for m in modelname_lst if DEFAULT_LLM.lower() in m.lower()), modelname_lst[0])
                return modelname_lst, default_model
            except Exception as e:
                client = None
                print(f"Getting LLM list failure: {e}")
                logging.debug("Getting LLM list failure: {error}".format(error=e))
                return None, None
    except requests.exceptions.RequestException:
        client = None
        print(f"Genie Server is offline")
        logging.debug("Genie Server is offline")
        return None, None
"""
def is_android():
    print("is_android: {system}, {sdcard}".format(system=platform.system(), sdcard=Path("/sdcard").exists()))
    logging.debug("is_android: {system}, {sdcard}".format(system=platform.system(), sdcard=Path("/sdcard").exists()))
    return (
        platform.system() == "Linux" and
        Path("/sdcard").exists()
    )

def start_genie_service() -> bool:
    global client, server_host, server_port, api_key, running_llm

    if is_android() == True:
        try:
            subprocess.Popen(["adb", "shell", "am", "start-foreground-service", "-n", "com.example.genieapiservice/.ForegroundService"])
            print("GenieAPIService start successfully in Android")
            logging.debug("GenieAPIService start successfully in Android")
            llm_lst, running_llm = get_llm_list(server_host, server_port, api_key)
            if llm_lst is None:
                print("Get llm failure")
                logging.debug("Get llm failure")
                return False
            else:
                print("Get llm successfully")
                logging.debug("Get llm successfully")                
                return True
        except Exception as e:
            client = None
            print(f"GenieAPIService start failure in Android: {e}")
            logging.debug("GenieAPIService start failure in Android: {error}".format(error=e))
            return False
    else:
        print("Not Android. Don't start genie service")
        logging.debug("Not Android. Don't start genie service")
        return False
"""
def check_genie_service() -> bool:
    global server_host, server_port, api_key, running_llm

    llm_lst, running_llm = get_llm_list(server_host, server_port, api_key)
    if llm_lst is None:
        #return start_genie_service()
        return False
    else:
        return True

def edit_prompt(user_input: str, context: str="") -> tuple[str, str]:
    global func_id, customized_system_prompt
    match func_id:
        case FuncID.FUNC_ID_SOLUTION:
            sys_prompt  = FUNC_SYSTEM_PROMPT_SOLUTION
            user_prompt = FUNC_USER_PROMPT_SOLUTION.format(question=user_input)            
            return sys_prompt, user_prompt
        case FuncID.FUNC_ID_TRANSLATE:
            sys_prompt  = FUNC_SYSTEM_PROMPT_TRANSLATE
            target_lang = "英文" if has_chinese(user_input) else "中文"
            user_prompt = FUNC_USER_PROMPT_TRANSLATE.format(sentence=user_input, lang=target_lang)
            return sys_prompt, user_prompt
        case FuncID.FUNC_ID_CUSTOMIZE:
            sys_prompt  = customized_system_prompt
            user_prompt = FUNC_USER_PROMPT_CUSTOMIZE.format(question=user_input)
            return sys_prompt, user_prompt
        case _:
            return None, None

def get_all_controls(control):
    all_controls = [control]
    if hasattr(control, "controls") and control.controls:
        for child in control.controls:
            all_controls.extend(get_all_controls(child))
    return all_controls

async def disable_controls_by_type(page, target_types: tuple, exception_keys: list, status: bool):
    for top_control in page.controls:
        for control in get_all_controls(top_control):
            if isinstance(control, target_types) and getattr(control, "key", None) not in exception_keys:
                control.disabled = status
    page.update()

def clear_server_history():
    global server_host, server_port
    response = requests.post(f"http://{server_host}:{server_port}/clear", json={"text": "clear"})
    print(f"Clear server history: {response.status_code}")
    logging.debug("Clear server history: {error}".format(error=response.status_code))

def generate_summary(query: str, stream_output: bool, context: str=""):
    global running_llm, extrabody, client

    if client == None:
        if check_genie_service() == False:
            return None, "Connection error."

    sys_prompt, user_prompt = edit_prompt(user_input=query, context=context)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt}
    ]

    print(f"Start reference: max_length - {extrabody["n_predict"]}. temp - {extrabody["temp"]}. top_k - {extrabody["top_k"]}. top_p - {extrabody["top_p"]}")
    print(f"System prompt: {sys_prompt}")
    logging.debug("System prompt: {prompt}".format(prompt=sys_prompt))
    print(f"User prompt: {user_prompt}")
    logging.debug("User prompt: {prompt}".format(prompt=user_prompt))
    
    try:
        if stream_output is True:
            response = client.chat.completions.create(model=running_llm, stream=True, messages=messages, extra_body=extrabody)
            return response, None
        else:
            response = client.chat.completions.create(model=running_llm, messages=messages)
            return response.choices[0].message.content, None
    except Exception as e:
        print(f"inference failure: {e}")
        logging.debug("inference failure: {error}".format(error=e))        
        return None, str(e)

async def send_message_click(e, page: ft.Page, chat: ft.ListView, new_message: ft.TextField, sendbutton: ft.IconButton):
    global selected_file, current_fontcolor, performance_data, server_host, server_port, running_llm, client
    if sendbutton.icon == ft.Icons.SEND_ROUNDED:
        if new_message.value.strip():
            m_user = Message("User", new_message.value)
            cm_user = ChatMessage(m_user, page.width, current_fontcolor)
            chat.controls.append(cm_user)

            new_message.value = ""
            new_message.focus()
            sendbutton.icon = ft.Icons.STOP_ROUNDED
            sendbutton.tooltip = "Stop"
            page.update()
            await disable_controls_by_type(page, target_types=(ft.TextField, ft.ElevatedButton, ft.IconButton, ft.PopupMenuButton), exception_keys=[sendbutton.key], status=True)   # ✅ 禁用所有用户可操作控件(除send_button)
            await asyncio.sleep(0.2)  # 让 UI 先处理

            response, err_msg = generate_summary(m_user.text, True)

            cm_ai = ChatMessage(Message("AI", ""), page.width, current_fontcolor)
            chat.controls.append(cm_ai)
            ai_text_component = cm_ai.controls[1]

            if response is not None:
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        ai_text_component.value += chunk.choices[0].delta.content
                        chat.scroll_to(len(chat.controls) - 1)   # 滚动到最下一行
                        page.update()
                        await asyncio.sleep(0)  # ✅ 让事件循环处理，给page机会刷新UI

                if performance_data:
                    response = requests.get(f"http://{server_host}:{server_port}/profile", timeout=2)
                    if response.status_code == 200: 
                        content = json.loads(response.content.decode("utf-8"))
                        performance_message = f"{content['prompt_processing_rate']} tokens/sec ({content['num_prompt_tokens']} tokens) · {content['token_generation_rate']} tokens/sec ({content['num_generated_tokens']} tokens) · {content['time_to_first_token']} ms to first token · generation time: {content['token_generation_time']} ms · [{running_llm}]"
                        cm_pd = ChatMessage(Message("Performance data", performance_message), page.width)
                        chat.controls.append(cm_pd)
                        chat.scroll_to(len(chat.controls) - 1)
            else:
                if "Connection error.".lower() in err_msg.lower() or "Error code: 500".lower() in err_msg.lower():
                    if check_genie_service() == True:
                        ai_text_component.value = ERROR_MESSAGE_GENIESERVER_RESTART_SUCCESS
                    else:
                        ai_text_component.value = ERROR_MESSAGE_GENIESERVER_NOT_AVAILABLE
                        client = None
                elif "There is connection in using.".lower() in err_msg.lower():
                    ai_text_component.value = ERROR_MESSAGE_GENIESERVER_IN_USING
                else:
                    ai_text_component.value = ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR.format(errmsg=err_msg)
                    client = None
                chat.scroll_to(len(chat.controls) - 1)

            await disable_controls_by_type(page, target_types=(ft.TextField, ft.ElevatedButton, ft.IconButton, ft.PopupMenuButton), exception_keys=[sendbutton.key], status=False)   # ✅ 任务完成，恢复所有控件
            sendbutton.icon = ft.Icons.SEND_ROUNDED
            sendbutton.tooltip = "Send message"
            page.update()
    else:
        sendbutton.icon = ft.Icons.SEND_ROUNDED
        sendbutton.tooltip = "Send message"
        await disable_controls_by_type(page, target_types=(ft.TextField, ft.ElevatedButton, ft.IconButton, ft.PopupMenuButton), exception_keys=[sendbutton.key], status=False)
        page.update()

        response = requests.post(f"http://{server_host}:{server_port}/stop", json={"text": "stop"})
        print(f"Stop inference: {response.status_code}")     
        logging.debug("Stop inference: {error}".format(error=response.status_code))   

def create_server_setting_dialog(page: ft.Page):
    global server_host, server_port, api_key, running_llm
    
    def host_onchange(e):
        confirm_button.disabled=True
        page.update()

    def port_onchange(e):
        confirm_button.disabled=True
        page.update()

    def key_onchange(e):
        confirm_button.disabled=True
        page.update()

    def on_llm_change(e):
        global running_llm
        running_llm = e.control.value
        page.update()

    host_str = ft.TextField(label="Host", value=server_host, text_size=13, on_change=host_onchange)
    port_str = ft.TextField(label="Port", value=server_port, text_size=13, keyboard_type=ft.KeyboardType.NUMBER, on_change=port_onchange)
    key_str  = ft.TextField(label="Key", value=api_key, text_size=13, on_change=key_onchange)
    running_llm_dropdown = ft.Dropdown(label="LLM", expand=True, text_size=13, disabled=True, options=[], on_change=on_llm_change)

    def confirm_settings():
        global server_host, server_port, api_key, running_llm

        server_host = host_str.value
        server_port = port_str.value
        api_key     = key_str.value
        running_llm = running_llm_dropdown.value

        print(f"Server info: http://{server_host}:{server_port}/v1. API Key: {api_key}. LLM: {running_llm}")

        page.close(snack_bar)
        page.close(server_settings_dialog)
        page.update()

    confirm_button = ft.TextButton("OK", disabled=True, on_click=lambda e: confirm_settings())

    snack_bar = ft.SnackBar(content=ft.Text(""), duration=2500)
    page.add(snack_bar)

    def fetch_llm():
        running_llm_dropdown.disabled = True
        page.update()

        llm_lst, running_llm = get_llm_list(host_str.value.strip(), port_str.value.strip(), key_str.value.strip())
        if llm_lst is not None:
            running_llm_dropdown.options = [ft.dropdown.Option(opt) for opt in llm_lst]
            running_llm_dropdown.value = running_llm
            running_llm_dropdown.disabled = False
            confirm_button.disabled = False
            snack_bar.content.value = "Get large language model list successfully!"
        else:
            snack_bar.content.value = "Fail to get large language model list. Please check Host / Port / Key value!"
            running_llm_dropdown.options = []
            running_llm_dropdown.disabled = True
            confirm_button.disabled = True
        page.open(snack_bar)
        page.update()

    server_settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Genie Server Settings"),
        content=ft.Container(
            height=320,
            content=ft.Column([host_str, port_str, key_str, running_llm_dropdown, ft.ElevatedButton("Verify", icon=ft.Icons.CHECK, on_click=lambda e: fetch_llm())])
        ),
        adaptive=False,
        actions=[confirm_button]
    )

    return server_settings_dialog

def create_llm_setting_dialog(page: ft.Page):
    global n_predict, temperature, top_k, top_p, performance_data

    n_predict_val = ft.Text(str(4096), size=12)
    temp_val = ft.Text("0.90", size=12)
    top_k_val = ft.Text("13", size=12)
    top_p_val = ft.Text("0.60", size=12)

    n_predict_slider = ft.Slider(label="maxlength (1 - 4096)", min=1, max=4096, divisions=4095, value=n_predict, width=130)
    temp_slider = ft.Slider(label="temperature (0.01 - 1.0)", min=0.01, max=1.0, divisions=99, value=temperature, width=130)
    top_k_slider = ft.Slider(label="Top K (1 - 100)", min=1, max=100, divisions=99, value=top_k, width=130)
    top_p_slider = ft.Slider(label="Top K (0.0 - 1.0)", min=0.0, max=1.0, divisions=100, value=top_p, width=130)

    def update_n_predict(e):
        n_predict_val.value = str(int(e.control.value))
        page.update()

    def update_temp(e):
        temp_val.value = f"{e.control.value:.2f}"
        page.update()

    def update_top_k(e):
        top_k_val.value = str(int(e.control.value))
        page.update()

    def update_top_p(e):
        top_p_val.value = f"{e.control.value:.2f}"
        page.update()

    n_predict_slider.on_change = update_n_predict
    temp_slider.on_change = update_temp
    top_k_slider.on_change = update_top_k
    top_p_slider.on_change = update_top_p

    def on_performancedata_change(e: ft.ControlEvent):
        global performance_data
        performance_data = e.control.value
        page.update()

    performance_data_switch = ft.Switch(label="Show performance data", value=performance_data, on_change=on_performancedata_change)

    def slider_group(title: str, slider: ft.Slider, value_text: ft.Text):
        return ft.Column([
            ft.Text(title, size=12, weight=ft.FontWeight.NORMAL),
            ft.Row([value_text, slider], alignment=ft.MainAxisAlignment.START, spacing=0, tight=True),
        ])

    def confirm_settings():
        global extrabody, n_predict, temperature, top_k, top_p

        n_predict   = int(n_predict_slider.value)
        temperature = round(temp_slider.value, 2)
        top_k       = int(top_k_slider.value)
        top_p       = round(top_p_slider.value, 2)

        extrabody["n_predict"] = n_predict
        extrabody["temp"] = temperature
        extrabody["top_k"] = top_k
        extrabody["top_p"] = top_p

        print(f"LLM parameters info: max_length - {n_predict}. temp - {temperature}. top_k - {top_k}. top_p - {top_p}")

        page.close(llm_settings_dialog)
        page.update()

    confirm_button = ft.TextButton("OK", on_click=lambda e: confirm_settings())

    llm_settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("LLM Settings"),
        content=ft.Container(
            height=400,
            content=ft.Column([
                    slider_group("maxlength", n_predict_slider, n_predict_val),
                    slider_group("temp", temp_slider, temp_val),
                    slider_group("Top K", top_k_slider, top_k_val),
                    slider_group("Top P", top_p_slider, top_p_val),
                    performance_data_switch
                ])),
        adaptive=False,
        actions=[confirm_button]
    )

    return llm_settings_dialog

def create_ui_setting_dialog(page: ft.Page):
    def on_theme_change(e):
        global current_theme
        current_theme = e.control.value
        page.theme_mode = current_theme
        page.update()

    theme_dropdown = ft.Dropdown(
        label="Theme style",
        options=[
            ft.dropdown.Option("light"),
            ft.dropdown.Option("dark"),
        ],
        value=current_theme,
        expand=True,
        on_change=on_theme_change
    )

    def on_fontcolor_change(e):
        global current_fontcolor
        current_fontcolor = e.control.value

    fontcolor_dropdown = ft.Dropdown(
        label="User font color",
        options=[
            ft.dropdown.Option("cyan700"),  # ft.Colors.CYAN700
            ft.dropdown.Option("yellow"),
            ft.dropdown.Option("red"),
            ft.dropdown.Option("green"),
            ft.dropdown.Option("black"),
            ft.dropdown.Option("white"),
        ],
        value=current_fontcolor,
        expand=True,
        on_change=on_fontcolor_change
    )

    def confirm_settings():
        page.close(ui_settings_dialog)
        page.update()

    confirm_button = ft.TextButton("OK", on_click=lambda e: confirm_settings())

    ui_settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("UI Settings"),
        content=ft.Container(
            height=180,
            content=ft.Column([theme_dropdown, fontcolor_dropdown])
        ),
        adaptive=False,
        actions=[confirm_button]
    )

    return ui_settings_dialog

def create_sysprompt_setting_dialog(page: ft.Page):
    global customized_system_prompt

    customized_system_prompt = load_prompt()

    sysprompt_text = ft.TextField(
        value=customized_system_prompt,
        label="✏️ Customize your system prompt",
        text_size=12,
        autofocus=True,
        shift_enter=True,
        min_lines=1,
        max_lines=100,
        filled=True
    )

    def reset_settings():
        sysprompt_text.value=FUNC_SYSTEM_PROMPT_CUSTOMIZE
        page.update()

    def confirm_settings():
        global customized_system_prompt
        customized_system_prompt = sysprompt_text.value
        save_prompt(customized_system_prompt)
        page.close(sysprompt_settings_dialog)
        page.update()

    reset_button = ft.TextButton("Reset", on_click=lambda e: reset_settings())
    confirm_button = ft.TextButton("OK", on_click=lambda e: confirm_settings())

    sysprompt_settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("System Prompt"),
        content=ft.Container(
            height=300,
            content=sysprompt_text
        ),
        adaptive=False,
        actions=[reset_button, confirm_button]
    )

    return sysprompt_settings_dialog

def main(page: ft.Page):
    global func_id, current_theme, current_fontcolor

    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.title = "AI Chat v2.0.0"
    page.theme_mode = current_theme

    server_settings_dialog = create_server_setting_dialog(page)
    llm_settings_dialog = create_llm_setting_dialog(page)
    ui_settings_dialog = create_ui_setting_dialog(page)
    sysprompt_settings_dialog = create_sysprompt_setting_dialog(page)

    def on_menu_click(e: ft.ControlEvent):
        selected = e.control.text
        if selected == "Server Settings":
            page.open(server_settings_dialog)
        elif selected == "LLM Settings":
            page.open(llm_settings_dialog)
        elif selected == "System Prompt Settings":
            page.open(sysprompt_settings_dialog)
        elif selected == "UI Settings":
            page.open(ui_settings_dialog)

        page.update()

    settings_menu_button = ft.PopupMenuButton(
        icon=ft.Icons.MORE_VERT,
        items=[
            ft.PopupMenuItem(text="Server Settings", on_click=on_menu_click),
            ft.PopupMenuItem(text="LLM Settings", on_click=on_menu_click),
            ft.PopupMenuItem(text="System Prompt Settings", on_click=on_menu_click),
            ft.PopupMenuItem(text="UI Settings", on_click=on_menu_click),            
        ]
    )

    def new_session_click(e):
        clear_server_history()
        chat.controls.clear()
        page.update()

    new_session_button = ft.IconButton(
        icon=ft.Icons.RESTART_ALT_ROUNDED,
        tooltip="New session",
        on_click=new_session_click
    )

    def click_button_solution(e):
        global func_id
        if func_id != FuncID.FUNC_ID_SOLUTION:
            func_id = FuncID.FUNC_ID_SOLUTION
            update_func_UI()
            clear_server_history()

    def click_button_translate(e):
        global func_id
        if func_id != FuncID.FUNC_ID_TRANSLATE:
            func_id = FuncID.FUNC_ID_TRANSLATE
            update_func_UI()
            clear_server_history()

    def click_button_customize(e):
        global func_id
        if func_id != FuncID.FUNC_ID_CUSTOMIZE:
            func_id = FuncID.FUNC_ID_CUSTOMIZE
            update_func_UI()
            clear_server_history()

    FUNC_LIST = [
        {
            "id": FuncID.FUNC_ID_SOLUTION,
            "name": FUNC_NAME_SOLUTION,
            "user_prompt": FUNC_USER_PROMPT_SOLUTION,
            "sys_prompt": FUNC_SYSTEM_PROMPT_SOLUTION,
            "hint": FUNC_HINT_SOLUTION,
            "handler": click_button_solution
        },
        {
            "id": FuncID.FUNC_ID_TRANSLATE,
            "name": FUNC_NAME_TRANSLATE,
            "user_prompt": FUNC_USER_PROMPT_TRANSLATE,
            "sys_prompt": FUNC_SYSTEM_PROMPT_TRANSLATE,
            "hint": FUNC_HINT_TRANSLATE,
            "handler": click_button_translate
        },
        {
            "id": FuncID.FUNC_ID_CUSTOMIZE,
            "name": FUNC_NAME_CUSTOMIZE,
            "user_prompt": FUNC_USER_PROMPT_CUSTOMIZE,
            "sys_prompt": FUNC_SYSTEM_PROMPT_CUSTOMIZE,
            "hint": FUNC_HINT_CUSTOMIZE,
            "handler": click_button_customize
        }
    ]

    async def send_message_click_wrapper(e):
        asyncio.create_task(send_message_click(e, page, chat, new_message, send_button))

    chat = ft.ListView(expand=True, spacing=10, auto_scroll=True)   # Q&A window

    # input title
    input_title = ft.Text(FUNC_LIST[func_id.value]["name"])

    # question input
    new_message = ft.TextField(
        hint_text=FUNC_LIST[func_id.value]["hint"],
        autofocus=True,
        shift_enter=True,
        min_lines=1,
        max_lines=5,
        filled=True,
        on_submit=send_message_click_wrapper
    )

    # question send/stop button
    send_button = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED,
        tooltip="Send message",
        key="send_button",
        on_click=send_message_click_wrapper
    )

    input_row = ft.Row(
        controls = [
            ft.Column([input_title, new_message], spacing=5, expand=True),
            ft.Column([ft.Container(height=15, bgcolor="transparent"), send_button])   # ft.Container is placeholder so that send_button aligns to new_message.
        ],
        spacing=10,
        tight=True,
    )

    def resize_handler(e):
        """在窗口大小改变时调整 `ChatMessage` 宽度"""
        for item in chat.controls:
            if isinstance(item, ChatMessage):
                item.update_width(page.width)  # 让消息框宽度适应窗口大小
        page.update()

    page.on_resized = resize_handler

    # bottom function row
    def update_func_UI():
        global func_id
        item = next((f for f in FUNC_LIST if f["id"] == func_id), None)
        input_row.controls[0].controls[0].value = item["name"]
        input_row.controls[0].controls[1].hint_text = item["hint"]
        input_row.controls[0].controls[1].value = ""
        input_row.controls[0].controls[1].read_only = False
        page.update()

    Func_Row = ft.Row(controls=[], alignment=ft.MainAxisAlignment.SPACE_AROUND)
    for item in FUNC_LIST:
        new_button = ft.ElevatedButton(text=item["name"])
        new_button.on_click = item["handler"]
        Func_Row.controls.append(new_button)

    page.add(
        ft.Row(controls=[new_session_button, settings_menu_button], spacing=10, alignment=ft.MainAxisAlignment.END),
        ft.Container(content=chat, border=ft.border.all(1, ft.Colors.OUTLINE), border_radius=5, padding=10, expand=True),
        input_row,
        Func_Row
    )

    page.add(server_settings_dialog)
    page.add(llm_settings_dialog)
    page.add(ui_settings_dialog)
    page.add(sysprompt_settings_dialog)

    if check_genie_service() == False:
        page.open(server_settings_dialog)   # try starting genie service. If failure, pop up server setting dialog.

if __name__ == "__main__":
    ft.app(target=main)
