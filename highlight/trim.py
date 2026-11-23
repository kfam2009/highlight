"""Recorte automatico por cortes de camara.

Calibrado con las marcas de entrada/salida que puso el editor el 23/11
(113 jugadas): el corta unos 3 s antes del contacto y entre 4 y 7 s despues,
segun la jugada, sin el replay. El contacto se ve en la imagen: la camara del
lanzamiento (la toma fija que mira hacia la tribuna) corta a otra toma justo
cuando la bateadora le pega o la receptora agarra la pelota.

La toma del lanzamiento se reconoce sola en cada partido: es la imagen que
mas se repite parecida a una plantilla general (pitchcam.npy)."""
from pathlib import Path

import numpy as np

FPS = 2.0
BEFORE = 3.2                       # segundos antes del contacto
AFTER = {"ponche": 4.0, "out": 4.5, "tercer_out": 4.5, "base_por_bolas": 4.5,
         "hit": 7.0, "carrera": 7.0}
AFTER_DEFAULT = 5.0
SKIP = {"final", "revisar", "previa", "himno", "saludo"}
_TPL = None


def _template():
    global _TPL
    if _TPL is None:
        p = Path(__file__).parent / "profiles" / "pitchcam.npy"
        _TPL = np.load(p).astype(np.float32) if p.exists() else False
    return _TPL if _TPL is not False else None


def _dist(ctx, t, chunk=2000):
    out = np.empty(len(ctx), np.float32)
    for i in range(0, len(ctx), chunk):
        out[i:i + chunk] = np.abs(ctx[i:i + chunk].astype(np.float32) - t).mean((1, 2, 3))
    return out


def pitch_template(ctx, frac=0.08, it=2):
    """Plantilla de la toma del lanzamiento de este partido."""
    t = _template()
    if t is None or ctx is None or len(ctx) < 600 or ctx.shape[1:] != t.shape:
        return None
    for _ in range(it):
        d = _dist(ctx, t)
        k = d <= np.percentile(d, frac * 100)
        t = np.median(ctx[k].astype(np.float32), 0)
    return t


def pitch_shots(ctx, tpl, thr=15.0, minlen=3):
    """(inicio, fin) en segundos de cada toma del lanzamiento."""
    if tpl is None or ctx is None:
        return []
    on = _dist(ctx, tpl) < thr
    out, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and (on[j] or (j + 1 < n and on[j + 1])):
                j += 1
            if j - i >= minlen:
                out.append((i / FPS, j / FPS))
            i = j
        else:
            i += 1
    return out


def contact_time(c, shots):
    ends = [s[1] for s in shots]
    if c.get("timing") == "mph":
        cand = [t for t in ends if abs(t - c["pitchTime"]) <= 4.0]
        return min(cand, key=lambda t: abs(t - c["pitchTime"])) if cand else None
    sb = c.get("scorebugTime") or c["end"]
    cand = [t for t in ends if c["start"] <= t <= sb + 5.0]
    return cand[0] if cand else None


def apply(clips, shots):
    """Achica cada jugada a lanzamiento + jugada. Guarda el corte original en
    origStart/origEnd (la vista previa lo sigue mostrando entero)."""
    if not shots:
        return clips
    for c in clips:
        if c["type"] in SKIP or c.get("half") in ("PRE", "FIN") or c.get("timing") == "tramo":
            continue
        t = contact_time(c, shots)
        if t is None:
            continue
        c.setdefault("origStart", c["start"])
        c.setdefault("origEnd", c["end"])
        c["start"] = round(max(0.0, t - BEFORE), 2)
        c["end"] = round(t + AFTER.get(c["type"], AFTER_DEFAULT), 2)
        c["duration"] = round(c["end"] - c["start"], 2)
        c["contact"] = round(t, 2)
        c["autoTrim"] = True
    return clips
