"""新闻跨来源去重键：同花顺、证券之星对同一事件写法不同，md5 / 链接对不上。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

TITLE_KEY_LENGTH = 30
# 过短的标题（如「暴雷」「快讯」）容易撞车，不参与去重。
TITLE_KEY_MIN_LENGTH = 8
# 实测：0.85 以上为同一事件（跨来源改写、同品种盘中更新）；0.77 起出现「现货白银 / COMEX 白银」「多晶硅 / 沪锡」这类不同事件。
TITLE_SIMILARITY_THRESHOLD = 0.85
# 短标题被长标题完整包含时视为同一条（如一方多了「符合市场预期」）；太短的包含关系不可靠。
TITLE_CONTAIN_MIN_LENGTH = 10
_TITLE_SPLIT = re.compile(r"[。！？!?]|（原标题")
_TITLE_STRIP = re.compile(r"[\s，,：:、；;“”\"'‘’（）()【】\[\]《》<>·…—_-]")


def news_title_key(text: str | None) -> str:
    """取第一句作标题，去掉标点空白后截前 30 字；不足 8 字返回空串。"""
    head = _TITLE_SPLIT.split((text or "").strip(), maxsplit=1)[0]
    key = _TITLE_STRIP.sub("", head)[:TITLE_KEY_LENGTH]
    return key if len(key) >= TITLE_KEY_MIN_LENGTH else ""


def titles_similar(a: str, b: str) -> bool:
    return has_similar_title(a, [b])


def has_similar_title(key: str, candidates: Iterable[str]) -> bool:
    """key 与候选标题中任意一条相似即返回 True。"""
    if not key:
        return False
    matcher = SequenceMatcher(None)
    matcher.set_seq2(key)
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == key:
            return True
        shorter, longer = (key, candidate) if len(key) <= len(candidate) else (candidate, key)
        if len(shorter) >= TITLE_CONTAIN_MIN_LENGTH and shorter in longer:
            return True
        matcher.set_seq1(candidate)
        if matcher.real_quick_ratio() < TITLE_SIMILARITY_THRESHOLD:
            continue
        if matcher.quick_ratio() < TITLE_SIMILARITY_THRESHOLD:
            continue
        if matcher.ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False
