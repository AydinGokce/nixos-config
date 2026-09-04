# bio-evolvepro — EVOLVEpro-style few-shot directed evolution.
#
#   bio-evolvepro embed  -i variants.fasta -o emb.csv     (GPU/PLM env)
#   bio-evolvepro evolve -e emb.csv -l labels.csv -n 12 -o next.csv  (CPU env)
#   bio-evolvepro repo -- <cmd...>   run a command inside the PLM venv from the
#                                    cloned upstream repo (for its exact scripts)
#
# labels.csv columns: variant,activity  (only the variants measured so far).
# With no --labels, `evolve` proposes a diverse first round.

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

use_env() {
  local venv="$BIO_DATA_DIR/envs/$1"
  if [ ! -x "$venv/bin/python" ]; then
    echo "bio-evolvepro: env '$1' not set up. Run:  bio-setup evolvepro" >&2
    exit 3
  fi
  # shellcheck source=/dev/null
  source "$venv/bin/activate"
}

sub="${1:-}"; [ $# -gt 0 ] && shift
case "$sub" in
  embed)
    use_env evolvepro-plm
    exec python /etc/bio/py/evolvepro_cli.py embed "$@" ;;
  evolve)
    use_env evolvepro-core
    exec python /etc/bio/py/evolvepro_cli.py evolve "$@" ;;
  repo)
    use_env evolvepro-plm
    [ "${1:-}" = "--" ] && shift
    cd "$BIO_DATA_DIR/src/evolvepro" || { echo "bio-evolvepro: repo not cloned" >&2; exit 3; }
    exec "$@" ;;
  -h|--help|"")
    cat <<'USAGE'
bio-evolvepro — EVOLVEpro-style few-shot directed evolution.
  bio-evolvepro embed  -i variants.fasta -o emb.csv          (GPU/PLM env)
  bio-evolvepro evolve -e emb.csv -l labels.csv -n 12 -o next.csv  (CPU env)
  bio-evolvepro repo -- <cmd...>   run inside the cloned upstream repo (PLM env)
labels.csv columns: variant,activity  (only variants measured so far).
With no --labels, `evolve` proposes a diverse first round.
USAGE
    ;;
  *)
    echo "bio-evolvepro: unknown subcommand '$sub' (embed|evolve|repo)" >&2; exit 2 ;;
esac
