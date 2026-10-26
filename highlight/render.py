"""Cortes de video: vistas previas para el panel y exportacion para Premiere.

Todo se corta desde el video ORIGINAL. La vista previa es liviana (720p,
rapida). La exportacion se rehace desde el original con los ajustes del
operador (recortes / extensiones), en buena calidad.
"""
import re
import subprocess
from pathlib import Path

from .ffmpeg import ffmpeg_exe, hms


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_")
    return value[:80] or "video"


def cut(video, out_path, start, length, mode="preview"):
    start = max(0.0, float(start))
    length = max(0.5, float(length))
    pre = max(0.0, start - 5.0)
    offset = start - pre
    if mode == "export":
        vargs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        aargs = ["-c:a", "aac", "-b:a", "160k"]
        timeout = max(120, min(900, int(length * 8)))
    else:
        vargs = ["-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p"]
        aargs = ["-c:a", "aac", "-b:a", "96k"]
        timeout = max(60, min(300, int(length * 4)))
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-ss", hms(pre), "-i", str(video), "-ss", f"{offset:.3f}",
           "-t", f"{length:.3f}", *vargs, *aargs, "-movflags", "+faststart", str(out_path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        Path(out_path).unlink(missing_ok=True)
        return False
    p = Path(out_path)
    if not p.exists() or p.stat().st_size < 1024:
        p.unlink(missing_ok=True)
        return False
    return True


def poster(video, out_path, at_time):
    try:
        subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-ss", f"{max(0, at_time):.3f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        Path(out_path).unlink(missing_ok=True)


def adjusted_window(clip, decision):
    """Inicio y duracion en el video original aplicando los ajustes."""
    d = decision or {}
    trim_start = max(0.0, float(d.get("trimStart") or 0))
    trim_end = max(0.0, float(d.get("trimEnd") or 0))
    ext_start = max(0.0, float(d.get("extendStart") or 0))
    ext_end = max(0.0, float(d.get("extendEnd") or 0))
    start = max(0.0, float(clip["start"]) - ext_start + trim_start)
    end = float(clip["end"]) + ext_end - trim_end
    return start, max(1.0, end - start)
