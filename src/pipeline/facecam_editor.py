"""Facecam: prepare source-timed decisions, review, render, verify, version."""
from __future__ import annotations

import copy
import logging
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session
from src.config import STORAGE_PATH
from src.db.models import Video, Channel
from src.pipeline import facecam_broll, facecam_cards, facecam_cuts, facecam_verify
from src.pipeline import facecam_project as project, facecam_render as render
from src.pipeline.audio_extract import ensure_extracted_audio
from src.pipeline.facecam_transcribe import transcribe_words
from src.utils.billing import FACECAM_EDIT_CREDITS, STOCK_MEDIA_CREDITS, debit_izivoice_usage_by_user_id

logger = logging.getLogger(__name__)


def _build_delete_ranges(words, user_id, video_id, duration=None):
    """Compatibility helper; expected counts include the surviving best take."""
    duration = duration if duration is not None else (words[-1]["end"] if words else 0)
    cuts = project.plan_cuts(words, duration, user_id, video_id)
    deletes = facecam_cuts.merge_delete_ranges([(c['start'], c['end']) for c in cuts])
    keeps = facecam_cuts.keep_ranges_from_deletes(deletes, duration)
    kept_text = ' '.join(w['text'] for w in facecam_cuts.remap_words_to_output_timeline(words, keeps)).lower()
    ledger = [{"removes": c['text'], "expect": kept_text.count(c['text'].lower())}
              for c in cuts if c['kind'] != 'silence' and c['text']]
    return deletes, ledger


def run_facecam_pipeline(video_id: str, db: Session) -> None:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.raw_asset_path:
        raise ValueError("Les rushs de ce montage ne sont pas disponibles.")
    channel = db.query(Channel).filter(Channel.id == video.channel_id).first()
    source_path = STORAGE_PATH / video.raw_asset_path
    folder = project.project_dir(video.id)
    folder.mkdir(parents=True, exist_ok=True)

    def checkpoint(stage, percent):
        db.refresh(video)
        if video.status == 'cancelled':
            from src.worker.queue_runner import VideoCancelledError
            raise VideoCancelledError()
        video.progress_stage, video.progress_percent = stage, percent
        db.commit()

    checkpoint('transcription', 5)
    transcript = project.read_json(folder / 'transcript.json')
    if transcript is None:
        transcript = transcribe_words(ensure_extracted_audio(source_path))
        # ffprobe is authoritative, including trailing silence after the last word.
        transcript['duration'] = render.probe(source_path)[2]
        project.write_json(folder / 'transcript.json', transcript)
    words = transcript['words']
    if not words:
        raise ValueError("Aucune parole détectée. Vérifie la piste audio de ton enregistrement.")

    checkpoint('cuts', 20)
    data = project.read_json(folder / 'project.json')
    if data is None:
        settings = project.settings_for(video, channel)
        cuts = project.plan_cuts(words, transcript['duration'], channel.user_id, video.id)
        overlays = []
        for beat in facecam_cards.detect_beats(words):
            length = min(facecam_cards.CARD_DURATION_SECONDS, transcript['duration'] - beat['time'])
            if length >= .5:
                overlays.append({'id': f'overlay-{len(overlays)}', 'kind': 'card', 'text': beat['text'],
                                 'start': beat['time'], 'duration': length, 'enabled': True})
        for trigger in facecam_broll.detect_broll_triggers(words):
            length = min(3, transcript['duration'] - trigger['time'])
            if trigger.get('query') and length >= .5:
                overlays.append({'id': f'overlay-{len(overlays)}', 'kind': 'broll', 'text': trigger['query'],
                                 'source_kind': trigger['kind'], 'start': trigger['time'], 'duration': length, 'enabled': True})
        data = {'revision': 1, 'duration': transcript['duration'], 'cuts': cuts, 'overlays': overlays,
                'settings': settings, 'approved': not settings['review_before_render'], 'activity': []}
        project.append_activity(data, 'Transcription et propositions de montage prêtes')
        project.write_json(folder / 'project.json', data)
    checkpoint('cuts', 25)
    if not data.get('approved'):
        video.status, video.progress_stage = 'review', 'Coupes à valider'
        db.commit()
        return

    # Preparation does not debit the fixed rendering fee. It is charged once
    # per explicit rendering attempt; provider editorial calls keep their own billing.
    if not debit_izivoice_usage_by_user_id(channel.user_id, FACECAM_EDIT_CREDITS, 'facecam_edit', video_id=video.id):
        raise ValueError('Crédits insuffisants pour lancer le rendu Facecam.')
    settings = data['settings']
    deletes = project.selected_ranges(data)
    keeps = facecam_cuts.keep_ranges_from_deletes(deletes, transcript['duration'])
    duration = sum(e - s for s, e in keeps)
    cut_words = facecam_cuts.remap_words_to_output_timeline(words, keeps)
    cut_path = folder / 'cut.mp4'
    # audio_extract caches by basename. Never reuse audio from an older cut.
    cached_audio = cut_path.with_suffix('.extracted.mp3')
    cached_audio.unlink(missing_ok=True)
    facecam_cuts.apply_cuts(source_path, keeps, cut_path)
    checkpoint('verification', 45)
    # Use a unique filename for each attempt to bypass extraction caches safely.
    import uuid
    verify_audio = folder / f'verify-{uuid.uuid4().hex}.wav'
    render.run(['-i', str(cut_path), '-vn', '-ac', '1', '-ar', '16000', str(verify_audio)])
    try:
        rendered_words = transcribe_words(verify_audio)['words']
    finally:
        verify_audio.unlink(missing_ok=True)
    rendered_text = ' '.join(w['text'] for w in rendered_words)
    expected_text = ' '.join(w['text'] for w in cut_words).lower()
    ledger = []
    for cut in data['cuts']:
        if cut['enabled'] and cut['kind'] != 'silence' and cut['text'] and (settings['mistakes'] or cut['kind'] == 'manual'):
            ledger.append({'removes': cut['text'], 'expect': expected_text.count(cut['text'].lower())})
    pass2, warnings = facecam_verify.verify_cuts_sweep(rendered_text, ledger)
    failures = facecam_verify.verify_edl_ledger(transcript['duration'], duration, deletes, cut_path) + pass2
    if not rendered_words:
        failures.append('Aucune parole retrouvée dans le rendu.')
    # A consciously retained pause is an editorial choice, not a failed render.
    silence_warnings = facecam_verify.verify_silence_sweep(rendered_words, duration)
    warnings.extend(silence_warnings)
    report = {'passed': not failures, 'failures': failures, 'warnings': warnings,
              'revision': data['revision'], 'checked_at': project.now()}
    project.write_json(folder / 'verify-report.json', report)
    if failures:
        raise ValueError('Vérification des coupes : ' + '; '.join(failures))

    checkpoint('broll_and_cards', 60)
    size = render.output_size(render.probe(cut_path)[:2], settings['format'])
    fitted = folder / 'fitted.mp4'
    render.run(['-i', str(cut_path), '-vf', render.fit_filter(size), '-c:v', 'libx264', '-preset', 'fast',
                '-crf', '18', '-c:a', 'copy', str(fitted)])
    base = fitted
    applied = []
    for i, overlay in enumerate(data['overlays']):
        if not overlay['enabled'] or not settings['motion' if overlay['kind'] == 'card' else 'broll']:
            continue
        start = project.source_to_output(overlay['start'], keeps)
        if start is None or duration - start < .5:
            continue
        length = min(overlay['duration'], duration - start)
        checkpoint('broll_and_cards', 60 + round(20 * i / max(1, len(data['overlays']))))
        if overlay['kind'] == 'card':
            asset = render.card_image(folder / f"{overlay['id']}.png", overlay['text'], size, settings)
        else:
            asset = facecam_broll.source_broll_asset(overlay['text'], overlay.get('source_kind', 'cutaway'), db)
            if not asset:
                warnings.append(f"Illustration indisponible : {overlay['text']}")
                continue
            if not debit_izivoice_usage_by_user_id(channel.user_id, STOCK_MEDIA_CREDITS, 'facecam_broll', video_id=video.id):
                raise ValueError('Crédits insuffisants pour ajouter cette illustration.')
        next_path = folder / f'layer-{i}.mp4'
        render.overlay_clip(base, asset, next_path, start, length, size, overlay['kind'] == 'card')
        if base != fitted:
            base.unlink(missing_ok=True)
        base = next_path
        applied.append({**overlay, 'output_start': start, 'duration': length})

    checkpoint('final_mux', 85)
    output_dir = STORAGE_PATH / 'channels' / str(video.channel_id) / 'videos' / str(video.id)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Never overwrite the previous published output until the new render passes.
    candidate = folder / 'candidate.mp4'
    render.finish(base, candidate, cut_words, size, settings, channel.branding or {}, folder)
    actual_duration = render.probe(candidate)[2]
    if abs(actual_duration - duration) > .5:
        raise ValueError('La durée du rendu final ne correspond pas au montage validé.')
    checkpoint('final_mux', 94)
    versions = project.read_json(folder / 'versions.json', [])
    version_id = f"v{len(versions) + 1}-{uuid.uuid4().hex[:8]}"
    version_dir = folder / 'versions'
    version_dir.mkdir(exist_ok=True)
    version_file = version_dir / f'{version_id}.mp4'
    candidate.replace(version_file)
    report['warnings'] = warnings
    report['version'] = version_id
    project.write_json(folder / 'verify-report.json', report)
    project.write_json(version_dir / f'{version_id}.json', {'project': copy.deepcopy(data), 'words': cut_words,
                        'overlays': applied, 'verification': report})
    record = {'id': version_id, 'number': len(versions) + 1, 'quality': settings['quality'], 'format': settings['format'],
              'created_at': project.now(), 'duration': actual_duration, 'width': size[0], 'height': size[1],
              'size': version_file.stat().st_size, 'revision': data['revision'], 'verified': True}
    project.write_json(folder / 'versions.json', versions + [record])
    project.append_activity(data, f"Version {record['number']} vérifiée et prête")
    project.write_json(folder / 'project.json', data)
    output_path = output_dir / 'output.mp4'
    shutil.copy2(version_file, output_path)

    from src.pipeline.youtube_metadata import generate_thumbnail
    from src.pipeline.transcode import try_ensure_sd_variant, sd_variant_path
    from src.worker.queue_runner import _finalize_output_storage
    try:
        generate_thumbnail(output_path, output_dir / 'thumbnail.jpg', video.title or 'Vidéo', None, video.id, strict=False)
        video.thumbnail_error = None
    except Exception as exc:
        video.thumbnail_error = "La miniature n'a pas pu être générée."
        logger.warning('Facecam thumbnail failed for %s: %s', video.id, exc)
    sd_variant_path(output_path).unlink(missing_ok=True)
    try_ensure_sd_variant(output_path)
    _finalize_output_storage(db, video, output_path)
    video.duration_seconds = actual_duration
    video.status, video.progress_stage, video.progress_percent = 'done', 'Vidéo prête', 100
    video.finished_at = datetime.utcnow()
    video.is_reassembly = False
    db.commit()
    # Keep decisions and versions; only intermediates are disposable.
    for temporary in [cut_path, fitted, base, *folder.glob('overlay-*.png')]:
        temporary.unlink(missing_ok=True)
