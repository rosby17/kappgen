from __future__ import annotations

import re
import hashlib
from pathlib import Path
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

_VISUAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _visual_asset_hashes(video) -> set[str]:
    raw_path = getattr(video, "source_assets_path", None)
    if not raw_path:
        return set()
    path = Path(raw_path)
    if not path.is_absolute():
        from src.config import STORAGE_PATH
        path = STORAGE_PATH / path
    if not path.exists():
        return set()
    hashes = set()
    for asset in path.rglob("*"):
        if asset.is_file() and asset.suffix.lower() in _VISUAL_EXTENSIONS:
            try:
                hashes.add(hashlib.sha256(asset.read_bytes()).hexdigest())
            except OSError:
                continue
    return hashes


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


def evaluate_script_compliance(script: str, title: str, channel, previous_videos: Iterable = ()) -> dict:
    """Evaluate a narration before the expensive audio/video render starts.

    This is intentionally deterministic and conservative. It is a product
    guardrail, not a prediction or guarantee of a YouTube/YPP decision.
    """
    score = 100
    checks = []
    blockers = []
    script = (script or "").strip()
    title = (title or "").strip()
    words = re.findall(r"\b[\wÀ-ÿ'-]+\b", script)

    def add(code, label, state, message, penalty=0):
        nonlocal score
        score -= penalty
        # A global score is useful at a glance, but it must not hide the
        # reason behind it. Every control therefore carries its own clear
        # confidence score for the publication review UI.
        confidence = 100 if state == "pass" else max(60, 100 - penalty) if state == "warning" else max(0, 60 - penalty)
        checks.append({"code": code, "label": label, "state": state, "message": message, "score": confidence})

    if len(words) < 80:
        add("script_depth", "Substance", "fail", f"Scénario insuffisant ({len(words)} mots).", 45)
        blockers.append("Développez le scénario avant de lancer le rendu.")
    elif len(words) < 180:
        add("script_depth", "Substance", "warning", f"Scénario court ({len(words)} mots).", 14)
    else:
        add("script_depth", "Substance", "pass", f"Scénario suffisamment développé ({len(words)} mots).")

    if len(title) < 8:
        add("title_quality", "Titre", "warning", "Le titre est absent ou trop vague.", 8)
    else:
        add("title_quality", "Titre", "pass", "Le titre permet d’identifier clairement le sujet.")

    # Repeated substantial sentences are a strong signal of templated filler,
    # independently of whether the whole script resembles an older video.
    sentences = [
        _normalise(part)
        for part in re.split(r"(?<=[.!?])\s+|\n+", script)
        if len(re.findall(r"\b[\wÀ-ÿ'-]+\b", part)) >= 6
    ]
    sentence_counts = {}
    for sentence in sentences:
        sentence_counts[sentence] = sentence_counts.get(sentence, 0) + 1
    repeated = sum(count - 1 for count in sentence_counts.values() if count > 1)
    repetition_ratio = repeated / max(1, len(sentences))
    if repeated >= 3 and repetition_ratio >= 0.25:
        add("internal_repetition", "Répétitions", "fail", f"{round(repetition_ratio * 100)} % des phrases substantielles sont répétées.", 35)
        blockers.append("Supprimez les phrases répétées et le remplissage automatique.")
    elif repeated and repetition_ratio >= 0.10:
        add("internal_repetition", "Répétitions", "warning", f"Répétitions notables ({round(repetition_ratio * 100)} %).", 12)
    else:
        add("internal_repetition", "Répétitions", "pass", "Aucune répétition interne excessive détectée.")

    max_script_similarity = 0.0
    max_title_similarity = 0.0
    closest_title = None
    for previous in previous_videos:
        previous_script = getattr(previous, "script_text", "") or ""
        if not previous_script.strip():
            continue
        script_similarity = _word_similarity(script, previous_script)
        title_similarity = _word_similarity(title, getattr(previous, "title", "") or "")
        if script_similarity > max_script_similarity:
            closest_title = getattr(previous, "title", None)
        max_script_similarity = max(max_script_similarity, script_similarity)
        max_title_similarity = max(max_title_similarity, title_similarity)
    similarity_pct = round(max_script_similarity * 100)
    if max_script_similarity >= 0.72 or max_title_similarity >= 0.88:
        add("originality", "Originalité", "fail", f"Scénario trop proche d’une ancienne vidéo ({similarity_pct} %).", 45)
        blockers.append("Changez l’angle, les exemples et la structure du scénario.")
    elif max_script_similarity >= 0.48 or max_title_similarity >= 0.70:
        add("originality", "Originalité", "warning", f"Ressemblance avec « {closest_title or 'une ancienne vidéo'} » ({similarity_pct} %).", 22)
    else:
        add("originality", "Originalité", "pass", f"Faible ressemblance avec l’historique ({similarity_pct} %).")

    profile_key, profile = detect_niche_profile(channel)
    if profile and profile["review"]:
        add("sensitive_topic", "Sujet sensible", "warning", f"Validation humaine requise : {PROFILE_LABELS[profile_key]}.", 8)
    else:
        add("sensitive_topic", "Sujet sensible", "pass", "Aucun profil sensible imposant une révision détecté.")

    combined = _normalise(f"{title} {script}")
    prohibited_claims = (
        "guérit à coup sûr", "guerit a coup sur", "remplace votre médecin", "remplace votre medecin",
        "profit garanti", "rendement garanti", "sans aucun risque", "devenez riche rapidement",
        "guaranteed cure", "guaranteed profit", "risk free return", "get rich quick",
    )
    matched_claim = next((claim for claim in prohibited_claims if claim in combined), None)
    if matched_claim:
        add("dangerous_claim", "Promesse sensible", "fail", f"Promesse absolue détectée : « {matched_claim} ».", 40)
        blockers.append("Supprimez toute garantie médicale ou financière absolue.")
    else:
        add("dangerous_claim", "Promesse sensible", "pass", "Aucune promesse absolue évidente détectée.")

    if profile_key == "kids" and not bool(getattr(channel, "youtube_made_for_kids", False)):
        add("made_for_kids", "Audience enfant", "fail", "La déclaration destinée aux enfants est désactivée.", 40)
        blockers.append("Activez la déclaration « destiné aux enfants ».")

    score = max(0, min(100, score))
    sensitive_review = bool(profile and profile["review"])
    status = "red" if blockers or score < 60 else "orange" if sensitive_review or score < 80 else "green"
    return {
        "version": 1,
        "phase": "script_preflight",
        "score": score,
        "status": status,
        "requires_human_review": status == "orange",
        "can_render": status != "red",
        "checks": checks,
        "blockers": blockers,
        "disclaimer": "Contrôle préventif KappGen : il ne garantit pas la monétisation YouTube.",
    }


def evaluate_audio_compliance(transcript_info: dict | None, title: str, channel, previous_videos: Iterable = (), source_type: str = "personal") -> dict:
    """Pre-render policy gate for creator-uploaded narration audio."""
    transcript_info = transcript_info or {}
    source = transcript_info.get("transcription_source")
    source_labels = {
        "personal": "Voix personnelle",
        "licensed": "Audio sous licence",
        "cloned": "Voix clonée avec consentement",
        "third_party": "Podcast ou contenu tiers",
        "music": "Musique ou extrait protégé",
    }
    source_requires_review = source_type != "personal"
    rights_check = {
        "code": "audio_rights",
        "label": "Droits de l’audio et de la voix",
        "state": "warning" if source_requires_review else "pass",
        "message": f"Provenance déclarée : {source_labels.get(source_type, 'Non précisée')}." + (" Les justificatifs doivent être vérifiés." if source_requires_review else ""),
    }
    if source != "izivoice":
        return {
            "version": 1,
            "phase": "audio_preflight",
            "score": 65,
            "status": "orange",
            "requires_human_review": True,
            "can_render": True,
            "checks": [
                rights_check,
                {
                    "code": "audio_transcript",
                    "label": "Contenu parlé",
                    "state": "warning",
                    "message": "Audio non transcrit : son contenu doit être vérifié manuellement.",
                },
            ],
            "blockers": [],
            "disclaimer": "Le contenu audio n’a pas pu être contrôlé automatiquement ; publication humaine obligatoire.",
        }

    report = evaluate_script_compliance(
        transcript_info.get("text") or "", title, channel, previous_videos
    )
    report["phase"] = "audio_preflight"
    report["checks"].insert(0, rights_check)
    report["checks"].insert(1, {
        "code": "audio_transcript",
        "label": "Transcription",
        "state": "warning" if transcript_info.get("transcription_partial") else "pass",
        "message": "Transcription partielle : validation humaine requise." if transcript_info.get("transcription_partial") else "Le contenu parlé a été transcrit avant le rendu.",
    })
    if transcript_info.get("transcription_partial") and report["status"] == "green":
        report["score"] = min(report["score"], 75)
        report["status"] = "orange"
        report["requires_human_review"] = True
    if source_requires_review and report["status"] == "green":
        report["score"] = min(report["score"], 75)
        report["status"] = "orange"
        report["requires_human_review"] = True
    return report


def build_compliance_dossier(video, channel) -> dict:
    """Snapshot of the evidence KappGen actually knows for this video."""
    script = video.script_text or ""
    description = video.youtube_description or ""
    image_style = getattr(channel, "image_style", None) or {}
    music = getattr(channel, "music_preference", None) or {}
    enabled_sources = image_style.get("sources") or ([image_style.get("source")] if image_style.get("source") else [])
    source_urls = re.findall(r"https?://[^\s)\]]+", description)
    return {
        "version": 1,
        "video_id": getattr(video, "id", None),
        "content": {
            "title": video.title or "",
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "script_word_count": len(re.findall(r"\b[\wÀ-ÿ'-]+\b", script)),
            "description_source_urls": source_urls,
        },
        "media": {
            "visual_sources": [source for source in enabled_sources if source],
            "visual_style_prompt_present": bool(image_style.get("style_prompt")),
            "music_mode": music.get("mode"),
            "music_tracks": [str(track).split("/")[-1] for track in (music.get("tracks") or [])],
            "voice_id": getattr(video, "voice_id", None) or getattr(channel, "voice_id", None),
            "audio_source_type": getattr(video, "audio_source_type", None),
            "audio_rights_confirmed": bool(getattr(video, "audio_rights_confirmed", False)),
        },
        "youtube_declarations": {
            "contains_synthetic_media": True,
            "made_for_kids": bool(getattr(channel, "youtube_made_for_kids", False)),
        },
        "compliance_report": getattr(video, "youtube_compliance_report", None),
        "audit_history": list(getattr(video, "youtube_compliance_history", None) or []),
        "reviewed_at": getattr(video, "youtube_compliance_reviewed_at", None).isoformat() if getattr(video, "youtube_compliance_reviewed_at", None) else None,
        "reviewed_by": getattr(video, "youtube_compliance_reviewed_by", None),
        "overrides": {
            "script_render": bool(getattr(video, "script_compliance_overridden", False)),
            "script_render_at": getattr(video, "script_compliance_overridden_at", None).isoformat() if getattr(video, "script_compliance_overridden_at", None) else None,
            "script_render_by": getattr(video, "script_compliance_overridden_by", None),
            "publication": bool(getattr(video, "publication_compliance_overridden", False)),
            "publication_at": getattr(video, "publication_compliance_overridden_at", None).isoformat() if getattr(video, "publication_compliance_overridden_at", None) else None,
            "publication_by": getattr(video, "publication_compliance_overridden_by", None),
        },
    }


def evaluate_youtube_compliance(video, channel, previous_videos: Iterable = ()) -> dict:
    """Deterministic pre-publication guardrail; no claim of YouTube approval."""
    score = 100
    checks = []
    blockers = []
    script = video.script_text or ""
    title = video.title or ""
    description = video.youtube_description or ""
    word_count = len(re.findall(r"\b[\wÀ-ÿ'-]+\b", script))
    preflight = getattr(video, "youtube_compliance_report", None) or {}
    unverified_audio = bool(
        getattr(video, "input_type", None) == "audio"
        and (
            not bool(getattr(video, "transcribe_audio", False))
            or any(check.get("code") == "audio_transcript" and check.get("state") == "warning" for check in preflight.get("checks", []))
        )
    )

    def add(code, label, state, message, penalty=0):
        nonlocal score
        score -= penalty
        checks.append({"code": code, "label": label, "state": state, "message": message})

    if unverified_audio:
        add("script_depth", "Contenu parlé", "warning", "Audio non vérifiable automatiquement : validation humaine obligatoire.", 20)
    elif word_count < 120:
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
    if unverified_audio:
        add("originality", "Originalité", "warning", "L’originalité du contenu parlé doit être confirmée manuellement.", 8)
    elif max_script_similarity >= 0.72 or max_title_similarity >= 0.88:
        add("originality", "Originalité par rapport à la chaîne", "fail", f"Très proche d’une ancienne vidéo ({similarity_pct}% pour le scénario).", 40)
        blockers.append("Réécrivez la vidéo pour la différencier nettement de l’historique.")
    elif max_script_similarity >= 0.48 or max_title_similarity >= 0.70:
        add("originality", "Originalité par rapport à la chaîne", "warning", f"Ressemblance notable avec « {closest_title or 'une ancienne vidéo'} » ({similarity_pct}%).", 22)
    else:
        add("originality", "Originalité par rapport à la chaîne", "pass", f"Faible ressemblance avec les anciennes vidéos ({similarity_pct}%).")

    profile_key, profile = detect_niche_profile(channel)
    audio_source_type = getattr(video, "audio_source_type", None)
    source_review = bool(getattr(video, "input_type", None) == "audio" and audio_source_type not in {None, "personal"})
    sensitive = bool(profile and profile["review"]) or unverified_audio or source_review
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

    if getattr(video, "input_type", None) == "audio":
        if bool(getattr(video, "audio_rights_confirmed", False)):
            add("audio_rights", "Droits audio", "warning" if source_review else "pass", "Droits déclarés par le créateur ; justificatif à vérifier." if source_review else "Droits de la voix personnelle confirmés.", 4 if source_review else 0)
        else:
            add("audio_rights", "Droits audio", "fail", "Les droits sur l’audio et la voix ne sont pas confirmés.", 40)
            blockers.append("Confirmez les droits nécessaires sur l’audio et la voix.")

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

    # Second barrier: unlike the script preflight, these checks only run once
    # a real output and its source assets exist. They evaluate the assembled
    # result rather than trusting configuration intent.
    if getattr(video, "status", None) == "done":
        duration = float(getattr(video, "duration_seconds", None) or 0)
        if duration < 3:
            add("render_integrity", "Montage final", "fail", "Le rendu est vide ou anormalement court.", 45)
            blockers.append("Régénérez la vidéo : le montage final est incomplet.")
        else:
            add("render_integrity", "Montage final", "pass", f"Rendu final exploitable ({round(duration)} s).")

        if duration and word_count:
            speech_rate = round(word_count / (duration / 60))
            if speech_rate < 55 or speech_rate > 230:
                add("voice_pacing", "Voix off", "warning", f"Rythme inhabituel ({speech_rate} mots/min). Vérifiez l’audio.", 8)
            else:
                add("voice_pacing", "Voix off", "pass", f"Rythme de narration cohérent ({speech_rate} mots/min).")
        elif not unverified_audio:
            add("voice_pacing", "Voix off", "warning", "La cohérence entre narration et durée n’a pas pu être vérifiée.", 8)

        visual_hashes = _visual_asset_hashes(video)
        assets_purged = bool(getattr(video, "edit_assets_purged_at", None))
        if not visual_hashes:
            add("visual_diversity", "Visuels du montage", "warning", "Sources visuelles archivées : contrôle manuel nécessaire." if assets_purged else "Aucun visuel source vérifiable.", 4)
        elif len(visual_hashes) == 1 and duration > 45:
            add("visual_diversity", "Visuels du montage", "fail", "Un seul visuel unique est utilisé dans toute la vidéo.", 32)
            blockers.append("Diversifiez les images ou les scènes avant publication.")
        else:
            add("visual_diversity", "Visuels du montage", "pass", f"{len(visual_hashes)} visuel(s) unique(s) vérifié(s).")

        max_visual_overlap = 0.0
        if visual_hashes:
            for previous in previous_videos:
                old_hashes = _visual_asset_hashes(previous)
                if old_hashes:
                    max_visual_overlap = max(max_visual_overlap, len(visual_hashes & old_hashes) / len(visual_hashes))
        if len(visual_hashes) >= 3 and max_visual_overlap >= 0.80:
            add("visual_originality", "Originalité visuelle", "fail", f"{round(max_visual_overlap * 100)} % des visuels ont déjà été utilisés.", 35)
            blockers.append("Remplacez les visuels déjà utilisés sur la chaîne.")
        elif len(visual_hashes) >= 3 and max_visual_overlap >= 0.50:
            add("visual_originality", "Originalité visuelle", "warning", f"Réutilisation visuelle élevée ({round(max_visual_overlap * 100)} %).", 18)
        elif visual_hashes:
            add("visual_originality", "Originalité visuelle", "pass", f"Faible réutilisation visuelle ({round(max_visual_overlap * 100)} %).")

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
