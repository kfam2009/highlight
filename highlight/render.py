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


LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# ruido de fondo de la senal: zumbido grave constante (en el campo 2 medido
# en 44 Hz, +22 dB) y soplido. Se saca
# antes de nivelar. Solo al exportar; la grabacion queda como vino.
DENOISE = "highpass=f=100,highpass=f=100,bandreject=f=44:width_type=h:w=8,bandreject=f=88:width_type=h:w=8,afftdn=nf=-30:nr=15"


def audio_chain():
    try:
        from .server import load_config
        on = bool(load_config().get("denoise", False))
    except Exception:
        on = False
    return (DENOISE + "," + LOUDNORM) if on else LOUDNORM


def cut(video, out_path, start, length, mode="preview"):
    start = max(0.0, float(start))
    length = max(0.5, float(length))
    pre = max(0.0, start - 5.0)
    offset = start - pre
    if mode == "export":
        vargs = ["-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        # audio al nivel normal de una transmision (la senal suele venir baja)
        aargs = ["-af", audio_chain(), "-ar", "48000", "-c:a", "aac", "-b:a", "160k"]
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
    if d.get("inTime") is not None or d.get("outTime") is not None:
        # inicio y fin marcados a mano en el panel (segundos del video original)
        start = float(d["inTime"]) if d.get("inTime") is not None else float(clip["start"])
        end = float(d["outTime"]) if d.get("outTime") is not None else float(clip["end"])
        start = max(0.0, start)
        return start, max(1.0, end - start)
    trim_start = max(0.0, float(d.get("trimStart") or 0))
    trim_end = max(0.0, float(d.get("trimEnd") or 0))
    ext_start = max(0.0, float(d.get("extendStart") or 0))
    ext_end = max(0.0, float(d.get("extendEnd") or 0))
    start = max(0.0, float(clip["start"]) - ext_start + trim_start)
    end = float(clip["end"]) + ext_end - trim_end
    return start, max(1.0, end - start)


def audio_gain(video, start, length, target_peak_db=-3.0, max_gain_db=12.0):
    """Cuanto subir el audio de un tramo para que el pico quede en -3 dB
    (sin pasarse de +12 dB). Devuelve la ganancia lineal (1.0 = sin cambio)."""
    import re as _re
    cmd = [ffmpeg_exe(), "-hide_banner", "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.5, length):.3f}",
           "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    try:
        err = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="ignore",
                             timeout=120).stderr
    except subprocess.TimeoutExpired:
        return 1.0
    m = _re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    if not m:
        return 1.0
    peak = float(m.group(1))
    if peak <= -80:      # sin audio
        return 1.0
    g = max(0.0, min(max_gain_db, target_peak_db - peak))
    return round(10 ** (g / 20), 3)
