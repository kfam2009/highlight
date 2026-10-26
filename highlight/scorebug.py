"""Lectura del scorebug cuadro por cuadro.

Se trabaja sobre un video chico que contiene solo el recorte del scorebug
(lo genera extract.py). Para cada cuadro se obtiene:

  presente   si el scorebug esta en pantalla
  mph        si el rombo muestra la velocidad del lanzamiento (aparece ~2 s
             despues de cada pitcheo, sirve para ubicar lanzamientos)
  away/home  carreras
  inning     numero de entrada
  half       T (alta) / B (baja)
  outs       0, 1, 2
  count      bolas-strikes
  bases      "1 2 3" como string de 0/1 (ej "101" = primera y tercera)

Los numeros se reconocen comparando contra plantillas del perfil. Si aparece
un glifo que el perfil no conoce (por ejemplo una carrera 7 cuando el perfil
solo vio hasta 4) se le asigna un id "?n": no sabemos el valor, pero si
sabemos que CAMBIO, que es lo que importa para no perder jugadas.
"""
import json
from pathlib import Path

import cv2
import numpy as np

PROFILES = Path(__file__).resolve().parent / "profiles"


def list_profiles():
    out = []
    for p in sorted(PROFILES.iterdir()):
        f = p / "profile.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": data["name"], "description": data.get("description", ""), "sport": data.get("sport", "")})
    return out


class Profile:
    def __init__(self, name):
        base = PROFILES / name
        self.name = name
        self.cfg = json.loads((base / "profile.json").read_text(encoding="utf-8"))
        t = np.load(base / "templates.npz")
        self.ref = t["ref"].astype(np.float32)
        self.mask = t["mask"].astype(bool)
        self.templates = {}
        for key in t.files:
            if "|" not in key:
                continue
            field, label, _ = key.split("|")
            self.templates.setdefault(field, []).append((label, t[key].astype(np.int16).ravel()))
        st = base / "stinger.npy"
        self.stinger = np.load(st) if st.exists() else None

    @property
    def crop(self):
        return self.cfg["crop"]

    @property
    def crop_size(self):
        return tuple(self.cfg["crop_size"])

    @property
    def fps(self):
        return self.cfg.get("fps", 5)


class GlyphReader:
    """Clasifica parches de un campo contra plantillas conocidas y
    agrupa los desconocidos en clusters estables."""

    def __init__(self, templates, thr_ratio=0.08, glyph_threshold=140):
        self.labels = [lbl for lbl, _ in templates]
        self.mat = np.array([v for _, v in templates], dtype=np.int16) if templates else None
        self.unknown = []  # list of (vector, count)
        self.thr_ratio = thr_ratio
        self.gt = glyph_threshold

    def read(self, patch):
        b = (patch > self.gt).astype(np.int16).ravel()
        thr = self.thr_ratio * b.size
        if self.mat is not None:
            dd = np.abs(self.mat - b).sum(1)
            j = int(dd.argmin())
            if dd[j] < thr:
                return self.labels[j]
        # sin texto blanco: no hay glifo (fundido, cuadro negro)
        if b.sum() < 0.03 * b.size:
            return None
        for k, (vec, n) in enumerate(self.unknown):
            if np.abs(vec - b).sum() < thr:
                self.unknown[k] = (vec, n + 1)
                return f"?{k}"
        self.unknown.append((b, 1))
        return f"?{len(self.unknown) - 1}"


def read_scorebug_video(path, profile, progress=None):
    """Devuelve dict con listas por campo, una entrada por cuadro."""
    cfg = profile.cfg
    W, H = profile.crop_size
    regions = cfg["regions"]
    pts = cfg["points"]
    readers = {}
    for name, field in cfg["glyph_fields"].items():
        readers[name] = GlyphReader(profile.templates.get(field, []), glyph_threshold=cfg.get("glyph_threshold", 140))
    pthr = cfg.get("presence_threshold", 20)

    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    out = {k: [] for k in ["present", "mph", "away", "home", "inning", "half", "outs", "count", "bases", "pnum_sig"]}
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if fr.shape[1] != W or fr.shape[0] != H:
            fr = cv2.resize(fr, (W, H), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        score = float(np.abs(g.astype(np.float32) - profile.ref)[profile.mask].mean())
        present = score < pthr
        out["present"].append(present)

        def c(name, r=3):
            x, y = pts[name]
            return fr[y - r:y + r, x - r:x + r].reshape(-1, 3).mean(0)  # BGR

        def m(name, r=4):
            x, y = pts[name]
            return float(g[y - r:y + r, x - r:x + r].mean())

        ctr = c("center", 6)
        mph = present and ctr[2] > 160 and (ctr[2] - ctr[0]) > 45
        out["mph"].append(bool(mph))

        if not present:
            for k in ["away", "home", "inning", "half", "outs", "count", "bases"]:
                out[k].append(None)
            out["pnum_sig"].append(None)
        else:
            for name, reader in readers.items():
                x0, y0, x1, y1 = regions[name]
                val = reader.read(g[y0:y1, x0:x1])
                if name == "inning" and mph:
                    val = None  # el texto MPH tapa la entrada
                out[name].append(val)
            if mph:
                out["bases"].append(None)
                out["half"].append(None)
            else:
                b = [m("base1"), m("base2"), m("base3")]
                if all(v < 60 or v > 180 for v in b):
                    out["bases"].append("".join("1" if v > 180 else "0" for v in b))
                else:
                    out["bases"].append(None)
                up, dn = c("arrow_up", 2), c("arrow_down", 2)
                if up[1] > 70 and up[1] > up[2] + 15:
                    out["half"].append("T")
                elif dn[2] > 90 and dn[2] > dn[1] + 40:
                    out["half"].append("B")
                else:
                    out["half"].append(None)
            x0, y0, x1, y1 = regions["pnum"]
            p = cv2.GaussianBlur(g[y0:y1, x0:x1], (3, 3), 0)
            out["pnum_sig"].append(p[3:18, :30].astype(np.uint8))
        i += 1
        if progress and total and i % 500 == 0:
            progress(i / total)
    cap.release()
    out["fps"] = profile.fps
    out["frames"] = i
    return out
