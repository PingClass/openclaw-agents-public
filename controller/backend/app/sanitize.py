"""個資過濾：controller 的 API 回應與稽核紀錄不含電話、地址（計畫 3.7）。

agent 回報的結構化輸出可能夾帶客戶電話或地址，寫入前與輸出前都濾掉。
"""

from __future__ import annotations

import re
from typing import Any

# 欄位名稱含這些字就移除（不分大小寫）
_SENSITIVE_KEY = re.compile(
    r"(phone|mobile|tel(ephone)?$|^tel_|fax|address|addr|電話|手機|地址|住址)",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize(item)
            for key, item in value.items()
            if not (isinstance(key, str) and is_sensitive_key(key))
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    return value
