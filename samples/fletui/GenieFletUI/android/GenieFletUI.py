import flet as ft
import asyncio
from openai import OpenAI

HOST = "127.0.0.1"
PORT = "8910"
PAGE_CONTENT_FACTOR = 0.8
CIRCLEAVATAR_WIDTH = 50

DEFAULT_THEME = "light"
DEFAULT_LLM = "qwen"

FUNC_ID_SOLUTION    = 0
FUNC_ID_TRANSLATE   = 1
FUNC_ID_SOURCE_CODE = 2

'''
FUNC_NAME_SOLUTION    = "📐 解题答疑"
FUNC_NAME_TRANSLATE   = "🗛 AI 翻 译"
FUNC_NAME_SOURCE_CODE = "📜 代码分析"
'''
FUNC_NAME_SOLUTION    = "📐 Solution"
FUNC_NAME_TRANSLATE   = "🗛 Translate"
FUNC_NAME_SOURCE_CODE = "📜 Code Analyze"

FUNC_PROMPT_SOLUTION    = "{prompt}"
FUNC_PROMPT_TRANSLATE   = "将以下内容翻译成{lang}\n{prompt}"
FUNC_PROMPT_SOURCE_CODE = "请帮忙分析源代码，分析是否有潜在问题。如果没有问题，请给出详细注释。代码如下\n{prompt}"

FUNC_HINT_SOLUTION    = "What can I do for you? ..."
FUNC_HINT_TRANSLATE   = "Please input your sentence ..."
FUNC_HINT_SOURCE_CODE = "Please provide your source code ..."

func_id = FUNC_ID_SOLUTION
running_llm = ""
current_theme = DEFAULT_THEME

client = OpenAI(base_url=f"http://{HOST}:{PORT}/v1", api_key="123")

extra_body = {
    "n_predict": 4096, "seed": 146, "temp": 1.5,
    "top_k": 13, "top_p": 0.6, "penalty_last_n": 64,
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
    def __init__(self, message: Message, page_width: int = 1000, theme: str = DEFAULT_THEME):
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
                    color=ft.Colors.YELLOW if theme=="dark" else ft.Colors.CYAN_700,
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
            if isinstance(self.controls[0], ft.Text):  # this is "User" ChatMessage
                self.controls[0].width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            else:   # this is "AI" ChatMessage
                self.controls[1].width = (new_width-CIRCLEAVATAR_WIDTH)*PAGE_CONTENT_FACTOR
            self.update()

def get_model_list() -> tuple[list[str], str]:
    model_lst = client.models.list()
    modelname_lst = [model.id for model in model_lst.data]
    default_model = next((m for m in modelname_lst if DEFAULT_LLM.lower() in m.lower()), modelname_lst[0])
    return modelname_lst, default_model

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
    global running_llm

    prompt_txt = edit_prompt(text)

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt_txt}
    ]
    
    if stream_output is True:
        response = client.chat.completions.create(model=running_llm, stream=True, messages=messages, extra_body=extra_body)
        return response
    else:
        response = client.chat.completions.create(model=running_llm, messages=messages)
        return response.choices[0].message.content

async def send_message_click(e, page: ft.Page, chat: ft.ListView, new_message: ft.TextField):
    global selected_file, current_theme
    if new_message.value.strip():
        new_message.disabled = True

        m_user = Message("User", new_message.value)
        cm_user = ChatMessage(m_user, page.width, current_theme)
        chat.controls.append(cm_user)

        new_message.value = ""
        new_message.focus()
        page.update()
        await disable_all_controls(page, True)   # ✅ 禁用所有控件
        await asyncio.sleep(0.2)  # 让 UI 先处理

        response = generate_summary(m_user.text, True)

        cm_ai = ChatMessage(Message("AI", ""), page.width, current_theme)
        chat.controls.append(cm_ai)
        ai_text_component = cm_ai.controls[1]

        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                ai_text_component.value += chunk.choices[0].delta.content
                chat.scroll_to(len(chat.controls) - 1)   # 滚动到最下一行
                page.update()
                await asyncio.sleep(0)  # ✅ 让事件循环处理，给page机会刷新UI

        await disable_all_controls(page, False)   # ✅ 任务完成，恢复所有控件
        new_message.disabled = False   # **恢复输入框**
        page.update()    

def main(page: ft.Page):
    global func_id, running_llm, current_theme

    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.title = "AI Chat"
    page.theme_mode = current_theme    

    llm_lst, running_llm = get_model_list()

    def on_llm_change(e):
        global running_llm
        running_llm = e.control.value
        page.update()

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

    llm_dropdown = ft.Dropdown(
        label="Large language model",
        options=[ft.dropdown.Option(opt) for opt in llm_lst],
        value=running_llm,
        expand=True,
        on_change=on_llm_change
    )

    dropdown_row = ft.Row(
        controls=[theme_dropdown, llm_dropdown],
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

if __name__ == "__main__":
    ft.app(target=main)
