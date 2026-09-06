#!/usr/bin/env python3
"""bio-evolvepro — EVOLVEpro-style few-shot directed evolution.

Two stages, matching the EVOLVEpro method (Jiang/Abugu/Zhang): (1) embed variant
sequences with a protein language model, then (2) fit a light top-layer regressor
on the handful of measured variants and rank the next round to test.

  embed  -i variants.fasta -o emb.csv [--model esm2_t33_650M_UR50D]
         mean ESM-2 embedding per variant -> CSV (run in the GPU/PLM venv).

  evolve -e emb.csv -l labels.csv -n 12 -o next_round.csv [--model rf|xgb]
         train on measured variants (labels.csv: columns variant,activity),
         predict the rest, output the top-N unmeasured variants to test next
         (run in the core/CPU venv).

The upstream repo (mat10d/EvolvePro) is cloned under $BIO_DATA_DIR/src/evolvepro
for reference and its exact paper models; this CLI is a self-contained,
dependency-light implementation of the same active-learning loop.
"""
import argparse
import os
import sys


def default_model():
    return os.environ.get("BIO_ESM_DEFAULT_MODEL", "esm2_t33_650M_UR50D")


def read_fasta(path):
    if not os.path.exists(path):
        sys.exit(f"bio-evolvepro: FASTA not found: {path}")
    out, name, buf = [], None, []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip()
            if line.startswith(">"):
                if name is not None:
                    out.append((name, "".join(buf).upper()))
                name, buf = (line[1:].split()[0] if line[1:].strip() else f"var{len(out)}"), []
            elif line:
                buf.append(line)
    if name is not None:
        out.append((name, "".join(buf).upper()))
    if not out:
        sys.exit("bio-evolvepro: no sequences in FASTA")
    return out


def cmd_embed(args):
    import numpy as np
    import pandas as pd
    import torch
    from transformers import AutoTokenizer, AutoModelForMaskedLM
    repo = args.model if "/" in args.model else "facebook/" + args.model
    if args.device == "cuda" and not torch.cuda.is_available():
        sys.exit("bio-evolvepro: CUDA requested but unavailable")
    device = "cuda" if (torch.cuda.is_available() and args.device != "cpu") else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    tok = AutoTokenizer.from_pretrained(repo)
    model = AutoModelForMaskedLM.from_pretrained(repo, torch_dtype=dtype).to(device).eval()
    seqs = read_fasta(args.input)
    names, vecs = [], []
    for name, seq in seqs:
        enc = tok([seq], return_tensors="pt").to(device)
        with torch.no_grad():
            hs = model(**enc, output_hidden_states=True).hidden_states[-1][0]
        vecs.append(hs[1:1 + len(seq)].float().mean(0).cpu().numpy())
        names.append(name)
        print(f"bio-evolvepro: embedded {name} ({len(seq)} aa)", file=sys.stderr)
    df = pd.DataFrame(np.vstack(vecs), index=names)
    df.index.name = "variant"
    df.columns = [f"emb_{i}" for i in range(df.shape[1])]
    df.to_csv(args.out)
    print(f"bio-evolvepro: wrote {args.out} ({df.shape[0]} variants x {df.shape[1]} dims)")


def cmd_evolve(args):
    import numpy as np
    import pandas as pd
    # Numeric-looking identifiers and names like NA are still literal strings.
    emb = pd.read_csv(args.embeddings, dtype={"variant": str}, keep_default_na=False).set_index("variant")
    X_all = emb.values
    idx = list(emb.index)

    measured = {}
    if args.labels:
        lab = pd.read_csv(args.labels, dtype=str, keep_default_na=False)
        cols = {c.lower(): c for c in lab.columns}
        vcol = cols.get("variant") or lab.columns[0]
        acol = cols.get("activity") or cols.get("fitness") or lab.columns[1]
        for _, r in lab.iterrows():
            measured[str(r[vcol])] = float(r[acol])

    train_i = [i for i, v in enumerate(idx) if v in measured]
    test_i = [i for i, v in enumerate(idx) if v not in measured]

    if not train_i:
        # First round: no measurements yet — propose a diverse spread by k-means.
        from sklearn.cluster import KMeans
        # Identical embeddings cannot form separate clusters. Avoid empty
        # clusters and select one representative for each distinct point.
        k = min(args.n, len(np.unique(X_all, axis=0)))
        km = KMeans(n_clusters=k, n_init=10, random_state=args.seed).fit(X_all)
        chosen = []
        for c in range(k):
            members = np.where(km.labels_ == c)[0]
            center = km.cluster_centers_[c]
            best = members[np.argmin(((X_all[members] - center) ** 2).sum(1))]
            chosen.append(best)
        out = pd.DataFrame({"variant": [idx[i] for i in chosen],
                            "strategy": "first_round_kmeans_diverse"})
        out.to_csv(args.out, index=False)
        if args.full:
            out.to_csv(args.full, index=False)
        print(f"bio-evolvepro: no labels given -> proposed {len(chosen)} diverse variants "
              f"(first round) -> {args.out}")
        return

    if args.model == "xgb":
        from xgboost import XGBRegressor
        reg = XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.05,
                           subsample=0.8, random_state=args.seed)
    else:
        from sklearn.ensemble import RandomForestRegressor
        reg = RandomForestRegressor(n_estimators=400, random_state=args.seed, n_jobs=-1)

    y_train = np.array([measured[idx[i]] for i in train_i])
    reg.fit(X_all[train_i], y_train)
    preds = reg.predict(X_all[test_i]) if test_i else np.array([])

    ranked = sorted(zip([idx[i] for i in test_i], preds), key=lambda t: t[1], reverse=True)
    top = ranked[:args.n]
    out = pd.DataFrame(top, columns=["variant", "predicted_activity"])
    out.to_csv(args.out, index=False)
    print(f"bio-evolvepro: trained {args.model} on {len(train_i)} measured variants, "
          f"ranked {len(test_i)} candidates, wrote top {len(top)} -> {args.out}")
    if args.full:
        full = pd.DataFrame(ranked, columns=["variant", "predicted_activity"])
        full.to_csv(args.full, index=False)
        print(f"bio-evolvepro: full ranking -> {args.full}")


def main():
    p = argparse.ArgumentParser(prog="bio-evolvepro", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("embed")
    e.add_argument("-i", "--input", required=True, help="variants FASTA")
    e.add_argument("-o", "--out", required=True, help="output embeddings CSV")
    e.add_argument("--model", default=default_model())
    e.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    e.set_defaults(func=cmd_embed)

    v = sub.add_parser("evolve")
    v.add_argument("-e", "--embeddings", required=True)
    v.add_argument("-l", "--labels", help="CSV: variant,activity for measured variants")
    v.add_argument("-n", "--n", type=int, default=12, help="how many to propose")
    v.add_argument("-o", "--out", required=True)
    v.add_argument("--full", help="also write the full ranking here")
    v.add_argument("--model", default="rf", choices=["rf", "xgb"])
    v.add_argument("--seed", type=int, default=0)
    v.set_defaults(func=cmd_evolve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
