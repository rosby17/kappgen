import itertools

from src.pipeline.images import IMAGE_SOURCE_PRIORITY, resolve_enabled_image_sources
from src.pipeline.orchestrator import (
    plan_visual_slots,
    unresolved_video_slot_indices,
)


def _all_non_empty_source_combinations():
    for size in range(1, len(IMAGE_SOURCE_PRIORITY) + 1):
        yield from itertools.combinations(IMAGE_SOURCE_PRIORITY, size)


def test_every_image_source_combination_is_preserved_in_priority_order():
    for combination in _all_non_empty_source_combinations():
        style = {"sources": list(reversed(combination))}
        expected = [source for source in IMAGE_SOURCE_PRIORITY if source in combination]

        assert resolve_enabled_image_sources(style) == expected


def test_every_mode_and_source_combination_assigns_every_scene_once():
    scene_count = 24

    for mode in ("images", "mixed", "videos"):
        for combination in _all_non_empty_source_combinations():
            assert resolve_enabled_image_sources({"sources": list(combination)})
            video_indices, image_indices = plan_visual_slots(scene_count, mode)

            assert video_indices.isdisjoint(set(image_indices))
            assert video_indices | set(image_indices) == set(range(scene_count))


def test_mixed_mode_requests_stock_for_video_slots_even_when_image_slots_are_filled():
    planned = {0, 2, 4}
    visual_paths = [None, "local-1.jpg", None, "community-2.jpg", None, "ai-3.png"]

    assert unresolved_video_slot_indices(planned, visual_paths) == [0, 2, 4]


def test_image_only_mode_never_asks_pexels_to_fill_missing_image_slots():
    video_slots, image_slots = plan_visual_slots(8, "images")

    assert image_slots == list(range(8))
    assert unresolved_video_slot_indices(video_slots, [None] * 8) == []
