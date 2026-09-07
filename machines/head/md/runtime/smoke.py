#!/usr/bin/env python3
"""Exercise the installed engine interfaces, not molecular prediction accuracy."""
import argparse
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile


MDP = """integrator = md
dt = 0.001
nsteps = 30
nstlist = 10
cutoff-scheme = Verlet
rlist = 1.0
coulombtype = Cut-off
rcoulomb = 1.0
vdwtype = Cut-off
rvdw = 1.0
pbc = xyz
constraints = none
tcoupl = no
pcoupl = no
gen-vel = yes
gen-temp = 300
gen-seed = 314159
nstenergy = 1
nstcalcenergy = 1
nstlog = 5
nstxout-compressed = 5
"""
TOP = """[ defaults ]
1 2 yes 0.5 0.8333
[ atomtypes ]
AR 18 39.948 0.0 A 0.340 0.997
[ moleculetype ]
ARGON 1
[ atoms ]
1 AR 1 AR AR 1 0.0 39.948
[ system ]
Four-argon engine interface smoke fixture
[ molecules ]
ARGON 4
"""
COLVARS = """units gromacs
colvarsTrajFrequency 1
colvarsRestartFrequency 10
colvar {
  name d
  width 0.01
  outputAppliedForce yes
  distance {
    group1 { atomNumbers 1 }
    group2 { atomNumbers 2 }
  }
}
harmonic {
  name anchor
  colvars d
  centers 0.60
  forceConstant 50
  outputEnergy yes
}
"""
PLUMED = """d: DISTANCE ATOMS=1,2
meta: METAD ARG=d SIGMA=0.02 HEIGHT=0.20 PACE=5 BIASFACTOR=10 TEMP=300 FILE=HILLS
PRINT ARG=d,meta.bias FILE=COLVAR STRIDE=1
"""


def numeric_rows(path):
    return [[float(value) for value in line.split()] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "@"))]


def run(argv, root, env, log):
    with (root / log).open("w") as output:
        result = subprocess.run(list(map(str, argv)), cwd=root, env=env,
                                stdout=output, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode:
        raise RuntimeError("Engine smoke failed: " + str(root / log) + "\n" + (root / log).read_text()[-8000:])
    return (root / log).read_text()


def engine_case(prefix, root, kind, env, gpu):
    root.mkdir()
    (root / "topol.top").write_text(TOP)
    xyz = [(0.8, 1.0, 1.0), (1.3, 1.0, 1.0), (2.1, 2.0, 2.0), (2.8, 2.0, 2.0)]
    gro = "Argon interface smoke; no affinity claim\n    4\n"
    for index, (x, y, z) in enumerate(xyz, 1):
        gro += f"{index:5d}{'AR':<5}{'AR':>5}{index:5d}{x:8.3f}{y:8.3f}{z:8.3f}\n"
    (root / "conf.gro").write_text(gro + "   4.00000   4.00000   4.00000\n")
    mdp = MDP
    extra = []
    if kind == "plumed":
        (root / "plumed.dat").write_text(PLUMED)
        extra = ["-plumed", "plumed.dat"]
    elif kind == "colvars":
        (root / "colvars.dat").write_text(COLVARS)
        mdp += "colvars-active = yes\ncolvars-configfile = colvars.dat\n"
    elif kind == "alchemical":
        mdp += ("free-energy = yes\ninit-lambda-state = 0\nfep-lambdas = 0 0.5 1\n"
                "calc-lambda-neighbors = -1\nnstdhdl = 1\ndhdl-print-energy = potential\n"
                "dhdl-derivatives = yes\nseparate-dhdl-file = yes\n")
        extra = ["-dhdl", "dhdl.xvg"]
    (root / "smoke.mdp").write_text(mdp)
    gmx = prefix / "bin/gmx"
    run([gmx, "grompp", "-f", "smoke.mdp", "-c", "conf.gro", "-p", "topol.top", "-o", "smoke.tpr"], root, env, "grompp.log")
    run([gmx, "mdrun", "-s", "smoke.tpr", "-deffnm", "md", "-ntmpi", "1", "-ntomp", "2",
         "-nb", "gpu" if gpu else "cpu", "-pme", "cpu", "-bonded", "cpu", "-update", "cpu", *extra], root, env, "mdrun.log")
    if kind == "plumed":
        values = numeric_rows(root / "COLVAR")
        hills = numeric_rows(root / "HILLS")
        assert len(values) >= 20 and len(hills) >= 5 and max(row[2] for row in values) > 0
        assert all(math.isfinite(value) for row in values for value in row)
        return {"coupled_to_mdrun": True, "samples": len(values), "hill_depositions": len(hills), "max_bias_kj_mol": max(row[2] for row in values)}
    if kind == "colvars":
        trajectories = list(root.glob("*.colvars.traj"))
        assert len(trajectories) == 1
        values = numeric_rows(trajectories[0])
        assert len(values) >= 20 and all(math.isfinite(value) for row in values for value in row)
        assert any(abs(row[-1]) > 1e-9 for row in values)
        return {"coupled_to_mdrun": True, "samples": len(values), "restraint_energy_or_force_nonzero": True}
    from alchemlyb.parsing.gmx import extract_u_nk
    values = extract_u_nk(root / "dhdl.xvg", T=300)
    assert values.shape[0] >= 20 and values.shape[1] == 3
    assert all(math.isfinite(float(value)) for value in values.to_numpy().flat)
    return {"all_lambda_states_exported": True, "rows": values.shape[0], "states": values.shape[1],
            "fixture_scope": "Identical argon end states exercise file/energy schemas, not a mutation free energy."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu", action="store_true", help="Require real GPU nonbonded offload")
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.output))
    env = dict(os.environ)
    env.update(PATH=str(prefix / "bin") + os.pathsep + env.get("PATH", ""),
               PYTHONNOUSERSITE="1", MPLBACKEND="Agg", QT_QPA_PLATFORM="offscreen")
    env.pop("PYTHONPATH", None)
    import numpy as np
    import pmx
    from pmx.alchemy import mutate, gen_hybrid_top
    from pmx.forcefield import Topology
    from pmx.model import Model
    from BFEE2.version import __VERSION__ as bfee_version
    from BFEE2.inputGenerator import inputGenerator
    from BFEE2.postTreatment import postTreatment
    from rdkit import Chem
    from pymbar import MBAR
    from alchemlyb.estimators import MBAR as AlchemlybMBAR
    assert bfee_version == "3.2.1"
    assert pmx.__version__ == "0+g0dd5f0a9cdf2"
    assert importlib.metadata.version("alchemlyb") == "2.5.0"
    assert callable(postTreatment) and callable(AlchemlybMBAR)
    signature = inspect.signature(inputGenerator.generateGromacsGeometricFiles)
    assert all(name in signature.parameters for name in ("topFile", "ligandOnlyTopFile", "selectionPro", "selectionLig"))
    assert Chem.MolFromSmiles("C[C@H](O)C(=O)O") is not None
    ff = Path(pmx.__file__).parent / "data/mutff"
    env["GMXLIB"] = os.environ["GMXLIB"] = str(ff)
    kernels = list((prefix / "lib").glob("lib*lum*Kernel.so"))
    assert len(kernels) == 1
    env["PLUMED_KERNEL"] = str(kernels[0])
    if Path("/run/opengl-driver/lib").is_dir():
        env["LD_LIBRARY_PATH"] = "/run/opengl-driver/lib:" + env.get("LD_LIBRARY_PATH", "")
    fixtures = prefix / "share/bio-md-runtime/fixtures"
    original = Model(str(fixtures / "protein.pdb"), rename_atoms=True)
    mutated = mutate(m=original, mut_resid=6, mut_resname="F", ff="amber99sb-star-ildn-mut")
    mutated.write(str(work / "mutation.pdb"))
    assert mutated.residues[5].resname != original.residues[5].resname
    topology, _ = gen_hybrid_top(Topology(str(fixtures / "topol.top")))
    topology.write(str(work / "hybrid.top"))
    assert any(atom.typeB is not None for atom in topology.atoms)
    rng = np.random.default_rng(314159)
    samples = np.concatenate([rng.normal(0., 1., 2000), rng.normal(0.5, 1., 2000)])
    u_kn = np.array([0.5 * samples ** 2, 0.5 * (samples - 0.5) ** 2])
    fit = MBAR(u_kn, np.array([2000, 2000]), verbose=False)
    free = fit.compute_free_energy_differences()
    delta, sigma = float(free["Delta_f"][0, 1]), float(free["dDelta_f"][0, 1])
    assert math.isfinite(delta) and math.isfinite(sigma) and sigma > 0 and abs(delta) < 0.1
    version = run([prefix / "bin/gmx", "--version"], work, env, "gromacs-version.txt")
    assert "2026.3" in version
    plumed_version = run([prefix / "bin/plumed", "info", "--long-version"], work, env, "plumed-version.txt")
    assert "2.10.1" in plumed_version
    cases = {kind: engine_case(prefix, work / kind, kind, env, args.gpu)
             for kind in ("plumed", "colvars", "alchemical")}
    report = {"schema": "bio-md-runtime-smoke.v1", "passed": True, "gpu_required": args.gpu,
        "evidence_directory": str(work), "gromacs_version": version, "plumed_version": plumed_version.strip(),
        "packages": {name: importlib.metadata.version(name) for name in
                     ("numpy", "scipy", "pmx", "bfee2", "alchemlyb", "pymbar", "MDAnalysis", "ParmEd", "rdkit")},
        "pmx_mutation_and_hybrid_topology": True, "bfee3_backend_api": str(signature),
        "mbar_analytic_zero_kT": {"delta_f": delta, "standard_error": sigma}, "engines": cases,
        "scope": "Toolchain functionality only; no biomolecular accuracy or convergence claim."}
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "gpu_required": args.gpu, "report": str(args.output / "report.json")}))


if __name__ == "__main__":
    main()
