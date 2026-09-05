"""Owner-scoped Facecam project, editing, review and version APIs."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from src.db.models import Video, Channel, User
from src.db.session import get_db
from src.utils.auth import get_current_user
from src.utils.billing import FACECAM_EDIT_CREDITS, user_can_render
from src.pipeline import facecam_project as project
from src.pipeline.facecam_cuts import keep_ranges_from_deletes

router = APIRouter(prefix="/api/videos", tags=["videos"])


def owned(db, video_id, user, lock=False):
    query = db.query(Video).join(Channel).filter(Video.id == video_id, Channel.user_id == user.id, Video.input_type == "facecam")
    video = (query.with_for_update(of=Video) if lock else query).first()
    if not video:
        raise HTTPException(404, "Montage introuvable.")
    return video


def editable(video):
    if video.status in ("queued", "rendering"):
        raise HTTPException(409, "Attends la fin de l'opération en cours avant de modifier le montage.")


def load(video):
    data = project.read_json(project.project_dir(video.id) / "project.json")
    if not data:
        raise HTTPException(409, "L'analyse du montage n'est pas encore disponible.")
    return data


@router.get("/{video_id}/facecam/versions/{version}")
def get_version(video_id: str, version: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user)
    folder = project.project_dir(video.id)
    if not any(v["id"] == version for v in project.read_json(folder / "versions.json", [])):
        raise HTTPException(404, "Version introuvable.")
    return project.read_json(folder / "versions" / f"{version}.json")


@router.get("/{video_id}/facecam")
def get_project(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user)
    folder = project.project_dir(video.id)
    data = project.read_json(folder / "project.json", {})
    transcript = project.read_json(folder / "transcript.json", {"words": [], "duration": video.estimated_duration_seconds or 0})
    return {"video": video.to_dict(), "project": data,
            "settings": data.get("settings", project.settings_for(video, video.channel)),
            "transcript": transcript, "verification": project.read_json(folder / "verify-report.json"),
            "versions": project.read_json(folder / "versions.json", []),
            "notes": project.read_json(folder / "notes.json", []),
            "source_available": bool(video.raw_asset_path and (project.STORAGE_PATH / video.raw_asset_path).is_file()),
            "render_credits": FACECAM_EDIT_CREDITS}


class CutEdit(BaseModel):
    id: str
    enabled: bool


class OverlayEdit(BaseModel):
    id: str
    enabled: bool
    text: str = Field(max_length=160)
    start: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(ge=0.5, le=10, allow_inf_nan=False)


class ManualCut(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class NewOverlay(BaseModel):
    kind: Literal["card", "broll"]
    text: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(ge=.5, le=10, allow_inf_nan=False)


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    settings: project.FacecamSettings
    cuts: list[CutEdit] = Field(default_factory=list, max_length=10000)
    overlays: list[OverlayEdit] = Field(default_factory=list, max_length=1000)
    manual_cuts: list[ManualCut] = Field(default_factory=list, max_length=500)
    new_overlays: list[NewOverlay] = Field(default_factory=list, max_length=100)


@router.put("/{video_id}/facecam")
def save_project(video_id: str, body: EditRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user, True)
    editable(video)
    data = load(video)
    if data["revision"] != body.revision:
        raise HTTPException(409, "Le montage a changé dans une autre fenêtre. Actualise avant de réessayer.")
    import copy
    previous = copy.deepcopy(data)
    cut_map = {c["id"]: c for c in data["cuts"]}
    for edit in body.cuts:
        if edit.id not in cut_map:
            raise HTTPException(422, "Coupe inconnue.")
        cut_map[edit.id]["enabled"] = edit.enabled
    overlay_map = {o["id"]: o for o in data["overlays"]}
    for edit in body.overlays:
        if edit.id not in overlay_map or edit.start + edit.duration > data["duration"] + 0.05:
            raise HTTPException(422, "Habillage inconnu ou hors de la vidéo.")
        overlay_map[edit.id].update(edit.model_dump())
    for cut in body.manual_cuts:
        if cut.end <= cut.start or cut.end > data["duration"]:
            raise HTTPException(422, "La coupe doit se trouver dans la vidéo et avoir une durée positive.")
        words = project.read_json(project.project_dir(video.id) / "transcript.json")["words"]
        text = " ".join(w["text"] for w in words if cut.start <= w["start"] < cut.end)
        data["cuts"].append({"id": f"manual-{uuid.uuid4().hex}", "kind": "manual", "reason": "Coupe manuelle",
                             "text": text, "enabled": True, **cut.model_dump()})
    for overlay in body.new_overlays:
        if overlay.start + overlay.duration > data["duration"] + .05 or not overlay.text.strip():
            raise HTTPException(422, "L'habillage doit se trouver dans la vidéo et contenir du texte.")
        data["overlays"].append({"id": f"overlay-{uuid.uuid4().hex}", "enabled": True,
                                 "source_kind": "cutaway", **overlay.model_dump()})
    data["settings"] = body.settings.model_dump()
    keeps = keep_ranges_from_deletes(project.selected_ranges(data), data["duration"])
    if sum(e - s for s, e in keeps) < 0.1:
        raise HTTPException(422, "Conserve au moins un passage dans le montage.")
    data["approved"] = False
    data["revision"] += 1
    project.append_activity(data, "Décisions de montage enregistrées")
    history_path = project.project_dir(video.id) / "history.json"
    project.write_json(history_path, (project.read_json(history_path, []) + [previous])[-20:])
    project.write_json(project.project_dir(video.id) / "project.json", data)
    video.facecam_settings = data["settings"]
    db.commit()
    return data


@router.post("/{video_id}/facecam/undo")
def undo_project(video_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user, True)
    editable(video)
    data = load(video)
    path = project.project_dir(video.id) / "history.json"
    history = project.read_json(path, [])
    if not history:
        raise HTTPException(409, "Aucune modification à annuler.")
    previous = history.pop()
    previous["revision"] = data["revision"] + 1
    previous["approved"] = False
    project.append_activity(previous, "Modification précédente restaurée")
    project.write_json(project.project_dir(video.id) / "project.json", previous)
    project.write_json(path, history)
    video.facecam_settings = previous["settings"]
    db.commit()
    return previous


class RenderRequest(BaseModel):
    revision: int
    quality: Literal["draft", "master"] = "draft"


@router.post("/{video_id}/facecam/render")
def render_project(video_id: str, body: RenderRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user, True)
    editable(video)
    data = load(video)
    if data["revision"] != body.revision:
        raise HTTPException(409, "Le montage a changé. Actualise avant de lancer le rendu.")
    if not video.raw_asset_path or not (project.STORAGE_PATH / video.raw_asset_path).is_file():
        raise HTTPException(410, "Les rushs ont expiré. Importe-les à nouveau pour monter une nouvelle version.")
    if not video.channel.is_active:
        raise HTTPException(409, "Réactive cette chaîne avant de lancer un rendu.")
    allowed, reason = user_can_render(db, current_user, FACECAM_EDIT_CREDITS)
    if not allowed:
        raise HTTPException(402, reason)
    data["approved"] = True
    data["settings"]["quality"] = body.quality
    project.append_activity(data, "Export final demandé" if body.quality == "master" else "Aperçu demandé")
    project.write_json(project.project_dir(video.id) / "project.json", data)
    video.status, video.progress_stage, video.progress_percent = "queued", "En attente du rendu", 0
    video.error_message, video.finished_at, video.started_at = None, None, None
    video.restart_count = 0
    video.is_reassembly = bool(video.output_path)
    db.commit()
    return video.to_dict()


class NoteRequest(BaseModel):
    version: str = Field(max_length=80)
    time: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=2000)


@router.post("/{video_id}/facecam/notes")
def add_note(video_id: str, body: NoteRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user, True)
    folder = project.project_dir(video.id)
    versions = {v["id"]: v for v in project.read_json(folder / "versions.json", [])}
    duration = (versions.get(body.version) or {}).get("duration")
    if body.version == "source":
        duration = load(video)["duration"]
    if duration is None or body.time > duration or not body.text.strip():
        raise HTTPException(422, "Version ou timecode invalide.")
    notes = project.read_json(folder / "notes.json", [])
    if len(notes) >= 500:
        raise HTTPException(422, "Limite de 500 retours atteinte pour ce projet.")
    note = {"id": uuid.uuid4().hex, **body.model_dump(), "text": body.text.strip(), "resolved": False, "created_at": project.now()}
    project.write_json(folder / "notes.json", notes + [note])
    db.commit()
    return note


class ResolveRequest(BaseModel):
    resolved: bool


@router.patch("/{video_id}/facecam/notes/{note_id}")
def resolve_note(video_id: str, note_id: str, body: ResolveRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user, True)
    path = project.project_dir(video.id) / "notes.json"
    notes = project.read_json(path, [])
    note = next((n for n in notes if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, "Retour introuvable.")
    note["resolved"] = body.resolved
    project.write_json(path, notes)
    db.commit()
    return note


@router.get("/{video_id}/facecam/media/{version}")
def get_media(video_id: str, version: str, download: bool = False, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user)
    folder = project.project_dir(video.id)
    if version == "source" and video.raw_asset_path:
        path = project.STORAGE_PATH / video.raw_asset_path
    else:
        record = next((v for v in project.read_json(folder / "versions.json", []) if v["id"] == version), None)
        if not record:
            raise HTTPException(404, "Version introuvable.")
        path = folder / "versions" / f"{record['id']}.mp4"
    if not path.is_file():
        raise HTTPException(410, "Ce fichier n'est plus disponible.")
    return FileResponse(path, filename=f"facecam-{version}{path.suffix}" if download else None,
                        headers={"Cache-Control": "private, no-store"})


@router.get("/{video_id}/facecam/export/{kind}")
def export_project(video_id: str, kind: Literal["srt", "json", "txt"], version: str = "source", current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    video = owned(db, video_id, current_user)
    folder = project.project_dir(video.id)
    data = load(video)
    words = project.read_json(folder / "transcript.json", {"words": []})["words"]
    if version != "source":
        versions = project.read_json(folder / "versions.json", [])
        if not any(v["id"] == version for v in versions):
            raise HTTPException(404, "Version introuvable.")
        snapshot = project.read_json(folder / "versions" / f"{version}.json")
        words = snapshot["words"]
        data = snapshot["project"]
    if kind == "json":
        import json
        text, media = json.dumps(data, ensure_ascii=False, indent=2), "application/json"
    elif kind == "srt":
        text, media = project.srt_text(words), "application/x-subrip"
    else:
        text, media = " ".join(w["text"] for w in words), "text/plain"
    return Response(text, media_type=media, headers={"Content-Disposition": f'attachment; filename="facecam-{version}.{kind}"'})
