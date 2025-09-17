# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from pathlib import Path
from typing import List, Optional
from enum import Enum
import json
import time

class SaveStatus(Enum):
    NOT_EXIST = 0       # ❌ there is no valid database
    WRITING = 1         # 🟡 database is being written
    DONE = 2            # ✅ database is complete (valid)


class RagSaveStatusManager:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.writing_file = self.base_dir / "save_status.writing"
        self.done_file = self.base_dir / "save_record.done"

    def begin_writing(self):
        self.writing_file.parent.mkdir(parents=True, exist_ok=True)
        self.writing_file.touch()

    def end_writing(self):
        if self.writing_file.exists():
            self.writing_file.unlink()

    def create_done(self, doc_names: List[str]):
        content = {
            "doc_names": doc_names,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        self.done_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.done_file, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)

    def is_writing(self) -> bool:
        return self.writing_file.exists()

    def is_done(self) -> bool:
        return self.done_file.exists()

    def get_status(self) -> SaveStatus:
        if self.is_writing():
            return SaveStatus.WRITING
        elif self.is_done():
            return SaveStatus.DONE
        else:
            return SaveStatus.NOT_EXIST

    def status_to_str(self) -> str:
        status_map = {
            SaveStatus.WRITING: "🟡 database is being written",
            SaveStatus.DONE: "✅ database is complete (valid)",
            SaveStatus.NOT_EXIST: "❌ there is no valid database"
        }
        return status_map.get(self.get_status(), "⚠️ Known status")

    def get_done_info(self) -> Optional[dict]:
        if not self.done_file.exists():
            return None
        try:
            with open(self.done_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None