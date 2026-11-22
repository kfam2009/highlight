"""Fixture del torneo: para ponerle a cada grabacion el nombre del partido.

El fixture viene en highlight/fixtures/<archivo>.json con los horarios en hora
del lugar (utc_offset). Los equipos que se definen sobre la marcha (super
ronda, finales) se completan desde el panel y se guardan en datos/fixture_cambios.json.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIX_DIR = Path(__file__).resolve().parent / "fixtures"


def list_fixtures():
    out = []
    for p in sorted(FIX_DIR.glob("*.json")):
        try:
            out.append({"file": p.name, "name": json.loads(p.read_text(encoding="utf-8")).get("name", p.stem)})
        except Exception:
            pass
    return out


def load(fixture_file, edits_path):
    if not fixture_file:
        return None
    p = FIX_DIR / fixture_file
    if not p.exists():
        return None
    fx = json.loads(p.read_text(encoding="utf-8"))
    edits = {}
    if edits_path and Path(edits_path).exists():
        try:
            edits = json.loads(Path(edits_path).read_text(encoding="utf-8"))
        except Exception:
            edits = {}
    tz = timezone(timedelta(hours=fx.get("utc_offset", 0)))
    for g in fx["games"]:
        e = edits.get(str(g["n"]))
        if e:
            g.update({k: v for k, v in e.items() if k in ("away", "home", "date", "time", "field")})
        g["start"] = datetime.fromisoformat(f"{g['date']}T{g['time']}").replace(tzinfo=tz).timestamp()
    return fx


def save_edit(edits_path, n, data):
    p = Path(edits_path)
    edits = {}
    if p.exists():
        try:
            edits = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            edits = {}
    cur = edits.get(str(n), {})
    cur.update({k: str(v).strip() for k, v in data.items() if k in ("away", "home", "date", "time", "field")})
    edits[str(n)] = cur
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")


def match(fx, field, when=None):
    """El partido de esa cancha que se esta jugando o esta por empezar.

    - si en esa cancha empezo un partido hace menos de 1 h 50 (o empieza en
      menos de 20 min), es ese: puede ser una reconexion en medio del juego;
    - si no, el proximo que empieza en menos de 75 min (la productora manda la
      senal un rato antes);
    - si no, uno que empezo hace menos de 3 h (partido largo).
    """
    if not fx or not field:
        return None
    t = (when or datetime.now().astimezone()).timestamp()
    games = sorted((g for g in fx["games"] if g["field"] == field), key=lambda g: g["start"])
    prev = None
    nxt = None
    for g in games:
        if g["start"] <= t + 20 * 60:
            prev = g
        elif nxt is None:
            nxt = g
    if prev and t - prev["start"] <= 110 * 60:
        return prev
    if nxt and nxt["start"] - t <= 75 * 60:
        return nxt
    if prev and t - prev["start"] <= 180 * 60:
        return prev
    return None


def next_game(fx, field, when=None, within_h=4.0):
    """Proximo partido de esa cancha que todavia no empezo (en las proximas
    within_h horas), o None si ya no hay mas por hoy."""
    import time as _t
    if not fx or not field:
        return None
    t = when or _t.time()
    games = sorted((g for g in fx["games"] if g["field"] == field and g["start"] > t - 15 * 60), key=lambda g: g["start"])
    for g in games:
        if g["start"] - t <= within_h * 3600:
            return g
    return None


def title(g):
    name = f"Gm {g['n']:02d} {g['away']} vs {g['home']}"
    return re.sub(r'[<>:"/\\|?*]+', "", name).strip()
