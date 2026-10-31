"""LLM token 使用量落库（对齐 AlphaLom usage_recorder）。

与 AIClient 解耦：调用方通过 ContextVar 标注 scene；落库使用独立 Session，
失败静默忽略，绝不影响主链路。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

_MAX_TEXT_LEN = 6000

_usage_scene: ContextVar[str | None] = ContextVar("llm_usage_scene", default=None)
_usage_platform: ContextVar[str | None] = ContextVar("llm_usage_platform", default=None)
_usage_user_id: ContextVar[int | None] = ContextVar("llm_usage_user_id", default=None)


def current_usage_scene(default: str | None = None) -> str | None:
    return _usage_scene.get() or default


def current_usage_platform(default: str | None = None) -> str | None:
    return _usage_platform.get() or default


def current_usage_user_id() -> int | None:
    return _usage_user_id.get()


@contextmanager
def llm_usage_context(
    *,
    scene: str | None = None,
    platform: str | None = None,
    user_id: int | None = None,
) -> Iterator[None]:
    """临时标注本次调用的业务场景 / 平台 / 用户。"""
    tokens = []
    if scene is not None:
        tokens.append((_usage_scene, _usage_scene.set(scene)))
    if platform is not None:
        tokens.append((_usage_platform, _usage_platform.set(platform)))
    if user_id is not None:
        tokens.append((_usage_user_id, _usage_user_id.set(user_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _truncate(text, limit: int = _MAX_TEXT_LEN) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[已截断，原始长度 {len(value)} 字符]"


def messages_to_input_text(messages) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or ""
        content = item.get("content")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    texts.append(part)
            content = "\n".join(t for t in texts if t)
        parts.append(f"[{role}] {content or ''}")
    return "\n".join(parts)


def record_llm_usage(
    *,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    input_text: str | None = None,
    output_text: str | None = None,
    scene: str | None = None,
    platform: str | None = None,
    user_id: int | None = None,
) -> None:
    """记录一次 LLM 调用；失败静默忽略。"""
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.persistence.models import LlmTokenUsage

        resolved_scene = scene if scene is not None else current_usage_scene()
        resolved_platform = platform if platform is not None else current_usage_platform()
        resolved_user = user_id if user_id is not None else current_usage_user_id()
        prompt_n = int(prompt_tokens or 0)
        completion_n = int(completion_tokens or 0)
        total_n = int(total_tokens or 0) or (prompt_n + completion_n)
        with SessionLocal() as session:
            session.add(
                LlmTokenUsage(
                    user_id=int(resolved_user) if resolved_user is not None else None,
                    scene=(resolved_scene or None),
                    platform=(resolved_platform or None),
                    model=(model or ""),
                    prompt_tokens=prompt_n,
                    completion_tokens=completion_n,
                    total_tokens=total_n,
                    input_text=_truncate(input_text),
                    output_text=_truncate(output_text),
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 — 统计失败不得影响主链路
        logger.warning("LLM token 使用记录失败（已忽略）：%s", exc)
