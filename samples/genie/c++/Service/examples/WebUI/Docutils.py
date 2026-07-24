# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
from typing import Generator
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, PythonLoader, TextLoader
from langchain_community.document_loaders import DirectoryLoader
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.group import GroupShape
from chat import Chat

class PPTXLoader(BaseLoader):
    def __init__(self, file_path):
        
        self.file_path = file_path
        self.content = ""

    def extract_text_frame_info(self, text_frame):
        if text_frame and hasattr(text_frame, 'paragraphs'):
            for paragraph in text_frame.paragraphs:
                for run in paragraph.runs:
                    self.content += " " + run.text

    def extract_table_info(self, table):
        for row_idx, row in enumerate(table.rows):
            for col_idx, cell in enumerate(row.cells):
                self.extract_text_frame_info(cell.text_frame)

    def extract_shape_info(self, shape):
        if shape.shape_type == MSO_SHAPE_TYPE.TABLE:
            self.extract_table_info(shape.table)
        elif shape.has_text_frame:
            self.extract_text_frame_info(shape.text_frame)
        elif isinstance(shape, GroupShape):
            for sub_shape in shape.shapes:
                self.extract_shape_info(sub_shape)

    def extract_pptx_info(self, pptx_path):
        prs = Presentation(pptx_path)
        for slide_index, slide in enumerate(prs.slides):
            for shape in slide.shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.TABLE:
                    self.extract_table_info(shape.table)
                elif shape.has_text_frame:
                    self.extract_text_frame_info(shape.text_frame)
                elif isinstance(shape, GroupShape):
                    for sub_shape in shape.shapes:
                        self.extract_shape_info(sub_shape)

    def load(self):
        self.extract_pptx_info(self.file_path)
        documents = []
        documents.append(Document(page_content=self.content, metadata={"source": self.file_path}))
        return documents

class ManualDocSummarizer:
    def __init__(self):
        """
        :param service: 有 chat(prompt) 方法，返回 generator（流式 token）
        :param llm_tokenizer: 可选，用于估算 token 数量（如 tiktoken）
        """
        self.llm_service = Chat()
        self.split_documents = []
        self.is_initialized = False

    def _estimate_tokens(self, text: str) -> int:
        """估算文本 token 数量"""
        return self.llm_service.textsplit(text, max_length=2048)

    def load_and_split_docs(self, file_path: str, chunk_size: int = 1024):
        """
        使用 LangChain 加载并切分文档
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"路径不存在: {file_path}")

        documents = []

        # 定义各种加载器
        loaders = [
            (file_path, "**/*.pdf", PyPDFLoader, {"mode": "single"}),
            (file_path, "**/*.docx", Docx2txtLoader, {}),
            (file_path, "**/*.pptx", PPTXLoader, {}),
            (file_path, "**/*.txt", TextLoader, {"encoding": "utf-8"}),
            (file_path, "**/*.md", TextLoader, {"encoding": "utf-8"}),
            (file_path, "**/*.py", PythonLoader, {}),
            (file_path, "**/*.c", TextLoader, {"encoding": "utf-8"}),
            (file_path, "**/*.cpp", TextLoader, {"encoding": "utf-8"}),
            (file_path, "**/*.h", TextLoader, {"encoding": "utf-8"}),
            (file_path, "**/*.hpp", TextLoader, {"encoding": "utf-8"}),
        ]

        for path, glob, loader_cls, kwargs in loaders:
            try:
                loader = DirectoryLoader(
                    path,
                    glob=glob,
                    loader_cls=loader_cls,
                    loader_kwargs=kwargs,
                    use_multithreading=True,
                )
                docs = loader.load()
                documents.extend(docs)
            except Exception as e:
                print(f"加载 {glob} 时出错: {e}")

        if not documents:
            raise ValueError("未加载到任何文档")

        self.full_text = "\n\n".join([doc.page_content.strip() for doc in documents if doc.page_content.strip()])
        self.split_documents = self.llm_service.textsplit(self.full_text, max_length=chunk_size)
        self.is_initialized = True
        return f"✅ 已加载并切分文档，共 {len(self.split_documents)} 个片段\n"

    def _stream_chat(self, prompt: str) -> Generator[str, None, str]:
        """
        调用 service.chat 并流式返回结果。
        注意：必须确保 generator 被完全消费，避免并发。
        """
        try:
            # 这里 chat() 返回一个 generator
            token_gen = self.llm_service.chat(prompt)
            response = ""
            for token in token_gen:
                response += token
                yield token  # 实时输出给外层
            # ✅ generator 被完全消费，请求结束
        except Exception as e:
            error = f"[ERROR] chat 调用失败: {e}"
            yield error
            return

    def summarize_map_reduce(self, custom_prompt: str = "", max_chunk_tokens: int = 1024) -> Generator[str, None, str]:
        if not self.is_initialized:
            yield "❌ 请先调用 load_and_split_docs\n"
            return

        yield "🚀 开始 Map 阶段：逐段生成摘要...\n"

        intermediate_summaries = []
        total_chunks = len(self.split_documents)
        docs = []
        for doc in self.split_documents:
            docs.append(doc["text"])

        for idx, text in enumerate(docs):
            if not text.strip():
                continue

            map_prompt = f"""
            你的工作是用中文写出以下文档的摘要:
            ================文档内容开始================
            {text}
            ================文档内容结束================
            编写摘要的附加说明或要求：{custom_prompt}
            中文摘要:
            """
            yield f"📌 正在处理第 {idx+1}/{total_chunks} 段...\n"
            yield "💬 摘要生成中："

            summary_tokens = []
            try:
                for token in self._stream_chat(map_prompt):
                    summary_tokens.append(token)
                    yield token  # 实时转发给前端
                summary = ''.join(summary_tokens)
                yield "\n✅ 第 %d 段摘要完成\n\n" % (idx+1)
            except Exception as e:
                yield f"\n❌ 第 {idx+1} 段生成失败: {e}\n"
                summary = ""

            intermediate_summaries.append(summary)

        if not intermediate_summaries:
            yield "❌ 未生成任何摘要\n"
            return
        if total_chunks > 1:
            yield "🔗 开始 Reduce 阶段：合并所有摘要...\n"
            combined = "\n\n".join([f"[摘要 {i+1}]\n{s}" for i, s in enumerate(intermediate_summaries)])

            reduce_prompt = f"""
                            你是一个专业文档摘要助手。请将以下多个段落的摘要整合成一篇连贯、简洁的最终摘要，突出整体核心要点，避免重复。

                            编写摘要的附加说明或要求：{custom_prompt}\n

                            要合并的段落内容：{combined}\n

                            最终中文摘要：
                            """
            yield "🎯 正在生成最终摘要...\n"
            # ✅ 同样：完全消费 reduce 阶段的 generator
            for token in self._stream_chat(reduce_prompt):
                yield token
   

    def summarize_refine(self, custom_prompt: str = "", max_chunk_tokens: int = 3500) -> Generator[str, None, str]:
        """
        手动实现 refine 模式：逐步精炼摘要
        """
        if not self.is_initialized:
            yield "❌ 请先调用 load_and_split_docs"
            return

        yield "🔄 开始 Refine 模式：逐步精炼摘要...\n"

        current_summary = ""

        for idx, doc in enumerate(self.split_documents):
            text = doc["text"]
            if not text:
                continue


            if idx == 0:
                prompt = f"""编写摘要的附加说明或要求：{custom_prompt}\n
                请对以下文本进行首次摘要，简洁明了：
                {text}
                首次摘要：
                """
                yield "📝 生成首次摘要...\n"
            else:
                prompt = f"""
                你的工作是编写最终摘要
                我们已经提供了一定程度的现有摘要：
                ================现有摘要开始================
                {current_summary}
                ================现有摘要结束================
                我们有机会完善现有的摘要"
                (only if needed) 下面有更多背景信息：
                ================新的背景信息开始================
                {text}\n"
                ================新的背景信息结束================
                鉴于新的背景信息，完善摘要。\n"
                编写摘要的附加说明或要求：{custom_prompt}\n中文摘要:
                """
                yield f"🔁 正在融合第 {idx+1} 段信息...\n"

            new_summary = ""
            for token in self._stream_chat(prompt):
                new_summary += token
            current_summary = new_summary

            yield f"✅ 当前摘要已更新（共 {idx+1}/{len(self.split_documents)} 段）\n\n"

        yield "✅ Refine 模式完成！最终摘要：\n"
        yield current_summary


if __name__ == "__main__":
    docx = ManualDocSummarizer()
    docx.load_and_split_docs(file_path="dir")

    for text in docx.summarize_refine(custom_prompt="你的工作是用中文写出以下文档的摘要:"):
        print(text, end="")
