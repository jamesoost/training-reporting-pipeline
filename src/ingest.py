"""Step 1 - Ingest: check every source file, then land it unchanged in data/raw/ with its SHA-256.

The run stops (after checking all files, so every problem is reported at once) if a file is
missing, empty, unreadable, lacks an expected sheet or required column, has no data rows, or
(CHW) has a module date column without a module title above it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Sequence

import pandas as pd

from config import ALL_SOURCES, CHW_DATE_SUFFIX, RAW_DIR, SOURCE_DIR, PipelineError, Source

log = logging.getLogger(__name__)


def normalise_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def read_grid(source: Source, base_dir: Path, sheet: str | None = None) -> pd.DataFrame:
    """Whole sheet as text, no header handling; row i of the frame is file row i + 1."""
    path = base_dir / source.path
    if source.is_csv:
        return pd.read_csv(path, header=None, dtype=str, keep_default_na=False, skip_blank_lines=False, encoding="utf-8-sig")
    return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str, keep_default_na=False)


def _is_blank(value: object) -> bool:
    return pd.isna(value) or not str(value).strip()


def check_source(source: Source, base_dir: Path) -> list[str]:
    path = base_dir / source.path
    if not path.is_file():
        return [f"{source.key}: FILE_MISSING {path} (copy the provided files into data/source/, see README)"]
    if path.stat().st_size == 0:
        return [f"{source.key}: FILE_EMPTY {path} is 0 bytes"]
    try:
        grids = {None: read_grid(source, base_dir)} if source.is_csv else pd.read_excel(
            path, sheet_name=None, header=None, dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001 - any failure to parse means the file is unreadable
        return [f"{source.key}: FILE_UNREADABLE {path}: {type(exc).__name__}: {exc}"]

    problems = []
    for sheet in source.sheets or (None,):
        where = f"{source.key}/{sheet or 'csv'}"
        if sheet not in grids:
            problems.append(f"{where}: SHEET_MISSING (found: {', '.join(map(str, grids))})")
            continue
        grid = grids[sheet]
        header = list(grid.iloc[source.header_row - 1]) if len(grid) >= source.header_row else []
        present = {normalise_header(h) for h in header if not _is_blank(h)}
        missing = [c for c in source.required_columns if normalise_header(c) not in present]
        if missing:
            problems.append(f"{where}: MISSING_COLUMNS {missing}")
        if not grid.iloc[source.header_row:].map(lambda v: not _is_blank(v)).to_numpy().any():
            problems.append(f"{where}: NO_DATA_ROWS below the header")
        if source.wide_modules and len(grid) >= source.header_row:
            titles = list(grid.iloc[source.header_row - 2])
            untitled = [h for h, t in zip(header, titles) if not _is_blank(h) and str(h).strip().endswith(CHW_DATE_SUFFIX) and _is_blank(t)]
            if untitled:
                problems.append(f"{where}: MISSING_MODULE_TITLE above {untitled}")
    return problems


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(source_dir: Path = SOURCE_DIR, raw_dir: Path = RAW_DIR, sources: Sequence[Source] = ALL_SOURCES) -> list[dict]:
    problems = [p for s in sources for p in check_source(s, source_dir)]
    for problem in problems:
        log.error(problem)
    if problems:
        raise PipelineError("INPUT_CHECK_FAILED", f"{len(problems)} problem(s) with the source files: " + "; ".join(problems))

    inputs = []
    for rel_path in dict.fromkeys(s.path for s in sources):  # the lookup workbook backs two sources
        dst = raw_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / rel_path, dst)
        inputs.append({"path": rel_path, "sha256": sha256(dst), "bytes": dst.stat().st_size})
    log.info("checked %d sources, landed %d files in %s", len(sources), len(inputs), raw_dir)
    return inputs


def describe_raw(raw_dir: Path = RAW_DIR, sources: Sequence[Source] = ALL_SOURCES) -> list[dict]:
    """--from-raw: record what is in data/raw/ without touching data/source/."""
    inputs = []
    for rel_path in dict.fromkeys(s.path for s in sources):
        path = raw_dir / rel_path
        if not path.is_file():
            raise PipelineError("RAW_MISSING", f"{path} not found; run the full pipeline once before using --from-raw")
        inputs.append({"path": rel_path, "sha256": sha256(path), "bytes": path.stat().st_size})
    return inputs
