"""塔罗冷知识，/塔罗知识 随机发一条；内容在 data/lore.txt，一行一条。"""

from functools import cache
from pathlib import Path

PATH = Path(__file__).parent / "data" / "lore.txt"


@cache
def facts() -> list[str]:
    lines = PATH.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]
