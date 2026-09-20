"""读取并验证项目领域资料。"""

import json
from pathlib import Path

def load_domain(path: Path) -> dict:
    """返回结构完整的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "facts", "entities", "rules", "sample"}
    if not required.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if not value["facts"] or not value["entities"] or not value["rules"]:
        raise ValueError("领域资料清单不能为空")
    return value
