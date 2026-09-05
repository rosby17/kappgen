from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import pytest
from pydantic import ValidationError
from src.pipeline import facecam_project as project, facecam_cuts as cuts, facecam_render as render


def test_settings_reject_invalid_color_and_format():
    with pytest.raises(ValidationError):
        project.FacecamSettings(accent_color='red;display:none')
    with pytest.raises(ValidationError):
        project.FacecamSettings(format='cinema')
    assert project.FacecamSettings(editing_style='vox', format_template='tutorial').editing_style == 'vox'
    with pytest.raises(ValidationError):
        project.FacecamSettings(editing_style='unknown-style')


def test_silence_plan_uses_full_source_duration():
    words = [{'text': 'Bonjour', 'start': .2, 'end': 1.0}]
    with patch.object(cuts, 'pick_best_takes_via_llm', return_value=[]):
        proposals = project.plan_cuts(words, 5)
    assert proposals[-1]['end'] == 5
    assert proposals[-1]['start'] == 1.34


def test_overlapping_decisions_and_disabled_categories():
    data = {'settings': {'silences': False, 'mistakes': True}, 'cuts': [
        {'kind':'silence','start':0,'end':2,'enabled':True},
        {'kind':'retake','start':3,'end':5,'enabled':True},
        {'kind':'manual','start':4,'end':6,'enabled':True},
        {'kind':'stutter','start':8,'end':9,'enabled':False}]}
    assert project.selected_ranges(data) == [(3,6)]
    keeps = cuts.keep_ranges_from_deletes(project.selected_ranges(data), 10)
    assert project.source_to_output(4, keeps) is None
    assert project.source_to_output(7, keeps) == 4


def test_repeated_word_verification_counts_surviving_take():
    from src.pipeline.facecam_editor import _build_delete_ranges
    words = [{'text':'oui','start':0,'end':.3}, {'text':'oui','start':.35,'end':.7}]
    with patch.object(cuts, 'pick_best_takes_via_llm', return_value=[]):
        deletes, ledger = _build_delete_ranges(words, 'user', 'video', 1)
    assert deletes
    assert ledger[0]['expect'] == 1


def test_srt_and_atomic_write(tmp_path):
    words = [{'text':'Bonjour','start':1.25,'end':2.5}]
    assert '00:00:01,250 --> 00:00:02,500' in project.srt_text(words)
    path = tmp_path / 'project.json'
    project.write_json(path, {'text':'épuré'})
    assert project.read_json(path)['text'] == 'épuré'
    assert not list(tmp_path.glob('*.tmp'))


def test_real_render_without_overlays_and_with_branded_card(tmp_path):
    source = tmp_path / 'source.mp4'
    render.run(['-f','lavfi','-i','color=c=navy:s=320x240:r=25:d=2','-f','lavfi','-i','sine=frequency=440:duration=2',
                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)])
    words = [{'text':'Bonjour','start':.2,'end':.7},{'text':'KappGen','start':.8,'end':1.6}]
    settings = project.FacecamSettings(quality='master').model_dump()
    plain = tmp_path / 'plain.mp4'
    render.finish(source, plain, words, (320,240), settings, {}, tmp_path)
    assert abs(render.probe(plain)[2] - 2) < .2
    card = render.card_image(tmp_path/'card.png','Un montage à ton image',(320,240),settings)
    decorated = render.overlay_clip(source,card,tmp_path/'decorated.mp4',.3,1,(320,240),True)
    assert abs(render.probe(decorated)[2] - 2) < .2
    ass = (tmp_path/'captions.ass').read_text()
    assert 'PlayResX: 320' in ass
    assert '&H00ffc200' in ass


def test_prepare_stops_for_review_without_render_charge(tmp_path):
    from src.pipeline import facecam_editor as editor
    video = SimpleNamespace(id='v1',channel_id='c1',raw_asset_path='raw.mp4',facecam_settings=None,status='rendering',progress_stage='',progress_percent=0)
    channel = SimpleNamespace(id='c1',user_id='u1',branding={})
    from unittest.mock import MagicMock
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [video,channel]
    words = [{'text':'Bonjour','start':.2,'end':1.0}]
    with patch.object(project,'STORAGE_PATH',tmp_path), patch.object(editor,'STORAGE_PATH',tmp_path), patch.object(editor,'ensure_extracted_audio',return_value=tmp_path/'audio.wav'), patch.object(editor,'transcribe_words',return_value={'words':words,'duration':2}), patch.object(render,'probe',return_value=(320,240,2)), patch.object(project,'plan_cuts',return_value=[]), patch.object(editor,'debit_izivoice_usage_by_user_id') as charge:
        editor.run_facecam_pipeline('v1',db)
    assert video.status == 'review'
    charge.assert_not_called()
    assert project.read_json(tmp_path/'facecam/v1/project.json')['revision'] == 1


def test_api_ownership_revision_and_review_flow(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.db.session import Base, get_db
    from src.db.models import User, Channel, Video
    from src.utils.auth import get_current_user
    from src.api.routes import facecam
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(id='owner',email='owner@example.test',name='Owner',hashed_password='unused-test-hash')
    other = User(id='other',email='other@example.test',name='Other',hashed_password='unused-test-hash')
    channel = Channel(id='channel',name='Test',user_id='owner',content_type='facecam')
    video = Video(id='video',channel_id='channel',input_type='facecam',status='review',raw_asset_path='raw.mp4')
    db.add_all([user,other,channel,video]); db.commit()
    settings = project.FacecamSettings().model_dump()
    data = {'revision':1,'duration':10,'settings':settings,'approved':False,'cuts':[], 'overlays':[], 'activity':[]}
    with patch.object(project,'STORAGE_PATH',tmp_path):
        project.write_json(tmp_path/'facecam/video/project.json',data)
        project.write_json(tmp_path/'facecam/video/transcript.json',{'words':[],'duration':10})
        (tmp_path/'raw.mp4').write_bytes(b'0123456789')
        app = FastAPI(); app.include_router(facecam.router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: other
        client = TestClient(app)
        assert client.get('/api/videos/video/facecam').status_code == 404
        assert client.get('/api/videos/video/facecam/media/source').status_code == 404
        app.dependency_overrides[get_current_user] = lambda: user
        assert client.get('/api/videos/video/facecam').status_code == 200
        media = client.get('/api/videos/video/facecam/media/source',headers={'Range':'bytes=0-3'})
        assert media.status_code == 206 and media.content == b'0123'
        assert client.get('/api/videos/video/facecam/media/unknown').status_code == 404
        body = {'revision':1,'settings':settings,'manual_cuts':[{'start':1,'end':2}]}
        saved = client.put('/api/videos/video/facecam',json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()['revision'] == 2
        assert client.put('/api/videos/video/facecam',json=body).status_code == 409
        note = client.post('/api/videos/video/facecam/notes',json={'version':'source','time':1.5,'text':'À revoir'})
        assert note.status_code == 200
        assert client.patch(f"/api/videos/video/facecam/notes/{note.json()['id']}",json={'resolved':True}).json()['resolved'] is True
        assert client.post('/api/videos/video/facecam/notes',json={'version':'wrong','time':1,'text':'x'}).status_code == 422
        undo = client.post('/api/videos/video/facecam/undo')
        assert undo.json()['cuts'] == [] and undo.json()['revision'] == 3
        with patch.object(facecam,'user_can_render',return_value=(True,'')):
            res = client.post('/api/videos/video/facecam/render',json={'revision':3,'quality':'master'})
        assert res.status_code == 200, res.text
        assert res.json()['status'] == 'queued'
        assert client.put('/api/videos/video/facecam',json={'revision':3,'settings':settings}).status_code == 409
        assert client.post('/api/videos/video/facecam/render',json={'revision':3}).status_code == 409
    db.close(); engine.dispose()


def test_full_render_produces_version_snapshot_and_output(tmp_path):
    from src.pipeline import facecam_editor as editor
    from unittest.mock import MagicMock
    source = tmp_path/'raw.mp4'
    render.run(['-f','lavfi','-i','color=c=navy:s=320x240:r=25:d=2','-f','lavfi','-i','sine=frequency=440:duration=2',
                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)])
    video = SimpleNamespace(id='v1',channel_id='c1',raw_asset_path='raw.mp4',facecam_settings=None,status='rendering',progress_stage='',progress_percent=0)
    channel = SimpleNamespace(id='c1',user_id='u1',branding={})
    db = MagicMock(); db.query.return_value.filter.return_value.first.side_effect=[video,channel]
    words = [{'text':'Bonjour','start':.2,'end':.7},{'text':'KappGen','start':.8,'end':1.6}]
    folder = tmp_path/'facecam/v1'
    project.write_json(folder/'transcript.json',{'words':words,'duration':2})
    project.write_json(folder/'project.json',{'revision':1,'duration':2,'settings':project.FacecamSettings().model_dump(),
        'cuts':[],'overlays':[{'id':'overlay-1','kind':'card','text':'Bonjour KappGen','start':.3,'duration':1,'enabled':True}], 'approved':True,'activity':[]})
    with patch.object(project,'STORAGE_PATH',tmp_path), patch.object(editor,'STORAGE_PATH',tmp_path), patch.object(editor,'transcribe_words',return_value={'words':words,'duration':2}), patch.object(editor,'debit_izivoice_usage_by_user_id'), patch('src.pipeline.youtube_metadata.generate_thumbnail'), patch('src.pipeline.transcode.try_ensure_sd_variant'), patch('src.worker.queue_runner._finalize_output_storage'):
        editor.run_facecam_pipeline('v1',db)
    assert video.status == 'done'
    versions = project.read_json(folder/'versions.json')
    assert len(versions) == 1 and versions[0]['verified']
    snapshot = project.read_json(folder/'versions'/f"{versions[0]['id']}.json")
    assert snapshot['words'] == words
    assert snapshot['verification']['passed']
    assert (tmp_path/'channels/c1/videos/v1/output.mp4').is_file()
