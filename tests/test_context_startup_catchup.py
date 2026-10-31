"""启动补跑机会刷新的判定。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.modules.research.context_scheduler import needs_startup_catchup

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_catchup_when_started_after_first_slot_without_today_snapshot():
    now = datetime(2026, 10, 29, 10, 42, tzinfo=SHANGHAI)
    assert needs_startup_catchup(now, "2026-10-28") is True


def test_no_catchup_before_first_slot():
    now = datetime(2026, 10, 29, 9, 0, tzinfo=SHANGHAI)
    assert needs_startup_catchup(now, "2026-10-28") is False


def test_no_catchup_when_today_snapshot_exists():
    now = datetime(2026, 10, 29, 13, 0, tzinfo=SHANGHAI)
    assert needs_startup_catchup(now, "2026-10-29") is False


def test_catchup_when_no_snapshot_at_all():
    now = datetime(2026, 10, 29, 23, 0, tzinfo=SHANGHAI)
    assert needs_startup_catchup(now, "") is True
