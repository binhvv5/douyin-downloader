import json
import os
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from utils.logger import setup_logger

logger = setup_logger("ContentTranslator")

DEFAULT_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


def resolve_translation_api_key(translation_cfg: Dict[str, Any]) -> Tuple[str, str]:
    settings_value = str(translation_cfg.get("api_key", "") or "").strip()
    if settings_value:
        return settings_value, "settings"

    api_key_env = str(translation_cfg.get("api_key_env", DEFAULT_API_KEY_ENV) or "").strip()
    if api_key_env:
        env_value = os.getenv(api_key_env, "").strip()
        if env_value:
            return env_value, "env"
    return "", "none"


def _is_enabled(translation_cfg: Dict[str, Any]) -> bool:
    if not isinstance(translation_cfg, dict):
        return False
    enabled = translation_cfg.get("enabled")
    if isinstance(enabled, str):
        return enabled.strip().lower() in {"1", "true", "yes", "on"}
    return bool(enabled)


def build_fallback_vi_content(desc: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "title_vi": "",
        "description_vi": "",
        "tags_vi": [],
    }


async def translate_aweme_content(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not _is_enabled(translation_cfg):
        return None

    api_key, source = resolve_translation_api_key(translation_cfg)
    if not api_key:
        logger.warning(
            "Translation enabled but no API key found (env %s)",
            translation_cfg.get("api_key_env", DEFAULT_API_KEY_ENV),
        )
        return None

    model = str(translation_cfg.get("model", DEFAULT_MODEL) or DEFAULT_MODEL)
    api_url = str(translation_cfg.get("api_url", DEFAULT_API_URL) or DEFAULT_API_URL)
    payload = {
        "desc": desc or "",
        "tags": tags or [],
    }
    system_prompt = (
        "You translate Douyin video metadata from Chinese to natural Vietnamese for "
        "Facebook/YouTube uploads. Return strict JSON only with keys: "
        "title_vi (short catchy title, no hashtags), "
        "description_vi (full Vietnamese caption/body, no hashtags), "
        "tags_vi (array of Vietnamese hashtag strings without # prefix). "
        "Keep brand names and proper nouns when appropriate."
    )
    request_body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, headers=headers, json=request_body) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning(
                        "Translation API failed (status=%s, source=%s): %s",
                        resp.status,
                        source,
                        body[:300],
                    )
                    return None
                data = json.loads(body)
        content = (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
        parsed = json.loads(content)
        title_vi = str(parsed.get("title_vi") or "").strip()
        description_vi = str(parsed.get("description_vi") or title_vi or "").strip()
        raw_tags = parsed.get("tags_vi") or []
        tags_vi = [str(tag).strip().lstrip("#") for tag in raw_tags if str(tag).strip()]
        if not title_vi and not description_vi and not tags_vi:
            return None
        return {
            "title_vi": title_vi or description_vi,
            "description_vi": description_vi or title_vi,
            "tags_vi": tags_vi,
        }
    except Exception as exc:
        logger.warning("Translation failed: %s", exc)
        return None


async def resolve_aweme_vi_content(
    desc: str,
    tags: List[str],
    translation_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    translated = await translate_aweme_content(desc, tags, translation_cfg)
    if translated:
        return translated, True

    empty = build_fallback_vi_content(desc, tags)
    if _is_enabled(translation_cfg):
        logger.warning(
            "Leaving title_vi/description_vi/tags_vi empty because ChatGPT translation "
            "was unavailable (will not copy Chinese source text)"
        )
    return empty, False
