#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import os
import sys
import importlib.util

from qai_appbuilder.qnncontext import _register_context, _unregister_context

spec = importlib.util.find_spec("qai_appbuilder.geniebuilder")
if spec is not None:
    from qai_appbuilder import geniebuilder
# else:
#    print("geniebuilder is not exist.")

class GenieContext:
    """High-level Python wrapper for a GenieBuilder model."""

    _released = False

    def __init__(self,
                config: str = "None",
                debug: bool = False
    ) -> None:
        qnn_lib_path = "None"
        if qnn_lib_path in (None, "None", "") or not os.path.exists(qnn_lib_path):
            base_path = os.path.dirname(os.path.abspath(__file__))
            qnn_lib_path = os.path.join(base_path, "libs")

        if not sys.platform.startswith("win"):
            ADSP_LIBRARY_PATH = os.environ.get('ADSP_LIBRARY_PATH')
            if ADSP_LIBRARY_PATH is None or len(ADSP_LIBRARY_PATH) < 2:
                os.environ["ADSP_LIBRARY_PATH"] = qnn_lib_path
        self.config = config
        self.debug = debug
        self.m_context = geniebuilder.GenieContext(config, debug)
        _register_context(self)

    def Query(self, prompt, callback):
        return self.m_context.Query(prompt, callback)

    def QueryByEmbedding(self, embedding, callback):
        """Query using an embedding vector.

        Args:
            embedding (list[float]): Embedding vector to query with.
            callback (Callable[[str], bool]): Callback receiving streamed text.

        Returns:
            string: query response.
        """
        return self.m_context.QueryByEmbedding(embedding, callback)
    def SetEmbeddingTable(self, embedding_table):
        """Set the embedding table for retrieval.

        Args:
            embedding_table (list[dict]): List of dicts with 'id' and 'embedding' keys.
        """
        return self.m_context.SetEmbeddingTable(embedding_table)

    def Stop(self):
        return self.m_context.Stop()

    def SetParams(self, max_length, temp, top_k, top_p):
        return self.m_context.SetParams(max_length, temp, top_k, top_p)

    def SetStopSequence(self, stop_sequences):
        return self.m_context.SetStopSequence(stop_sequences)

    def GetProfile(self):
        return self.m_context.GetProfile()

    def SetLora(self, adapter_name, alpha_value):
        return self.m_context.SetLora(adapter_name, alpha_value)

    def TokenLength(self, text):
        return self.m_context.TokenLength(text)

    def release(self):
        """Deterministically release the underlying Genie context (idempotent)."""
        if getattr(self, "_released", False):
            return
        self._released = True
        m_context = getattr(self, "m_context", None)
        if m_context is not None:
            self.m_context = None
            try:
                del m_context
            except Exception as e:
                print(f"[WARN] Failed to release Genie context: {e}")
        try:
            _unregister_context(self)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass