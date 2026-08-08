import pytest
import os
import subprocess
from pathlib import Path

# Tests must never touch the production Postgres database or paid AI services.
os.environ["DATABASE_URL"] = "sqlite:////tmp/nichecut-pytest.db"
os.environ["STORAGE_PATH"] = "/tmp/nichecut-pytest-storage"
os.environ["IZIVOICE_API_KEY"] = ""
os.environ["AI_IMAGE_PROVIDER_API_KEY"] = ""

from fastapi.testclient import TestClient
from PIL import Image
from src.api.app import app
from src.db.session import init_db, SessionLocal
from src.db.models import User, Channel, Video
from src.models.project import VideoStatus
from src.pipeline.orchestrator import run_video_pipeline
from src.config import STORAGE_PATH

client = TestClient(app)

def attach_test_library(channel_id: str, tmp_path: Path):
    image_path = tmp_path / f"{channel_id}.png"
    Image.new("RGB", (320, 180), "#123456").save(image_path)
    with open(image_path, "rb") as image_file:
        response = client.post(
            f"/api/channels/{channel_id}/library-images",
            files={"files": (image_path.name, image_file, "image/png")},
        )
    assert response.status_code == 200

@pytest.fixture(autouse=True)
def setup_database():
    init_db()
    db = SessionLocal()
    db.query(User).filter(User.email == "user_test@nichecut.com").delete()
    db.commit()
    db.close()

def test_api_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_user_registration_and_login():
    reg_payload = {
        "email": "user_test@nichecut.com",
        "name": "Testeur Supabase",
        "password": "SecretPassword123"
    }
    reg_res = client.post("/api/auth/register", json=reg_payload)
    assert reg_res.status_code == 201
    user_data = reg_res.json()
    assert user_data["email"] == "user_test@nichecut.com"
    user_id = user_data["id"]

    # Login
    login_res = client.post("/api/auth/login", json={
        "email": "user_test@nichecut.com",
        "password": "SecretPassword123"
    })
    assert login_res.status_code == 200
    assert login_res.json()["id"] == user_id

def test_channel_crud_flow(tmp_path):
    payload = {
        "name": "Chaîne Test Pytest",
        "niche": "Philosophie & Spiritualité",
        "subtitle_style": {
            "font": "Arial",
            "size": 44,
            "color": "&H00FFFFFF",
            "outline_color": "&H00000000",
            "outline_width": 3,
            "position": "bottom",
            "karaoke": True
        },
        "branding": {"channel_name_text": "@TestChannel"},
        "music_preference": {"enabled": True, "track_id_or_style": "ambient", "volume": 0.15},
        "image_style": {"source": "library", "style_prompt": "cinematic"},
        "effects_config": {"grain": True, "color_grade": "warm", "zoom_min_pct": 1.0, "zoom_max_pct": 1.15}
    }
    create_res = client.post("/api/channels", json=payload)
    assert create_res.status_code == 201
    chan_data = create_res.json()
    channel_id = chan_data["id"]
    attach_test_library(channel_id, tmp_path)

    # Single video script submission
    vid_res = client.post("/api/videos", data={
        "channel_id": channel_id,
        "input_type": "text",
        "script_text": "Ceci est un script de test automatique."
    })
    assert vid_res.status_code == 201
    vid_data = vid_res.json()[0]
    assert vid_data["status"] == "queued"
    assert vid_data["input_type"] == "text"

    # Delete channel
    del_res = client.delete(f"/api/channels/{channel_id}")
    assert del_res.status_code == 200

def test_audio_upload_submission(tmp_path):
    chan_res = client.post("/api/channels", json={"name": "Audio Channel Test"})
    channel_id = chan_res.json()["id"]
    attach_test_library(channel_id, tmp_path)

    dummy_audio = tmp_path / "test_voice.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "libmp3lame", str(dummy_audio)
    ], check=True, capture_output=True)

    with open(dummy_audio, "rb") as f:
        response = client.post(
            "/api/videos",
            data={
                "channel_id": channel_id,
                "input_type": "audio",
                "script_text": "Audio préenregistré de test"
            },
            files={"audio_files": ("test_voice.mp3", f, "audio/mpeg")}
        )

    assert response.status_code == 201
    data = response.json()[0]
    assert data["input_type"] == "audio"
    assert data["audio_input_path"] is not None
    assert Path(data["audio_input_path"]).exists()

    corrupt_audio = tmp_path / "corrupt.mp3"
    corrupt_audio.write_bytes(b"this is not audio")
    with open(corrupt_audio, "rb") as f:
        rejected = client.post(
            "/api/videos",
            data={"channel_id": channel_id, "input_type": "audio"},
            files={"audio_files": ("corrupt.mp3", f, "audio/mpeg")}
        )
    assert rejected.status_code == 400
    assert "corrompu" in rejected.json()["detail"]

def test_rendering_pipeline_isolated(tmp_path):
    library_dir = STORAGE_PATH / "test-library"
    library_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), "#123456").save(library_dir / "scene.png")
    config = {
        "name": "Isolated Pipeline Test",
        "subtitle_style": {"font": "Arial", "size": 40, "karaoke": True},
        "effects_config": {"color_grade": "warm", "grain": True},
        "image_style": {"source": "library", "library_path": "test-library"},
    }
    script = "Le temps passe mais la sagesse demeure pour toujours."
    output_file = run_video_pipeline(config, script, tmp_path)
    assert output_file.exists()
    assert output_file.name == "output.mp4"
    assert (tmp_path / "source" / "script.txt").exists()
    assert (tmp_path / "source" / "subtitles.ass").exists()
