"""Hold one CPU-preparation permit until the submitting shell releases its pipe.

The permit lives in this dedicated process, never in model subprocesses. Its
stdin is a Bash coprocess pipe (close-on-exec in the caller), so an interrupted
submission releases its permit even if an unrelated child outlives that shell.
"""
import argparse
import fcntl
from pathlib import Path
import select
import sys
import time


def hold(state: Path, timeout: float, source=sys.stdin, ready=sys.stdout):
    state.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    slots = [open(state / f"head-preparation-{slot}.lock", "a") for slot in range(2)]
    try:
        while True:
            for slot in slots:
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                print("ready", file=ready, flush=True)
                # A release line or EOF (including caller death) ends the lease.
                source.readline()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("head preparation slots remained occupied")
            if select.select([source], [], [], min(0.5, remaining))[0]:
                # The shell cannot request work before receiving readiness.
                # Any early input or EOF means the request has been abandoned.
                return
    finally:
        for slot in slots:
            slot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument('--capacity-wait-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 0 < args.timeout <= 85500:
        parser.error("timeout must be positive and at most 85500 seconds")
    if not 0 <= args.capacity_wait_seconds <= 7200:
        parser.error('capacity wait allowance must be 0..7200 seconds')
    try:
        hold(args.state, args.timeout + args.capacity_wait_seconds)
    except (OSError, TimeoutError) as error:
        print(f"bio-submit: {error}", file=sys.stderr)
        raise SystemExit(2)
