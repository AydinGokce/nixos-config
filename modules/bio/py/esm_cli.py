#!/usr/bin/env python3
"""bio-esm — a small command-line front-end for ESM-2 (via HuggingFace transformers).

Subcommands:
  list-models                        show ESM-2 checkpoints + sizes/VRAM hints
  embed   [-i FASTA|-s SEQ] -o OUT   per-sequence or per-residue embeddings -> .npz
  logits  [-i FASTA|-s SEQ] -o OUT   per-position amino-acid logits -> .npz
  score   [-i FASTA|-s SEQ] [-o CSV] sum/mean of unmasked per-residue model log probabilities
  mutate  -s WTSEQ -m A24G,... [-o CSV]
                                     masked-marginal effect score per mutation

Model defaults to $BIO_ESM_DEFAULT_MODEL (else esm2_t33_650M_UR50D). Short names
like "esm2_t33_650M_UR50D" are resolved to "facebook/<name>".
"""
import argparse
import csv
import os
import sys

MODELS = {
    "esm2_t6_8M_UR50D": (8, 320, "~0.5GB VRAM"),
    "esm2_t12_35M_UR50D": (35, 480, "~0.7GB VRAM"),
    "esm2_t30_150M_UR50D": (150, 640, "~1.2GB VRAM"),
    "esm2_t33_650M_UR50D": (650, 1280, "~3-4GB VRAM (fp16) — default"),
    "esm2_t36_3B_UR50D": (3000, 2560, "~9-12GB — borderline/OOM on 8GB"),
    "esm2_t48_15B_UR50D": (15000, 5120, "~30GB+ — does NOT fit on 8GB"),
}


def resolve(name):
    if "/" in name:
        return name
    if name.startswith("esm"):
        return "facebook/" + name
    return name


def default_model():
    return os.environ.get("BIO_ESM_DEFAULT_MODEL", "esm2_t33_650M_UR50D")


def read_seqs(args):
    if getattr(args, "seq", None):
        return [("seq", args.seq.strip().upper())]
    path = args.input
    if not path or not os.path.exists(path):
        sys.exit(f"bio-esm: input FASTA not found: {path}")
    out, name, buf = [], None, []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip()
            if line.startswith(">"):
                if name is not None:
                    out.append((name, "".join(buf).upper()))
                name, buf = line[1:].split()[0] if line[1:].strip() else f"seq{len(out)}", []
            elif line:
                buf.append(line)
    if name is not None:
        out.append((name, "".join(buf).upper()))
    if not out:
        sys.exit("bio-esm: no sequences parsed from FASTA")
    return out


def pick_device(pref):
    import torch
    if pref == "cpu":
        return "cpu"
    if pref == "cuda" or (pref == "auto" and torch.cuda.is_available()):
        if not torch.cuda.is_available():
            sys.exit("bio-esm: --device cuda requested but CUDA is unavailable")
        return "cuda"
    return "cpu"


def load(model_name, device, fp16, masked_lm=True):
    import torch
    from transformers import AutoTokenizer, AutoModelForMaskedLM, AutoModel
    repo = resolve(model_name)
    short = repo.split("/")[-1]
    if short in MODELS and MODELS[short][0] >= 15000 and device == "cuda":
        print(f"bio-esm: warning: {short} needs ~30GB VRAM; likely to OOM.", file=sys.stderr)
    tok = AutoTokenizer.from_pretrained(repo)
    dtype = torch.float16 if (fp16 and device == "cuda") else torch.float32
    cls = AutoModelForMaskedLM if masked_lm else AutoModel
    model = cls.from_pretrained(repo, torch_dtype=dtype).to(device).eval()
    return tok, model


def cmd_list(_args):
    print(f"{'model':28} {'params(M)':>10} {'dim':>6}  note")
    for name, (p, dim, note) in MODELS.items():
        star = " *" if name == default_model() else ""
        print(f"{name:28} {p:>10} {dim:>6}  {note}{star}")


def cmd_embed(args):
    import numpy as np
    import torch
    device = pick_device(args.device)
    tok, model = load(args.model, device, args.fp16)
    layer = args.layer
    seqs = read_seqs(args)
    result = {}
    for name, seq in seqs:
        enc = tok([seq], return_tensors="pt").to(device)
        with torch.no_grad():
            out = model(**enc, output_hidden_states=True)
        hidden = out.hidden_states[layer][0]  # [L(+special), H]
        # strip CLS (pos 0) and EOS (last) special tokens
        res = hidden[1:1 + len(seq)].float().cpu().numpy()
        if args.pooling == "mean":
            result[name] = res.mean(axis=0)
        elif args.pooling == "per_tok":
            result[name] = res
        elif args.pooling == "cls":
            result[name] = hidden[0].float().cpu().numpy()
        print(f"bio-esm: embedded {name} (len {len(seq)}) -> {result[name].shape}", file=sys.stderr)
    np.savez_compressed(args.out, **result)
    print(f"bio-esm: wrote {args.out} ({len(result)} sequences)")


def cmd_logits(args):
    import numpy as np
    import torch
    device = pick_device(args.device)
    tok, model = load(args.model, device, args.fp16)
    seqs = read_seqs(args)
    result = {}
    for name, seq in seqs:
        enc = tok([seq], return_tensors="pt").to(device)
        with torch.no_grad():
            logits = model(**enc).logits[0]  # [L(+special), V]
        result[name] = logits[1:1 + len(seq)].float().cpu().numpy()
        print(f"bio-esm: logits {name} -> {result[name].shape}", file=sys.stderr)
    np.savez_compressed(args.out, vocab="".join(tok.convert_ids_to_tokens(range(tok.vocab_size))),
                        **result)
    print(f"bio-esm: wrote {args.out}")


def cmd_score(args):
    import torch
    import torch.nn.functional as F
    device = pick_device(args.device)
    tok, model = load(args.model, device, args.fp16)
    seqs = read_seqs(args)
    rows = []
    for name, seq in seqs:
        enc = tok([seq], return_tensors="pt").to(device)
        ids = enc["input_ids"][0]
        with torch.no_grad():
            logp = F.log_softmax(model(**enc).logits[0].float(), dim=-1)
        total = 0.0
        for i in range(1, 1 + len(seq)):
            total += logp[i, ids[i]].item()
        rows.append((name, len(seq), total, total / max(len(seq), 1)))
        print(f"bio-esm: {name} loglik={total:.2f} mean={total/max(len(seq),1):.3f}", file=sys.stderr)
    _emit_csv(args.out, ["id", "length", "loglik", "mean_loglik"], rows)


def cmd_mutate(args):
    import torch
    import torch.nn.functional as F
    device = pick_device(args.device)
    tok, model = load(args.model, device, args.fp16)
    wt = args.seq.strip().upper()
    muts = [m.strip() for m in args.mutations.split(",") if m.strip()]
    mask_id = tok.mask_token_id
    # cache masked-position log-probs so multi-mutation variants reuse forwards
    cache = {}

    def logp_at(pos0):
        if pos0 in cache:
            return cache[pos0]
        enc = tok([wt], return_tensors="pt").to(device)
        enc["input_ids"][0, pos0 + 1] = mask_id
        with torch.no_grad():
            lp = F.log_softmax(model(**enc).logits[0, pos0 + 1].float(), dim=-1)
        cache[pos0] = lp
        return lp

    rows = []
    for mut in muts:
        # supports single "A24G" or multi "A24G:D50E" (colon-separated)
        parts = mut.replace(",", ":").split(":") if ":" in mut else [mut]
        score = 0.0
        ok = True
        for p in parts:
            wt_aa, mt_aa, pos = p[0], p[-1], int(p[1:-1]) - args.offset
            if pos < 0 or pos >= len(wt) or wt[pos] != wt_aa:
                print(f"bio-esm: warning: {p} does not match WT at position", file=sys.stderr)
                ok = False
                break
            lp = logp_at(pos)
            score += (lp[tok.convert_tokens_to_ids(mt_aa)] - lp[tok.convert_tokens_to_ids(wt_aa)]).item()
        rows.append((mut, f"{score:.4f}" if ok else "NA"))
    _emit_csv(args.out, ["mutation", "masked_marginal_score"], rows)


def _emit_csv(out, header, rows):
    fh = open(out, "w", newline="") if out else sys.stdout
    w = csv.writer(fh)
    w.writerow(header)
    w.writerows(rows)
    if out:
        fh.close()
        print(f"bio-esm: wrote {out}")


def main():
    p = argparse.ArgumentParser(prog="bio-esm", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, need_out=True):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("-i", "--input", help="input FASTA")
        g.add_argument("-s", "--seq", help="a single raw sequence")
        sp.add_argument("--model", default=default_model())
        sp.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
        sp.add_argument("--fp16", action="store_true", default=True)
        sp.add_argument("--fp32", dest="fp16", action="store_false")
        if need_out:
            sp.add_argument("-o", "--out", required=True)

    sp = sub.add_parser("list-models"); sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("embed"); add_common(sp)
    sp.add_argument("--layer", type=int, default=-1)
    sp.add_argument("--pooling", default="mean", choices=["mean", "per_tok", "cls"])
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("logits"); add_common(sp); sp.set_defaults(func=cmd_logits)

    sp = sub.add_parser("score"); add_common(sp, need_out=False)
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("mutate"); add_common(sp, need_out=False)
    sp.add_argument("-m", "--mutations", required=True,
                    help="comma-separated, e.g. A24G,D50E ; use ':' to combine into one variant")
    sp.add_argument("-o", "--out")
    sp.add_argument("--offset", type=int, default=1, help="1-based residue numbering by default")
    sp.set_defaults(func=cmd_mutate)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
