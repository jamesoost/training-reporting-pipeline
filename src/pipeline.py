"""Run the pipeline.

    python src/pipeline.py              # full run: ingest -> transform -> report
    python src/pipeline.py --from-raw   # rebuild from data/raw/ without reading data/source/

Writes timestamped logs to data/logs/pipeline_YYYYMMDDTHHMMSSZ.log and outputs/metadata/run_manifest.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import ingest
import report
import transform
from config import METADATA_DIR, PipelineError

LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
MANIFEST_FILE = METADATA_DIR / "run_manifest.json"
log = logging.getLogger("pipeline")


def setup_logging() -> Path:
    """Initialize logging with a timestamped log file. Returns the log file path."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    # Create timestamped log file (e.g., pipeline_20260104T123456Z.log)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = LOG_DIR / f"pipeline_{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        handlers=[logging.FileHandler(log_file, mode="w", encoding="utf-8")],
        force=True,
    )
    return log_file


def timed(manifest: dict, name: str, step: Callable, *args):
    start = time.perf_counter()
    result = step(*args)
    seconds = round(time.perf_counter() - start, 2)
    manifest["stage_seconds"][name] = seconds
    log.info("%s finished in %.1fs", name, seconds)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Training reporting pipeline: source -> raw -> clean -> reports")
    parser.add_argument("--from-raw", action="store_true", help="skip ingest; rebuild from data/raw/")
    args = parser.parse_args(argv)

    log_file = setup_logging()
    started = datetime.now(timezone.utc)
    manifest = {"run_id": f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}", "started_at": started.isoformat(),
                "from_raw": args.from_raw, "status": "running", "stage_seconds": {}}
    log.info("run %s started (from_raw=%s)", manifest["run_id"], args.from_raw)
    try:
        manifest["inputs"] = ingest.describe_raw() if args.from_raw else timed(manifest, "ingest", ingest.run)
        manifest["transform"] = timed(manifest, "transform", transform.run)
        manifest["report"] = timed(manifest, "report", report.run, manifest["transform"]["sources"])
        manifest["status"] = "completed"
        return 0
    except Exception as exc:
        log.error("run failed: %s", exc, exc_info=isinstance(exc, PipelineError))
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        return 1 if isinstance(exc, PipelineError) else 2
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        log.info("run %s %s; manifest at %s, logs at %s", manifest["run_id"], manifest["status"], MANIFEST_FILE, log_file)


if __name__ == "__main__":
    sys.exit(main())
