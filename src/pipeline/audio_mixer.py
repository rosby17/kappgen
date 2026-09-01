from pathlib import Path
from typing import Optional, Dict, Any

from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg, get_audio_duration


def _unit(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def build_studio_mix_filter(duration: float, music_volume: float, settings: Dict[str, Any]) -> str:
    """Build functional server-side equivalents of a polished FL insert chain."""
    duration = max(0.1, duration)
    fade_in = min(max(0.0, float(settings.get("fade_in_seconds", 2.0))), duration / 2)
    fade_out = min(max(0.0, float(settings.get("fade_out_seconds", 3.0))), duration / 2)
    fade_out_start = max(0.0, duration - fade_out)
    parts = [
        # The narration is normalized once and then kept at unity gain. One
        # branch drives music ducking; the other receives voice-only effects.
        "[0:a]highpass=f=70,loudnorm=I=-15:TP=-1.2:LRA=11,asplit=2[voice_fx][voice_sc]",
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=duration={duration:.3f},asetpts=N/SR/TB,"
        f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}[music0]",
    ]
    volume = max(0.0, min(1.0, float(music_volume)))
    parts.append(f"[music0]volume={volume:.3f}[music_level]")
    music_current = "music_level"

    if settings.get("auto_ducking", True):
        amount = _unit(settings.get("ducking_amount"), 0.70)
        ratio = 2.0 + amount * 10.0
        release = 220 + amount * 380
        parts.append(
            f"[{music_current}][voice_sc]sidechaincompress=threshold=0.025:ratio={ratio:.2f}:"
            f"attack=18:release={release:.0f}:makeup=1[music_ducked]"
        )
        music_current = "music_ducked"

    # Studio effects belong to the narration, not to the background music.
    # Keep the music clean and process the voice before the final mix.
    current = "voice_fx"

    if settings.get("soundgoodizer_enabled", False):
        amount = _unit(settings.get("soundgoodizer_amount"), 0.35)
        ratio = 2.0 + amount * 6.0
        makeup = 1.0 + amount * 0.55
        parts.append(
            f"[{current}]equalizer=f=105:t=q:w=0.9:g={2.0 + amount * 4.5:.2f},"
            f"equalizer=f=7200:t=q:w=0.8:g={1.5 + amount * 5.0:.2f},"
            f"acompressor=threshold={0.18 - amount * 0.09:.3f}:ratio={ratio:.2f}:"
            f"attack=8:release=160:makeup={makeup:.2f}[voice_good]"
        )
        current = "voice_good"

    if settings.get("maximus_enabled", True):
        amount = _unit(settings.get("maximus_amount"), 0.40)
        ratio = 2.0 + amount * 4.5
        parts.extend([
            f"[{current}]asplit=3[mlo0][mmid0][mhi0]",
            f"[mlo0]lowpass=f=180,acompressor=threshold=0.13:ratio={ratio + 0.8:.2f}:attack=16:release=240[mlo]",
            f"[mmid0]highpass=f=180,lowpass=f=5000,acompressor=threshold=0.11:ratio={ratio:.2f}:attack=8:release=160[mmid]",
            f"[mhi0]highpass=f=5000,acompressor=threshold=0.09:ratio={max(1.5, ratio - 0.6):.2f}:attack=4:release=100[mhi]",
            "[mlo][mmid][mhi]amix=inputs=3:normalize=0,volume=1.10,alimiter=limit=0.94[voice_master]",
        ])
        current = "voice_master"

    if settings.get("reverb_enabled", False):
        amount = _unit(settings.get("reverb_amount"), 0.15)
        # Parallel reverb: the untouched narration remains at full level while
        # a filtered, quieter echo tail sits behind it. This avoids the hollow,
        # lower-volume sound caused by inserting reverb on the whole master.
        wet_level = 0.06 + amount * 0.24
        echo_gain_1 = 0.22 + amount * 0.30
        echo_gain_2 = 0.10 + amount * 0.22
        parts.extend([
            f"[{current}]asplit=2[voice_dry][voice_reverb_in]",
            f"[voice_reverb_in]highpass=f=180,lowpass=f=6500,"
            f"aecho=0.72:0.42:85|175:{echo_gain_1:.3f}|{echo_gain_2:.3f},"
            f"volume={wet_level:.3f}[voice_reverb_wet]",
            "[voice_dry][voice_reverb_wet]amix=inputs=2:normalize=0:duration=first[voice_space]",
        ])
        current = "voice_space"

    # normalize=0 is intentional: amix's default normalization halves the
    # narration when a music input is present, which made processed voices
    # noticeably quieter. Ducking and the music volume already control balance.
    parts.append(f"[{current}][{music_current}]amix=inputs=2:normalize=0:duration=first:dropout_transition=2[mix0]")
    current = "mix0"

    parts.append(f"[{current}]alimiter=limit=0.95[aout]")
    return ";".join(parts)


def mix_audio_tracks(
    voiceover_path: Path,
    music_path: Optional[Path],
    output_audio_path: Path,
    music_volume: float = 0.15,
    processing: Optional[Dict[str, Any]] = None,
) -> Path:
    """Mix narration and music with fades, voice-led ducking and mastering."""
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)
    vo_duration = get_audio_duration(voiceover_path)
    if not music_path or not music_path.exists() or music_volume <= 0:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", str(voiceover_path),
            "-af", "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95",
            "-c:a", "libmp3lame", "-b:a", "192k", str(output_audio_path),
        ])
        return output_audio_path

    settings = processing or {}
    run_ffmpeg([
        "ffmpeg", "-y", "-i", str(voiceover_path), "-i", str(music_path),
        "-filter_complex", build_studio_mix_filter(vo_duration, music_volume, settings),
        "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "192k", str(output_audio_path),
    ])
    logger.info(
        "Studio mix complete (music %.0f%%, ducking=%s, enhancer=%s, reverb=%s, multiband=%s) -> %s",
        music_volume * 100, settings.get("auto_ducking", True),
        settings.get("soundgoodizer_enabled", False), settings.get("reverb_enabled", False),
        settings.get("maximus_enabled", True), output_audio_path,
    )
    return output_audio_path
