from llama_cpp import Llama
from langchain.embeddings.base import Embeddings
from concurrent.futures import ThreadPoolExecutor, as_completed

class GGUFEmbedding(Embeddings):
    def __init__(self, model_path, n_threads=4, batch_size=32):
        self.llm = Llama(model_path=model_path, embedding=True, n_threads=n_threads)
        self.batch_size = batch_size
        self.max_tokens = self._detect_context_window()
        print(f"Model context: {self.max_tokens} tokens")

    def _detect_context_window(self):
        try:
            return self.llm.context_params.ctx_len
        except:
            return int(self.llm.metadata.get("context_length", 512))

    def _clean_text(self, text):
        text = text.strip().replace('\n', ' ')
        return text[:self.max_tokens]  # 避免超长输入

    def embed_documents(self, texts):
        texts = [self._clean_text(t) for t in texts if t.strip()]  # 过滤空文本 + 截断

        embeddings = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(self._safe_embed, t) for t in batch]
                for future in as_completed(futures):
                    result = future.result()
                    if result is not None:
                        embeddings.append(result)
        return embeddings

    def _safe_embed(self, text):
        try:
            return self.llm.embed(text)
        except Exception as e:
            print(f"[警告] 文本嵌入失败: {e}")
            return None

    def embed_query(self, text):
        return self._safe_embed(self._clean_text(text))
