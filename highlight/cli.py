"""Analizar un partido desde la consola (sin el panel).

    python -m highlight.cli "D:\\Grabaciones\\partido.mkv"
"""
import argparse
import sys
import time

from .analyze import analyze
from .server import runs_dir


def main():
    ap = argparse.ArgumentParser(description="Analiza un partido grabado y arma los candidatos por media entrada")
    ap.add_argument("video")
    ap.add_argument("--perfil", default="auto")
    ap.add_argument("--sin-previas", action="store_true", help="no cortar vistas previas")
    a = ap.parse_args()
    t = time.time()
    last = [0]

    def prog(stage, frac, msg):
        if frac - last[0] >= 0.02 or stage == "listo":
            last[0] = frac
            print(f"\r{int(frac * 100):3d}%  {msg[:60]:60s}", end="", flush=True)

    m = analyze(a.video, runs_dir(), a.perfil, on_progress=prog, render_previews=not a.sin_previas)
    print()
    for h in m["halves"]:
        print(f"{h['label']:10s} {len(h['clips'])} jugadas")
    print(f"Total {len(m['clips'])} jugadas · final {m['stats']['finalScore']} · {time.time() - t:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
