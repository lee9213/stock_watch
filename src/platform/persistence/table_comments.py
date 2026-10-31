"""各业务表中文用途说明。SQLite 无原生表备注，写入 schema_table_comments 供 Navicat 查阅。"""

from __future__ import annotations

# (表名, 分组, 说明)
TABLE_COMMENTS: tuple[tuple[str, str, str], ...] = (
    ("schema_migrations", "系统", "数据库版本迁移记录。记录已执行的 schema 变更版本、校验和与成败，启动时用来判断是否还要跑迁移。"),
    ("schema_table_comments", "系统", "本表：各业务表的中文用途说明。SQLite 不能给表加 COMMENT，查库时先看这里或视图「表说明」。"),
    ("app_settings", "系统配置", "应用键值配置。头像路径、时区、功能开关等页面设置存在这里，不是行情或交易数据。"),
    ("ai_services", "系统配置", "AI 服务商接入。保存 OpenAI / 智谱 / DeepSeek 等的 base_url 与 api_key。"),
    ("ai_models", "系统配置", "具体语言模型。挂在某个 AI 服务商下，标记默认模型，供 Agent、新闻分析、助手调用。"),
    ("data_sources", "系统配置", "行情/新闻/K 线等外部数据源配置。含启用状态、优先级、测试代码，采集失败时按优先级切换。"),
    ("notify_channels", "系统配置", "通知渠道。如 Telegram 的 bot_token / chat_id，Agent 和价格提醒发消息时用。"),
    ("personal_access_tokens", "系统配置", "MCP 个人访问令牌。只存哈希，用于外部 MCP 客户端长期只读访问，可独立吊销。"),
    ("accounts", "自选与持仓", "真实交易账户（券商账户）。记录账户名与可用资金，自选持仓挂在账户下。"),
    ("stocks", "自选与持仓", "自选标的主数据。代码、名称、市场（CN/HK/US）、资产类型；不再存持仓数量（已迁到 positions）。"),
    ("positions", "自选与持仓", "真实持仓。账户 × 股票的成本、数量、投入金额、交易风格（短线/波段/长线）。"),
    ("stock_agents", "自选与持仓", "自选与 Agent 的绑定。一只股票可被多个 Agent 监控，并覆盖该绑定的模型与通知渠道。"),
    ("agent_configs", "Agent", "内置 Agent 配置。盘中监控、盘前展望、日报、TradingAgents 等的启停、日程、执行模式与参数。"),
    ("agent_runs", "Agent", "Agent 单次执行记录。状态、耗时、模型、结果摘要或错误，用于运行历史和排障。"),
    ("agent_context_runs", "Agent", "某次 Agent 运行实际喂给模型的上下文摘要，便于事后对照「当时看了什么」。"),
    ("agent_prediction_outcomes", "Agent", "Agent 建议的后验评估。对照事后涨跌，统计建议效果，不做下单。"),
    ("analysis_history", "Agent", "盘前/盘后等分析报告历史，页面「分析记录」读取这里。"),
    ("stock_suggestions", "机会与策略", "各 Agent 汇总后的股票建议池。AI 建议面板的数据源。"),
    ("stock_context_snapshots", "机会与策略", "按股票、按日保存的结构化上下文快照，给跨天记忆和后续分析复用。"),
    ("entry_candidates", "机会与策略", "入场候选榜。机会页热榜/候选，按日去重，带来源建议与证据，模拟盘扫描主要读这张。"),
    ("market_scan_snapshots", "机会与策略", "市场扫描原始池快照。记录热榜多源回退结果，用来诊断覆盖率和源切换。"),
    ("entry_candidate_outcomes", "机会与策略", "入场候选后验。候选给出后自动对照后续行情，评估候选质量。"),
    ("strategy_catalog", "机会与策略", "策略目录。动量、放量突破、回调等策略的代码、名称、是否启用与版本。"),
    ("strategy_weights", "机会与策略", "策略当前权重。机会打分时各策略的生效权重。"),
    ("strategy_weight_history", "机会与策略", "策略调权历史，审计何时改过权重。"),
    ("strategy_signal_runs", "机会与策略", "策略信号快照。按日/股票/策略一行，含动作、分数、门禁原因，机会列表主表。"),
    ("strategy_factor_snapshots", "机会与策略", "单条策略信号的因子分解（动量、资金、新闻等分项），解释为什么得这个分。"),
    ("strategy_outcomes", "机会与策略", "策略信号后验结果，用来评估该策略历史有效性。"),
    ("factor_weights", "机会与策略", "因子当前权重。按因子 × 市场，可由 IC/IR 标定或手工覆盖。"),
    ("factor_weight_history", "机会与策略", "因子调权历史，审计用。"),
    ("market_regime_snapshots", "机会与策略", "市场状态快照（趋势/震荡等），用于按市场动态调权和解释。"),
    ("portfolio_risk_snapshots", "机会与策略", "组合风险画像快照，按市场聚合集中度、敞口等。"),
    ("backtest_runs", "机会与策略", "策略历史回测任务。保存入参、状态和结果，供回看。"),
    ("market_news", "事件驱动", "事件驱动页新闻流。同花顺 / 华尔街见闻 / 证券之星入库；分析后利多利空不足 ±2（中性或 ±1 弱信号）的直接删除，只有方向明确的才保留并进机会催化。title_key 用于跨来源相似标题去重（相似度 ≥0.85 或短标题被包含，发布时间 12 小时内只留一条）。raw_text 为来源原文，digest 分析后改写为模型摘要。bullish_level_reason / relation_reason 为模型分析理由。analyze_fail_count 超限不再重试。只保留近 30 天。"),
    ("market_news_discards", "事件驱动", "已删除的中性 / 弱信号新闻去重键（md5 / 链接 / 标题 + 发布时间）。下一轮采集遇到同一条直接跳过，避免反复入库和分析；保留 48 小时。"),
    ("news_digest", "事件驱动", "事件驱动页顶部 AI 小时速览快照，不是逐条新闻。input_hash 相同则跳过重复生成。"),
    ("news_topic_snapshots", "事件驱动", "按日期和窗口聚合的新闻主题快照，给主题热度分析。"),
    ("paper_trading_account", "模拟盘", "模拟盘账户（单例）。初始/当前资金、盈亏、回撤、是否启用、分市场仓位比例。"),
    ("paper_trading_positions", "模拟盘", "模拟盘当前持仓。代码、成本、数量、止损止盈与开仓来源信号。"),
    ("paper_trading_trades", "模拟盘", "模拟盘已平仓成交记录，用于战绩统计。"),
    ("paper_trading_settlements", "模拟盘", "模拟盘待交收卖出款。A 股 T+1：卖出后现金已记账，本表跟踪尚未交收余额。"),
    ("price_alert_rules", "价格提醒", "用户设置的价格提醒规则（涨到/跌到某价等）。"),
    ("price_alert_hits", "价格提醒", "规则命中记录，一次触发一行。"),
    ("price_alert_health", "价格提醒", "每条规则最近一次扫描健康度（上次成功、连续失败）。"),
    ("price_alert_scan_health", "价格提醒", "价格提醒扫描器全局心跳（单例），看调度是否在跑。"),
    ("price_alert_deliveries", "价格提醒", "命中后的通知投递队列表（发往哪个渠道、重试、是否送达）。"),
    ("scheduler_job_configs", "调度", "系统定时任务的页面覆盖配置（表达式、启停），启动时应用到调度器。"),
    ("scheduler_job_runs", "调度", "定时任务执行历史。新闻采集、机会刷新、模拟盘扫描、后验等每次跑的结果。"),
    ("chat_conversations", "助手", "研究助手会话列表（标题、创建时间）。"),
    ("chat_messages", "助手", "研究助手对话消息正文。"),
    ("assistant_context_snapshots", "助手", "助手上下文的最新摘要快照，用来组装下一轮提示。"),
    ("assistant_context_exports", "助手", "助手上下文导出为 Markdown 的后台任务进度。"),
    ("assistant_task_runs", "助手", "一次助手请求的持久化执行快照（状态、错误、最终回复）。"),
    ("assistant_task_events", "助手", "助手任务只追加事件流，用于刷新页面后重放进度。"),
    ("assistant_task_steps", "助手", "助手任务的计划/已执行步骤。"),
    ("assistant_task_notifications", "助手", "助手任务在应用内的提醒（完成、失败、待审批）。"),
    ("assistant_tool_invocations", "助手", "助手调用过的工具记录（事件流过期后仍可查）。"),
    ("assistant_tool_approvals", "助手", "高风险工具调用的人工审批单。"),
    ("assistant_tool_permissions", "助手", "本地对某工具或风险类别的长期授权偏好。"),
    ("assistant_artifacts", "助手", "助手生成的用户可见产物（报告片段、导出文件等）。"),
    ("research_runs", "研究", "投资研究一次运行。含引擎、范围哈希、状态、进度与 payload。"),
    ("research_artifacts", "研究", "某次研究运行产出的中间/最终产物 JSON。"),
    ("research_deliveries", "研究", "研究结论投递记录（已生成、待发送或已发送的报告载荷）。"),
    ("research_notification_outbox", "研究", "研究通知发件箱。按渠道投递状态与 TTL，失败可重试。"),
    ("notification_events", "通知", "全局通知事件本体（标题、内容、来源），多个收件箱共享。"),
    ("notification_receipts", "通知", "通知已读/送达回执，按收件人记录。"),
    ("notify_throttle", "通知", "通知节流。同一股票短时间重复通知时在这里挡掉。"),
    ("log_entries", "审计日志", "应用运行日志落库。级别、trace、Agent 事件，日志页查询用。"),
    ("llm_token_usage", "审计日志", "每次 LLM 调用的 token 消耗与输入输出摘要，用来看成本和调用量。"),
    ("mcp_call_logs", "审计日志", "MCP 工具调用审计。只存元数据，不存完整参数/结果明文。"),
)


def seed_table_comments() -> None:
    """创建说明表并全量覆盖写入，启动时可重复执行。"""
    from sqlalchemy.orm import Session

    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import SchemaTableComment

    db: Session = SessionLocal()
    try:
        existing = {
            row.table_name: row for row in db.query(SchemaTableComment).all()
        }
        wanted = {item[0] for item in TABLE_COMMENTS}
        for table_name, category, comment in TABLE_COMMENTS:
            row = existing.get(table_name)
            if row is None:
                db.add(
                    SchemaTableComment(
                        table_name=table_name,
                        category=category,
                        comment=comment,
                    )
                )
                continue
            row.category = category
            row.comment = comment
        for table_name, row in existing.items():
            if table_name not in wanted:
                db.delete(row)
        db.commit()
    finally:
        db.close()
