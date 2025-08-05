import flet as ft
import asyncio
from openai import OpenAI

PAGE_CONTENT_FACTOR = 0.8
CIRCLEAVATAR_WIDTH = 50

DEFAULT_THEME = "dark"
DEFAULT_FONTCOLOR = "cyan700"
DEFAULT_LLM = "qwen"

FUNC_ID_SOLUTION    = 0
FUNC_ID_TRANSLATE   = 1
FUNC_ID_SOURCE_CODE = 2

'''
FUNC_NAME_SOLUTION    = "📐 解题答疑"
FUNC_NAME_TRANSLATE   = "🌐 AI 翻 译"
FUNC_NAME_SOURCE_CODE = "📜 代码分析"
'''
FUNC_NAME_SOLUTION    = "📐 Solution"
FUNC_NAME_TRANSLATE   = "🌐 Translate"
FUNC_NAME_SOURCE_CODE = "📜 Code Analyze"

FUNC_PROMPT_SOLUTION    = "{prompt}"
FUNC_PROMPT_TRANSLATE   = "将以下内容翻译成{lang}\n{prompt}"
FUNC_PROMPT_SOURCE_CODE = "请帮忙分析源代码，分析是否有潜在问题。如果没有问题，请给出详细注释。代码如下\n{prompt}"

FUNC_HINT_SOLUTION    = "What can I do for you? ..."
FUNC_HINT_TRANSLATE   = "Please input your sentence ..."
FUNC_HINT_SOURCE_CODE = "Please provide your source code ..."

ERROR_MESSAGE_GENIESERVER_IN_USING      = "Genie server is in using. Please try again later."
ERROR_MESSAGE_GENIESERVER_NOT_AVAILABLE = "Genie server is not available. Please check if network or GenieAPIService (Address or Port) is working or not."
ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR = "Genie server exception happens. Error message: {errmsg}"
ERROR_MESSAGE_MODEL_LIST_FAILURE        = "Genie server is not working\n\nPlease launch GenieAPIService and restart this application!"

func_id = FUNC_ID_SOLUTION
current_theme = DEFAULT_THEME
current_fontcolor = DEFAULT_FONTCOLOR

server_host = "127.0.0.1"
server_port = "8910"
api_key = "123"
n_predict = 4096
temperature = 0.9
top_k = 13
top_p = 0.6
running_llm = ""
client = None

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
                #ft.Container(
                #    content=ft.Text(message.text, color=ft.Colors.WHITE, selectable=True, no_wrap=False, width=(page_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR, text_align=ft.TextAlign.RIGHT),
                #    bgcolor=ft.Colors.BLUE_500,
                #    padding=10,
                #    border_radius=15,
                #),
                ft.CircleAvatar(
                    content=ft.Text(message.user, weight="bold"),
                    color=ft.Colors.WHITE,
                    bgcolor=ft.Colors.BLUE,
                    width=CIRCLEAVATAR_WIDTH
                )
            ]
        else:
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

    def update_width(self, new_width):
        """动态调整消息框的宽度"""
        if new_width - CIRCLEAVATAR_WIDTH > 200:
            if isinstance(self.controls[0], ft.Container):  # this is "User" ChatMessage
                self.controls[0].content.width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            else:   # this is "AI" ChatMessage
                self.controls[1].width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            self.update()

def get_llm_list(host:str, port:str, key:str) -> tuple[list[str], str]:
    global client
    try:
        client = OpenAI(base_url=f"http://{host}:{port}/v1", api_key=key)
        model_lst = client.models.list()
        modelname_lst = [model.id for model in model_lst.data]
        default_model = next((m for m in modelname_lst if DEFAULT_LLM.lower() in m.lower()), modelname_lst[0])
        return modelname_lst, default_model
    except Exception as e:
        print(f"Getting LLM list failure: {e}")
        return None, None

def edit_prompt(user_input: str) -> str:
    match func_id:
        case 0:
            prompt_format = FUNC_PROMPT_SOLUTION.format(prompt=user_input)
            return prompt_format
        case 1:
            target_lang = "英文" if has_chinese(user_input) else "中文"
            prompt_format = FUNC_PROMPT_TRANSLATE.format(prompt=user_input, lang=target_lang)
            return prompt_format
        case 2:
            prompt_format = FUNC_PROMPT_SOURCE_CODE.format(prompt=user_input)
            return prompt_format
        case _:
            return None

async def disable_all_controls(page, status: bool):
    for control in page.controls:
        control.disabled = status
    page.update()

def generate_summary(text, stream_output: bool):
    global running_llm, extrabody

    prompt_txt = edit_prompt(text)

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt_txt}
    ]

    print(f"Start reference: max_length - {extrabody["n_predict"]}. temp - {extrabody["temp"]}. top_k - {extrabody["top_k"]}. top_p - {extrabody["top_p"]}")
    
    try:
        if stream_output is True:
            response = client.chat.completions.create(model=running_llm, stream=True, messages=messages, extra_body=extrabody)
            return response, None
        else:
            response = client.chat.completions.create(model=running_llm, messages=messages)
            return response.choices[0].message.content, None
    except Exception as e:
        print(f"inference failure: {e}")        
        return None, str(e)

async def send_message_click(e, page: ft.Page, chat: ft.ListView, new_message: ft.TextField):
    global selected_file, current_fontcolor
    if new_message.value.strip():
        new_message.disabled = True

        m_user = Message("User", new_message.value)
        cm_user = ChatMessage(m_user, page.width, current_fontcolor)
        chat.controls.append(cm_user)

        new_message.value = ""
        new_message.focus()
        page.update()
        await disable_all_controls(page, True)   # ✅ 禁用所有控件
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
        else:
            if "Connection error.".lower() in err_msg.lower():
                ai_text_component.value = ERROR_MESSAGE_GENIESERVER_NOT_AVAILABLE
            elif "There is connection in using.".lower() in err_msg.lower():
                ai_text_component.value = ERROR_MESSAGE_GENIESERVER_IN_USING
            else:
                ai_text_component.value = ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR.format(errmsg=err_msg)
            chat.scroll_to(len(chat.controls) - 1)

        await disable_all_controls(page, False)   # ✅ 任务完成，恢复所有控件
        new_message.disabled = False   # **恢复输入框**
        page.update()    

def create_setting_dialog(page: ft.Page):
    global server_host, server_port, api_key, running_llm, n_predict, temperature, top_k, top_p
    
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

    n_predict_val = ft.Text(str(4096), size=12)
    temp_val = ft.Text("0.90", size=12)
    top_k_val = ft.Text("13", size=12)
    top_p_val = ft.Text("0.60", size=12)

    n_predict_slider = ft.Slider(label="maxlength (1 - 4096)", min=1, max=4096, divisions=4095, value=n_predict, width=130, disabled=True)
    temp_slider = ft.Slider(label="temperature (0.01 - 1.0)", min=0.01, max=1.0, divisions=99, value=temperature, width=130, disabled=True)
    top_k_slider = ft.Slider(label="Top K (1 - 100)", min=1, max=100, divisions=99, value=top_k, width=130, disabled=True)
    top_p_slider = ft.Slider(label="Top K (0.0 - 1.0)", min=0.0, max=1.0, divisions=100, value=top_p, width=130, disabled=True)

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

    def slider_group(title: str, slider: ft.Slider, value_text: ft.Text):
        return ft.Column([
            ft.Text(title, size=12, weight=ft.FontWeight.NORMAL),
            ft.Row([value_text, slider], alignment=ft.MainAxisAlignment.START, spacing=0, tight=True),
        ])

    def confirm_settings():
        global server_host, server_port, api_key, running_llm, n_predict, temperature, top_k, top_p, extrabody

        server_host = host_str.value
        server_port = port_str.value
        api_key     = key_str.value
        running_llm = running_llm_dropdown.value
        n_predict   = int(n_predict_slider.value)
        temperature = round(temp_slider.value, 2)
        top_k       = int(top_k_slider.value)
        top_p       = round(top_p_slider.value, 2)

        extrabody["n_predict"] = n_predict
        extrabody["temp"] = temperature
        extrabody["top_k"] = top_k
        extrabody["top_p"] = top_p

        print(f"Server info: http://{server_host}:{server_port}/v1. API Key: {api_key}")
        print(f"Model info: name - {running_llm}. max_length - {n_predict}. temp - {temperature}. top_k - {top_k}. top_p - {top_p}")

        page.close(snack_bar)
        page.close(settings_dialog)
        page.update()

    confirm_button = ft.TextButton("OK", disabled=True, on_click=lambda e: confirm_settings())

    snack_bar = ft.SnackBar(content=ft.Text(""), duration=2500)
    page.add(snack_bar)

    # ===== 模型获取模拟 =====
    def fetch_llm():
        running_llm_dropdown.disabled = True
        right_column_enable(False)
        page.update()

        llm_lst, running_llm = get_llm_list(host_str.value.strip(), port_str.value.strip(), key_str.value.strip())
        if llm_lst is not None:
            running_llm_dropdown.options = [ft.dropdown.Option(opt) for opt in llm_lst]
            running_llm_dropdown.value = running_llm
            running_llm_dropdown.disabled = False
            confirm_button.disabled = False
            right_column_enable(True)
            snack_bar.content.value = "Get large language model list successfully!"
        else:
            snack_bar.content.value = "Fail to get large language model list. Please check Host / Port / Key value!"
            running_llm_dropdown.options = []
            running_llm_dropdown.disabled = True
            confirm_button.disabled = True
            right_column_enable(False)
        page.open(snack_bar)
        page.update()

    def right_column_enable(enable: bool):
        for slider in [n_predict_slider, temp_slider, top_k_slider, top_p_slider]:
            slider.disabled = not enable

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Genie Server Settings"),
        content=ft.Container(
            height=320,
            content=ft.Row([
                ft.Column([
                    slider_group("maxlength", n_predict_slider, n_predict_val),
                    slider_group("temp", temp_slider, temp_val),
                    slider_group("Top K", top_k_slider, top_k_val),
                    slider_group("Top P", top_p_slider, top_p_val)
                ], width=140),
                ft.Column([
                    host_str,
                    port_str,
                    key_str,
                    running_llm_dropdown,
                    ft.ElevatedButton("Verify", icon=ft.Icons.CHECK, on_click=lambda e: fetch_llm()),
                ], width=120),
            ], tight=True),
        ),
        adaptive=False,
        actions=[confirm_button]
    )

    return settings_dialog

def main(page: ft.Page):
    global func_id, current_theme, current_fontcolor

    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.title = "AI Chat"
    page.theme_mode = current_theme

    settings_dialog = create_setting_dialog(page)

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

    def open_settings(e):
        page.open(settings_dialog)
        page.update()

    settings_button = ft.IconButton(icon=ft.Icons.SETTINGS, tooltip="Settings", on_click=open_settings)

    dropdown_row = ft.Row(
        controls=[theme_dropdown, fontcolor_dropdown, settings_button],
        spacing=20
    )

    def click_button_solution(e):
        global func_id
        func_id = FUNC_ID_SOLUTION
        update_func_UI()

    def click_button_translate(e):
        global func_id
        func_id = FUNC_ID_TRANSLATE
        update_func_UI()

    def click_button_source_code(e):
        global func_id
        func_id = FUNC_ID_SOURCE_CODE
        update_func_UI()

    FUNC_LIST = [
        {
            "id": FUNC_ID_SOLUTION,
            "name": FUNC_NAME_SOLUTION,
            "prompt": FUNC_PROMPT_SOLUTION,
            "hint": FUNC_HINT_SOLUTION,
            "handler": click_button_solution
        },
        {
            "id": FUNC_ID_TRANSLATE,
            "name": FUNC_NAME_TRANSLATE,
            "prompt": FUNC_PROMPT_TRANSLATE,
            "hint": FUNC_HINT_TRANSLATE,
            "handler": click_button_translate
        },
        {
            "id": FUNC_ID_SOURCE_CODE,
            "name": FUNC_NAME_SOURCE_CODE,
            "prompt": FUNC_PROMPT_SOURCE_CODE,
            "hint": FUNC_HINT_SOURCE_CODE,
            "handler": click_button_source_code
        }
    ]

    async def send_message_click_wrapper(e):
        asyncio.create_task(send_message_click(e, page, chat, new_message))

    chat = ft.ListView(expand=True, spacing=10, auto_scroll=True)   # Q&A window

    # input title
    input_title = ft.Text(FUNC_LIST[func_id]["name"])

    # question input
    new_message = ft.TextField(
        hint_text=FUNC_LIST[func_id]["hint"],
        autofocus=True,
        shift_enter=True,
        min_lines=1,
        max_lines=5,
        filled=True,
        on_submit=send_message_click_wrapper,  # ✅ 按回车键触发发送
    )

    # question send button
    send_button = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED,
        tooltip="Send message",
        on_click=send_message_click_wrapper,
    )

    input_row = ft.Row(
        controls = [
            ft.Column([input_title, new_message], spacing=5, expand=True),
            ft.Column([send_button], spacing=5),
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
        ft.Container(content=chat, border=ft.border.all(1, ft.Colors.OUTLINE), border_radius=5, padding=10, expand=True),
        dropdown_row,
        input_row,
        Func_Row
    )

    page.add(settings_dialog)
    page.open(settings_dialog)

if __name__ == "__main__":
    ft.app(target=main)
