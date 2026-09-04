"""PyMOL helper for bio-viz. Run as:  pymol [-cq] viz.py -- <mode> [opts] files...

Modes:
  render  [--out IMG] [--ray] file...   load, style, save a PNG (headless-friendly)
  overlay file...                       load all + cealign onto the first (GUI)

Invoked only via PyMOL, which does not set __name__ == "__main__", so main() is
called unconditionally at import.
"""
import sys

from pymol import cmd


def get_args():
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return argv[1:]


def style():
    cmd.hide("everything")
    cmd.show("cartoon")
    cmd.show("sticks", "hetatm and not solvent")
    cmd.util.cbc()          # color by chain
    cmd.bg_color("white")
    cmd.orient()


def main():
    args = get_args()
    if not args:
        print("viz: no arguments", file=sys.stderr)
        sys.exit(2)
    mode, rest = args[0], args[1:]

    out, ray, files = None, False, []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--out":
            out = rest[i + 1]
            i += 2
        elif a == "--ray":
            ray = True
            i += 1
        else:
            files.append(a)
            i += 1

    if not files:
        print("viz: no input structures", file=sys.stderr)
        sys.exit(2)
    for f in files:
        cmd.load(f)
    style()

    objs = cmd.get_object_list()
    if mode == "overlay" and len(objs) > 1:
        for o in objs[1:]:
            try:
                r = cmd.cealign(objs[0], o)
                print(f"viz: cealign {o} -> {objs[0]}: RMSD {r.get('RMSD', '?')}")
            except Exception as e:  # noqa: BLE001
                print(f"viz: could not align {o}: {e}", file=sys.stderr)
        cmd.orient()

    if mode == "render":
        if not out:
            out = files[0].rsplit(".", 1)[0] + ".png"
        cmd.set("ray_trace_mode", 1)
        cmd.png(out, width=1600, height=1200, dpi=150, ray=1 if ray else 0)
        print("viz: wrote " + out)


main()
