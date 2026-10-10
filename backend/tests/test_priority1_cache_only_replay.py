import json

import pytest

from app.cache_only_replay import (
    CacheMiss,
    ModelTransportBlocked,
    block_model_transports,
    load_model_cache,
)
from app.config import Settings


def _counters():
    return {
        "model_cache_hits": 0,
        "model_cache_misses": 0,
        "historical_cached_calls": 0,
        "cached_items": 0,
        "cached_participants": 0,
    }


def test_cache_miss_fails_before_any_model_transport(tmp_path):
    settings = Settings(model_name="qwen-cache-only-test")
    counters = _counters()

    with block_model_transports() as transport_calls:
        with pytest.raises(CacheMiss, match="model cache miss"):
            load_model_cache(
                tmp_path,
                mode="model",
                filename="notice.html",
                text="cached input text",
                settings=settings,
                include_participants=True,
                counters=counters,
            )
        assert transport_calls == {"sync": 0, "async": 0}
        assert counters["model_cache_misses"] == 1

        with pytest.raises(ModelTransportBlocked):
            from app import model_adapter

            model_adapter.extract_unstructured_items(
                filename="notice.html",
                text="nonempty fixture text",
                settings=Settings(
                    model_base_url="http://invalid.test/v1",
                    model_api_key="dummy-not-a-secret",
                    model_name="qwen-cache-only-test",
                ),
            )
        assert transport_calls == {"sync": 1, "async": 0}


def test_cache_hit_restores_validated_candidates_without_transport(tmp_path):
    settings = Settings(model_name="qwen-cache-only-test")
    identity = {
        "version": 1,
        "mode": "model",
        "filename": "notice.html",
        "model": settings.model_name,
        "max_chars": settings.model_max_chars,
        "max_output_tokens": settings.model_max_output_tokens,
        "disable_thinking": settings.model_disable_thinking,
        "include_participants": True,
    }
    import hashlib

    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8") + b"cached input text"
    ).hexdigest()
    target = tmp_path / "model-cache" / "model" / f"{digest}.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "metadata": {},
                "items": [],
                "participants": [],
                "warnings": [],
                "model_calls": [{"transport": "success"}],
            }
        ),
        encoding="utf-8",
    )
    counters = _counters()

    with block_model_transports() as transport_calls:
        metadata, items, participants, warnings = load_model_cache(
            tmp_path,
            mode="model",
            filename="notice.html",
            text="cached input text",
            settings=settings,
            include_participants=True,
            counters=counters,
        )

    assert metadata.model_dump(mode="json")
    assert items == participants == warnings == []
    assert counters["model_cache_hits"] == 1
    assert counters["model_cache_misses"] == 0
    assert counters["historical_cached_calls"] == 1
    assert transport_calls == {"sync": 0, "async": 0}
