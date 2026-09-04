from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from src.utils.logger import logger
from src.utils.ffmpeg_runner import run_ffmpeg, get_audio_duration

# Keep this off globally until the advanced treatment chain has been rebuilt
# and approved. It applies equally to legacy channel configurations.
ADVANCED_STUDIO_MIX_ENABLED = False


def _unit(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _sfx_filter_parts(sfx_specs: Optional[List[Dict[str, Any]]]) -> Tuple[List[str], List[str]]:
    """One `adelay` chain per sound effect input, so each clip starts at its
    matched moment (src/pipeline/sound_effects.py) instead of at t=0.
    `all=1` applies the delay to every channel regardless of whether the
    uploaded clip is mono or stereo — without it ffmpeg only delays channel 0
    and the effect plays instantly out of the other channel(s)."""
    parts, labels = [], []
    for i, spec in enumerate(sfx_specs or []):
        delay_ms = max(0, round(float(spec["start"]) * 1000))
        label = f"sfx{i}"
        parts.append(f"[{spec['input_index']}:a]adelay={delay_ms}:all=1,volume={float(spec.get('volume', 0.9)):.2f}[{label}]")
        labels.append(label)
    return parts, labels


def build_studio_mix_filter(
    duration: float,
    music_volume: float,
    settings: Dict[str, Any],
    sfx_specs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the safe baseline music/voice mix, plus any matched sound
    effects (each a separate ffmpeg input, see mix_audio_tracks) layered in
    at the final mix stage.

    Advanced studio processing is intentionally suspended product-wide while
    its sound quality is being reworked. Existing channels may still carry old
    opt-in flags in their JSON configuration; never let those legacy values
    alter a new render during the suspension.
    """
    duration = max(0.1, duration)
    fade_in = min(max(0.0, float(settings.get("fade_in_seconds", 2.0))), duration / 2)
    fade_out = min(max(0.0, float(settings.get("fade_out_seconds", 3.0))), duration / 2)
    fade_out_start = max(0.0, duration - fade_out)
    # Only split the narration when sidechain ducking will consume the second
    # branch. FFmpeg rejects an unconnected asplit output when ducking is off.
    voice_input = "[0:a]highpass=f=70,loudnorm=I=-15:TP=-1.2:LRA=11"
    ducking_enabled = ADVANCED_STUDIO_MIX_ENABLED and bool(settings.get("auto_ducking", False))
    parts = [
        voice_input + (",asplit=2[voice_fx][voice_sc]" if ducking_enabled else "[voice_fx]"),
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=duration={duration:.3f},asetpts=N/SR/TB,"
        f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}[music0]",
    ]
    volume = max(0.0, min(1.0, float(music_volume)))
    parts.append(f"[music0]volume={volume:.3f}[music_level]")
    music_current = "music_level"

    if ducking_enabled:
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

    if ADVANCED_STUDIO_MIX_ENABLED and settings.get("soundgoodizer_enabled", False):
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

    if ADVANCED_STUDIO_MIX_ENABLED and settings.get("maximus_enabled", False):
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

    if ADVANCED_STUDIO_MIX_ENABLED and settings.get("reverb_enabled", False):
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

    sfx_parts, sfx_labels = _sfx_filter_parts(sfx_specs)
    parts.extend(sfx_parts)

    # normalize=0 is intentional: amix's default normalization halves the
    # narration when a music input is present, which made processed voices
    # noticeably quieter. Ducking and the music volume already control balance.
    mix_inputs = [current, music_current] + sfx_labels
    mix_labels = "".join(f"[{label}]" for label in mix_inputs)
    parts.append(f"{mix_labels}amix=inputs={len(mix_inputs)}:normalize=0:duration=first:dropout_transition=2[mix0]")
    current = "mix0"

    parts.append(f"[{current}]alimiter=limit=0.95[aout]")
    return ";".join(parts)


def mix_audio_tracks(
    voiceover_path: Path,
    music_path: Optional[Path],
    output_audio_path: Path,
    music_volume: float = 0.15,
    processing: Optional[Dict[str, Any]] = None,
    sfx_clips: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Mix narration and music with fades, voice-led ducking and mastering,
    plus any matched sound effects layered in at their cued timestamps.

    sfx_clips: [{"path": Path, "start": float}, ...] — from
    src/pipeline/sound_effects.py's cues, resolved to real file paths by the
    caller. Each becomes its own ffmpeg input rather than being pre-mixed,
    so its delay/volume stays independently adjustable in the filter graph."""
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)
    vo_duration = get_audio_duration(voiceover_path)
    sfx_clips = [c for c in (sfx_clips or []) if c.get("path") and Path(c["path"]).exists()]
    has_music = bool(music_path and music_path.exists() and music_volume > 0)

    if not has_music and not sfx_clips:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", str(voiceover_path),
            "-af", "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95",
            "-c:a", "libmp3lame", "-b:a", "192k", str(output_audio_path),
        ])
        return output_audio_path

    settings = processing or {}
    inputs = ["-i", str(voiceover_path)]
    next_index = 1
    if has_music:
        inputs += ["-i", str(music_path)]
        next_index = 2

    sfx_specs = []
    for clip in sfx_clips:
        inputs += ["-i", str(clip["path"])]
        sfx_specs.append({"input_index": next_index, "start": clip["start"]})
        next_index += 1

    if has_music:
        filter_complex = build_studio_mix_filter(vo_duration, music_volume, settings, sfx_specs)
    else:
        # No music track: a lighter graph — just cleaned-up narration plus
        # whatever sound effects were cued, no ducking/fades to coordinate
        # against a music bed that doesn't exist here.
        sfx_parts, sfx_labels = _sfx_filter_parts(sfx_specs)
        parts = ["[0:a]highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11[voice_fx]"] + sfx_parts
        mix_inputs = ["voice_fx"] + sfx_labels
        mix_labels = "".join(f"[{label}]" for label in mix_inputs)
        parts.append(f"{mix_labels}amix=inputs={len(mix_inputs)}:normalize=0:duration=first:dropout_transition=2[mix0]")
        parts.append("[mix0]alimiter=limit=0.95[aout]")
        filter_complex = ";".join(parts)

    run_ffmpeg([
        "ffmpeg", "-y", *inputs,
        # Forces single-threaded filter-graph initialization. This studio-mix
        # graph chains multiple asplit nodes (multiband + reverb), and under
        # concurrent load (several videos rendering at once on the same VPS)
        # FFmpeg's default multi-threaded graph init has a known race that
        # spuriously reports "Filter asplit has an unconnected output" on an
        # otherwise-valid graph — confirmed here: the exact same command with
        # the exact same files succeeds every time when run in isolation, but
        # failed twice in production under load. Audio filtering is cheap
        # enough that single-threaded init costs nothing noticeable.
        "-filter_complex_threads", "1",
        "-filter_complex", filter_complex,
        "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "192k", str(output_audio_path),
    ])
    logger.info(
        "Studio mix complete (music %.0f%%, ducking=%s, enhancer=%s, reverb=%s, multiband=%s) -> %s",
        music_volume * 100, ADVANCED_STUDIO_MIX_ENABLED and settings.get("auto_ducking", False),
        ADVANCED_STUDIO_MIX_ENABLED and settings.get("soundgoodizer_enabled", False), ADVANCED_STUDIO_MIX_ENABLED and settings.get("reverb_enabled", False),
        ADVANCED_STUDIO_MIX_ENABLED and settings.get("maximus_enabled", False), output_audio_path,
    )
    return output_audio_path
