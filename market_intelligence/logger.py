from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Dict


class JsonlLogger:
    def __init__(self, log_dir: str, file_name: str):
        self.path = Path(log_dir) / file_name
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def log(self, payload: Dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
