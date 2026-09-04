# bio-viz — visualize structures/trajectories in PyMOL.
#
#   bio-viz a.pdb [b.cif ...]              open interactively in PyMOL
#   bio-viz --render a.pdb [-o img.png]    headless ray-traced PNG
#   bio-viz --overlay a.pdb b.pdb [...]    load all + align onto the first
#
# Any structure format PyMOL understands works (.pdb .cif .mmcif .sdf .mol2 ...).

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh

usage() { cat <<'USAGE'
bio-viz — visualize structures/trajectories in PyMOL.
  bio-viz a.pdb [b.cif ...]            open interactively in PyMOL
  bio-viz --render a.pdb [-o img.png]  headless ray-traced PNG
  bio-viz --overlay a.pdb b.pdb [...]  load all + align onto the first
USAGE
}

mode=open
out=""
ray=1
files=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)     usage; exit 0 ;;
    --render)      mode=render ;;
    --overlay)     mode=overlay ;;
    -o|--out)      out="$2"; shift ;;
    --no-ray)      ray=0 ;;
    --)            shift; while [ $# -gt 0 ]; do files+=("$1"); shift; done; break ;;
    -*)            echo "bio-viz: unknown option $1" >&2; usage; exit 2 ;;
    *)             files+=("$1") ;;
  esac
  shift
done

if [ "${#files[@]}" -eq 0 ]; then
  echo "bio-viz: no input structures given" >&2; usage; exit 2
fi
for f in "${files[@]}"; do
  [ -r "$f" ] || { echo "bio-viz: cannot read '$f'" >&2; exit 1; }
done

case "$mode" in
  open)
    exec pymol "${files[@]}"
    ;;
  overlay)
    exec pymol /etc/bio/py/viz.py -- overlay "${files[@]}"
    ;;
  render)
    [ -n "$out" ] || out="${files[0]%.*}.png"
    args=(render --out "$out")
    [ "$ray" -eq 1 ] && args+=(--ray)
    pymol -cq /etc/bio/py/viz.py -- "${args[@]}" "${files[@]}"
    if [ -f "$out" ]; then echo "bio-viz: wrote $out"; else echo "bio-viz: render failed (no $out)" >&2; exit 1; fi
    ;;
esac
