"""Step 3 - Report: participant matching, de-duplication, the reportable rule, two reports, reconciliation.

Every clean record gets one outcome: reportable, or one `exclusion_reason` in precedence order
quarantined > test_internal > unidentifiable > duplicate. Headline = reportable completions (a count).
Unique participants is a distinct count and is never summed across groups. The reports are only
written if every reconciliation check passes; reconciliation_checks.csv is always written.
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

from config import CLEAN_DIR, OUTPUT_DIR, REPORTS_DIR, RECONCILIATION_DIR, DIAGNOSTICS_DIR, TEST_INTERNAL_KEYWORDS, PipelineError
from transform import CLEAN_FILE, match_key

log = logging.getLogger(__name__)

RECORDS_FILE = CLEAN_DIR / "reportable_records.csv"
SUMMARY_FILE = DIAGNOSTICS_DIR / "dedup_summary.csv"
DISTRICT_REPORT = REPORTS_DIR / "completions_by_district.csv"
PARTICIPANT_REPORT = REPORTS_DIR / "participants_vs_completions_vs_enrolments.csv"
RECONCILIATION_FILE = RECONCILIATION_DIR / "reconciliation_checks.csv"

EXCLUSION_REASONS = ("quarantined", "test_internal", "unidentifiable", "duplicate")
TIER_B = ("trainee_number", "persal_number", "email")
# Only the system-generated trainee number may lend an ID to a row without one: manually captured
# persal/email values map to more than one ID too often (5.1% / 3.9% in-person).
LINK_COLUMNS = ("trainee_number",)
COMPLETENESS_COLUMNS = ("id_number", "persal_number", "email", "facility_raw", "district_raw")
DISTRICT_ORDER = ["Cape Winelands", "Central Karoo", "City of Cape Town", "Garden Route", "Overberg",
                  "West Coast", "Other / Out of province", "Unknown / Unmapped"]
COUNTS = ["completions_count", "not_completed_count", "enrolments_count"]
TOTAL = "TOTAL"


# ---- participants and duplicates -------------------------------------------------------------

def normalise_id(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.replace(r"[\s\-]", "", regex=True).str.casefold()
    return cleaned.mask(cleaned == "")


def is_test_internal(df: pd.DataFrame, keywords=TEST_INTERNAL_KEYWORDS) -> pd.Series:
    if not keywords:
        return pd.Series(False, index=df.index)
    text = df[["course_name_raw", "facility_raw", "facility_other_raw", "email"]].fillna("").astype(str).agg(" ".join, axis=1)
    return text.str.casefold().str.contains("|".join(re.escape(k.casefold()) for k in keywords))


def assign_participants(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """participant_key + match_tier. A = national ID (A_linked = borrowed via a trainee number that maps to
    exactly one ID); B = source-scoped trainee / persal / email key; none = unidentifiable. Names are never used."""
    pid = normalise_id(df["id_number"])
    tier = pd.Series(np.where(pid.notna(), "A", "none"), index=df.index)
    uncertain: list[dict] = []

    for col in LINK_COLUMNS:
        scoped = df["dataset_key"] + ":" + normalise_id(df[col])
        known = pd.DataFrame({"b": scoped, "id": pid}).dropna()
        ids_per_b = known.groupby("b")["id"].nunique()
        for b, n in ids_per_b[ids_per_b > 1].items():
            uncertain.append({"issue": f"{col}_linked_to_multiple_ids", "dataset_key": b.split(":", 1)[0], "key": b,
                              "detail": f"{n} distinct ID numbers; not linked", "records": int((scoped == b).sum())})
        link = known[known["b"].isin(ids_per_b[ids_per_b == 1].index)].drop_duplicates("b").set_index("b")["id"]
        fill = pid.isna() & scoped.isin(link.index)
        pid = pid.mask(fill, scoped.map(link))
        tier = tier.mask(fill, "A_linked")

        per_id = pd.DataFrame({"id": df["dataset_key"] + ":" + pid, "b": normalise_id(df[col])}).dropna()
        counts = per_id.groupby("id")["b"].nunique()
        for key, n in counts[counts > 1].items():
            uncertain.append({"issue": f"id_linked_to_multiple_{col}", "dataset_key": key.split(":", 1)[0], "key": key,
                              "detail": f"{n} distinct {col} values; counted as one participant",
                              "records": int((per_id["id"] == key).sum())})

    fallback = pd.Series(pd.NA, index=df.index, dtype="string")
    for col in TIER_B:
        fallback = fallback.fillna(df["dataset_key"] + ":" + col + ":" + normalise_id(df[col]))
    key = ("ID:" + pid).fillna(fallback)
    tier = tier.mask(pid.isna() & fallback.notna(), "B").mask(key.isna(), "none")
    return pd.DataFrame({"participant_key": key, "match_tier": tier}, index=df.index), uncertain


def deduplicate(df: pd.DataFrame, eligible: pd.Series) -> tuple[pd.DataFrame, list[dict]]:
    """Duplicate = same participant + course + training date. Keeps completed, then most complete row, then
    source order. Course is the conformed name, not the code: LU_Courses reuses codes such as C999."""
    course = df["course_name"].map(match_key).astype("string")
    dup_key = (df["participant_key"] + "|" + course + "|" + df["training_date"]).where(eligible)
    ranked = pd.DataFrame({
        "dup_key": dup_key,
        "completed": df["completion_status"].eq("completed"),
        "completeness": df[list(COMPLETENESS_COLUMNS)].notna().sum(axis=1),
        "order": np.arange(len(df)),
    }, index=df.index).dropna(subset=["dup_key"])
    ranked = ranked.sort_values(["dup_key", "completed", "completeness", "order"], ascending=[True, False, False, True])
    survivors = ranked.groupby("dup_key", sort=False).head(1).index
    survivor_of = pd.Series(df.loc[survivors, "record_id"].values, index=ranked.loc[survivors, "dup_key"].values)
    size = ranked.groupby("dup_key")["order"].transform("size").reindex(df.index)

    out = pd.DataFrame({"duplicate_key": dup_key, "duplicate_group_size": size,
                        "duplicate_of": dup_key.map(survivor_of).where(size > 1)}, index=df.index)
    out["is_duplicate"] = out["duplicate_of"].notna() & ~df.index.isin(survivors)

    uncertain = []
    conflicting = ranked[ranked["dup_key"].map(ranked["dup_key"].value_counts()) > 1].groupby("dup_key")["completed"].nunique()
    for key in conflicting[conflicting > 1].index:
        members = dup_key == key
        uncertain.append({"issue": "duplicate_group_conflicting_completion",
                          "dataset_key": ",".join(sorted(df.loc[members, "dataset_key"].unique())),
                          "key": key, "detail": "kept the completed record", "records": int(members.sum())})
    return out, uncertain


def apply_rules(clean: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = clean.reset_index(drop=True)
    df["is_test_internal"] = is_test_internal(df)
    participants, uncertain = assign_participants(df)
    df = pd.concat([df, participants], axis=1)

    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason = reason.mask(df["quarantine_rules"].notna(), "quarantined")
    reason = reason.mask(reason.isna() & df["is_test_internal"], "test_internal")
    reason = reason.mask(reason.isna() & df["participant_key"].isna(), "unidentifiable")
    dups, dup_uncertain = deduplicate(df, eligible=reason.isna())
    df = pd.concat([df, dups], axis=1)
    reason = reason.mask(reason.isna() & df["is_duplicate"], "duplicate")

    df["exclusion_reason"] = reason
    df["is_reportable"] = reason.isna()
    return df, pd.DataFrame(uncertain + dup_uncertain, columns=["issue", "dataset_key", "key", "detail", "records"])


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    """Record funnel per source: records = reportable + each exclusion reason."""
    rows = []
    for key, g in list(df.groupby("dataset_key")) + [(TOTAL, df)]:
        reasons = g["exclusion_reason"].value_counts()
        rep = g[g["is_reportable"]]
        rows.append({
            "dataset_key": key,
            "stage_records": len(g),
            **{r: int(reasons.get(r, 0)) for r in EXCLUSION_REASONS},
            "reportable_records": len(rep),
            "duplicate_groups": int(g.loc[g["duplicate_group_size"] > 1, "duplicate_key"].nunique()),
            "participants_distinct": int(rep["participant_key"].nunique()),
            **{f"match_tier_{t}": int((rep["match_tier"] == t).sum()) for t in ("A", "A_linked", "B")},
        })
    return pd.DataFrame(rows)


# ---- reports and reconciliation --------------------------------------------------------------

def _counts(g: pd.DataFrame) -> dict:
    completed = int(g["completion_status"].eq("completed").sum())
    return {"completions_count": completed, "not_completed_count": len(g) - completed, "enrolments_count": len(g)}


def district_report(rep: pd.DataFrame) -> pd.DataFrame:
    present = set(rep["district"])
    districts = [d for d in DISTRICT_ORDER if d in present] + sorted(present - set(DISTRICT_ORDER))
    return pd.DataFrame([{"district": d, **_counts(rep[rep["district"] == d])} for d in districts]
                        + [{"district": TOTAL, **_counts(rep)}])


def participant_report(rep: pd.DataFrame) -> pd.DataFrame:
    rows = [{"dataset_key": k, "participants_distinct": g["participant_key"].nunique(), **_counts(g)}
            for k, g in rep.groupby("dataset_key")]
    # Distinct across all sources, not a sum: the same person can appear in more than one source.
    rows.append({"dataset_key": TOTAL, "participants_distinct": rep["participant_key"].nunique(), **_counts(rep)})
    return pd.DataFrame(rows)


def reconcile(records: pd.DataFrame, r1: pd.DataFrame, r2: pd.DataFrame, source_counts: dict) -> pd.DataFrame:
    rep = records[records["is_reportable"]]
    t1, t2 = r1.set_index("district").loc[TOTAL], r2.set_index("dataset_key").loc[TOTAL]
    body1, body2 = r1[r1["district"] != TOTAL], r2[r2["dataset_key"] != TOTAL]
    headline = int(rep["completion_status"].eq("completed").sum())
    excluded = sum(int(records["exclusion_reason"].eq(r).sum()) for r in EXCLUSION_REASONS)
    checks = [
        ("R01_district_completions_sum_to_headline", "Sum of district completions = headline reportable completions",
         headline, int(body1["completions_count"].sum())),
        ("R02_district_enrolments_sum_to_total", "Sum of district enrolments = TOTAL enrolments (report 1)",
         int(t1["enrolments_count"]), int(body1["enrolments_count"].sum())),
        ("R03_source_completions_sum_to_headline", "Sum of source completions = headline (report 2)",
         headline, int(body2["completions_count"].sum())),
        ("R04_source_enrolments_sum_to_total", "Sum of source enrolments = TOTAL enrolments (report 2)",
         int(t2["enrolments_count"]), int(body2["enrolments_count"].sum())),
        ("R05_completed_plus_not_completed", "Rows where completed + not completed != enrolments (both reports)",
         0, int(sum(((r["completions_count"] + r["not_completed_count"]) != r["enrolments_count"]).sum() for r in (r1, r2)))),
        ("R06_report_totals_agree", "Count columns where report 1 TOTAL != report 2 TOTAL",
         0, int(sum(int(t1[c]) != int(t2[c]) for c in COUNTS))),
        ("R07_headline_equals_report_total", "Headline = report 1 TOTAL completions (R06 ties report 2)",
         headline, int(t1["completions_count"])),
        ("R08_record_funnel", "Records = reportable + quarantined + test_internal + unidentifiable + duplicate",
         len(records), int(records["is_reportable"].sum()) + excluded),
        ("R09_one_outcome_per_record", "Records with neither or both of reportable / exclusion reason",
         0, int((records["is_reportable"] == records["exclusion_reason"].notna()).sum())),
        ("R10_records_match_raw_rows", "Sources where records != rows read from raw (non-CHW sources)",
         0, sum(c["rows_read"] != c["records"] for c in source_counts.values() if not c["wide"])),
        ("R11_records_carried_through", "Records reaching reporting = records produced by transform",
         sum(c["records"] for c in source_counts.values()), len(records)),
        ("R12_participants_le_enrolments", "Distinct participants <= enrolments (TOTAL), 1 = true",
         1, int(int(t2["participants_distinct"]) <= int(t2["enrolments_count"]))),
        ("R13_participants_distinct_not_summed", "TOTAL distinct participants between max and sum of sources, 1 = true",
         1, int(body2["participants_distinct"].max() <= int(t2["participants_distinct"]) <= body2["participants_distinct"].sum())),
    ]
    out = pd.DataFrame(checks, columns=["check_id", "description", "expected", "actual"])
    out["status"] = np.where(out["expected"] == out["actual"], "PASS", "FAIL")
    return out


def run(source_counts: dict) -> dict:
    clean = pd.read_csv(CLEAN_FILE, dtype=str, keep_default_na=False).replace("", pd.NA)
    df, uncertain = apply_rules(clean)
    summary = summarise(df)
    df.to_csv(RECORDS_FILE, index=False)
    summary.to_csv(SUMMARY_FILE, index=False)
    if len(uncertain):
        log.info("%d uncertain identity/duplicate case(s) flagged, not merged", len(uncertain))

    rep = df[df["is_reportable"]]
    r1, r2 = district_report(rep), participant_report(rep)
    checks = reconcile(df, r1, r2, source_counts)
    checks.to_csv(RECONCILIATION_FILE, index=False)
    failed = checks.loc[checks["status"] == "FAIL", "check_id"].tolist()
    if failed:
        DISTRICT_REPORT.unlink(missing_ok=True)
        PARTICIPANT_REPORT.unlink(missing_ok=True)
        raise PipelineError("RECONCILIATION_FAILED", f"reports not published; failed checks: {failed}")
    r1.to_csv(DISTRICT_REPORT, index=False)
    r2.to_csv(PARTICIPANT_REPORT, index=False)

    total = r2.set_index("dataset_key").loc[TOTAL]
    funnel = summary.set_index("dataset_key").loc[TOTAL]
    result = {
        "headline_reportable_completions": int(total["completions_count"]),
        "not_completed": int(total["not_completed_count"]),
        "enrolments": int(total["enrolments_count"]),
        "participants_distinct": int(total["participants_distinct"]),
        "excluded": {r: int(funnel[r]) for r in EXCLUSION_REASONS},
        "uncertain_matches": len(uncertain),
        "reconciliation": f"{len(checks) - len(failed)}/{len(checks)} passed",
    }
    log.info("headline reportable completions = %d (reconciliation %s)", result["headline_reportable_completions"],
             result["reconciliation"])
    return result
