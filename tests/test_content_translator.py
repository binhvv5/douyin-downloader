import json

import pytest

from core.content_translator import (
    build_fallback_vi_content,
    resolve_aweme_vi_content,
    resolve_translation_api_key,
    translate_aweme_content,
)


@pytest.mark.asyncio
async def test_translate_aweme_content_uses_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    captured = {}

    class _FakeResponse:
        status = 200

        async def text(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title_vi": "Tiêu đề",
                                        "description_vi": "Mô tả video",
                                        "tags_vi": ["robot", "đồ chơi"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr("core.content_translator.aiohttp.ClientSession", lambda **kwargs: _FakeSession())

    result = await translate_aweme_content(
        "#标签 中文标题",
        ["标签"],
        {"enabled": True, "api_key_env": "OPENAI_API_KEY"},
    )

    assert result is not None
    assert result["title_vi"] == "Tiêu đề"
    assert result["description_vi"] == "Mô tả video"
    assert result["tags_vi"] == ["robot", "đồ chơi"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key"


def test_resolve_translation_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    key, source = resolve_translation_api_key({"api_key_env": "OPENAI_API_KEY"})
    assert key == "sk-env"
    assert source == "env"


def test_resolve_translation_api_key_prefers_settings_over_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-old")
    key, source = resolve_translation_api_key(
        {"api_key_env": "OPENAI_API_KEY", "api_key": "sk-from-yaml"}
    )
    assert key == "sk-from-yaml"
    assert source == "settings"


def test_build_fallback_vi_content_keeps_fields_empty():
    result = build_fallback_vi_content(
        "简单一翻，终极经典名菜就能吃到！ #晓田酸辣汤#酸辣汤#美食",
        ["美食"],
    )

    assert result["title_vi"] == ""
    assert result["description_vi"] == ""
    assert result["tags_vi"] == []


@pytest.mark.asyncio
async def test_resolve_aweme_vi_content_uses_llm_when_available(monkeypatch):
    async def _fake_translate(desc, tags, translation_cfg):
        return {
            "title_vi": "Tiêu đề VI",
            "description_vi": "Mô tả VI",
            "tags_vi": ["tag1"],
        }

    monkeypatch.setattr("core.content_translator.translate_aweme_content", _fake_translate)
    content, llm_ok = await resolve_aweme_vi_content("中文 #标签", ["标签"], {"enabled": True})
    assert llm_ok is True
    assert content["title_vi"] == "Tiêu đề VI"
    assert content["tags_vi"] == ["tag1"]


@pytest.mark.asyncio
async def test_resolve_aweme_vi_content_falls_back_when_llm_unavailable(monkeypatch):
    async def _fake_translate(desc, tags, translation_cfg):
        return None

    monkeypatch.setattr("core.content_translator.translate_aweme_content", _fake_translate)
    content, llm_ok = await resolve_aweme_vi_content(
        "中文标题 #标签A #标签B",
        ["标签A"],
        {"enabled": True},
    )
    assert llm_ok is False
    assert content["title_vi"] == ""
    assert content["description_vi"] == ""
    assert content["tags_vi"] == []
