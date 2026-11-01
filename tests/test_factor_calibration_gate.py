"""因子自校准的 |IC| 闸门。

只看 IR 会踩的坑：某因子每天 IC 都是 0.001、std 极小 → IR 可以到 10，
于是被顶到最高权重 1.4。那是「稳定地无效」，不是 alpha。
"""

from __future__ import annotations

from src.modules.strategy.factor_calibration import IC_MIN_ABS, compute_target


def test_stable_but_useless_factor_is_not_upweighted():
    # IC≈0 但极其稳定 -> IR 极高
    assert compute_target("alpha_score", 0.001, 10.0) is None


def test_missing_ic_is_skipped():
    assert compute_target("alpha_score", None, 0.9) is None


def test_meaningful_ic_with_good_ir_is_upweighted():
    target = compute_target("alpha_score", 0.03, 0.8)
    assert target is not None and target > 1.0


def test_weak_but_valid_ic_falls_back_to_ic_term():
    # IC 过闸但 IR 缺失：用 IC/IC_REF 归一
    target = compute_target("quality_score", 0.02, None)
    assert target is not None
    assert 1.0 < target < 1.2


def test_penalty_factor_flips_sign():
    # 惩罚因子的 IC 预期为负：越负越该提权
    neg = compute_target("risk_penalty", -0.03, -0.8)
    pos = compute_target("risk_penalty", 0.03, 0.8)
    assert neg is not None and pos is not None
    assert neg > 1.0 > pos


def test_floor_is_documented_threshold():
    assert 0 < IC_MIN_ABS < 0.05
