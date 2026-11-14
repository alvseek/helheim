"""Local response measurements, not a model's billed conversation usage."""

from __future__ import annotations

import json
from typing import Any

import tiktoken


class TokenCounter:
    encoding_name = "o200k_base"

    def __init__(self) -> None:
        # tiktoken caches the encoding in-process and its vocabulary on disk.
        self._encoding = tiktoken.get_encoding(self.encoding_name)

    def text(self, text: str) -> int:
        # Tool output is ordinary text, even when it contains special-token spellings.
        return len(self._encoding.encode_ordinary(text))

    def value(self, value: Any) -> int:
        return self.text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
