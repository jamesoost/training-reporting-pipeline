"""Idempotency check: run the pipeline twice from source and once with --from-raw, then compare
the business outputs and clean layers byte-for-byte (the manifest and log carry run ids and timings).

Run from the repo root: python scripts/check_idempotency.py
"""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "outputs/reports/completions_by_district.csv",
    "outputs/reports/participants_vs_completions_vs_enrolments.csv",
    "outputs/reconciliation/reconciliation_checks.csv",
    "outputs/diagnostics/dedup_summary.csv",
    "outputs/diagnostics/unmapped_values.csv",
    "outputs/diagnostics/quarantine_records.csv",
    "data/clean/records.csv",
    "data/clean/reportable_records.csv",
]


def run(*args: str) -> dict[str, str]:
    result = subprocess.run([sys.executable, "src/pipeline.py", *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"pipeline {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr[-2000:]}")
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES}


def main() -> int:
    first, rerun, from_raw = run(), run(), run("--from-raw")
    same = {f: first[f] == rerun[f] == from_raw[f] for f in FILES}
    for f, ok in same.items():
        print(f"{'SAME' if ok else 'DIFF'}  {f}")
    print("idempotent" if all(same.values()) else "NOT idempotent")
    return 0 if all(same.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
