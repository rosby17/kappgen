import json
import re

from src.pipeline import scene_director


def _batch_bounds(prompt: str):
    match = re.search(r"This batch covers scenes (\d+) to (\d+) of (\d+) total", prompt)
    assert match, "the director prompt must expose stable global batch bounds"
    return tuple(int(value) for value in match.groups())


def test_long_video_builds_all_170_image_prompts_in_five_batches(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        start, end, total = _batch_bounds(prompt)
        calls.append((start, end, total, kwargs["max_tokens"]))
        return json.dumps({
            "visual_bible": "deep blue documentary photography",
            "scene_prompts": [f"image prompt {index}" for index in range(start, end + 1)],
        })

    monkeypatch.setattr(scene_director, "any_text_provider_configured", lambda: True)
    monkeypatch.setattr(scene_director, "generate_text", fake_generate)
    scenes = [f"Narration scene {index}" for index in range(1, 171)]

    prompts = scene_director.build_scene_prompts("Full long script", scenes, niche="astronomy")

    assert [call[:2] for call in calls] == [(1, 40), (41, 80), (81, 120), (121, 160), (161, 170)]
    assert len(prompts) == 170
    assert prompts == [f"image prompt {index}" for index in range(1, 171)]
    assert all(call[2] == 170 and call[3] == 1800 for call in calls)


def test_long_video_builds_all_170_stock_queries_in_five_batches(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        start, end, total = _batch_bounds(prompt)
        calls.append((start, end, total, kwargs["max_tokens"]))
        return json.dumps({"queries": [f"stock query {index}" for index in range(start, end + 1)]})

    monkeypatch.setattr(scene_director, "any_text_provider_configured", lambda: True)
    monkeypatch.setattr(scene_director, "generate_text", fake_generate)
    scenes = [f"Narration scene {index}" for index in range(1, 171)]

    queries = scene_director.build_stock_search_queries(scenes, niche="astronomy")

    assert [call[:2] for call in calls] == [(1, 40), (41, 80), (81, 120), (121, 160), (161, 170)]
    assert len(queries) == 170
    assert queries == [f"stock query {index}" for index in range(1, 171)]
    assert all(call[2] == 170 and call[3] == 1500 for call in calls)


def test_one_failed_batch_does_not_discard_other_long_video_scenes(monkeypatch):
    def fake_generate(prompt, **kwargs):
        start, end, _ = _batch_bounds(prompt)
        if start == 81:
            raise RuntimeError("simulated provider interruption")
        if kwargs["operation"] == "scene_direction":
            return json.dumps({
                "visual_bible": "consistent style",
                "scene_prompts": [f"image prompt {index}" for index in range(start, end + 1)],
            })
        return json.dumps({"queries": [f"stock query {index}" for index in range(start, end + 1)]})

    monkeypatch.setattr(scene_director, "any_text_provider_configured", lambda: True)
    monkeypatch.setattr(scene_director, "generate_text", fake_generate)
    scenes = [f"Narration scene {index}" for index in range(1, 171)]

    prompts = scene_director.build_scene_prompts("Full long script", scenes)
    queries = scene_director.build_stock_search_queries(scenes)

    assert len(prompts) == len(queries) == 170
    assert prompts[79] == "image prompt 80"
    assert prompts[80:120] == scenes[80:120]
    assert prompts[120] == "image prompt 121"
    assert queries[79] == "stock query 80"
    assert queries[80:120] == [None] * 40
    assert queries[120] == "stock query 121"
