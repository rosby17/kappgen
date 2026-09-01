"""Niche-aware editorial direction for motion and transitions."""
import unicodedata
from typing import Dict, Any, Tuple


PROFILES: Dict[str, Dict[str, Any]] = {
    "contemplative": {
        "motion_scale": 0.62,
        "motions": ["zoom_in", "pan_right", "zoom_out", "pan_left"],
        "transition_min": 0.90,
        "transition_max": 1.45,
        "transitions": ("dissolve", "fade"),
    },
    "documentary": {
        "motion_scale": 0.88,
        "motions": ["zoom_in_pan_right", "pan_left", "zoom_out", "zoom_in_pan_left"],
        "transition_min": 0.55,
        "transition_max": 1.10,
        "transitions": ("dissolve", "fade"),
    },
    "precise": {
        "motion_scale": 0.52,
        "motions": ["pan_right", "zoom_in", "pan_left", "zoom_out"],
        "transition_min": 0.45,
        "transition_max": 0.82,
        "transitions": ("fade", "dissolve"),
    },
    "dynamic": {
        "motion_scale": 1.0,
        "motions": ["zoom_in_pan_right", "zoom_out_pan_left", "pan_right", "zoom_in_pan_left"],
        "transition_min": 0.32,
        "transition_max": 0.70,
        "transitions": ("fade", "dissolve"),
    },
    "dramatic": {
        "motion_scale": 0.95,
        "motions": ["zoom_in", "zoom_out_pan_right", "zoom_in_pan_left", "pan_left"],
        "transition_min": 0.48,
        "transition_max": 1.15,
        "transitions": ("fadeblack", "dissolve"),
    },
    "editorial": {
        "motion_scale": 0.78,
        "motions": ["zoom_in", "pan_right", "zoom_out_pan_left", "pan_left"],
        "transition_min": 0.50,
        "transition_max": 1.05,
        "transitions": ("dissolve", "fade"),
    },
}


def _plain(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)).lower()


def resolve_editing_profile(niche: str) -> Dict[str, Any]:
    """Choose restraint, movement vocabulary and cut rhythm for the niche."""
    value = _plain(niche)
    if any(k in value for k in ("spiritual", "priere", "meditation", "bouddh", "islam", "stoic", "bien-etre")):
        name = "contemplative"
    elif any(k in value for k in ("histoire", "mytholog", "antique", "recit", "voyage")):
        name = "documentary"
    elif any(k in value for k in ("finance", "business", "sante", "science", "technolog", "education")):
        name = "precise"
    elif any(k in value for k in ("true crime", "crime", "mystere", "faits divers", "horreur")):
        name = "dramatic"
    elif any(k in value for k in ("football", "sport", "motivation", "divertissement", "gaming")):
        name = "dynamic"
    else:
        name = "editorial"
    return {"name": name, **PROFILES[name]}


def transition_for_energy(profile: Dict[str, Any], energy: float) -> Tuple[str, float]:
    energy = max(0.0, min(1.0, energy))
    duration = profile["transition_max"] - energy * (profile["transition_max"] - profile["transition_min"])
    calm, energetic = profile["transitions"]
    return (energetic if energy >= 0.68 else calm), duration
