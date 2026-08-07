from typing import List, Dict, Any
from src.utils.logger import logger

def calculate_pacing_segments(total_duration: float) -> List[Dict[str, float]]:
    """
    Calculates dynamic image display durations over the video runtime (Section 6.2).
    - Accroche (0 - 20s): ~3.5s per image
    - Mise en route (20s - 2min): ~7.0s per image
    - Corps (début) (2min - 30min): ~15.0s per image
    - Corps (long) (>30min): ~35.0s per image
    """
    segments = []
    current_time = 0.0
    
    while current_time < total_duration:
        remaining = total_duration - current_time
        
        if current_time < 20.0:
            duration = min(3.5, remaining)
        elif current_time < 120.0:
            duration = min(7.0, remaining)
        elif current_time < 1800.0:
            duration = min(15.0, remaining)
        else:
            duration = min(35.0, remaining)
            
        if duration < 1.0 and segments:
            segments[-1]["duration"] += duration
            break
            
        segments.append({
            "start": round(current_time, 2),
            "duration": round(duration, 2),
            "end": round(current_time + duration, 2)
        })
        current_time += duration

    logger.info(f"Generated {len(segments)} pacing segments for total duration {total_duration:.2f}s")
    return segments
