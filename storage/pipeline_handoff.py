from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from utils.logger import setup_logger
from utils.path_mapping import map_to_path2

logger = setup_logger("PipelineHandoff")

ASSET_SUFFIX_MAP = (
    ("_data.json", "metadata_json"),
    ("_cover.", "cover"),
    ("_music.", "music"),
)


def classify_downloaded_file(path: Path) -> Optional[str]:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix == ".mp4":
        return "source_mp4"
    for token, asset_type in ASSET_SUFFIX_MAP:
        if token in name:
            return asset_type
    if suffix == ".json" and name.endswith("_data.json"):
        return "metadata_json"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"} and "_cover" in name:
        return "cover"
    if suffix in {".mp3", ".m4a"} and "_music" in name:
        return "music"
    return None


def find_local_source_mp4(base_path: Path, aweme_id: str) -> Optional[Path]:
    if not aweme_id or not base_path.exists():
        return None
    candidates: List[Path] = []
    for path in base_path.rglob("*.mp4"):
        if not path.is_file():
            continue
        if aweme_id not in path.name:
            continue
        if "_music" in path.name.lower():
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        candidates.append(path.resolve())
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def build_asset_entries(
    downloaded_files: List[Path],
    base_path: Optional[Union[Path, str]] = None,
    path2: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen_types: set = set()
    base = Path(base_path).resolve() if base_path else None
    path2_str = str(path2).strip() if path2 else ""
    for raw_path in downloaded_files:
        path = Path(raw_path).resolve()
        asset_type = classify_downloaded_file(path)
        if not asset_type or asset_type in seen_types:
            continue
        seen_types.add(asset_type)
        file_size = None
        try:
            file_size = path.stat().st_size
        except OSError:
            pass
        mime_type = None
        if asset_type == "source_mp4":
            mime_type = "video/mp4"
        elif asset_type == "cover":
            mime_type = "image/jpeg"
        elif asset_type == "music":
            mime_type = "audio/mpeg"
        elif asset_type == "metadata_json":
            mime_type = "application/json"
        entry: Dict[str, Any] = {
            "asset_type": asset_type,
            "file_path": str(path),
            "file_size": file_size,
            "mime_type": mime_type,
        }
        if base is not None and path2_str:
            mapped = map_to_path2(path, base, path2_str)
            if mapped:
                entry["file_path2"] = mapped
        entries.append(entry)
    return entries
