# bio-esm — ESM-2 embeddings / logits / scoring. Thin wrapper around esm_cli.py.
#   bio-esm list-models
#   bio-esm embed  -i seqs.fasta -o emb.npz [--model esm2_t33_650M_UR50D]
#   bio-esm score  -i seqs.fasta [-o scores.csv]
#   bio-esm mutate -s MKTAY... -m A24G,D50E [-o eff.csv]
# See: bio-esm <subcommand> --help

# shellcheck source=/dev/null
[ -r /etc/bio/config.sh ] && source /etc/bio/config.sh
: "${BIO_DATA_DIR:=/opt/bio}"
export LD_LIBRARY_PATH="${BIO_DRIVER_LIB:-/run/opengl-driver/lib}${BIO_WHEEL_LIB:+:$BIO_WHEEL_LIB}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

venv="$BIO_DATA_DIR/envs/esm2"
if [ ! -x "$venv/bin/python" ]; then
  echo "bio-esm: ESM-2 environment not set up. Run:  bio-setup esm2" >&2
  exit 3
fi
# shellcheck source=/dev/null
source "$venv/bin/activate"
exec python /etc/bio/py/esm_cli.py "$@"
