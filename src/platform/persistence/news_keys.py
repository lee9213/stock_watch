"""新闻跨来源去重键：同花顺、证券之星对同一事件写法不同，md5 / 链接对不上。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# 正文相似度：标题判重的补充
#
# 同一事件被多家改写时，首句往往**完全不同**，标题判重必然漏：
#   海洋王(002724.SZ)公告股价10个交易日涨幅偏离值累计超100%…
#   海洋王11月1日披露股票严重异常波动公告，称AI+智慧照明业务收入规模有限…
# 但正文里的事件要素（股票、异常波动、10个交易日、AI+智慧照明、收入规模有限）
# 高度重合。所以再加一层「全文 2-gram Jaccard」。
#
# 实测 168 条历史新闻标定：真重复对落在 0.33~0.68，而假重复对（赛力斯 vs 上汽
# 集团，两条都是「XX公告10月销量…同比下降X%」模板）落在 0.30~0.32。光看文本
# 相似度两个区间是重叠的，必须叠加「实体」约束：两条都认出了主体且主体不同时，
# 直接不合并；有共同主体才放宽到 0.30。
# ---------------------------------------------------------------------------
CONTENT_GRAM_SIZE = 2
# 正文只取前 400 字做指纹：事件要素几乎都在开头，全文参与会让不同长度的报道
# 相似度被稀释，也白白消耗 CPU。
CONTENT_SIGNATURE_LENGTH = 400
# 两条都认出主体且主体相同（同一只股票/同一家机构）时的阈值。
CONTENT_JACCARD_SAME_ENTITY = 0.30
# 至少一方认不出主体时（多为宏观/政策类）的阈值：调高是因为此时没有主体兜底，
# 0.30 会误伤「不同公司但因公告模板相似」的条目。
CONTENT_JACCARD_NO_ENTITY = 0.45

# 事件类型：同一主体 + 同一类型，基本可以断定是同一件事，不必再看文本相似度。
# 实测「上汽集团 10 月销量」三条正文相似度只有 0.19~0.54、「海洋王异常波动」四条
# 低到 0.18，单看阈值要放到 0.18 才抓得住，而那个区间又会把同公司不同类型公告
# 卷进来。分类后 14 对同主体同类型的样本全部是真重复，无一误合。
# 顺序即优先级：先命中者为准。
_EVENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("异常波动", ("异常波动", "涨幅偏离值", "跌幅偏离值", "严重异常")),
    ("立案", ("立案", "行政处罚", "警示函", "监管措施", "退市风险")),
    ("回购", ("回购",)),
    ("增减持", ("增持", "减持")),
    ("业绩", ("三季报", "半年报", "年报", "业绩预告", "业绩快报", "净利润", "营收")),
    ("重组", ("重组", "收购", "并购", "资产购买", "发行股份购买", "股权转让")),
    ("订单", ("中标", "订单", "签约", "合同金额", "框架协议")),
    ("分红", ("分红", "派息", "派现", "权益分派")),
    ("产销", ("销量", "产销", "销售量", "交付量")),
    ("停牌", ("停牌", "复牌")),
)

# 6 位证券代码：002724 / 002724.SZ / 688613.SH 都认。
_ENTITY_CODE = re.compile(r"(?<!\d)(\d{6})(?:\.(?:SZ|SH|BJ|sz|sh|bj))?")
# 「奥精医疗(688613.SH)」这种自带代码的写法最可靠。
_ENTITY_NAMED_CODE = re.compile(r"([A-Za-z\u4e00-\u9fa5*]{2,6})[（(](\d{6})")
# 句首「XX公告 / XX披露 / XX公布 / XX拟 / XX收到」——只在开头找，避免把正文里
# 随手提到的公司都当成主体（实测全文扫描会抽出「日俄乌双方」「发布分析报告」这类噪声）。
# 名字部分用**非贪婪**：贪婪会把「奥精医疗公告」整个吃掉（拿「收到」当动作词）。
_ENTITY_HEAD = re.compile(
    r"([A-Za-z\u4e00-\u9fa5*]{2,6}?)"
    r"(?:\d{1,2}月\d{1,2}日(?:晚|早)?|晚间|早间)?"
    r"(?:公告|公布|披露|发布|拟|宣布|收到|签约|中标|增持|回购)"
)
# 句首之前常有的日期/媒体署名，先剥掉再找主体。
_LEAD_NOISE = re.compile(
    r"^(?:"
    r"(?:财联社|证券时报|券商中国|上海证券报|中国证券报|每日经济新闻|界面新闻|"
    r"新浪财经|华尔街见闻|金十数据|智通财经|新华社|人民日报)"
    r"(?:\d{1,2}月\d{1,2}日)?(?:报道|讯|获悉|消息)?[，,：:]?"
    r"|(?:19|20)\d{2}年\d{1,2}月\d{1,2}日(?:晚|早)?[，,]?"
    r"|\d{1,2}月\d{1,2}日(?:晚|早)?[，,]?"
    r"|当地时间\d{1,2}月\d{1,2}日[，,]?"
    r"|(?:近日|昨日|今日|本周|上周|晚间|早间)[，,]?"
    r")+"
)
# 这些词后面跟着「公告/发布」也不能当主体（它们不是特定公司）。
_ENTITY_STOP = frozenset(
    {
        "公司", "公告", "三季报", "半年报", "年报", "季报", "业绩", "市场", "行业",
        "银行", "券商", "记者", "报道", "消息", "数据", "今日", "昨日", "本周",
        "上周", "我国", "全国", "美国", "韩国", "日本", "印度", "欧盟", "双方",
        "团队", "分析师", "机构", "部门", "会议", "政策", "报告",
    }
)


def content_grams(text: str | None, *, limit: int = CONTENT_SIGNATURE_LENGTH) -> frozenset[str]:
    """正文前 N 字的 2-gram 集合（去标点空白）。"""
    cleaned = _TITLE_STRIP.sub("", (text or "")[:limit])
    size = CONTENT_GRAM_SIZE
    if len(cleaned) < size:
        return frozenset()
    return frozenset(cleaned[i : i + size] for i in range(len(cleaned) - size + 1))


def news_entities(text: str | None) -> frozenset[str]:
    """抽取新闻主体：证券代码 + 句首「XX公告」里的公司名。

    只认三种高置信写法，宁可抽不到也不引入噪声——抽错的代价是把两家不同公司
    的新闻当成一条合并掉。
    """
    blob = (text or "").strip()
    if not blob:
        return frozenset()
    head = blob[:60]
    entities: set[str] = set()

    for name, code in _ENTITY_NAMED_CODE.findall(head):
        if name not in _ENTITY_STOP:
            entities.add(name)
        entities.add(code)
    entities.update(_ENTITY_CODE.findall(head))

    stripped = _LEAD_NOISE.sub("", head)
    match = _ENTITY_HEAD.match(stripped)
    if match and match.group(1) not in _ENTITY_STOP:
        entities.add(match.group(1))
    return frozenset(entities)


@dataclass(frozen=True)
class ContentSignature:
    """一条新闻的正文指纹：2-gram 集合 + 主体 + 事件类型。"""

    grams: frozenset[str]
    entities: frozenset[str]
    event: str | None = None


def contents_duplicate(
    a: "ContentSignature",
    b: "ContentSignature",
) -> bool:
    """正文指纹判重。

    判定顺序（每一层都在实测样本上标定过）：

    1. 两条都认出了主体且主体**不同** → 直接放行。这一步挡掉「赛力斯 / 上汽集团
       都发『10月销量同比下降X%』」这类同模板不同公司的误合并。
    2. 主体相同且事件类型相同（都是「异常波动」「回购」…）→ 同一件事，合并。
    3. 主体相同但类型不同 → 看文本相似度是否 ≥ 0.30。
    4. 至少一方认不出主体（宏观/政策/海外）→ 文本相似度要达到 0.45。
    """
    if not a.grams or not b.grams:
        return False
    overlap = len(a.grams & b.grams)
    if not overlap:
        return False
    if a.entities and b.entities:
        if not (a.entities & b.entities):
            return False
        if a.event and a.event == b.event:
            return True
        jaccard = overlap / (len(a.grams) + len(b.grams) - overlap)
        return jaccard >= CONTENT_JACCARD_SAME_ENTITY
    jaccard = overlap / (len(a.grams) + len(b.grams) - overlap)
    return jaccard >= CONTENT_JACCARD_NO_ENTITY


def news_event_type(text: str | None) -> str | None:
    """粗分事件类型；认不出返回 None。"""
    blob = text or ""
    for name, keywords in _EVENT_KEYWORDS:
        if any(keyword in blob for keyword in keywords):
            return name
    return None


def content_signature(text: str | None) -> "ContentSignature":
    """一条新闻的正文指纹。"""
    return ContentSignature(
        grams=content_grams(text),
        entities=news_entities(text),
        event=news_event_type(text),
    )
