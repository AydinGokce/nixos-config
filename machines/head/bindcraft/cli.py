"""Prepare portable BindCraft jobs and launch them through bio-submit."""
import argparse
import json
import os
from pathlib import Path
import sys

from bundle import create, defaults, encoded, inspect, read_json
from runtime import installation, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Bundle a target PDB and native settings; no GPU rental")
    prepare.add_argument("--pdb", type=Path, required=True)
    prepare.add_argument("--chains", required=True)
    prepare.add_argument("--name", default="binder")
    prepare.add_argument("--hotspots")
    prepare.add_argument("--lengths", type=int, nargs=2, default=[65, 150], metavar=("MIN", "MAX"))
    prepare.add_argument("--designs", type=int, default=100)
    prepare.add_argument('--seed', type=int, help='Campaign RNG seed; native trajectory seeds are retained in output CSVs')
    prepare.add_argument("--advanced", type=Path, help="Complete native advanced JSON; defaults to pinned upstream 4-stage settings")
    prepare.add_argument("--filters", type=Path, help="Complete native filter JSON; defaults to pinned upstream filters")
    prepare.add_argument("--out", type=Path, required=True)
    template = commands.add_parser("defaults", help="Print editable pinned upstream settings")
    template.add_argument("kind", choices=["advanced", "filters"])
    doctor = commands.add_parser("doctor", help="Inspect installed components without renting a worker")
    doctor.add_argument("--shared", type=Path, default=Path("/mnt/bio-shared"))
    doctor.add_argument("--verify-assets", action="store_true")
    validate = commands.add_parser("validate", help="Validate bundle and installed runtime before submission")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--shared", type=Path, default=Path("/mnt/bio-shared"))
    submit = commands.add_parser("submit", help="Run a bundled design job under the managed cloud budget and timeout")
    submit.add_argument("--bundle", type=Path, required=True)
    submit.add_argument("--timeout", type=int, default=7200)
    submit.add_argument("--gpu")
    submit.add_argument("--spot", action="store_true")
    submit.add_argument("--name")
    submit.add_argument('--max-cost-usd', type=float, help='Maximum quoted GPU plus disposable OS reservation')
    test = commands.add_parser("test", help="Run real GPU component checks on the pinned public fixture")
    test.add_argument("--out", type=Path, required=True, help="New directory for the immutable test input")
    test.add_argument("--shared", type=Path, default=Path("/mnt/bio-shared"))
    test.add_argument("--timeout", type=int, default=3600)
    test.add_argument("--gpu")
    test.add_argument("--spot", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            settings = {"binder_name": args.name, "chains": args.chains,
                        "target_hotspot_residues": args.hotspots, "lengths": args.lengths,
                        "number_of_final_designs": args.designs}
            advanced = read_json(args.advanced.read_bytes()) if args.advanced else defaults("advanced")
            filters = read_json(args.filters.read_bytes()) if args.filters else defaults("filters")
            result = create(args.pdb, settings, advanced, filters, args.out,
                            execution={'seed': args.seed} if args.seed is not None else None)
        elif args.command == "defaults":
            result = defaults(args.kind)
        elif args.command == "doctor":
            _, result = installation(args.shared, require_ready=False, verify_assets=args.verify_assets)
        elif args.command == "validate":
            result = preflight(args.bundle, args.shared)
        else:
            if args.command == "test":
                root, value = installation(args.shared)
                args.out.mkdir(parents=True, exist_ok=False)
                args.bundle = args.out / "smoke-input.tar.gz"
                args.name = "BindCraft component diagnostic"
                create(root / value["bindcraft_source"] / "example/PDL1.pdb",
                       {"binder_name": "installation_test", "chains": "A",
                        "target_hotspot_residues": "56", "lengths": [65, 65],
                        "number_of_final_designs": 1}, defaults("advanced"), defaults("filters"), args.bundle)
            inspect(args.bundle)
            argv = ["bio-submit", "bindcraft", "--in", str(args.bundle.resolve()),
                    "--timeout", str(args.timeout)]
            if args.gpu:
                argv += ["--gpu", args.gpu]
            if args.spot:
                argv.append("--spot")
            if args.name:
                argv += ["--name", args.name]
            if getattr(args, 'max_cost_usd', None) is not None:
                import math
                if not math.isfinite(args.max_cost_usd) or not 0 < args.max_cost_usd <= 750:
                    raise ValueError('Maximum cost must be finite and in (0,750] USD')
                prior = float(os.environ.get('DC_MAX_JOB_COST_USD', args.max_cost_usd))
                os.environ['DC_MAX_JOB_COST_USD'] = str(min(prior, args.max_cost_usd))
            if args.command == "test":
                argv += ["--sub", "smoke"]
            os.execvp(argv[0], argv)
        sys.stdout.buffer.write(encoded(result))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"bio-bindcraft: {error}\n")


if __name__ == "__main__":
    main()
