# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""
guard64.dll Python 封装 (guard_wrapper.py)
==========================================

.. note::
   VERBATIM COPY of ``native/file-guard/guard/python/guard.py`` (2026-07-04
   native-hook integration, PR-1). The canonical source lives in the native
   source tree; this in-package copy exists so the production package does
   NOT depend on the ``native/file-guard/`` source directory at runtime
   (release excludes native sources). Keep this file byte-for-byte in sync
   with the upstream wrapper — do NOT hand-edit; re-copy on any DLL API
   change. See ``docs/85-tasks/native-file-guard-integration-2026-07-04.md``.

对应 C 头文件: samples/guard/guard.h

V1 导出函数（保持向后兼容，不动）:
    bool Init(filter)                          — 安装 hook 并注册回调（fail-open）
    bool InitEx(filter, fail_closed)           — 安装 hook，fail_closed=True 时管道异常返回 deny
    void Destroy()                             — 卸载 hook + 关闭管道
    bool AddRules(str, session_only)           — 添加黑名单前缀规则（命中则 deny）
    const char* ListRules()                    — 返回黑名单规则 JSON: {"rules":[]}
    bool DeleteRules(str, session_only)        — 删除黑名单前缀规则
    bool AddWhiteRules(str, session_only)      — 添加白名单前缀规则
    bool DeleteWhiteRules(str, session_only)   — 删除白名单前缀规则
    const char* ListWhiteRules()               — 返回白名单规则 JSON

V2 新增导出（本次 R8 / R10 / R11 / R12）:
    bool InitV2(filter_v2, fail_closed, callback_timeout_ms)
        R8: callback_timeout_ms 默认 60000 = 60 秒；超时按 fail_closed 决定 allow/deny
        R10: filter_v2 接收一个 FilterEventV2 struct 指针（含 parent_pid /
             process_path / command_line 三个新字段）
    bool AddProcessException(exe_path)          — R11: 添加进程豁免（前缀匹配）
    bool RemoveProcessException(exe_path)       — R11: 移除
    const char* ListProcessExceptions()         — R11: 返回 {"processes":[]}
    const char* GetDiagnostics()                — R12: 返回内部计数器/状态 JSON

事件类型 (Event):
    NONE = 0, WRITE = 1, DELETE = 2, EXECUTE = 3, READ = 4

规则匹配优先级:
    1. 命中黑名单（AddRules）→ deny
    2. 命中白名单（AddWhiteRules）→ allow（跳过 preFilter）
    3. 两者均未命中 → 调用 preFilter 回调
    进程豁免（AddProcessException）优先级最高：命中则整个进程绕过所有 hook。

V1 用法（保持不变）:
    from guard import Guard, Event
    g = Guard(r"path\\to\\guard64.dll")
    def my_filter(pid, event, path): return True
    g.init(my_filter)
    ...
    g.destroy()

V2 用法:
    from guard import Guard, Event, FilterEventV2
    g = Guard(r"path\\to\\guard64.dll")
    def my_filter_v2(evt: FilterEventV2) -> bool:
        print(f"pid={evt.pid} parent_pid={evt.parent_pid}")
        print(f"process_path={evt.process_path}")
        print(f"command_line={evt.command_line}")
        return True
    g.init_v2(my_filter_v2, fail_closed=True, callback_timeout_ms=5000)
    g.add_process_exception(r"C:\\Windows\\System32\\svchost.exe")
    print(g.list_process_exceptions())
    print(g.get_diagnostics())
    g.destroy()
"""

from __future__ import annotations

__all__ = [
    "Guard",
    "Event",
    "PreFilterFunc",
    "PreFilterFuncV2",
    "FilterEventV2",
    "GuardLoadError",
]

import ctypes
import json
import os
from ctypes import (
    c_bool,
    c_char_p,
    c_uint32,
    c_int,
    POINTER,
    Structure,
    CFUNCTYPE,
)
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional, List, Any


class Event(IntEnum):
    """文件操作事件类型 (对应 guard.h enum Event)"""
    NONE = 0
    WRITE = 1
    DELETE = 2
    EXECUTE = 3
    READ = 4


# V1 callback: bool (*)(DWORD dwPid, Event event, LPSTR chFilePath)
PreFilterFunc = CFUNCTYPE(c_bool, c_uint32, c_int, c_char_p)

# V1 Python-side callable
FilterCallable = Callable[[int, int, str], bool]


class _FilterEventV2Native(Structure):
    """ctypes 映射 guard.h::FilterEventV2 struct。仅内部使用。"""
    _fields_ = [
        ("pid", c_uint32),
        ("parent_pid", c_uint32),
        ("event", c_int),
        ("file_path", c_char_p),
        ("process_path", c_char_p),
        ("command_line", c_char_p),
    ]


# V2 callback: bool (*)(const FilterEventV2 *evt)
PreFilterFuncV2 = CFUNCTYPE(c_bool, POINTER(_FilterEventV2Native))


@dataclass
class FilterEventV2:
    """
    R10: filter callback 拿到的 rich event 结构（Python 侧）。

    Fields:
        pid          当前触发事件的进程 PID
        parent_pid   父进程 PID (0 表示未知)
        event        Event 数值 (0..4)
        file_path    被访问文件的 utf-8 绝对路径
        process_path 当前进程 exe 的 utf-8 绝对路径
        command_line 当前进程命令行 (utf-8)，可能为 ""（DLL 未获取到时）
    """
    pid: int
    parent_pid: int
    event: int
    file_path: str
    process_path: str
    command_line: str


#: V2 Python-side callable
FilterCallableV2 = Callable[[FilterEventV2], bool]


class GuardLoadError(OSError):
    """guard64.dll 加载失败时抛出的异常。"""
    pass


class Guard:
    """
    guard64.dll 的 Python 封装类。

    Parameters
    ----------
    dll_path : str
        guard64.dll 的完整路径。
    """

    def __init__(self, dll_path: str):
        if not dll_path:
            raise GuardLoadError("dll_path 不能为空")
        self._dll_path: str = os.path.normpath(dll_path)
        if not os.path.isfile(self._dll_path):
            raise GuardLoadError(f"DLL 文件不存在: {self._dll_path}")
        try:
            self._dll: ctypes.CDLL = ctypes.CDLL(self._dll_path)
        except OSError as e:
            raise GuardLoadError(f"加载 DLL 失败: {self._dll_path}") from e
        self._setup_prototypes()
        # Persistent ctypes callback holders (must outlive the DLL side).
        self._filter_ref: Optional[Any] = None       # PreFilterFunc instance
        self._filter_ref_v2: Optional[Any] = None    # PreFilterFuncV2 instance
        self._inited: bool = False

    @property
    def dll_path(self) -> str:
        return self._dll_path

    @property
    def is_inited(self) -> bool:
        return self._inited

    def _setup_prototypes(self):
        # V1 --------------------------------------------------------------
        self._dll.Init.argtypes = [PreFilterFunc]
        self._dll.Init.restype = c_bool

        self._dll.InitEx.argtypes = [PreFilterFunc, c_bool]
        self._dll.InitEx.restype = c_bool

        self._dll.Destroy.argtypes = []
        self._dll.Destroy.restype = None

        self._dll.AddRules.argtypes = [c_char_p, c_bool]
        self._dll.AddRules.restype = c_bool

        self._dll.ListRules.argtypes = []
        self._dll.ListRules.restype = c_char_p

        self._dll.DeleteRules.argtypes = [c_char_p, c_bool]
        self._dll.DeleteRules.restype = c_bool

        self._dll.AddWhiteRules.argtypes = [c_char_p, c_bool]
        self._dll.AddWhiteRules.restype = c_bool

        self._dll.DeleteWhiteRules.argtypes = [c_char_p, c_bool]
        self._dll.DeleteWhiteRules.restype = c_bool

        self._dll.ListWhiteRules.argtypes = []
        self._dll.ListWhiteRules.restype = c_char_p

        # V2 additive (guard by hasattr for backwards compat with older DLL) ---
        if hasattr(self._dll, "InitV2"):
            self._dll.InitV2.argtypes = [PreFilterFuncV2, c_bool, c_uint32]
            self._dll.InitV2.restype = c_bool
        if hasattr(self._dll, "AddProcessException"):
            self._dll.AddProcessException.argtypes = [c_char_p]
            self._dll.AddProcessException.restype = c_bool
        if hasattr(self._dll, "RemoveProcessException"):
            self._dll.RemoveProcessException.argtypes = [c_char_p]
            self._dll.RemoveProcessException.restype = c_bool
        if hasattr(self._dll, "ListProcessExceptions"):
            self._dll.ListProcessExceptions.argtypes = []
            self._dll.ListProcessExceptions.restype = c_char_p
        if hasattr(self._dll, "GetDiagnostics"):
            self._dll.GetDiagnostics.argtypes = []
            self._dll.GetDiagnostics.restype = c_char_p
        # Op-aware read-only whitelist (additive; guard by hasattr so an older
        # DLL without these exports still loads).
        if hasattr(self._dll, "AddReadOnlyWhiteRules"):
            self._dll.AddReadOnlyWhiteRules.argtypes = [c_char_p, c_bool]
            self._dll.AddReadOnlyWhiteRules.restype = c_bool
        if hasattr(self._dll, "DeleteReadOnlyWhiteRules"):
            self._dll.DeleteReadOnlyWhiteRules.argtypes = [c_char_p, c_bool]
            self._dll.DeleteReadOnlyWhiteRules.restype = c_bool
        if hasattr(self._dll, "ListReadOnlyWhiteRules"):
            self._dll.ListReadOnlyWhiteRules.argtypes = []
            self._dll.ListReadOnlyWhiteRules.restype = c_char_p
        # Op-masked whitelist (fully general op-aware allow; additive; guard by
        # hasattr so an older DLL without these exports still loads).
        if hasattr(self._dll, "AddOpMaskWhiteRules"):
            self._dll.AddOpMaskWhiteRules.argtypes = [c_char_p, c_int, c_bool]
            self._dll.AddOpMaskWhiteRules.restype = c_bool
        if hasattr(self._dll, "DeleteOpMaskWhiteRules"):
            self._dll.DeleteOpMaskWhiteRules.argtypes = [c_char_p, c_bool]
            self._dll.DeleteOpMaskWhiteRules.restype = c_bool
        if hasattr(self._dll, "ListOpMaskWhiteRules"):
            self._dll.ListOpMaskWhiteRules.argtypes = []
            self._dll.ListOpMaskWhiteRules.restype = c_char_p

    # =====================================================================
    # V1 API (kept verbatim — DO NOT alter behaviour)
    # =====================================================================
    def init(self, fn: FilterCallable, fail_closed: bool = False) -> bool:
        """
        V1 安装 hook 并注册回调。签名: fn(pid, event, path) -> bool
        """
        _fail_closed_local = bool(fail_closed)

        @PreFilterFunc
        def _wrapper(pid: int, event: int, raw_path) -> bool:
            try:
                path = raw_path.decode("utf-8", errors="replace") if raw_path else ""
                return fn(pid, event, path)
            except Exception:
                return not _fail_closed_local

        self._filter_ref = _wrapper
        if fail_closed:
            ok = self._dll.InitEx(self._filter_ref, True)
        else:
            ok = self._dll.Init(self._filter_ref)
        self._inited = bool(ok)
        return self._inited

    def destroy(self) -> None:
        """卸载 hook 并关闭管道。"""
        self._dll.Destroy()
        self._inited = False

    def add_rules(self, rule: str, session_only: bool = False) -> bool:
        return bool(self._dll.AddRules(rule.encode("utf-8"), session_only))

    def delete_rules(self, rule: str, session_only: bool = False) -> bool:
        return bool(self._dll.DeleteRules(rule.encode("utf-8"), session_only))

    def list_rules(self) -> str:
        result = self._dll.ListRules()
        return result.decode("utf-8", errors="replace") if result else ""

    def list_rules_parsed(self) -> List[str]:
        raw = self.list_rules()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data.get("rules", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def add_white_rules(self, rule: str, session_only: bool = False) -> bool:
        return bool(self._dll.AddWhiteRules(rule.encode("utf-8"), session_only))

    def delete_white_rules(self, rule: str, session_only: bool = False) -> bool:
        return bool(self._dll.DeleteWhiteRules(rule.encode("utf-8"), session_only))

    def list_white_rules(self) -> str:
        result = self._dll.ListWhiteRules()
        return result.decode("utf-8", errors="replace") if result else ""

    def list_white_rules_parsed(self) -> List[str]:
        raw = self.list_white_rules()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data.get("rules", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def add_read_only_white_rules(self, rule: str, session_only: bool = False) -> bool:
        # Op-aware read-only whitelist: read is allowed, write/delete/execute
        # still go through the callback. Returns False when the loaded DLL
        # predates the AddReadOnlyWhiteRules export.
        if not hasattr(self._dll, "AddReadOnlyWhiteRules"):
            return False
        return bool(self._dll.AddReadOnlyWhiteRules(rule.encode("utf-8"), session_only))

    def delete_read_only_white_rules(self, rule: str, session_only: bool = False) -> bool:
        if not hasattr(self._dll, "DeleteReadOnlyWhiteRules"):
            return False
        return bool(self._dll.DeleteReadOnlyWhiteRules(rule.encode("utf-8"), session_only))

    def list_read_only_white_rules(self) -> str:
        if not hasattr(self._dll, "ListReadOnlyWhiteRules"):
            return ""
        result = self._dll.ListReadOnlyWhiteRules()
        return result.decode("utf-8", errors="replace") if result else ""

    def list_read_only_white_rules_parsed(self) -> List[str]:
        raw = self.list_read_only_white_rules()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data.get("rules", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def add_op_mask_white_rules(
        self, rule: str, mask: int, session_only: bool = False
    ) -> bool:
        # Op-masked whitelist: an op whose bit is set in ``mask`` (READ=1,
        # WRITE=2, EXECUTE=4, DELETE=8) is allowed; an op whose bit is unset
        # falls through to the callback (ASK). Returns False when the loaded DLL
        # predates the AddOpMaskWhiteRules export.
        if not hasattr(self._dll, "AddOpMaskWhiteRules"):
            return False
        return bool(
            self._dll.AddOpMaskWhiteRules(
                rule.encode("utf-8"), int(mask), session_only
            )
        )

    def delete_op_mask_white_rules(
        self, rule: str, session_only: bool = False
    ) -> bool:
        if not hasattr(self._dll, "DeleteOpMaskWhiteRules"):
            return False
        return bool(
            self._dll.DeleteOpMaskWhiteRules(rule.encode("utf-8"), session_only)
        )

    def list_op_mask_white_rules(self) -> str:
        if not hasattr(self._dll, "ListOpMaskWhiteRules"):
            return ""
        result = self._dll.ListOpMaskWhiteRules()
        return result.decode("utf-8", errors="replace") if result else ""

    def list_op_mask_white_rules_parsed(self) -> List[dict]:
        raw = self.list_op_mask_white_rules()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data.get("rules", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def check_conflict(self, path: str) -> bool:
        norm = path.replace("/", "\\").rstrip("\\").lower()
        black = [r.replace("/", "\\").rstrip("\\").lower() for r in self.list_rules_parsed()]
        white = [r.replace("/", "\\").rstrip("\\").lower() for r in self.list_white_rules_parsed()]
        in_black = any(norm.startswith(b) for b in black)
        in_white = any(norm.startswith(w) for w in white)
        return in_black and in_white

    # =====================================================================
    # V2 API (additive — R8 / R10 / R11 / R12)
    # =====================================================================
    def init_v2(
        self,
        fn: FilterCallableV2,
        fail_closed: bool = True,
        callback_timeout_ms: int = 60000,
    ) -> bool:
        """
        R8 + R10: 安装 hook 并注册 V2 回调。

        Parameters
        ----------
        fn: 回调签名 fn(evt: FilterEventV2) -> bool；返回 True 放行, False 拦截。
        fail_closed: 回调抛异常 / 超时时的策略。True = deny, False = allow。
        callback_timeout_ms: 管道等待回调返回的超时（毫秒）。
                             默认 60000 (60 秒)。0 表示使用 DLL 默认值。

        Notes
        -----
        与 init() / init_ex() 互斥：DLL 内单实例保护，同时只能装一次。
        """
        if not hasattr(self._dll, "InitV2"):
            raise RuntimeError("guard64.dll 不含 InitV2 导出，需要 V2 版本的 DLL")

        _fail_closed_local = bool(fail_closed)

        @PreFilterFuncV2
        def _wrapper(evt_ptr) -> bool:
            try:
                evt = evt_ptr.contents
                fp = evt.file_path.decode("utf-8", errors="replace") if evt.file_path else ""
                pp = evt.process_path.decode("utf-8", errors="replace") if evt.process_path else ""
                cl = evt.command_line.decode("utf-8", errors="replace") if evt.command_line else ""
                py_evt = FilterEventV2(
                    pid=int(evt.pid),
                    parent_pid=int(evt.parent_pid),
                    event=int(evt.event),
                    file_path=fp,
                    process_path=pp,
                    command_line=cl,
                )
                return bool(fn(py_evt))
            except Exception:
                # Honour fail_closed on any Python-side exception.
                return not _fail_closed_local

        self._filter_ref_v2 = _wrapper
        ok = self._dll.InitV2(
            self._filter_ref_v2,
            c_bool(fail_closed),
            c_uint32(callback_timeout_ms),
        )
        self._inited = bool(ok)
        return self._inited

    # ---- R11: process exception ------------------------------------------
    def add_process_exception(self, exe_path: str) -> bool:
        """
        R11: 添加进程豁免（exe 路径前缀匹配, case-insensitive）。
        命中的进程整个绕过所有 hook。
        """
        if not hasattr(self._dll, "AddProcessException"):
            raise RuntimeError("guard64.dll 不含 AddProcessException 导出")
        return bool(self._dll.AddProcessException(exe_path.encode("utf-8")))

    def remove_process_exception(self, exe_path: str) -> bool:
        """R11: 移除进程豁免。"""
        if not hasattr(self._dll, "RemoveProcessException"):
            raise RuntimeError("guard64.dll 不含 RemoveProcessException 导出")
        return bool(self._dll.RemoveProcessException(exe_path.encode("utf-8")))

    def exempt_self(self) -> bool:
        """豁免当前进程自身（用 DLL 内部 GetModuleFileNameW(NULL) 取真实映像路径）。

        宿主进程用它 bypass 自己的文件 I/O，同时仍拦被 spawn 的子进程。比
        add_process_exception(sys.executable) 更可靠：venv 启动器路径与真实
        解释器映像可能不同，外部传路径会静默匹配失败。
        """
        if not hasattr(self._dll, "ExemptSelf"):
            raise RuntimeError("guard64.dll 不含 ExemptSelf 导出，需要 R13+ 版本的 DLL")
        return bool(self._dll.ExemptSelf())

    def set_trusted_infra_token(self, token: str) -> bool:
        """Register a random trust-token with the DLL so the host can classify
        its own spawned children as TrustedInfra (Phase 1 identity — env-based).

        The DLL stores the token opaquely; Phase 1 does not compare token values
        in the child (env-presence is the signal). Phase 3 upgrades to a pid
        registry with value comparison.
        """
        try:
            fn = getattr(self._dll, "SetTrustedInfraToken", None)
        except Exception:
            fn = None
        if fn is None:
            return False
        try:
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_bool
            enc = token.encode("ascii") if isinstance(token, str) else bytes(token)
            return bool(fn(enc))
        except Exception:
            return False

    def list_process_exceptions(self) -> List[str]:
        """R11: 列出当前所有进程豁免路径。"""
        if not hasattr(self._dll, "ListProcessExceptions"):
            return []
        raw = self._dll.ListProcessExceptions()
        if not raw:
            return []
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            return data.get("processes", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    # ---- R12: diagnostics --------------------------------------------------
    def get_diagnostics(self) -> dict:
        """
        R12: 返回 DLL 内部计数器 + 配置快照。

        Returns
        -------
        dict with keys:
            hooked_apis, callback_count, allow_count, deny_count,
            timeout_count, exception_count, pipe_error_count,
            last_error, init_thread_id, callback_timeout_ms,
            fail_closed, is_inited, self_process_exempt
        """
        if not hasattr(self._dll, "GetDiagnostics"):
            return {}
        raw = self._dll.GetDiagnostics()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, TypeError):
            return {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._inited:
            self.destroy()

    def __repr__(self) -> str:
        status = "inited" if self._inited else "not inited"
        return f"<Guard dll={self._dll_path!r} {status}>"
