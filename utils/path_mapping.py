from pathlib import Path
from typing import Optional, Union


def map_to_path2(
    local_path: Union[str, Path],
    base_path: Union[str, Path],
    path2: Union[str, Path],
) -> Optional[str]:
    path2_text = str(path2 or "").strip()
    if not path2_text:
        return None
    base = Path(base_path).resolve()
    local = Path(local_path).resolve()
    try:
        relative = local.relative_to(base)
    except ValueError:
        return None
    return str(Path(path2_text) / relative)
