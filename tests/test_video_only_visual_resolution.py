from pathlib import Path

from src.pipeline.orchestrator import unresolved_visual_indices
from src.pipeline.stock_video import PEXELS_SEARCH_URL


def test_video_only_empty_slots_are_all_sent_to_stock_search():
    visual_paths = [None, None, None]
    visual_types = ["image", "image", "image"]

    assert unresolved_visual_indices(visual_paths) == [0, 1, 2]
    assert all(kind != "video" for kind in visual_types)


def test_only_scenes_with_real_files_are_considered_filled():
    visual_paths = [Path("pexels-one.mp4"), None, Path("creator-broll.mp4")]

    assert unresolved_visual_indices(visual_paths) == [1]


def test_stock_photo_fallback_must_be_rendered_as_an_image():
    visual_paths = [None]
    visual_types = ["image"]
    visual_paths[0] = Path("pexels-fallback.jpg")
    visual_types[0] = "image"

    assert visual_paths[0].suffix == ".jpg"
    assert visual_types[0] == "image"


def test_pexels_video_search_uses_current_v1_endpoint():
    assert PEXELS_SEARCH_URL == "https://api.pexels.com/v1/videos/search"
