"""数据源管理 API"""

import logging
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.platform.persistence.database import get_db
from src.platform.persistence.models import DataSource
from src.web.errors import api_error

logger = logging.getLogger(__name__)

router = APIRouter()


# 数据源类型说明
TYPE_LABELS = {
    "news": "新闻资讯",
    "kline": "K线数据",
    "capital_flow": "资金流向",
    "quote": "实时行情",
    "events": "事件日历",
    "chart": "K线截图",
    "flash_news": "快讯",
    "event_news": "事件驱动新闻",
    "fundamentals": "基本面",
    "dragon_tiger": "龙虎榜",
    "margin": "融资融券",
    "shareholders": "股东户数",
    "dividend": "分红",
    "northbound": "北向资金",
}


class DataSourceCreate(BaseModel):
    name: str
    type: str  # news / kline / capital_flow / quote / events / chart / flash_news
    provider: str
    config: dict = {}
    enabled: bool = True
    priority: int = 0
    supports_batch: bool = False
    test_symbols: list[str] = []


class DataSourceUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    provider: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    priority: int | None = None
    supports_batch: bool | None = None
    test_symbols: list[str] | None = None


class DataSourceOrderItem(BaseModel):
    id: int
    priority: int


class DataSourceReorder(BaseModel):
    items: list[DataSourceOrderItem]


class DataSourceResponse(BaseModel):
    id: int
    name: str
    type: str
    type_label: str = ""
    provider: str
    config: dict
    enabled: bool
    priority: int
    supports_batch: bool = False
    test_symbols: list[str] = []

    class Config:
        from_attributes = True


# 已接入 marketdata 新引擎的数据类型(随各类型逐步迁移扩充)
_ENGINE_ATTACHED_TYPES = {
    "news",
    "quote",
    "kline",
    "capital_flow",
    "events",
    "flash_news",
    "fundamentals",
    "dragon_tiger",
    "margin",
    "shareholders",
    "dividend",
    "northbound",
}


def _is_orphan(type_: str, provider: str) -> bool:
    """判定 (type, provider) 是否为孤儿数据源:不在包内引擎 vendor 集合、也不在当前 seed 列表里。

    与 server.reconcile_data_sources 的孤儿判定保持一致(legal = 包内集合 | seed 集合)。
    """
    from marketdata import PACKAGE_VENDORS_BY_TYPE
    from server import _seed_providers_by_type

    legal = PACKAGE_VENDORS_BY_TYPE.get(type_, frozenset()) | _seed_providers_by_type().get(type_, set())
    return provider not in legal


def _to_response(source: DataSource, health_map: dict | None = None) -> dict:
    """转换为响应格式。health_map: {provider: 指标快照};缺失则 health=None。"""
    health = (health_map or {}).get(source.provider)
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type,
        "type_label": TYPE_LABELS.get(source.type, source.type),
        "provider": source.provider,
        "config": source.config or {},
        "enabled": source.enabled,
        "priority": source.priority,
        "supports_batch": source.supports_batch or False,
        "test_symbols": source.test_symbols or [],
        "engine_attached": source.type in _ENGINE_ATTACHED_TYPES,
        "health": health,
        "is_orphan": _is_orphan(source.type, source.provider),
    }


@router.get("")
def list_datasources(type: str | None = None, db: Session = Depends(get_db)):
    """获取数据源列表，可按类型筛选"""
    query = db.query(DataSource)
    if type:
        query = query.filter(DataSource.type == type)
    sources = query.order_by(DataSource.type, DataSource.priority, DataSource.id).all()
    from src.platform.marketdata.marketdata_client import get_market_data
    health_map = get_market_data().health()
    return [_to_response(s, health_map) for s in sources]


@router.get("/types")
def get_datasource_types():
    """获取数据源类型列表"""
    return [{"type": k, "label": v} for k, v in TYPE_LABELS.items()]


@router.post("/reset-to-seed")
def reset_datasources_to_seed(db: Session = Depends(get_db)):
    """恢复内置数据源默认值:补齐/删除孤儿并重置测试股票,保留用户配置与凭证。"""
    from server import reconcile_data_sources

    summary = reconcile_data_sources(db, reset_test_symbols=True)
    logger.info(f"数据源手动对账完成: {summary}")
    return summary


@router.put("/priority")
def reorder_datasources(data: DataSourceReorder, db: Session = Depends(get_db)):
    """批量更新一组数据源的优先级（页面上的「上移/下移」）。

    为什么要单独开一个批量接口，而不是前端连续发两个 PUT：

    1. *原子性*。SQLite 同一时刻只有一个写者。前端 ``Promise.all`` 并发发两个
       PUT 时，后到的连接会以只读方式打开库（Windows + WAL 下拿不到 shm 独占），
       写操作直接抛 ``attempt to write a readonly database`` → 其中一个请求 500，
       另一个已经提交。表现出来就是「页面提示调整失败，刷新却发现已经生效」。
    2. *重复优先级*。交换两条记录的 priority 要求两者取值不同；一旦组内出现
       相同 priority（例如都为 5），交换后数值不变，点上下移会毫无反应。
       这里由前端提交整组的新顺序，服务端按提交顺序重新分配，杜绝该情况。

    路由必须注册在 ``PUT /{source_id}`` 之前，否则 "priority" 会被当成 id 解析。
    """
    if not data.items:
        return {"updated": 0}

    wanted = {item.id: item.priority for item in data.items}
    if len(wanted) != len(data.items):
        raise api_error(400, "datasource_duplicate_id", "同一个数据源不能在一次排序里出现两次")

    sources = db.query(DataSource).filter(DataSource.id.in_(list(wanted))).all()
    missing = sorted(set(wanted) - {s.id for s in sources})
    if missing:
        raise api_error(404, "datasource_not_found", f"数据源不存在: {missing}")

    changed = 0
    for source in sources:
        priority = wanted[source.id]
        if source.priority != priority:
            source.priority = priority
            changed += 1
    db.commit()
    logger.info("数据源顺序更新: %s", wanted)
    return {"updated": changed}


@router.get("/{source_id}")
def get_datasource(source_id: int, db: Session = Depends(get_db)):
    """获取单个数据源"""
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise api_error(404, "datasource_not_found", "数据源不存在")
    return _to_response(source)


@router.post("")
def create_datasource(data: DataSourceCreate, db: Session = Depends(get_db)):
    """创建数据源"""
    source = DataSource(
        name=data.name,
        type=data.type,
        provider=data.provider,
        config=data.config,
        enabled=data.enabled,
        priority=data.priority,
        supports_batch=data.supports_batch,
        test_symbols=data.test_symbols,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    logger.info(f"创建数据源: {source.name} ({source.provider})")
    return _to_response(source)


@router.put("/{source_id}")
def update_datasource(
    source_id: int, data: DataSourceUpdate, db: Session = Depends(get_db)
):
    """更新数据源"""
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise api_error(404, "datasource_not_found", "数据源不存在")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(source, key, value)

    db.commit()
    db.refresh(source)
    logger.info(f"更新数据源: {source.name}")
    return _to_response(source)


@router.delete("/{source_id}")
def delete_datasource(source_id: int, db: Session = Depends(get_db)):
    """删除数据源"""
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise api_error(404, "datasource_not_found", "数据源不存在")

    db.delete(source)
    db.commit()
    logger.info(f"删除数据源: {source.name}")
    return {"ok": True, "message": f"已删除 {source.name}"}


@router.post("/{source_id}/test")
async def test_datasource(source_id: int, db: Session = Depends(get_db)):
    """测试数据源连接"""
    source = db.query(DataSource).filter(DataSource.id == source_id).first()
    if not source:
        raise api_error(404, "datasource_not_found", "数据源不存在")

    from src.modules.market.data_collector import get_collector_manager

    manager = get_collector_manager()
    manager.clear_logs()

    result = await manager.test_source(source)

    # 不用 success / data 作为顶层字段,避免被 ResponseWrapperMiddleware 当成业务响应
    # 拆解后导致 metadata 丢失(详见 src/web/response.py:59 的特殊分支)。
    return {
        "test_passed": result.success,
        "source_name": source.name,
        "source_type": source.type,
        "type_label": TYPE_LABELS.get(source.type, source.type),
        "provider": source.provider,
        "supports_batch": source.supports_batch or False,
        "test_symbols": result.test_symbols or source.test_symbols or [],
        "count": result.count,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "items": result.data,
        "errors": result.errors,
        "logs": manager.get_logs(),
    }
