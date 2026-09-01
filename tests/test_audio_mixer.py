from src.pipeline.audio_mixer import build_studio_mix_filter


def test_studio_mix_has_fades_ducking_and_voice_protection():
    graph = build_studio_mix_filter(60, 0.10, {})
    assert "afade=t=in" in graph
    assert "afade=t=out" in graph
    assert "sidechaincompress" in graph
    assert "loudnorm=I=-15" in graph
    assert "amix=inputs=2:normalize=0:duration=first" in graph
    assert "alimiter=limit=0.95" in graph


def test_optional_fl_studio_equivalents_are_in_filter_graph():
    graph = build_studio_mix_filter(30, 0.12, {
        "soundgoodizer_enabled": True, "reverb_enabled": True, "maximus_enabled": True,
    })
    assert "equalizer=f=105" in graph
    assert "[voice_space][music_ducked]amix=inputs=2:normalize=0" in graph
    assert "aecho=" in graph
    assert "[voice_dry][voice_reverb_wet]amix=inputs=2:normalize=0" in graph
    assert "asplit=3" in graph
    assert "[mlo][mmid][mhi]amix" in graph
