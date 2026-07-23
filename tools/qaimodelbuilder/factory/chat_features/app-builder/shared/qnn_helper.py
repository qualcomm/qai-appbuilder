# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
qnn_helper
==========

QNNContext 加载与推理封装。基于 Qualcomm 的 `qai_appbuilder` 模块。

设计原则：
  • 仅做"加载 + 一次 inference 调用"两个最常用动作，最多支持多输入多输出 numpy 数组。
  • 不强制依赖 qai_appbuilder：import 失败时给出清晰的 NotImplementedError，让 Pack runner emit error。
  • 不耦合具体 Pack 的张量布局；Pack 自己负责前后处理。

多模型同进程注意事项（sticky worker）：
  • model_name 必须全局唯一 — 用 parent_dir + stem 组合（如 "whisper-base_encoder"）。
  • QNNConfig.Config() 每进程只调一次 — 由 _qnn_configured 全局标记保证。
  • input_data_type / output_data_type 是 per-context 的，推荐统一使用 "native"。

典型用法（Pack runner.py 内）：
    from qnn_helper import QnnContext
    ctx = QnnContext.load(
        Path('weights/encoder.bin'),
        runtime='Htp',
        input_data_type='native',
        output_data_type='native',
    )
    out = ctx.run([np.expand_dims(input_arr, 0)])
    # out 是 list[np.ndarray]
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("app_builder.qnn_helper")


# ── lazy import：所有 qai_appbuilder 调用走这一个入口 ────────────────────────
_QAI = None
_QAI_IMPORT_ERR: Optional[BaseException] = None


def _qai():
    """惰性 import qai_appbuilder；缓存第一次结果。失败时把异常存起来供 raise."""
    global _QAI, _QAI_IMPORT_ERR
    if _QAI is not None:
        return _QAI
    if _QAI_IMPORT_ERR is not None:
        raise NotImplementedError(
            f"qai_appbuilder import failed: {_QAI_IMPORT_ERR}. "
            "Real QNN inference requires the ARM64 Python venv with QAIRT installed; "
            "run Setup.bat to provision it."
        ) from _QAI_IMPORT_ERR
    try:
        import qai_appbuilder as _m   # type: ignore[import-not-found]
        _QAI = _m
        return _m
    except Exception as e:   # pylint: disable=broad-except
        _QAI_IMPORT_ERR = e
        raise NotImplementedError(
            f"qai_appbuilder is not available: {e}. "
            "Real QNN inference requires the ARM64 Python venv with QAIRT installed; "
            "run Setup.bat to provision it."
        ) from e


# ── 全局标记：QNNConfig.Config 在进程内只需调一次 ──────────────────────────────
# sticky worker 多模型场景中，第一个 QnnContext.load() 调 Config，后续 load 跳过。
# 重复调用 Config 会导致已加载 graph 的 runtime 状态异常。
_qnn_configured = False

# ── 活跃 context 引用计数 ──────────────────────────────────────────────────────
_active_context_count = 0


def reset_qnn_configured() -> None:
    """Reset the QNN configuration flag.

    Call this when all QNN contexts have been released (e.g., model unload
    in the sticky worker). The next QnnContext.load() will re-call
    QNNConfig.Config() to re-initialize the HTP runtime.

    WARNING: Only safe to call when NO QnnContext instances are alive
    in the process. Calling Config() with live graphs may corrupt state.
    """
    global _qnn_configured
    _qnn_configured = False
    logger.info("QNN configuration flag reset; next load will re-init HTP runtime")


# ── QnnContext 封装 ────────────────────────────────────────────────────────────

class QnnContext:
    """单段 QNN context binary 的薄封装。

    提供：
      QnnContext.load(bin_path, ...)  → QnnContext
      ctx.run(inputs: list[np.ndarray]) → list[np.ndarray]
      ctx.close()                       → None  (release backend; idempotent)

    多段（如 Whisper encoder/decoder）由调用方各创建一个 QnnContext，
    或自定义 wrapper 聚合。
    """

    def __init__(self, ctx: Any, name: str) -> None:
        global _active_context_count
        self._ctx = ctx
        self._name = name
        self._closed = False
        _active_context_count += 1
        logger.debug("QnnContext '%s' created (active=%d)", name, _active_context_count)

    @classmethod
    def load(
        cls,
        bin_path: Path | str,
        *,
        runtime: str = "Htp",
        log_level: int = 1,
        configured: bool = False,       # 保留向后兼容（ppocrv4 等仍传此参数），但已无实际作用
        input_data_type: str | None = None,
        output_data_type: str | None = None,
    ) -> "QnnContext":
        """Load a QNN context binary.

        Args:
            bin_path: Path to the .bin context binary file.
            runtime: QNN runtime backend ("Htp" or "Cpu").
            log_level: Log level (legacy int; mapped to LogLevel enum internally).
            input_data_type: "native" or "float" (default: None → QNNContext default).
                Use "native" for best performance — tensors are passed in
                model's native dtype without conversion.
            output_data_type: Same as input_data_type, for outputs.

        Returns:
            QnnContext wrapping the loaded model.
        """
        bin_path = Path(bin_path)
        if not bin_path.is_file():
            raise FileNotFoundError(f"QNN context bin not found: {bin_path}")

        m = _qai()

        # ── QNNConfig.Config：进程内只调一次 ────────────────────────────────
        # qai_appbuilder 2.47 的签名（我们锁定的目标版本）：
        #   QNNConfig.Config(runtime, log_level, profiling_level, log_path)
        # 不再有旧版的 lib_dir 首参（内部固定用包内 libs/）。
        # 失败绝不外抛：QNN 未配置最多让后续 QNNContext 构造报错，由调用方
        # 处理；配置这一步本身出错（版本/环境问题）不应中断进程或让宿主
        # 启动失败——记 warning 后带默认值继续。
        global _qnn_configured
        if not _qnn_configured and hasattr(m, "QNNConfig"):
            try:
                RT = getattr(m, "Runtime", None)
                LL = getattr(m, "LogLevel", None)
                PL = getattr(m, "ProfilingLevel", None)
                rt_val = getattr(RT, "HTP", runtime) if RT else runtime
                ll_val = getattr(LL, "WARN", log_level) if LL else log_level
                pl_val = getattr(PL, "BASIC", None) if PL else None
                if pl_val is not None:
                    m.QNNConfig.Config(rt_val, ll_val, pl_val)
                else:
                    m.QNNConfig.Config(rt_val, ll_val)
                _qnn_configured = True
            except Exception as e:   # pylint: disable=broad-except
                logger.warning(
                    "QNNConfig.Config raised %s; continuing with defaults", e
                )

        Cls = getattr(m, "QNNContext", None)
        if Cls is None:
            raise NotImplementedError("qai_appbuilder.QNNContext class missing")

        # ── 构建 kwargs ────────────────────────────────────────────────────
        kwargs: dict[str, Any] = {}
        if input_data_type is not None:
            kwargs["input_data_type"] = input_data_type
        if output_data_type is not None:
            kwargs["output_data_type"] = output_data_type

        # ── model_name 全局唯一 ────────────────────────────────────────────
        # 多模型同进程中不同模型可能都有 encoder.bin / decoder.bin，用 stem
        # 会撞名导致 QNN runtime 复用错误的 graph。
        # 用 parent_dir + stem 组合确保唯一，如 "whisper-base_encoder"。
        unique_name = f"{bin_path.parent.name}_{bin_path.stem}"

        try:
            ctx = Cls(model_name=unique_name, model_path=str(bin_path), **kwargs)
        except TypeError:
            # fallback：关键字参数不被接受时用位置参数。
            try:
                ctx = Cls(unique_name, str(bin_path))
            except TypeError as e:
                raise NotImplementedError(
                    f"QNNContext signature incompatible (kwargs={kwargs}): {e}"
                ) from e

        return cls(ctx, name=unique_name)

    def run(self, inputs: list) -> list:
        """执行一次推理。inputs 是 list[np.ndarray]；输出顺序与模型签名一致。"""
        if self._closed:
            raise RuntimeError(f"QnnContext '{self._name}' is closed")
        # qai_appbuilder 不同版本方法名不同：Inference / inference / __call__
        for method in ("Inference", "inference", "__call__"):
            fn = getattr(self._ctx, method, None)
            if callable(fn):
                out = fn(inputs)
                # 统一规范化为 list
                if out is None:
                    return []
                if isinstance(out, (list, tuple)):
                    return list(out)
                return [out]
        raise NotImplementedError(
            f"QNNContext object lacks Inference/inference/__call__: {type(self._ctx)}"
        )

    def close(self) -> None:
        global _active_context_count
        if self._closed:
            return
        self._closed = True
        try:
            release = getattr(self._ctx, "Release", None) or getattr(self._ctx, "release", None)
            if callable(release):
                release()
        except Exception as e:   # pylint: disable=broad-except
            logger.warning("QnnContext '%s' release raised: %s", self._name, e)
        _active_context_count = max(0, _active_context_count - 1)
        logger.debug("QnnContext '%s' closed (active=%d)", self._name, _active_context_count)
        if _active_context_count == 0:
            reset_qnn_configured()

    def __enter__(self) -> "QnnContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
