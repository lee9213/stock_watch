from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.administration.api.token_usage import token_usage_records, token_usage_summary
from src.platform.ai.usage_recorder import llm_usage_context, messages_to_input_text, record_llm_usage
from src.platform.persistence.models import LlmTokenUsage


def _db():
    engine = create_engine("sqlite://")
    LlmTokenUsage.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_messages_to_input_text():
    text = messages_to_input_text([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ])
    assert "[system] sys" in text
    assert "[user] hello" in text


def test_record_and_summary(monkeypatch):
    db = _db()

    class _Session:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.platform.persistence.database.SessionLocal", lambda: _Session())

    with llm_usage_context(scene="news_analysis", platform="智谱"):
        record_llm_usage(
            model="glm-4",
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
            input_text="in",
            output_text="out",
        )
    record_llm_usage(
        model="glm-4",
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
        scene="news_digest",
        platform="智谱",
    )

    summary = token_usage_summary(db=db)
    assert summary["total_calls"] == 2
    assert summary["total_tokens"] == 25
    assert summary["by_model"][0]["model"] == "glm-4"
    scenes = {item["scene"]: item["calls"] for item in summary["by_scene"]}
    assert scenes["news_analysis"] == 1
    assert scenes["news_digest"] == 1

    page = token_usage_records(db=db, page=1, page_size=10)
    assert page["total"] == 2
    assert page["records"][0]["model"] == "glm-4"
