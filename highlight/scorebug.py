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
        # logos / nombres de equipos cambian de partido a partido: no sirven
        # para saber si el scorebug esta en pantalla
        for x0, y0, x1, y1 in self.cfg.get("locate_exclude", []):
            self.mask[y0:y1, x0:x1] = False
        self.templates = {}
        for key in t.files:
            if "|" not in key:
                continue
            field, label, _ = key.split("|")
            self.templates.setdefault(field, []).append((label, t[key].astype(np.uint8)))
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
    agrupa los desconocidos en clusters estables.

    binarize "fixed": texto claro sobre fondo oscuro, umbral fijo.
             "normalized": se estira el contraste del parche antes del umbral
             (sirve para texto oscuro sobre fondo claro y fundidos).
    match    "area": tolerancia proporcional al tamano del parche.
             "ink":  tolerancia proporcional a la tinta del glifo (mas
             estricta; distingue 4TH de 5TH en textos largos).
    """

    def __init__(self, templates, thr_ratio=0.08, glyph_threshold=140, binarize="fixed", match="area"):
        self.labels = [lbl for lbl, _ in templates]
        # todas las plantillas de un mismo set se llevan al tamano de la primera
        self.shape = templates[0][1].shape if templates else None
        mats = []
        for _, v in templates:
            if v.shape != self.shape:
                v = cv2.resize(v, (self.shape[1], self.shape[0]), interpolation=cv2.INTER_NEAREST)
            mats.append(v.astype(np.int16).ravel())
        self.mat = np.array(mats, dtype=np.int16) if templates else None
        self.ink = self.mat.sum(1) if self.mat is not None else None
        self.unknown = []  # list of (vector, count)
        self.thr_ratio = thr_ratio
        self.gt = glyph_threshold
        self.binarize = binarize
        self.match = match

    def _bin(self, patch):
        if self.binarize == "normalized":
            q = patch.astype(np.float32)
            lo, hi = np.percentile(q, 5), np.percentile(q, 99.5)
            if hi - lo < 40:
                return np.zeros(q.size, np.int16)
            return (((q - lo) / (hi - lo)) > 0.5).astype(np.int16).ravel()
        return (patch > self.gt).astype(np.int16).ravel()

    def _thr(self, ink_a, ink_b, size):
        if self.match == "ink":
            return np.maximum(10, self.thr_ratio * np.minimum(ink_a, ink_b))
        return self.thr_ratio * size

    def read(self, patch):
        if self.shape is not None and patch.shape != self.shape:
            patch = cv2.resize(patch, (self.shape[1], self.shape[0]), interpolation=cv2.INTER_AREA)
        b = self._bin(patch)
        bink = int(b.sum())
        if self.mat is not None:
            dd = np.abs(self.mat - b).sum(1)
            thr = self._thr(self.ink, bink, b.size)
            ok = dd < thr
            if ok.any():
                j = int(np.argmin(np.where(ok, dd, 1 << 30)))
                return self.labels[j]
        # sin texto: no hay glifo (fundido, cuadro negro)
        if bink < 0.03 * b.size:
            return None
        for k, (vec, n) in enumerate(self.unknown):
            if np.abs(vec - b).sum() < self._thr(int(vec.sum()), bink, b.size):
                self.unknown[k] = (vec, n + 1)
                return f"?{k}"
        self.unknown.append((b, 1))
        return f"?{len(self.unknown) - 1}"


def _glyph_specs(cfg):
    """glyph_fields acepta "nombre": "set" (formato viejo) o
    "nombre": {"set": ..., "invert": true/false}. invert = texto oscuro."""
    specs = {}
    for name, v in cfg["glyph_fields"].items():
        if isinstance(v, str):
            specs[name] = {"set": v, "invert": False, "ratio": 0.08, "binarize": "fixed", "match": "area"}
        else:
            specs[name] = {"set": v["set"], "invert": bool(v.get("invert")), "ratio": v.get("ratio", 0.08),
                           "binarize": v.get("binarize", "fixed"), "match": v.get("match", "area")}
    return specs


def read_scorebug_video(path, profile, progress=None):
    """Devuelve dict con listas por campo, una entrada por cuadro.

    Reglas que cambian segun el diseno del grafico (profile.json):
      bases_rule  "bright" (cuadro blanco = ocupada) o "yellow"
      mph_rule    "red_center" (rombo rojo con la velocidad) o
                  "region_diff" (una zona cambia respecto de la referencia)
      inning_half si existe el campo de glifo "inning_half" (ej. "1ST ▲"),
                  sus etiquetas son tipo "1T" / "1B"; si no, se usan
                  "inning" + flechas de color.
    """
    cfg = profile.cfg
    W, H = profile.crop_size
    regions = cfg["regions"]
    pts = cfg["points"]
    gt = cfg.get("glyph_threshold", 140)
    specs = _glyph_specs(cfg)
    readers = {n: GlyphReader(profile.templates.get(sp["set"], []), thr_ratio=sp["ratio"], glyph_threshold=gt,
                              binarize=sp["binarize"], match=sp["match"]) for n, sp in specs.items()}
    pthr = cfg.get("presence_threshold", 20)
    bases_rule = cfg.get("bases_rule", "bright")
    mph_cfg = cfg.get("mph", {"rule": "red_center"})
    combined = "inning_half" in specs

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
        gf = g.astype(np.float32)
        score = float(np.abs(gf - profile.ref)[profile.mask].mean())
        present = score < pthr
        out["present"].append(present)

        def c(name, r=3):
            x, y = pts[name]
            return fr[y - r:y + r, x - r:x + r].reshape(-1, 3).mean(0)  # BGR

        def m(name, r=4):
            x, y = pts[name]
            return float(g[y - r:y + r, x - r:x + r].mean())

        mph = False
        if present:
            if mph_cfg["rule"] == "red_center":
                ctr = c("center", 6)
                mph = ctr[2] > 160 and (ctr[2] - ctr[0]) > 45
            else:
                x0, y0, x1, y1 = mph_cfg["region"]
                mph = float(np.abs(gf[y0:y1, x0:x1] - profile.ref[y0:y1, x0:x1]).mean()) > mph_cfg.get("thr", 25)
        out["mph"].append(bool(mph))

        if not present:
            for k in ["away", "home", "inning", "half", "outs", "count", "bases"]:
                out[k].append(None)
            out["pnum_sig"].append(None)
        else:
            vals = {}
            for name, reader in readers.items():
                x0, y0, x1, y1 = regions[name]
                patch = g[y0:y1, x0:x1]
                if specs[name]["invert"]:
                    patch = 255 - patch
                vals[name] = reader.read(patch)
            if combined:
                ih = vals.pop("inning_half")
                if ih and not ih.startswith("?") and ih[-1] in "TB":
                    vals["inning"], vals["half"] = ih[:-1], ih[-1]
                else:
                    vals["inning"], vals["half"] = None, None
            elif mph_cfg["rule"] == "red_center" and mph:
                vals["inning"] = None  # el texto MPH tapa la entrada
            for k in ["away", "home", "inning", "outs", "count"]:
                out[k].append(vals.get(k))

            bases_hidden = mph and mph_cfg["rule"] == "red_center"
            if bases_hidden:
                out["bases"].append(None)
            elif bases_rule == "yellow":
                flags = []
                for n in ("base1", "base2", "base3"):
                    b, gg, r = c(n, 3)
                    if r - b > 60 and r > 150:
                        flags.append("1")
                    elif abs(r - b) < 40:
                        flags.append("0")
                    else:
                        flags.append(None)
                out["bases"].append("".join(flags) if None not in flags else None)
            else:
                b = [m("base1"), m("base2"), m("base3")]
                if all(v < 60 or v > 180 for v in b):
                    out["bases"].append("".join("1" if v > 180 else "0" for v in b))
                else:
                    out["bases"].append(None)

            if combined:
                out["half"].append(vals.get("half"))
            elif mph:
                out["half"].append(None)
            else:
                up, dn = c("arrow_up", 2), c("arrow_down", 2)
                if up[1] > 70 and up[1] > up[2] + 15:
                    out["half"].append("T")
                elif dn[2] > 90 and dn[2] > dn[1] + 40:
                    out["half"].append("B")
                else:
                    out["half"].append(None)
            if "pnum" in regions:
                x0, y0, x1, y1 = regions["pnum"]
                p = cv2.GaussianBlur(g[y0:y1, x0:x1], (3, 3), 0)
                out["pnum_sig"].append(p[3:18, :30].astype(np.uint8))
            else:
                out["pnum_sig"].append(None)
        i += 1
        if progress and total and i % 500 == 0:
            progress(i / total)
    cap.release()
    out["fps"] = profile.fps
    out["frames"] = i
    return out
