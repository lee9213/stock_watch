"""新闻跨来源去重键：同花顺、证券之星对同一事件写法不同，md5 / 链接对不上。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

TITLE_KEY_LENGTH = 30
# 过短的标题（如「暴雷」「快讯」）容易撞车，不参与去重。
TITLE_KEY_MIN_LENGTH = 8
# 首句过短时（「利好来了！」「刚刚！」这类标题党）继续累积后续句子到这个长度：
# 只累到 8 字会得到「利好来了北京重磅发布」这种过于笼统的键，容易把同城不同
# 政策误判成同一事件；累到 16 字才能带上「事关氢能产业」这类区分信息。
TITLE_KEY_EXTEND_LENGTH = 16
# 实测：0.85 以上为同一事件（跨来源改写、同品种盘中更新）；0.77 起出现「现货白银 / COMEX 白银」「多晶硅 / 沪锡」这类不同事件。
TITLE_SIMILARITY_THRESHOLD = 0.85
# 短标题被长标题完整包含时视为同一条（如一方多了「符合市场预期」）；太短的包含关系不可靠。
TITLE_CONTAIN_MIN_LENGTH = 10
_SENTENCE_SPLIT = re.compile(r"[。！？!?]")
# 「（原标题：xxx）」之后是重复的原始标题，先截掉，否则会被拼进键里。
_ORIGINAL_TITLE = re.compile(r"[（(]\s*原标题[：:]?")
_TITLE_STRIP = re.compile(r"[\s，,：:、；;“”\"'‘’（）()【】\[\]《》<>·…—_-]")


def news_title_key(text: str | None) -> str:
    """取首句作标题，去标点空白后截前 30 字；不足 8 字返回空串。

    首句过短时继续累积后续句子——原实现直接返回空串，等于让「利好来了！北京，
    重磅发布！事关氢能产业」这类标题彻底退出跨来源判重（实测同一事件被两次
    分析、页面上出现两条）。累积到 8 字虽能成键，但「利好来了北京重磅发布」
    太笼统，故要求累积到 16 字。
    """
    cleaned = _ORIGINAL_TITLE.split((text or "").strip(), maxsplit=1)[0]
    sentences = _SENTENCE_SPLIT.split(cleaned)
    key = _TITLE_STRIP.sub("", sentences[0]) if sentences else ""
    if len(key) < TITLE_KEY_MIN_LENGTH:
        buffer = key
        for sentence in sentences[1:]:
            buffer += _TITLE_STRIP.sub("", sentence)
            if len(buffer) >= TITLE_KEY_EXTEND_LENGTH:
                break
        key = buffer
    key = key[:TITLE_KEY_LENGTH]
    return key if len(key) >= TITLE_KEY_MIN_LENGTH else ""


def titles_similar(a: str, b: str) -> bool:
    return has_similar_title(a, [b])


_BUCKET_SECONDS = 3600
DEFAULT_INDEX_WINDOW_HOURS = 12


class TitleIndex:
    """(标题键, 发布时间) 的小时桶索引。

    判重时只需要比较时间窗内的标题，但直接遍历几千条历史标题会对每条候选做
    一次全量过滤 + 字符串相似度计算。按小时分桶后只扫 2N+1 个桶，结果与
    ``has_similar_title`` 在全量列表上的判定完全一致。
    """

    __slots__ = ("_buckets", "_window_hours")

    def __init__(self, window_hours: int = DEFAULT_INDEX_WINDOW_HOURS):
        self._window_hours = max(1, int(window_hours))
        self._buckets: dict[int, list[str]] = {}

    @staticmethod
    def _bucket(moment) -> int:
        # 需要带时区的 datetime，naive 会被当作本地时间，跨时区会错位。
        return int(moment.timestamp() // _BUCKET_SECONDS)

    def add(self, key: str, moment) -> None:
        if key and moment is not None:
            self._buckets.setdefault(self._bucket(moment), []).append(key)

    def has_similar(self, key: str, moment) -> bool:
        if not key or moment is None or not self._buckets:
            return False
        bucket = self._bucket(moment)
        span = self._window_hours
        for offset in range(-span, span + 1):
            candidates = self._buckets.get(bucket + offset)
            if candidates and has_similar_title(key, candidates):
                return True
        return False

    def add_anchors(self, key: str, *moments) -> None:
        """同一条记录按多个时间锚入桶（发布时间 + 入库时间）。

        转载 / 旧文重发会让「发布时间」和「入库时间」相差十几小时，只按发布
        时间开窗时，同一条新闻换个来源再进来会落在窗口外而漏判。
        """
        for moment in moments:
            self.add(key, moment)

    @property
    def window_seconds(self) -> int:
        return self._window_hours * _BUCKET_SECONDS

    def __len__(self) -> int:
        return sum(len(keys) for keys in self._buckets.values())


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
