from pathlib import Path

from utils.path_mapping import map_to_path2


def test_map_to_path2_maps_relative_path():
    base = Path("/home/trung/Documents/linux-share/out")
    local = base / "author" / "post" / "2024_video.mp4"
    path2 = r"C:\Users\TrungBaNguyen\OneDrive - Fortna Inc\Documents\vm-share\out"
    mapped = map_to_path2(local, base, path2)
    assert mapped == (
        r"C:\Users\TrungBaNguyen\OneDrive - Fortna Inc\Documents\vm-share\out"
        r"\author\post\2024_video.mp4"
    )


def test_map_to_path2_returns_none_when_path2_empty():
    assert map_to_path2("/tmp/a.mp4", "/tmp", "") is None


def test_map_to_path2_returns_none_when_outside_base():
    assert map_to_path2("/other/a.mp4", "/tmp", "/mnt/share") is None
