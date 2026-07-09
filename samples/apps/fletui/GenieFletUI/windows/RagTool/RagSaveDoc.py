# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import flet as ft
import asyncio
import os
import sys
from langchain.text_splitter import RecursiveCharacterTextSplitter
import fitz  # PyMuPDF
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain.schema import Document
import jieba
import pickle
from datetime import datetime
from pathlib import Path
from RagSaveStatusManager import RagSaveStatusManager

base_path = ""
MODEL_LIST_CONFIG_FILE = ""

if getattr(sys, 'frozen', False):   # this is windows exe environment
    base_path = Path(sys.executable).parent
    package_path = sys._MEIPASS   # file 'models.yaml' is packaged into exe. So, the path is not .exe path, but package path
    MODEL_LIST_CONFIG_FILE = os.path.join(package_path, 'assets', 'models.yaml')
else:   # this is windows python environment
    base_path = Path(__file__).resolve().parent
    MODEL_LIST_CONFIG_FILE = os.path.join(str(base_path), 'assets', 'models.yaml')

BM25_DIR = str(base_path / "database" / "bm25")
CHROMADB_DIR = str(base_path / "database" / "chroma_db")
DONE_FILE_DIR = str(base_path / "database")

# Candidate: bge-m3, bge-base-zh-v1.5, Qwen3-Embedding-0.6B, Qwen3-Embedding-4B
EMBEDDING_MODEL_NAME = "bge-m3"
EMBEDDING_MODEL = str(base_path / "models" / EMBEDDING_MODEL_NAME)
EMBEDDING_CACHE_PATH = str(base_path / "database" / "embeddingCache" / EMBEDDING_MODEL_NAME)

SUPPORTED_DOCUMENT_TYPE = (".pdf", ".txt")

os.environ['TRANSFORMERS_OFFLINE'] = "1"
os.environ['HF_DATASETS_OFFLINE'] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ['HF_ENDPOINT'] = "https://hf-mirror.com"
os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"

total_page_num = 0

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
            print(f"❌ downloading mode failure: {e}")
            return False
        finally:            
            os.environ["HF_HUB_OFFLINE"] = "1"   # 确保下载后恢复离线模式（无论是否成功）
            os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"   # 确保下载后恢复30秒超时（无论是否成功）

def save_new_bm25_chunk(new_docs):
    os.makedirs(BM25_DIR, exist_ok=True)
    
    """create new tokenized_corpus file. Don't change previous data"""
    tokenized_corpus = [list(jieba.cut(doc.page_content)) for doc in new_docs if doc.page_content.strip()]
    
    chunk_count = len(os.listdir(BM25_DIR)) + 1  # generate file number
    chunk_path = os.path.join(BM25_DIR, f"bm25_chunk_{chunk_count}.pkl")
    
    with open(chunk_path, "wb") as f:
        pickle.dump(tokenized_corpus, f)

def load_documents(directory: str):
    global total_page_num

    documents = []
    file_name = []
    total_page_num = 0
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(SUPPORTED_DOCUMENT_TYPE):
                path = os.path.join(root, file)
                file_name.append(path)
                doc = fitz.open(path)
                total_page_num += len(doc)
                for i in range(len(doc)):
                    text = doc[i].get_text()
                    if text.strip():
                        documents.append({"page_content": text, "metadata": {"source": path, "page": i + 1}})
    return documents, file_name

def split_text(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=700, chunk_overlap=0,
        separators=["。", "！", "？", ".", "?", "!", " ", ""]
    )
    return splitter.split_documents([
        Document(
            page_content=doc["page_content"],
            metadata={
                "source": doc["metadata"]["source"],
                "page": doc["metadata"]["page"]
            }
        ) for doc in docs
    ])

def add_newDoc(docs, persist_dir, embeddings) -> int:
    db = Chroma(persist_directory=persist_dir, embedding_function=embeddings)
    # 获取已存在的 ID（用 source 和 page 组成键）
    existing = set()
    if db._collection.count() > 0:
        existing_metas = db.get(include=["metadatas"])["metadatas"]
        for m in existing_metas:
            if m and "source" in m and "page" in m:
                existing.add((m["source"], m["page"]))

    # 去重
    new_docs = [doc for doc in docs if (doc.metadata["source"], doc.metadata["page"]) not in existing]
    if not new_docs:
        print("All documents are embedded. Exit...")
        return 0
    
    db.add_documents(new_docs)
    return len(new_docs)

def store_vectors(docs, persist_dir=CHROMADB_DIR) -> int:
    if not docs:
        return None
    
    chunk_num = len(docs)

    os.makedirs(EMBEDDING_CACHE_PATH, exist_ok=True)

    time_start = datetime.now()
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        cache_folder=EMBEDDING_CACHE_PATH,
        model_kwargs={
            "trust_remote_code": True,
            "local_files_only": True
        },
        encode_kwargs={
            "normalize_embeddings": True,
            "batch_size": 64
        }
    )
    time_consume = datetime.now() - time_start
    print("creating Embedding model consume: {consume}".format(consume=time_consume))

    ChromaDB_file = persist_dir + "/chroma.sqlite3"
    time_start = datetime.now()
    if os.path.isfile(ChromaDB_file):
        print("load existed ChromaDB: ")
        chunk_num = add_newDoc(docs, persist_dir, embeddings)   # 加载已存在的向量数据库        
    else:
        print("create new ChromaDB: ")
        Chroma.from_documents(docs, embeddings, persist_directory=persist_dir)   # 创建新的向量数据库
    time_consume = datetime.now() - time_start
    print("Save ChromaDB consume: {consume}".format(consume=time_consume))
    return chunk_num

def write_to_database(selected_folder: str) -> tuple[int, list]:
    global total_page_num

    time_start = datetime.now()
    docs, file_list = load_documents(selected_folder)
    time_consume = datetime.now() - time_start
    print(f"Load document consume: {time_consume}")

    time_start = datetime.now()
    split_docs = split_text(docs)
    time_consume = datetime.now() - time_start
    print(f"Split docs consume: {time_consume}")
    filtered_docs = [doc for doc in split_docs if doc.page_content.strip()]

    time_start = datetime.now()
    save_new_bm25_chunk(filtered_docs)
    time_consume = datetime.now() - time_start
    print(f"Save bm25 consume: {time_consume}")

    time_start = datetime.now()
    chunk_num = store_vectors(filtered_docs)
    time_consume = datetime.now() - time_start
    print(f"Store vectors [Page: {total_page_num}, Chunk: {chunk_num}] consume: {time_consume}")
    return chunk_num, file_list

def handle_document(selected_folder: str):
    rssmanager = RagSaveStatusManager(DONE_FILE_DIR)
    rssmanager.begin_writing()

    try:
        chunk_num, file_list = write_to_database(selected_folder)
        rssmanager.create_done(file_list)
        return chunk_num
    except Exception as e:
        print(f"❌ Exception happens on database: {e}")
        return None
    finally:
        rssmanager.end_writing()

def main(page: ft.Page):
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.title = "Embedding Database"
    page.theme_mode = "dark"

    global total_page_num

    chat = ft.ListView(expand=True, spacing=10, auto_scroll=True)

    # file upload button
    async def on_folder_selected(e: ft.FilePickerResultEvent):
        if e.path and os.path.isdir(e.path):
            files = [f for f in os.listdir(e.path) if f.endswith(SUPPORTED_DOCUMENT_TYPE)]
            if files:
                chat.controls.append(ft.Text("Target {folder}: is processing [{time}]...".format(folder=e.path, time=datetime.now())))
                input_row.disabled = True
                page.update()
                await asyncio.sleep(0.1)
                total_chunk = handle_document(e.path)
                if total_chunk is None:
                    chat.controls.append(ft.Text(f"Target {e.path}: database is corrupt! [{datetime.now()}]"))
                else:
                    chat.controls.append(ft.Text(f"Target {e.path}: complete [Total pages: {total_page_num}, Total chunks: {total_chunk}] [{datetime.now()}]"))
                input_row.disabled = False
            else:
                chat.controls.append(ft.Text("There is no pdf/txt file. Please select another folder..."))
            page.update()

    file_picker = ft.FilePicker(on_result=on_folder_selected)
    upload_button = ft.IconButton(
        icon=ft.Icons.DRIVE_FOLDER_UPLOAD_ROUNDED,
        tooltip="Select a folder",
        on_click=lambda _: file_picker.get_directory_path(),
    )
    page.overlay.append(file_picker)

    input_row = ft.Row(controls = [ft.Text("Please select a folder containing pdf/txt files..."), upload_button], spacing=10, tight=True)

    page.add(
        ft.Container(content=chat, border=ft.border.all(1, ft.Colors.OUTLINE), border_radius=5, padding=10, expand=True),
        input_row
    )
    page.update()

    async def init_and_download():
        result = await asyncio.to_thread(download_model)
        if result is True:
            input_row.disabled = False
            chat.controls.append(ft.Text("✨ Model is ready! Please select a folder containing pdf/txt files"))
        else:
            chat.controls.append(ft.Text("❌ Downloading model failure. Please close this application and try again later"))
            
        page.update()

    if not os.path.exists(EMBEDDING_MODEL):
        chat.controls.append(ft.Text("⏬ Downloading model. Please wait..."))
        input_row.disabled = True
        page.run_task(init_and_download)

    page.update()

if __name__ == "__main__":
    ft.app(target=main)
