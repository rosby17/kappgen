import argparse
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.utils.logger import logger
from src.pipeline.orchestrator import run_video_pipeline

def run_phase0_test(test_script: str = None):
    """
    Runs a standalone CLI test of the render engine on an isolated script.
    """
    if not test_script:
        test_script = (
            "Bienvenue dans cette exploration de la philosophie stoïcienne. "
            "Le calme intérieur ne dépend pas du monde extérieur, mais de notre interprétation des événements. "
            "Cultivez la paix de l'esprit jour après jour."
        )
        
    logger.info("==========================================")
    logger.info("   Nichecut - Phase 0 CLI Pipeline Test   ")
    logger.info("==========================================")
    
    test_channel_config = {
        "name": "Niche Philo Test",
        "niche": "Philosophie & Spiritualité",
        "subtitle_style": {
            "font": "Arial",
            "size": 48,
            "color": "&H00FFFFFF",        # White
            "outline_color": "&H00000000",# Black outline
            "outline_width": 3,
            "position": "bottom",
            "karaoke": True
        },
        "branding": {
            "channel_name_text": "Sagesse & Esprit"
        },
        "music_preference": {
            "enabled": True,
            "track_id_or_style": "ambient",
            "volume": 0.15
        },
        "image_style": {
            "source": "library",
            "style_prompt": "cinematic dramatic lighting stoic aesthetic"
        },
        "effects_config": {
            "grain": True,
            "color_grade": "warm",
            "zoom_min_pct": 1.0,
            "zoom_max_pct": 1.12
        }
    }
    
    output_dir = BASE_DIR / "storage" / "test_phase0_output"
    output_path = run_video_pipeline(
        channel_config=test_channel_config,
        script_text=test_script,
        output_dir=output_dir
    )
    
    logger.info("==========================================")
    logger.info(f"TEST SUCCESSFUL! Generated MP4: {output_path}")
    logger.info("==========================================")

def main():
    parser = argparse.ArgumentParser(description="Nichecut CLI Pipeline & Runner")
    parser.add_argument("--test", action="store_true", help="Run Phase 0 isolated pipeline test")
    parser.add_argument("--script", type=str, default=None, help="Custom text script for Phase 0 test")
    parser.add_argument("--worker", action="store_true", help="Run background queue runner worker daemon")
    
    args = parser.parse_args()
    
    if args.worker:
        from src.worker.queue_runner import start_queue_worker
        start_queue_worker()
    else:
        run_phase0_test(args.script)

if __name__ == "__main__":
    main()
