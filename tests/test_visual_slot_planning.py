from src.pipeline.orchestrator import plan_visual_slots


def test_images_mode_reserves_no_video_slots():
    video_slots, image_slots = plan_visual_slots(30, "images")
    assert video_slots == set()
    assert image_slots == list(range(30))


def test_videos_mode_reserves_every_slot_for_video():
    video_slots, image_slots = plan_visual_slots(30, "videos")
    assert video_slots == set(range(30))
    assert image_slots == []


def test_mixed_mode_reserves_roughly_a_third_for_video():
    scene_count = 170
    video_slots, image_slots = plan_visual_slots(scene_count, "mixed")

    # Every index accounted for exactly once, no gaps and no overlap.
    assert video_slots.isdisjoint(image_slots)
    assert video_slots | set(image_slots) == set(range(scene_count))

    # The same ~1-in-3 cadence B-roll placement already uses (range(2, n, 3)),
    # so a mixed-mode channel actually gets a real mix instead of the whole
    # video silently falling back to 100% images.
    assert video_slots == set(range(2, scene_count, 3))
    ratio = len(video_slots) / scene_count
    assert 0.30 < ratio < 0.36


def test_unknown_media_mode_defaults_to_images_only():
    video_slots, image_slots = plan_visual_slots(10, "not_a_real_mode")
    assert video_slots == set()
    assert image_slots == list(range(10))


def test_plan_covers_every_scene_across_small_and_large_counts():
    for scene_count in (0, 1, 2, 3, 4, 40, 41, 170):
        for mode in ("images", "videos", "mixed"):
            video_slots, image_slots = plan_visual_slots(scene_count, mode)
            assert video_slots | set(image_slots) == set(range(scene_count))
            assert video_slots.isdisjoint(image_slots)
