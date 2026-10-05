"""Step 2 - Transform: raw -> canonical records -> validation -> mapped to lookups -> data/clean/records.csv.

One record per source row, except CHW (wide), unpivoted to one record per participant per completed
module. Values are matched exactly on a normalised key: first the lookup, then the reviewed alias
tables in mappings/. Nothing is dropped: failed validation is flagged (`quarantine_rules`), and
unresolved values are kept and listed in outputs/unmapped_values.csv.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from config import (ALLOWED_ATTENDANCE, ALLOWED_BOOKING, CHW_DATE_SUFFIX, CLEAN_DIR, COMPLETED_ATTENDANCE,
                    DIAGNOSTICS_DIR, LOOKUP_COURSES, LOOKUP_FACILITIES, MAPPINGS_DIR, MIN_TRAINING_DATE, MISSING_TOKENS, OUTPUT_DIR,
                    RAW_DIR, TRAINING_SOURCES, PipelineError, Source)
from ingest import normalise_header, read_grid, sha256

log = logging.getLogger(__name__)

CLEAN_FILE = CLEAN_DIR / "records.csv"
QUARANTINE_FILE = DIAGNOSTICS_DIR / "quarantine_records.csv"
UNMAPPED_FILE = DIAGNOSTICS_DIR / "unmapped_values.csv"
MAPPING_FILES = ("course_aliases.csv", "district_aliases.csv")
UNKNOWN_DISTRICT = "Unknown / Unmapped"
MAPPED_FIELDS = ("course", "facility", "district")

LINEAGE = ["record_id", "dataset_key", "source_file", "source_sheet", "source_row_number", "source_module"]
IDENTIFIERS = ["trainee_number", "id_number", "persal_number", "email"]
RAW_FIELDS = [
    "course_name_raw", "delivery_method_raw", "booking_status_raw", "attendance_status_raw",
    "approval_date_raw", "start_date_raw", "end_date_raw",
    "district_raw", "sub_district_raw", "facility_raw", "facility_other_raw",
]
DATE_FIELDS = ("approval_date", "start_date", "end_date")
COLUMNS = LINEAGE + IDENTIFIERS + RAW_FIELDS + ["delivery_method", *DATE_FIELDS, "training_date", "completion_status"]


# ---- read ------------------------------------------------------------------------------------

def read_sheet(source: Source, base_dir: Path, sheet: str | None = None) -> tuple[pd.DataFrame, dict[str, str]]:
    """Rows below the header as text (blank-like -> NA, blank rows dropped, headers in their configured
    spelling, `source_row_number` = file row), plus {module date column: title} for wide CHW sheets."""
    grid = read_grid(source, base_dir, sheet)
    canonical = {normalise_header(c): c for c in source.required_columns}
    raw_header = ["" if pd.isna(h) else str(h).strip() for h in grid.iloc[source.header_row - 1]]
    header = [canonical.get(normalise_header(h), h) for h in raw_header]

    df = grid.iloc[source.header_row:].copy()
    df.columns = header
    df = df.loc[:, [h != "" for h in header]]
    data_cols = list(df.columns)
    df[data_cols] = df[data_cols].apply(_blank_to_na)
    df["source_row_number"] = df.index + 1
    df = df.dropna(how="all", subset=data_cols).reset_index(drop=True)

    titles = {}
    if source.wide_modules:
        title_row = grid.iloc[source.header_row - 2]
        titles = {h: str(t).strip() for h, t in zip(header, title_row)
                  if h.endswith(CHW_DATE_SUFFIX) and isinstance(t, str) and t.strip()}
    return df, titles


def _blank_to_na(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    return text.mask(text.str.casefold().isin(MISSING_TOKENS))


# ---- standardise -----------------------------------------------------------------------------

def standardise(source: Source, base_dir: Path) -> tuple[pd.DataFrame, dict]:
    parts, rows_read, without_modules = [], 0, 0
    for sheet in source.sheets or (None,):
        df, titles = read_sheet(source, base_dir, sheet)
        rows_read += len(df)
        base = pd.DataFrame({canon: df[col] for canon, col in source.columns.items()})
        base["source_sheet"] = sheet or "csv"
        base["source_row_number"] = df["source_row_number"]
        if not source.wide_modules:
            parts.append(base)
            continue
        without_modules += int(df[list(titles)].isna().all(axis=1).sum())
        for date_col, title in titles.items():
            filled = df[date_col].notna()
            part = base[filled].copy()
            part["course_name_raw"] = title
            part["start_date_raw"] = df.loc[filled, date_col]
            part["source_module"] = re.match(r"\s*(\d+)", date_col).group(1)
            parts.append(part)

    out = pd.concat(parts, ignore_index=True).reindex(columns=COLUMNS)
    text_cols = IDENTIFIERS + RAW_FIELDS + ["source_module"]
    out[text_cols] = out[text_cols].astype("string")
    out["dataset_key"] = source.key
    out["source_file"] = source.path
    module = out["source_module"].fillna("").map(lambda m: f"|m{m}" if m else "")
    out["record_id"] = source.key + "|" + out["source_sheet"] + "|" + out["source_row_number"].astype(str) + module

    for field in DATE_FIELDS:
        out[field] = pd.to_datetime(out[f"{field}_raw"], format=source.date_format, errors="coerce")
    out["training_date"] = out["end_date"].fillna(out["start_date"]).fillna(out["approval_date"])

    if source.completion_rule == "end_date":
        completed = out["end_date_raw"].notna()
    elif source.completion_rule == "attendance":
        completed = out["attendance_status_raw"].str.casefold().isin(COMPLETED_ATTENDANCE).fillna(False)
    elif source.completion_rule == "module_date":
        completed = pd.Series(True, index=out.index)
    else:
        raise ValueError(f"{source.key}: unknown completion_rule {source.completion_rule!r}")
    out["completion_status"] = np.where(completed, "completed", "not_completed")
    out["delivery_method"] = source.delivery_method or out["delivery_method_raw"]

    if without_modules:
        log.warning("%s: %d participant row(s) have no module dates and produce no records", source.key, without_modules)
    return out, {"rows_read": rows_read, "records": len(out), "rows_without_modules": without_modules,
                 "wide": source.wide_modules}


# ---- validate --------------------------------------------------------------------------------

def quarantine_rules(df: pd.DataFrame, today: str | None = None) -> pd.Series:
    """Semicolon-separated rule ids a record fails; NA when it passes. Dates must already be ISO strings."""
    today = today or date.today().isoformat()
    attendance = df["attendance_status_raw"].str.casefold()
    booking = df["booking_status_raw"].str.casefold()
    unparsed = pd.Series(False, index=df.index)
    for field in DATE_FIELDS:
        unparsed |= df[f"{field}_raw"].notna() & df[field].isna()
    rules = {
        "Q01_MISSING_COURSE": df["course_name_raw"].isna(),
        "Q02_MISSING_DATE": df["training_date"].isna(),
        "Q03_INVALID_DATE": unparsed,
        "Q04_DATE_OUT_OF_RANGE": (df["training_date"] < MIN_TRAINING_DATE) | (df["training_date"] > today),
        "Q05_INVALID_ATTENDANCE_STATUS": attendance.notna() & ~attendance.isin(ALLOWED_ATTENDANCE),
        "Q06_INVALID_BOOKING_STATUS": booking.notna() & ~booking.isin(ALLOWED_BOOKING),
    }
    reasons = pd.Series("", index=df.index)
    for rule_id, failed in rules.items():
        reasons = reasons + np.where(failed.fillna(False), rule_id + ";", "")
    reasons = reasons.str.rstrip(";")
    return reasons.mask(reasons == "")


# ---- map -------------------------------------------------------------------------------------

def match_key(value: object) -> object:
    if pd.isna(value):
        return pd.NA
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip() or pd.NA


def course_key(value: object) -> object:
    """Also drops the CHW module number prefix ('5-...') and the online '- Online (WCGH)' suffix."""
    if pd.isna(value):
        return pd.NA
    text = re.sub(r"^\s*\d{1,2}\s*-\s*", "", str(value))
    text = re.sub(r"\s*-\s*online\s*\(wcgh\)\s*$|\s*\(wcgh\)\s*$", "", text, flags=re.I)
    return match_key(text)


def facility_key(value: object) -> object:
    if pd.isna(value):
        return pd.NA
    return match_key(re.sub(r"\s*\(western cape\)\s*$", "", str(value), flags=re.I))


@dataclass(frozen=True)
class Lookups:
    courses: pd.DataFrame  # index: key
    facilities: pd.DataFrame  # index: key
    course_aliases: pd.Series  # key -> CourseName
    district_aliases: pd.Series  # key -> district


def _keyed(df: pd.DataFrame, column: str) -> pd.DataFrame:
    return df.assign(key=df[column].map(match_key)).dropna(subset=["key"]).drop_duplicates("key").set_index("key")


def load_lookups(raw_dir: Path, mappings_dir: Path) -> Lookups:
    courses, _ = read_sheet(LOOKUP_COURSES, raw_dir, "LU_Courses")
    # Two names collide after normalisation: prefer Active, then lowest code.
    courses = courses.assign(inactive=courses["CourseStatus"].str.casefold().ne("active")).sort_values(["inactive", "CourseCode"])
    courses = _keyed(courses, "CourseName")[["CourseName", "CourseCode", "CourseGroup"]]

    fac, _ = read_sheet(LOOKUP_FACILITIES, raw_dir, "LU_Facility")
    by_name = _keyed(fac, "FacilityName")
    # Reporting names only count as keys where they identify a single facility.
    reporting = fac.assign(key=fac["FacilityReportingName"].map(match_key))
    reporting = reporting[~reporting["key"].duplicated(keep=False) & ~reporting["key"].isin(by_name.index)].dropna(subset=["key"])
    facilities = pd.concat([by_name, reporting.set_index("key")])[["FacilityName", "FacilityCode", "HealthDistrict", "HealthSubdistrict"]]

    read = lambda name: pd.read_csv(mappings_dir / name, dtype=str, keep_default_na=False)
    course_aliases = read("course_aliases.csv").set_index("source_key")["course_name"]
    district_aliases = read("district_aliases.csv").set_index("source_key")["district"]

    dangling = sorted(set(course_aliases.map(match_key)) - set(courses.index))
    if dangling:
        raise PipelineError("MAPPING_TABLE_INVALID", f"course_aliases.csv points to courses not in LU_Courses: {dangling[:10]}")
    return Lookups(courses, facilities, course_aliases, district_aliases)


def map_course(df: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    key = df["course_name_raw"].map(course_key).astype("string")
    direct = key.where(key.isin(lk.courses.index))
    via_alias = key.map(lk.course_aliases).map(match_key).astype("string")
    resolved = direct.fillna(via_alias.where(via_alias.isin(lk.courses.index)))
    return pd.DataFrame({
        "course_name": resolved.map(lk.courses["CourseName"]).fillna(df["course_name_raw"]),
        "course_code": resolved.map(lk.courses["CourseCode"]),
        "course_group": resolved.map(lk.courses["CourseGroup"]),
        "course_match": np.select([df["course_name_raw"].isna(), direct.notna(), resolved.notna()],
                                  ["missing", "lookup", "alias"], "unmapped"),
    }, index=df.index)


def map_facility(df: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    primary = df["facility_raw"].map(facility_key).astype("string")
    other = df["facility_other_raw"].map(facility_key).astype("string")
    primary_ok, other_ok = primary.isin(lk.facilities.index), other.isin(lk.facilities.index)
    resolved = primary.where(primary_ok).fillna(other.where(other_ok))
    district = resolved.map(lk.facilities["HealthDistrict"]).map(match_key).map(lk.district_aliases)
    return pd.DataFrame({
        "facility_name": resolved.map(lk.facilities["FacilityName"]).fillna(df["facility_raw"]).fillna(df["facility_other_raw"]),
        "facility_code": resolved.map(lk.facilities["FacilityCode"]),
        "facility_district": district.replace(UNKNOWN_DISTRICT, pd.NA),
        "facility_sub_district": resolved.map(lk.facilities["HealthSubdistrict"]),
        "facility_match": np.select([primary_ok, other_ok, df["facility_raw"].isna() & df["facility_other_raw"].isna()],
                                    ["lookup_primary", "lookup_other", "missing"], "unmapped"),
    }, index=df.index)


def map_district(df: pd.DataFrame, facility_district: pd.Series, lk: Lookups) -> pd.DataFrame:
    """Source district if recognised, else the matched facility's district, else Unknown / Unmapped."""
    from_source = df["district_raw"].map(match_key).map(lk.district_aliases).replace(UNKNOWN_DISTRICT, pd.NA)
    return pd.DataFrame({
        "district": from_source.fillna(facility_district).fillna(UNKNOWN_DISTRICT),
        "district_match": np.select([from_source.notna(), facility_district.notna(), df["district_raw"].isna()],
                                    ["source", "facility_lookup", "missing"], "unmapped"),
    }, index=df.index)


def apply_mappings(df: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    facility = map_facility(df, lk)
    return pd.concat([df, map_course(df, lk), facility, map_district(df, facility["facility_district"], lk)], axis=1)


def unmapped_values(df: pd.DataFrame) -> pd.DataFrame:
    """Flat list of unmapped/missing values with full source traceability: field, status, dataset_key, source_value,
    record_id, source_file, source_sheet, source_row_number, source_module (one row per unmapped field per record)."""
    raw_cols = {"course": "course_name_raw", "district": "district_raw"}
    frames = []
    for field in MAPPED_FIELDS:
        status = df[f"{field}_match"]
        if field == "district":
            status = status.mask(df["district_raw"].notna() & status.ne("source"), "unmapped")
        rows = df[status.isin(["unmapped", "missing"])]
        value = rows["facility_raw"].fillna(rows["facility_other_raw"]) if field == "facility" else rows[raw_cols[field]]
        frames.append(pd.DataFrame({
            "field": field,
            "status": status[rows.index],
            "dataset_key": rows["dataset_key"],
            "source_value": value.fillna("<missing>"),
            "record_id": rows["record_id"],
            "source_file": rows["source_file"],
            "source_sheet": rows["source_sheet"],
            "source_row_number": rows["source_row_number"],
            "source_module": rows["source_module"],
        }, index=rows.index))
    out = pd.concat(frames) if frames else pd.DataFrame(columns=["field", "status", "dataset_key", "source_value", 
                                                                  "record_id", "source_file", "source_sheet", 
                                                                  "source_row_number", "source_module"])
    return out.sort_values(["field", "dataset_key", "source_value"])


# ---- run -------------------------------------------------------------------------------------

def transform(raw_dir: Path, mappings_dir: Path, sources: Sequence[Source] = TRAINING_SOURCES,
              today: str | None = None) -> tuple[pd.DataFrame, dict]:
    frames, counts = [], {}
    for source in sources:
        frame, counts[source.key] = standardise(source, raw_dir)
        frames.append(frame)
    df = pd.concat(frames, ignore_index=True)
    for field in DATE_FIELDS + ("training_date",):
        df[field] = df[field].dt.strftime("%Y-%m-%d").astype("string")
    df["quarantine_rules"] = quarantine_rules(df, today)
    return apply_mappings(df, load_lookups(raw_dir, mappings_dir)), counts


def run(raw_dir: Path = RAW_DIR, mappings_dir: Path = MAPPINGS_DIR) -> dict:
    for source in TRAINING_SOURCES + (LOOKUP_COURSES,):
        if not (raw_dir / source.path).is_file():
            raise PipelineError("RAW_MISSING", f"{raw_dir / source.path} not found; run the full pipeline first")
    df, counts = transform(raw_dir, mappings_dir)
    quarantined = df[df["quarantine_rules"].notna()]
    unmapped = unmapped_values(df)

    CLEAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUARANTINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNMAPPED_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CLEAN_FILE, index=False)
    if len(quarantined) > 0:
        quarantine_out = quarantined[["record_id", "quarantine_rules", "source_file", "source_sheet", "source_row_number", "source_module"]].copy()
        quarantine_out = quarantine_out.sort_values(["source_file", "source_row_number"]).reset_index(drop=True)
        quarantine_out.to_csv(QUARANTINE_FILE, index=False)
    else:
        # Write empty file with headers if no quarantined records
        pd.DataFrame(columns=["record_id", "quarantine_rules", "source_file", "source_sheet", "source_row_number", "source_module"]).to_csv(QUARANTINE_FILE, index=False)
    unmapped.to_csv(UNMAPPED_FILE, index=False)

    matches = {f: df[f"{f}_match"].value_counts().to_dict() for f in MAPPED_FIELDS}
    log.info("transformed %d records (%d quarantined, %d unmapped) -> %s", len(df), len(quarantined), len(unmapped), CLEAN_FILE)
    return {"sources": counts, "records": len(df), "quarantined": len(quarantined), "unmapped": len(unmapped), "matches": matches,
            "mappings_sha256": {name: sha256(mappings_dir / name) for name in MAPPING_FILES}}
