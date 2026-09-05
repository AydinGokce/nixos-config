# RoseTTAFold-All-Atom — single-sequence, template-free (no MSA/template DBs).
# py3.10 + torch 2.0.1+cu118 + dgl 1.1.2 + pyg. IN = query FASTA. Reduced accuracy
# vs a real MSA, but needs zero databases. NAME optional.
PY=$(nfs_py310)
VENV="$SHARED/envs/rfaa"; SRC="$SHARED/src/rfaa"; NAME="${NAME:-rfaa}"
"$VENV/bin/python" -c '' >/dev/null 2>&1 || { rm -rf "$VENV"; uv venv --python "$PY" "$VENV"; }
P="$VENV/bin/python"
[ -d "$SRC/.git" ] || git clone https://github.com/baker-laboratory/RoseTTAFold-All-Atom "$SRC"
git -C "$SRC" checkout -q d69ab3a73f8ede31a4cc005fbc076a341d848469 || true
git -C "$SRC" submodule update --init --recursive -q || true
uv pip install --python "$P" -r "$SHARED/tools/requirements/rfaa.txt"
"$P" -c 'import dgl' >/dev/null 2>&1 || uv pip install --python "$P" dgl==1.1.2 -f https://data.dgl.ai/wheels/cu118/repo.html
uv pip install --python "$P" torch-scatter torch-sparse torch-cluster -f https://data.pyg.org/whl/torch-2.0.1+cu118.html || true
uv pip install --python "$P" torch-geometric==2.5.0 || true
uv pip install --python "$P" "git+https://github.com/NVIDIA/dllogger.git@0540a43971f4a8a16693a9de9de73c1072020769" || true
[ -d "$SRC/rf2aa/SE3Transformer" ] && uv pip install --python "$P" --no-deps "$SRC/rf2aa/SE3Transformer" || true
# template-free enablement: empty pdb100 FFindex stub + patch load_protein
mkdir -p "$SRC/pdb100_2021Mar03"
: > "$SRC/pdb100_2021Mar03/pdb100_2021Mar03_pdb.ffindex"; printf '\0' > "$SRC/pdb100_2021Mar03/pdb100_2021Mar03_pdb.ffdata"
"$P" "$SHARED/tools/py/rfaa_singleseq_patch.py" "$SRC/rf2aa/data/protein.py" || true
[ -s "$SRC/RFAA_paper_weights.pt" ] || dl "http://files.ipd.uw.edu/pub/RF-All-Atom/weights/RFAA_paper_weights.pt" "$SRC/RFAA_paper_weights.pt"
# query-only a3m so make_msa skips its DB search
CH="$OUT/$NAME/A"; mkdir -p "$CH"
{ echo '>query'; grep -v '^>' "$IN" | tr -d '\n\r \t'; echo; } > "$CH/t000_.msa0.a3m"; : > "$CH/t000_.atab"; : > "$CH/t000_.hhr"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
export PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512 WANDB_MODE=disabled
have_gpu
cd "$SRC"
# shellcheck disable=SC2086
"$P" -m rf2aa.run_inference --config-name protein job_name="$NAME" output_path="$OUT" \
  checkpoint_path="$SRC/RFAA_paper_weights.pt" protein_inputs.A.fasta_file="$IN" $EXTRA
