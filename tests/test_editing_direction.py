from src.pipeline.editing_direction import resolve_editing_profile, transition_for_energy


def test_niches_receive_distinct_editorial_directions():
    assert resolve_editing_profile("Santé & Bien-être")["name"] == "contemplative"
    assert resolve_editing_profile("Histoire Africaine")["name"] == "documentary"
    assert resolve_editing_profile("Finance")["name"] == "precise"
    assert resolve_editing_profile("True Crime")["name"] == "dramatic"
    assert resolve_editing_profile("Football")["name"] == "dynamic"


def test_energy_tightens_transitions_without_escaping_profile():
    profile = resolve_editing_profile("Spiritualité")
    calm_kind, calm_duration = transition_for_energy(profile, 0.0)
    energetic_kind, energetic_duration = transition_for_energy(profile, 1.0)

    assert calm_kind == "dissolve"
    assert energetic_kind == "fade"
    assert calm_duration > energetic_duration
    assert energetic_duration >= profile["transition_min"]
