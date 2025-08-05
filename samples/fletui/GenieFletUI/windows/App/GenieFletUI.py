import flet as ft
import asyncio
from openai import OpenAI
from datetime import datetime
from enum import Enum

class RunningEnv(Enum):
    ANDROID = 1
    WINDOWS = 2

running_env = RunningEnv.WINDOWS

CHROMADB_DIRECTORY = "database/chroma_db"  # ChromaDB 存储路径。用于私有问答功能
LOG_FILE = "app.log"
LOG_ENABLE = False
IMAGE_GEN_ENABLE = False
MODEL_LIST_CONFIG_FILE = ""

rssmanager = None
chromadb_path = ""
chroma_db = None
bm25 = None
embeddings = None
reranker = None
log_path = ""
base_path = ""

print("Start import libary: ", datetime.now())

if running_env is RunningEnv.WINDOWS:
    from pathlib import Path
    import logging
    import sys
    import os
    
    if getattr(sys, 'frozen', False):   # this is windows exe environment
        base_path = Path(sys.executable).parent
        package_path = sys._MEIPASS   # file 'models.yaml' is packaged into exe. So, the path is not .exe path, but package path
        MODEL_LIST_CONFIG_FILE = os.path.join(package_path, 'assets', 'models.yaml')
    else:   # this is windows python environment
        base_path = Path(__file__).resolve().parent
        MODEL_LIST_CONFIG_FILE = os.path.join(str(base_path), 'assets', 'models.yaml')

    if LOG_ENABLE is True:
        log_path = str(base_path / LOG_FILE)
        logging.basicConfig(filename=log_path, level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8')
        logging.debug("Import libary start: {time}".format(time=datetime.now()))
    else:
        logging.disable(logging.CRITICAL)

    from PIL import Image
    import base64
    from PyPDF2 import PdfReader
    from RagSaveStatusManager import RagSaveStatusManager, SaveStatus
    chromadb_path = base_path / CHROMADB_DIRECTORY
    rssmanager = RagSaveStatusManager(str(base_path / "database"))
    if rssmanager.get_status() == SaveStatus.DONE:   # chroma_db vector database is ready
        from GGUF import GGUFEmbedding        
        import pickle
        from rank_bm25 import BM25Okapi
        import jieba
        os.environ['TRANSFORMERS_OFFLINE'] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ['HF_DATASETS_OFFLINE'] = "1"
        os.environ['HF_ENDPOINT'] = "https://hf-mirror.com"
        os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
        os.environ["CHROMADB_DISABLE_TELEMETRY"] = "1"
    logging.debug("Import libary complete: {time}".format(time=datetime.now()))

print("Import libary complete: ", datetime.now())

PAGE_CONTENT_FACTOR = 0.8
CIRCLEAVATAR_WIDTH = 50

DEFAULT_THEME = "dark"
DEFAULT_FONTCOLOR = "cyan700"
DEFAULT_LLM = "qwen"

BM25_DIR = str(base_path / "database" / "bm25")

# Candidate: models/bge-m3-GGUF/bge-m3-Q4_K_M.gguf, ...
EMBEDDING_MODEL_PATH = base_path / "models" / "bge-m3-GGUF"
EMBEDDING_MODEL = str(EMBEDDING_MODEL_PATH / "bge-m3-Q4_K_M.gguf")
# Candidate: models/ms-marco-MiniLM-L-6-v2, models/bge-reranker-v2-m3, models/Qwen3-Reranker-0.6B, models/Qwen3-Reranker-4B, cross-encoder/ms-marco-MiniLM-L-6-v2 (online)
RERANKER_MODEL  = str(base_path / "models" / "ms-marco-MiniLM-L-6-v2")

FUNC_ID_SOLUTION    = 0
FUNC_ID_TRANSLATE   = 1
FUNC_ID_SOURCE_CODE = 2
FUNC_ID_DOC_SUMMARY = 3
FUNC_ID_IMAGE_GEN   = 4
FUNC_ID_PRIVATE_QA  = 5

FUNC_NAME_SOLUTION    = "📐 Solution"
FUNC_NAME_TRANSLATE   = "🌐 Translate"
FUNC_NAME_SOURCE_CODE = "📜 Code Analyze"
FUNC_NAME_DOC_SUMMARY = "📚 Doc Summary"
FUNC_NAME_IMAGE_GEN   = "🎨 Image Gen"
FUNC_NAME_PRIVATE_QA  = "🤖 Private Q&A"

FUNC_PROMPT_SOLUTION    = "{prompt}"
FUNC_PROMPT_TRANSLATE   = "将以下内容翻译成{lang}\n{prompt}"
FUNC_PROMPT_SOURCE_CODE = "请帮忙分析源代码，分析是否有潜在问题。如果没有问题，请给出详细注释。代码如下\n{prompt}"
FUNC_PROMPT_DOC_SUMMARY = "这是部分文档内容:\n{file_content}\n请总结这部分的主要内容"
FUNC_PROMPT_IMAGE_GEN   = "{prompt}"
FUNC_PROMPT_PRIVATE_QA  = "{prompt}"

FUNC_HINT_SOLUTION    = "What can I do for you? ..."
FUNC_HINT_TRANSLATE   = "Please input your sentence ..."
FUNC_HINT_SOURCE_CODE = "Please provide your source code ..."
FUNC_HINT_DOC_SUMMARY = "Click right button and upload your file. Only pdf or txt file which size less than 1 MB is supported ..."
FUNC_HINT_IMAGE_GEN   = "Please describe your picture ..."
FUNC_HINT_PRIVATE_QA  = "What is your question? ..."

ERROR_MESSAGE_GENIESERVER_IN_USING      = "Genie server is in using. Please try again later."
ERROR_MESSAGE_GENIESERVER_NOT_AVAILABLE = "Genie server is not available. Please check if network or GenieAPIService (Address or Port) is working or not."
ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR = "Genie server exception happens. Error message: {errmsg}"
ERROR_MESSAGE_MODEL_LIST_FAILURE        = "Genie server is not working\n\nPlease launch GenieAPIService and restart this application!"
ERROR_MESSAGE_DATABASE_ERROR            = "Something wrong with database. Please try again later."

selected_file = ""

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

def download_model():
    global base_path
    if not os.path.exists(EMBEDDING_MODEL):
        try:
            os.environ["HF_HUB_OFFLINE"] = "0"   # 临时切换为在线模式
            os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "3600"   # 临时设置超时1小时，下载模型可能会比较耗时
            from ModelDownloader import ModelDownloader
            downloader = ModelDownloader(max_workers=1)
            downloader.download_from_config(MODEL_LIST_CONFIG_FILE, str(base_path))
            return True
        except Exception as e:
            print(f"❌ downloading model failure: {e}")
            return False
        finally:            
            os.environ["HF_HUB_OFFLINE"] = "1"   # 确保下载后恢复离线模式（无论是否成功）
            os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"   # 确保下载后恢复30秒超时（无论是否成功）

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

# 读取 PDF 并按页提取文本
def extract_text_from_pdf(pdf_path):
    reader = PdfReader(pdf_path)
    pages_text = [reader.pages[page_num].extract_text() for page_num in range(len(reader.pages))]  # 按页提取文本
    return pages_text  # 返回每一页的文本列表

# 对单页或分批文本生成摘要
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

# 处理整个 PDF 并汇总最终摘要
def handle_large_pdf():
    global selected_file
    pages_text = extract_text_from_pdf(selected_file)
    partial_summaries = [generate_summary(text, False)[0] for text in pages_text]  # 逐页摘要
    combined_summary_prompt = "\n".join(partial_summaries)  # 合并所有摘要

    # 让 OpenAI 生成最终的整体摘要
    response, err_msg = generate_summary(combined_summary_prompt, True)    
    return response, err_msg

def generate_image(text: str) -> str:
    prompt_txt = edit_prompt(text)

    generation_response = client.images.generate(
        model = "Stable Diffusion 3",
        prompt=prompt_txt,
        n=1,
        size="512x512",
        response_format="url",
    )

    image_data = generation_response.data[0].b64_json
    if image_data:
        image_data = base64.b64decode(image_data)

        image_path = Path(__file__).resolve().parent / "images"
        if not image_path.is_dir():
            image_path.mkdir()
    
        image_file = image_path / "image.png"
        with open(image_file, mode="wb") as png:
            png.write(image_data)

        image = Image.open(image_file)
        image.show()

        return image_file
    else:
        return ""

def load_bm25():
    """load all chunk data and merge"""
    tokenized_corpus = []
    
    for filename in sorted(os.listdir(BM25_DIR)):
        if filename.endswith(".pkl"):
            with open(os.path.join(BM25_DIR, filename), "rb") as f:
                tokenized_corpus.extend(pickle.load(f))

    return BM25Okapi(tokenized_corpus)

def load_embedding_model():
    time_start = datetime.now()
    from langchain_chroma import Chroma
    time_consume = datetime.now() - time_start
    print("import Chroma consume: {consume}".format(consume=time_consume))
    logging.debug("creating Embedding model consume: {consume}".format(consume=time_consume))

    time_start = datetime.now()
    from sentence_transformers import CrossEncoder
    time_consume = datetime.now() - time_start
    print("import CrossEncoder consume: {consume}".format(consume=time_consume))
    logging.debug("creating Embedding model consume: {consume}".format(consume=time_consume))

    global embeddings, chroma_db, chromadb_path, reranker, bm25

    time_start = datetime.now()
    embeddings = GGUFEmbedding(EMBEDDING_MODEL, n_threads=8)
    time_consume = datetime.now() - time_start
    print("creating Embedding model consume: {consume}".format(consume=time_consume))
    logging.debug("creating Embedding model consume: {consume}".format(consume=time_consume))

    time_start = datetime.now()    
    chroma_db = Chroma(persist_directory=str(chromadb_path), embedding_function=embeddings)
    time_consume = datetime.now() - time_start
    print("load existed ChromaDB consume: {consume}".format(consume=time_consume))
    logging.debug("load existed ChromaDB consume: {consume}".format(consume=time_consume))

    time_start = datetime.now()
    reranker = CrossEncoder(RERANKER_MODEL, local_files_only=True, trust_remote_code=True)
    time_consume = datetime.now() - time_start
    print("create reranker model consume: {consume}".format(consume=time_consume))
    logging.debug("create reranker model consume: {consume}".format(consume=time_consume))

    time_start = datetime.now()
    bm25 = load_bm25()
    time_consume = datetime.now() - time_start
    print("load bm25 done consume: {consume}".format(consume=time_consume))
    logging.debug("load bm25 done consume: {consume}".format(consume=time_consume))
    
def min_max_normalize(scores):
    min_score = min(scores)
    max_score = max(scores)
    return [(s - min_score) / (max_score - min_score) if max_score > min_score else 0 for s in scores]

def hybrid_search(query, top_k=5, embedding_weight=0.6):
    global bm25, chroma_db, reranker

    # 1. BM25 search
    tokenized_query = list(jieba.cut(query))
    bm25_scores = bm25.get_scores(tokenized_query)   # total score list
    bm25_top_idx = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)[:top_k]
    top_k_bm25_score = [bm25_scores[i] for i in bm25_top_idx]   # top k score list
    normalized_bm25_scores = min_max_normalize(top_k_bm25_score)   # Normalization bm25 score range: 0~1
    bm25_weight = 1 - embedding_weight
    bm25_results = []
    print("bm25 chunk number: ", len(normalized_bm25_scores))
    for i in range(min(top_k, len(normalized_bm25_scores))):
        doc_content = chroma_db.get(include=["documents"])["documents"][bm25_top_idx[i]]
        bm25_results.append((doc_content, normalized_bm25_scores[i] * bm25_weight))

    # 2. Vector search
    time_start = datetime.now()
    dense_results = chroma_db.similarity_search_with_score(query, top_k)
    time_consume = datetime.now() - time_start
    print("Vector search consume: {consume}".format(consume=time_consume))
    # dense_results is list[tuple[Document, float]]. Where, float means distance for each. Lower value represents more similarity (higher score).
    vector_scores = [(1-score) for _, score in dense_results]
    normalized_vector_scores = min_max_normalize(vector_scores)   # Normalization vector score range: 0~1

    # 3. Merge search result
    hybrid_results = {}
    i=0
    for doc, s in dense_results:   # 5 bm25 results + 5 vector results = total 10 results
        hybrid_results[doc.page_content] = hybrid_results.get(doc.page_content, 0) + (normalized_vector_scores[i] * embedding_weight)
        i += 1

    for doc_content, score in bm25_results:
        hybrid_results[doc_content] = hybrid_results.get(doc_content, 0) + score

    # 4. Prepare for Reranking
    rerank_candidates = [{"text": doc, "score": score} for doc, score in hybrid_results.items()]
    pairs = [(query, candidate["text"]) for candidate in rerank_candidates]

    # 5. Apply Reranker
    time_start = datetime.now()
    rerank_scores = reranker.predict(pairs)
    time_consume = datetime.now() - time_start
    print("Reranker consume: {consume}".format(consume=time_consume))
    for idx, score in enumerate(rerank_scores):
        rerank_candidates[idx]["rerank_score"] = score

    # 6. Sort by Reranker scores
    sorted_result = sorted(rerank_candidates, key=lambda x: x["rerank_score"], reverse=True)
    print("search original chunk number: ", len(sorted_result))

    '''
    GenieAPI service has a limitation of max prompt tokens 3072. It will split the prompt if prompt token is more than 3072. 
    If split prompt, reference will be regarded as a new question and it brings a bad user experience. 
    So, searching result will be limited to max 7 or 8. 
    The reason is, each searching result (chunk + score + reranker score) has about 400 tokens (for debug purpose). 400 x 7 = 2800 tokes.
    Actually score and reranker score are not necessary to deliver to LLM. Only one chunk has about 300+ tokesn. So, top 8 is fine in this scenario.
    '''
    final_result = sorted_result[:6] if len(sorted_result) > 6 else sorted_result
    return final_result

def handle_private_QA(query: str):
    global rssmanager

    if rssmanager.get_status() != SaveStatus.DONE:
        print(f"❌ Database is not available")
        return None, "Database is not available"
    
    try:
        docs = hybrid_search(query)
    except Exception as e:
        print(f"❌ Exception happens on hybrid_search: {e}")
        return None, str(e)

    print("hybrid_search return chunk number: ", len(docs))
    #docs_text = "\n".join(map(str, docs))   # this includes scores for debugging
    docs_text = "\n".join(item["text"] for item in docs)   # only reference txt, doesn't includes scores

    prompt = (
        "你是一个私人智能问答助手，能够严格基于提供的参考资料回答问题。如果参考资料中没有与问题相关的信息，请回答“未找到相关信息”，不要回答与参考资料无关的信息。\n"
        f"问题: {query}\n"
        f"参考资料:\n{docs_text}\n"
    )

    response, err_msg = generate_summary(prompt, True)    
    return response, err_msg

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
        case 3:
            # 这里实际输入的是文档的内容，而不是用户的输入
            prompt_format = FUNC_PROMPT_DOC_SUMMARY.format(file_content=user_input)
            return prompt_format
        case 4:            
            prompt_format = FUNC_PROMPT_IMAGE_GEN.format(prompt=user_input)
            return prompt_format
        case 5:            
            prompt_format = FUNC_PROMPT_PRIVATE_QA.format(prompt=user_input)
            return prompt_format
        case _:
            return None

async def disable_all_controls(page, status: bool):
    for control in page.controls:
        control.disabled = status
    page.update()

async def send_message_click(e, page: ft.Page, chat: ft.ListView, new_message: ft.TextField):
    global selected_file, current_fontcolor
    if new_message.value.strip():
        if func_id is FUNC_ID_DOC_SUMMARY and selected_file == "":
            new_message.hint_text = "请选择一个文件！"
            new_message.value = ""
            page.update()
            return

        new_message.disabled = True

        m_user = Message("User", new_message.value)
        cm_user = ChatMessage(m_user, page.width, current_fontcolor)
        chat.controls.append(cm_user)

        if func_id is FUNC_ID_DOC_SUMMARY or func_id is FUNC_ID_IMAGE_GEN or func_id is FUNC_ID_PRIVATE_QA:
            cm_ai = ChatMessage(Message("AI", "分析生成中，请耐心等待..."), page.width, current_fontcolor)
            chat.controls.append(cm_ai)

        new_message.value = ""
        new_message.focus()
        page.update()
        await disable_all_controls(page, True)   # ✅ 禁用所有控件
        await asyncio.sleep(0.2)  # 让 UI 先处理

        if func_id is FUNC_ID_IMAGE_GEN:
            image_file = generate_image(m_user.text)
            ai_answer = "Generate image {imagefile} successfully".format(imagefile=image_file) if image_file else "Fail to generate image"
            cm_ai = ChatMessage(Message("AI", ai_answer), page.width, current_fontcolor)
            chat.controls.append(cm_ai)
            await disable_all_controls(page, False)   # ✅ 任务完成，恢复所有控件
            new_message.disabled = False   # **恢复输入框**
            page.update()
            return
        elif func_id is FUNC_ID_DOC_SUMMARY:
            response, err_msg = handle_large_pdf()  # 阻塞UI，分析过程中，App不响应
        elif func_id is FUNC_ID_PRIVATE_QA:
            response, err_msg = handle_private_QA(m_user.text)  # 阻塞UI，分析过程中，App不响应
        else:
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
            elif "Database is not available".lower() in err_msg.lower():
                ai_text_component.value = ERROR_MESSAGE_DATABASE_ERROR
            else:
                ai_text_component.value = ERROR_MESSAGE_GENIESERVER_GENERAL_ERROR.format(errmsg=err_msg)
            chat.scroll_to(len(chat.controls) - 1)

        await disable_all_controls(page, False)   # ✅ 任务完成，恢复所有控件
        new_message.disabled = False
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
    global func_id, rssmanager, running_env, running_llm, current_theme, current_fontcolor

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

    def click_button_doc_summary(e):
        global func_id
        func_id = FUNC_ID_DOC_SUMMARY
        update_func_UI()

    def click_button_translate(e):
        global func_id
        func_id = FUNC_ID_TRANSLATE
        update_func_UI()

    def click_button_image_gen(e):
        global func_id
        func_id = FUNC_ID_IMAGE_GEN
        update_func_UI()

    def click_button_source_code(e):
        global func_id
        func_id = FUNC_ID_SOURCE_CODE
        update_func_UI()

    async def click_button_private_QA(e):
        global func_id, chroma_db, current_fontcolor, rssmanager

        if rssmanager.get_status() != SaveStatus.DONE:
            cm_ai = ChatMessage(Message("AI", ERROR_MESSAGE_DATABASE_ERROR), page.width, current_fontcolor)
            chat.controls.append(cm_ai)
            page.update()
            return

        func_id = FUNC_ID_PRIVATE_QA
        update_func_UI()

        if chroma_db is not None:   # already load
            print("ChromaDB has already been loaded: ", datetime.now())
            return

        page.open(progress_dialog)
        page.update()
        await asyncio.sleep(0.1)
        time_start = datetime.now()
        download_model()
        load_embedding_model()
        passing_time = datetime.now() - time_start
        page.close(progress_dialog)
        cm_ai = ChatMessage(Message("AI", "Model loaded successfully! Time-consuming: {time}".format(time=passing_time)), page.width, current_fontcolor)
        chat.controls.append(cm_ai)
        page.update()

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
    if running_env is RunningEnv.WINDOWS:
        FUNC_LIST.append(
            {
                "id": FUNC_ID_DOC_SUMMARY,
                "name": FUNC_NAME_DOC_SUMMARY,
                "prompt": FUNC_PROMPT_DOC_SUMMARY,
                "hint": FUNC_HINT_DOC_SUMMARY,
                "handler": click_button_doc_summary
            }
        )
        if IMAGE_GEN_ENABLE is True:
            FUNC_LIST.append(
                {
                    "id": FUNC_ID_IMAGE_GEN,
                    "name": FUNC_NAME_IMAGE_GEN,
                    "prompt": FUNC_PROMPT_IMAGE_GEN,
                    "hint": FUNC_HINT_IMAGE_GEN,
                    "handler": click_button_image_gen
                }
            )
        if rssmanager.get_status() == SaveStatus.DONE:   # chroma_db vector database is ready
            FUNC_LIST.append(
                {
                    "id": FUNC_ID_PRIVATE_QA,
                    "name": FUNC_NAME_PRIVATE_QA,
                    "prompt": FUNC_PROMPT_PRIVATE_QA,
                    "hint": FUNC_HINT_PRIVATE_QA,
                    "handler": click_button_private_QA
                }
            )

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

    # file upload button
    def on_file_selected(e: ft.FilePickerResultEvent):
        global selected_file
        if e.files:
            file_path = Path(e.files[0].path)
            file_size = file_path.stat().st_size  # 文件大小（字节）
            if file_size > 0 and file_size < 1024*1024 and (e.files[0].path[-4:] == ".pdf" or e.files[0].path[-4:] == ".txt"):
                selected_file = e.files[0].path
                new_message.value = selected_file
            else:
                new_message.value = ""
                new_message.hint_text = "Please select pdf/txt file which size is less than 1 MB"
            page.update()

    file_picker = ft.FilePicker(on_result=on_file_selected)
    upload_button = ft.IconButton(
        icon=ft.Icons.UPLOAD_FILE,
        tooltip="Upload file",
        on_click=lambda _: file_picker.pick_files(allow_multiple=False, allowed_extensions=["pdf", "txt"]),
    )
    upload_button.visible = False
    page.overlay.append(file_picker)

    input_row = ft.Row(
        controls = [
            ft.Column([input_title, new_message], spacing=5, expand=True),
            ft.Column([upload_button, send_button], spacing=5),
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
        input_row.controls[0].controls[1].read_only = True if func_id is FUNC_ID_DOC_SUMMARY else False
        input_row.controls[1].controls[0].visible = True if func_id is FUNC_ID_DOC_SUMMARY else False
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
    page.get_control

    progress_dialog = ft.AlertDialog(
        modal=True,
        content=ft.Container(
            height=200,
            content=ft.Column([
                ft.Text("Loading model. Please wait...", size=18),
                ft.Container(height=40),
                ft.ProgressRing(width=50, height=50, stroke_width=3, color=ft.Colors.BLUE_600)
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            scroll="auto"
            )
        ),
        adaptive=True,
        inset_padding=100,  
        alignment=ft.alignment.center,
        content_padding=ft.padding.all(30),
    )
    page.add(progress_dialog)

    page.add(settings_dialog)
    page.open(settings_dialog)

    print("Launch GenieFletUI end: ", datetime.now())


if __name__ == "__main__":
    ft.app(target=main)
