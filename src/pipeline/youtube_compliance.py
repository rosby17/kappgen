import re
from difflib import SequenceMatcher
from typing import Iterable


SENSITIVE_NICHE_TERMS = {
    "santé", "sante", "health", "médical", "medical", "finance", "trading",
    "investissement", "crypto", "actualité", "actualite", "news", "politique",
    "psychologie", "faits divers", "true crime",
}


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"[a-zà-ÿ0-9]+", (text or "").lower()))


def _word_similarity(left: str, right: str) -> float:
    a, b = _normalise(left), _normalise(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:20000], b[:20000]).ratio()


def text_similarity(left: str, right: str) -> float:
    """Public similarity primitive shared by generation and publication guards."""
    return _word_similarity(left, right)


def evaluate_youtube_compliance(video, channel, previous_videos: Iterable = ()) -> dict:
    """Deterministic pre-publication guardrail; no claim of YouTube approval."""
    score = 100
    checks = []
    blockers = []
    script = video.script_text or ""
    title = video.title or ""
    description = video.youtube_description or ""
    word_count = len(re.findall(r"\b[\wÀ-ÿ'-]+\b", script))

    def add(code, label, state, message, penalty=0):
        nonlocal score
        score -= penalty
        checks.append({"code": code, "label": label, "state": state, "message": message})

    if word_count < 120:
        add("script_depth", "Substance du scénario", "fail", f"Scénario trop court ({word_count} mots).", 35)
        blockers.append("Le scénario doit être développé avant publication.")
    elif word_count < 350:
        add("script_depth", "Substance du scénario", "warning", f"Scénario assez court ({word_count} mots).", 12)
    else:
        add("script_depth", "Substance du scénario", "pass", f"Scénario substantiel ({word_count} mots).")

    if len(title.strip()) < 12:
        add("metadata_title", "Titre", "fail", "Titre absent ou trop vague.", 20)
        blockers.append("Ajoutez un titre YouTube précis.")
    else:
        add("metadata_title", "Titre", "pass", "Titre exploitable et relisible avant publication.")

    if len(description.strip()) < 80:
        add("metadata_description", "Description", "warning", "Description absente ou trop courte.", 8)
    else:
        add("metadata_description", "Description", "pass", "Description suffisamment renseignée.")

    max_script_similarity = 0.0
    max_title_similarity = 0.0
    closest_title = None
    for previous in previous_videos:
        if getattr(previous, "id", None) == getattr(video, "id", None):
            continue
        script_similarity = _word_similarity(script, getattr(previous, "script_text", "") or "")
        title_similarity = _word_similarity(title, getattr(previous, "title", "") or "")
        if max(script_similarity, title_similarity) > max(max_script_similarity, max_title_similarity):
            closest_title = getattr(previous, "title", None)
        max_script_similarity = max(max_script_similarity, script_similarity)
        max_title_similarity = max(max_title_similarity, title_similarity)

    similarity_pct = round(max_script_similarity * 100)
    if max_script_similarity >= 0.72 or max_title_similarity >= 0.88:
        add("originality", "Originalité par rapport à la chaîne", "fail", f"Très proche d’une ancienne vidéo ({similarity_pct}% pour le scénario).", 40)
        blockers.append("Réécrivez la vidéo pour la différencier nettement de l’historique.")
    elif max_script_similarity >= 0.48 or max_title_similarity >= 0.70:
        add("originality", "Originalité par rapport à la chaîne", "warning", f"Ressemblance notable avec « {closest_title or 'une ancienne vidéo'} » ({similarity_pct}%).", 22)
    else:
        add("originality", "Originalité par rapport à la chaîne", "pass", f"Faible ressemblance avec les anciennes vidéos ({similarity_pct}%).")

    niche = _normalise(f"{getattr(channel, 'niche', '')} {getattr(channel, 'description', '')}")
    sensitive = any(term in niche for term in SENSITIVE_NICHE_TERMS)
    if sensitive:
        add("sensitive_niche", "Sujet sensible", "warning", "Cette niche exige une validation humaine et des sources fiables.", 8)
    else:
        add("sensitive_niche", "Sujet sensible", "pass", "Aucune niche sensible détectée automatiquement.")

    add("synthetic_disclosure", "Déclaration IA", "pass", "KappGen déclare le contenu synthétique lors de l’envoi à YouTube.")
    score = max(0, min(100, score))
    status = "green" if score >= 80 and not blockers and not sensitive else "orange" if score >= 60 and not blockers else "red"
    return {
        "version": 1,
        "score": score,
        "status": status,
        "requires_human_review": status == "orange",
        "can_auto_publish": status == "green",
        "can_human_publish": status in {"green", "orange"},
        "checks": checks,
        "blockers": blockers,
        "disclaimer": "Ce contrôle réduit les risques mais ne garantit pas la monétisation par YouTube.",
    }
