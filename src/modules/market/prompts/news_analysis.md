# 新闻金融影响结构化分析

对下方新闻做结构化分析，用于事件驱动页的关联股票、标签与利多利空评估。

## 字段要求

- digest：50-100 字，重新组织语言，客观概括时间、主体、事件、结果；不要照抄原文句子。
- tags：3-5 个关键词（行业、政策、技术、事件、人物）。
- relation_level：市场关联度，0-10 分。
  - 0 分：与金融市场无关，或没有可交易标的。
  - 高分：直接影响 A 股 / 港股 / 美股上市公司，或外汇、贵金属、油气等可交易品种。
  - 纯宏观情绪且无映射标的给低分或 0；一句话点多名公司或整板块的板块综述不超过 4。
- bullish_level：-10 到 10 的整数，只评价对 relations_stocks 的影响；relation_level 为 0 时必须为 0。
  - -10 明显利空（加息超预期、订单取消、监管处罚等）；10 明显利好（显著提升估值或盈利）；无明显偏向为 0。
- bullish_level_reason：一句话说明利多利空的依据。
- relations_stocks：relation_level > 0 时必填，最多 3 只。
  - 先保留「原文已标注股票」，再补原文点名的上市公司、公告/财报主体、ETF。
  - 原文未写代码时按常见代码补全（A 股 6 位，港股保留前导 0，美股大写 ticker），不加 .SH / .SZ / .HK 后缀。
  - 不编造原文对不上的公司；板块综述只留最直接的 1-3 只，不罗列同板块一串股票。
  - 纯政治、灾害、无映射标的：relation_level=0 且 relations_stocks=[]。
- relation_reason：一句话说明为何关联这些股票。

## 补票排序参考

$prompt_155th

## 输出格式

仅输出一个合法 JSON 对象，所有字段必须存在，不加解释、注释或 ``` 分隔符：
{"digest": "新闻概要", "tags": ["关键词"], "relation_level": 0, "bullish_level": 0, "bullish_level_reason": "利多利空理由", "relations_stocks": [{"code": "股票代码", "name": "公司名称"}], "relation_reason": "关联理由"}

新闻发布时间：$news_time

原文已标注股票（优先保留，可补充，勿无故删除）：$known_stocks

用于分析的新闻内容：
$news_content
