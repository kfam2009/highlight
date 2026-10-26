"""Ubicar el scorebug en un video nuevo.

El perfil guarda donde estaba el scorebug en el partido de referencia. Si en
otro video esta corrido (otra transmision, otra resolucion, mas arriba o mas
chico), antes de leer el partido se sacan ~20 fotos de distintos momentos, se
busca el scorebug en toda la imagen y se ajusta el recorte.
"""
import subprocess

import cv2
import numpy as np

from .ffmpeg import ffmpeg_exe


def grab_frames(video, duration, n=20, width=960):
    """Fotos en escala de grises, repartidas por el partido (busqueda rapida)."""
    frames = []
    if not duration:
        return frames
    for k in range(n):
        t = duration * (0.08 + 0.84 * k / max(1, n - 1))
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
               "-frames:v", "1", "-vf", f"scale={width}:-2,format=gray", "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            data = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout
        except subprocess.TimeoutExpired:
            continue
        if data:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                frames.append(img)
    return frames


def _template(profile):
    """Parte fija del scorebug (segun la mascara del perfil) y su caja."""
    # la parte fija mas grande (el cuerpo del scorebug), sin pixeles sueltos
    m = profile.mask.astype(np.uint8).copy()
    # zonas que cambian de partido a partido (logos y nombres de equipos)
    for x0, y0, x1, y1 in profile.cfg.get("locate_exclude", []):
        m[y0:y1, x0:x1] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) if n > 1 else 0
    x0, y0, w, h = stats[big, :4]
    # unir otras partes fijas grandes que esten cerca
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 0.05 * stats[big, cv2.CC_STAT_AREA]:
            keep[lab == i] = 1
    ys, xs = np.where(keep)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return profile.ref[y0:y1, x0:x1].astype(np.uint8), (int(x0), int(y0)), (keep[y0:y1, x0:x1] * 255).astype(np.uint8)


def locate(video, profile, duration, n=20, min_score=0.8):
    """Devuelve (crop, info). crop es el dict {x,y,w,h} en fracciones del
    cuadro, o None si no hay que cambiar nada / no se encontro."""
    frames = grab_frames(video, duration, n=n)
    if not frames:
        return None, {"status": "sin fotos"}
    tpl, (bx, by), tmask = _template(profile)
    c = profile.crop
    cw, ch = profile.crop_size
    hits = []
    for img in frames:
        H, W = img.shape
        # escala del cuadro tal que el recorte por defecto mide crop_size
        f0 = cw / (c["w"] * W)
        base = cv2.resize(img, (int(W * f0), int(H * f0)), interpolation=cv2.INTER_AREA)
        best = None
        for s in np.arange(0.7, 1.45, 0.05):
            size = (max(8, int(tpl.shape[1] * s)), max(8, int(tpl.shape[0] * s)))
            t = cv2.resize(tpl, size, interpolation=cv2.INTER_AREA)
            tm = cv2.resize(tmask, size, interpolation=cv2.INTER_NEAREST)
            if t.shape[0] >= base.shape[0] or t.shape[1] >= base.shape[1]:
                continue
            r = cv2.matchTemplate(base, t, cv2.TM_CCOEFF_NORMED, mask=tm)
            r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if best is None or mx > best[0]:
                best = (mx, loc, s)
        if best and best[0] >= min_score:
            mx, (lx, ly), s = best
            ox, oy = lx - bx * s, ly - by * s
            bw, bh = base.shape[1], base.shape[0]
            hits.append((mx, ox / bw, oy / bh, cw * s / bw, ch * s / bh))
    if len(hits) < 3:
        return None, {"status": "no encontrado", "found": len(hits), "frames": len(frames)}
    arr = np.array([h[1:] for h in hits])
    med = np.median(arr, 0)
    x, y, w, h = [float(v) for v in med]
    default = (c["x"], c["y"], c["w"], c["h"])
    info = {"status": "ok", "found": len(hits), "frames": len(frames), "score": round(float(np.median([h[0] for h in hits])), 3),
            "crop": {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}}
    if max(abs(a - b) for a, b in zip((x, y, w, h), default)) < 0.01:
        info["status"] = "en su lugar"
        return None, info
    # que el recorte no se salga del cuadro
    x = min(max(0.0, x), 1.0 - w)
    y = min(max(0.0, y), 1.0 - h)
    info["status"] = "corrido"
    return {"x": x, "y": y, "w": w, "h": h}, info


def pick_profile(video, duration, names, n=12):
    """Prueba cada perfil y devuelve (nombre, crop, info) del que mejor
    encuentra su scorebug en el video."""
    from .scorebug import Profile

    best = None
    for name in names:
        prof = Profile(name)
        crop, info = locate(video, prof, duration, n=n)
        if info.get("status") in ("ok", "en su lugar", "corrido"):
            key = (info["found"], info.get("score", 0))
            if best is None or key > best[0]:
                best = (key, name, crop, info)
    if best is None:
        return None, None, {"status": "no encontrado"}
    return best[1], best[2], best[3]
