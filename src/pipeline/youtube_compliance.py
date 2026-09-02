import re
from difflib import SequenceMatcher
from typing import Iterable


SENSITIVE_NICHE_TERMS = {
    "santé", "sante", "health", "médical", "medical", "finance", "trading",
    "investissement", "crypto", "actualité", "actualite", "news", "politique",
    "psychologie", "faits divers", "true crime",
}

NICHE_PROFILES = {
    "kids": {"terms": ("enfant", "enfants", "kids", "comptine", "dessin animé"), "sources": False, "review": True},
    "health": {"terms": ("santé", "sante", "health", "médical", "medical", "nutrition"), "sources": True, "review": True},
    "finance": {"terms": ("finance", "trading", "investissement", "crypto", "bourse"), "sources": True, "review": True},
    "news": {"terms": ("actualité", "actualite", "news", "politique", "géopolitique"), "sources": True, "review": True},
    "true_crime": {"terms": ("faits divers", "true crime", "crime", "criminel"), "sources": True, "review": True},
    "psychology": {"terms": ("psychologie", "psychology", "thérapie", "therapie"), "sources": False, "review": True},
    "history": {"terms": ("histoire", "history", "historique", "archéologie"), "sources": True, "review": False},
    "religion": {"terms": ("religion", "chrétien", "chretien", "islam", "bible", "coran"), "sources": False, "review": False},
}

PROFILE_LABELS = {
    "kids": "Contenu pour enfants", "health": "Santé", "finance": "Finance",
    "news": "Actualité", "true_crime": "Faits divers", "psychology": "Psychologie",
    "history": "Histoire", "religion": "Religion",
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


def detect_niche_profile(channel) -> tuple[str, dict] | tuple[None, None]:
    niche = _normalise(f"{getattr(channel, 'niche', '')} {getattr(channel, 'description', '')}")
    for key, profile in NICHE_PROFILES.items():
        if any(term in niche for term in profile["terms"]):
            return key, profile
    return None, None


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

    profile_key, profile = detect_niche_profile(channel)
    sensitive = bool(profile and profile["review"])
    if profile:
        add("niche_profile", "Profil éditorial", "warning" if sensitive else "pass", f"Règles spécialisées appliquées : {PROFILE_LABELS[profile_key]}.", 4 if sensitive else 0)
    else:
        add("niche_profile", "Profil éditorial", "pass", "Profil général : contrôles standards appliqués.")

    if profile and profile["sources"]:
        source_urls = re.findall(r"https?://[^\s)\]]+", description)
        if source_urls:
            add("sources", "Sources", "pass", f"{len(source_urls)} source(s) liée(s) dans la description.")
        else:
            add("sources", "Sources", "warning", "Ajoutez au moins une source consultable dans la description.", 12)

    combined = _normalise(f"{title} {script}")
    dangerous_claims = {
        "health": ("guérit à coup sûr", "guerit a coup sur", "remplace votre médecin", "remplace votre medecin", "guaranteed cure"),
        "finance": ("profit garanti", "rendement garanti", "sans aucun risque", "guaranteed profit", "risk free return"),
    }
    matched_claim = next((claim for claim in dangerous_claims.get(profile_key, ()) if claim in combined), None)
    if matched_claim:
        add("dangerous_claim", "Promesse interdite", "fail", f"Promesse absolue détectée : « {matched_claim} ».", 35)
        blockers.append("Supprimez les promesses médicales ou financières garanties.")
    elif profile_key in dangerous_claims:
        add("dangerous_claim", "Promesses absolues", "pass", "Aucune promesse garantie évidente détectée.")

    if profile_key == "kids":
        if bool(getattr(channel, "youtube_made_for_kids", False)):
            add("made_for_kids", "Audience enfant", "pass", "La vidéo sera déclarée comme destinée aux enfants.")
        else:
            add("made_for_kids", "Audience enfant", "fail", "La chaîne semble destinée aux enfants mais la déclaration YouTube est désactivée.", 35)
            blockers.append("Activez « Contenu destiné principalement aux enfants » dans Publication YouTube.")
    elif bool(getattr(channel, "youtube_made_for_kids", False)):
        add("made_for_kids", "Audience enfant", "warning", "La déclaration enfant est activée : vérifiez qu’elle correspond réellement à l’audience.", 4)

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
