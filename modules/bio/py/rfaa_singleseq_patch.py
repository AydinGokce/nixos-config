#!/usr/bin/env python3
"""Idempotently patch RoseTTAFold-All-Atom's load_protein so that missing/empty
template files are treated as "no templates" (blank_template) instead of crashing
in parse_templates_raw.

This is what enables single-sequence, template-free inference on a machine that
does NOT have the ~81GB pdb100 template DB (or the ~399GB MSA DBs). With a
query-only a3m and empty t000_.hhr / t000_.atab, RFAA then runs with a blank
template. Usage: rfaa_singleseq_patch.py <path to rf2aa/data/protein.py>
"""
import sys

path = sys.argv[1]
src = open(path).read()

if "\nimport os\n" not in src and not src.startswith("import os"):
    src = "import os\n" + src

old = "    if hhr_fn is None or atab_fn is None:"
new = ("    if (hhr_fn is None or atab_fn is None or not os.path.exists(hhr_fn)\n"
       "            or not os.path.exists(atab_fn) or os.path.getsize(hhr_fn) == 0\n"
       "            or os.path.getsize(atab_fn) == 0):")

if old in src:
    open(path, "w").write(src.replace(old, new))
    print("rfaa: patched load_protein for template-free inference")
elif "os.path.getsize(hhr_fn)" in src:
    print("rfaa: load_protein already patched")
else:
    print("rfaa: patch pattern not found (upstream changed?)", file=sys.stderr)
    sys.exit(1)
