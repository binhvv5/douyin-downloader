import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _unique_strings(values: List[Any], *, limit: int = 40) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _parse_common_flags(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _chapter_context(info: Any, *, max_chapters: int = 8, max_points: int = 4) -> Dict[str, Any]:
    if not isinstance(info, dict) or not info:
        return {}
    chapters_out: List[Dict[str, Any]] = []
    for chapter in _as_list(info.get("recommend_chapter_list"))[:max_chapters]:
        if not isinstance(chapter, dict):
            continue
        points: List[str] = []
        for point in _as_list(chapter.get("points"))[:max_points]:
            if not isinstance(point, dict):
                continue
            desc = str(point.get("desc") or "").strip()
            detail = str(point.get("detail") or "").strip()
            if desc and detail:
                points.append(f"{desc}: {detail[:280]}")
            elif desc:
                points.append(desc)
            elif detail:
                points.append(detail[:280])
        item: Dict[str, Any] = {
            "title": str(chapter.get("desc") or "").strip(),
        }
        if points:
            item["points"] = points
        if item.get("title") or points:
            chapters_out.append(item)

    out: Dict[str, Any] = {}
    abstract = str(info.get("chapter_abstract") or "").strip()
    if abstract:
        out["chapter_abstract"] = abstract[:1200]
    if chapters_out:
        out["chapters"] = chapters_out
    return out


def extract_movie_context(aweme_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(aweme_data, dict) or not aweme_data:
        return {}

    author = aweme_data.get("author") if isinstance(aweme_data.get("author"), dict) else {}
    text_extra = _as_list(aweme_data.get("text_extra"))
    hashtags = _unique_strings(
        [
            item.get("hashtag_name")
            for item in text_extra
            if isinstance(item, dict)
        ]
    )

    video_tags = _unique_strings(
        [
            item.get("tag_name")
            for item in _as_list(aweme_data.get("video_tag"))
            if isinstance(item, dict)
        ]
    )

    suggest_words: List[str] = []
    suggest_root = aweme_data.get("suggest_words")
    if isinstance(suggest_root, dict):
        for block in _as_list(suggest_root.get("suggest_words")):
            if not isinstance(block, dict):
                continue
            for word in _as_list(block.get("words")):
                if isinstance(word, dict):
                    suggest_words.append(word.get("word"))
                else:
                    suggest_words.append(word)
    suggest_words = _unique_strings(suggest_words, limit=30)

    feed_cfg = aweme_data.get("feed_comment_config")
    feed_cfg = feed_cfg if isinstance(feed_cfg, dict) else {}
    flags = _parse_common_flags(feed_cfg.get("common_flags"))
    label_hints = _unique_strings(
        [
            flags.get("video_labels_v2_tag1"),
            flags.get("video_labels_v2_tag2"),
            flags.get("video_type"),
        ],
        limit=10,
    )

    context: Dict[str, Any] = {
        "aweme_id": str(aweme_data.get("aweme_id") or "").strip() or None,
        "desc": str(
            aweme_data.get("desc")
            or aweme_data.get("caption")
            or aweme_data.get("preview_title")
            or ""
        ).strip(),
        "author_nickname": str(author.get("nickname") or "").strip() or None,
        "hashtags": hashtags,
        "video_tags": video_tags,
        "suggest_words": suggest_words,
        "label_hints": label_hints,
    }

    chapter = _chapter_context(aweme_data.get("recommend_chapter_info"))
    if chapter:
        context["recommend_chapter_info"] = chapter

    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def load_movie_context_from_json_path(file_path: str) -> Dict[str, Any]:
    path = Path(str(file_path or "").strip())
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return extract_movie_context(data if isinstance(data, dict) else {})
