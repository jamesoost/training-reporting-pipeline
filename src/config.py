"""Paths, source definitions and business rules for the training reporting pipeline."""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path

# The capture templates use Excel data-validation extensions openpyxl cannot read; values are unaffected.
warnings.filterwarnings("ignore", message="Data Validation extension is not supported", category=UserWarning)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data" / "source"
RAW_DIR = ROOT / "data" / "raw"
CLEAN_DIR = ROOT / "data" / "clean"
OUTPUT_DIR = ROOT / "outputs"
MAPPINGS_DIR = ROOT / "mappings"

# Output subfolders
REPORTS_DIR = OUTPUT_DIR / "reports"
RECONCILIATION_DIR = OUTPUT_DIR / "reconciliation"
DIAGNOSTICS_DIR = OUTPUT_DIR / "diagnostics"
METADATA_DIR = OUTPUT_DIR / "metadata"

MISSING_TOKENS = frozenset({"", "n/a", "na", "null", "none", "-", "nan", "nat"})

# Business rules (evidence in docs/design.md).
COMPLETED_ATTENDANCE = frozenset({"attended", "replacement"})  # Replacement = substitute who attended
ALLOWED_ATTENDANCE = frozenset({"attended", "partial attendance", "non-attendance", "replacement", "cancelled"})
ALLOWED_BOOKING = frozenset({"confirmed", "waiting list", "cancelled", "no booking"})
MIN_TRAINING_DATE = "2020-01-01"
TEST_INTERNAL_KEYWORDS: tuple[str, ...] = ()  # none found in the data; KTU staff are real participants


class PipelineError(Exception):
    """Stops the run. `code` names the failure, e.g. INPUT_CHECK_FAILED."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True)
class Source:
    key: str
    path: str  # relative to data/source and data/raw
    sheets: tuple[str, ...] = ()  # empty for CSV
    header_row: int = 1
    columns: dict[str, str] = field(default_factory=dict)  # canonical name -> source column
    extra_required: tuple[str, ...] = ()
    date_format: str = "%Y-%m-%d %H:%M:%S"
    completion_rule: str = ""  # end_date | attendance | module_date
    delivery_method: str | None = None
    wide_modules: bool = False  # CHW: one date column per module, titles in the row above the header

    @property
    def is_csv(self) -> bool:
        return self.path.lower().endswith(".csv")

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(self.columns.values()) + self.extra_required


CHW_DATE_SUFFIX = "Start date (YYYY/MM/DD)"
# Module 1's header has a space after the dash in the source template; kept verbatim.
CHW_MODULE_DATE_COLUMNS = tuple(f"{n}{'- ' if n == 1 else '-'}{CHW_DATE_SUFFIX}" for n in range(1, 16))


def _chw(key: str, path: str) -> Source:
    return Source(
        key=key, path=f"Community Health Worker Completions/{path}", sheets=("Training Attendance",), header_row=2,
        columns={
            "id_number": "ID number", "persal_number": "Employee number", "email": "Email Address",
            "profession_raw": "Profession", "district_raw": "District",
            "sub_district_raw": "Sub-structure/sub-district", "facility_raw": "Facility",
            "facility_other_raw": "If facility name not on list, please provide details (Facility name/work location)",
        },
        extra_required=CHW_MODULE_DATE_COLUMNS, completion_rule="module_date", delivery_method="CHW programme",
        wide_modules=True,
    )


TRAINING_SOURCES: tuple[Source, ...] = (
    Source(
        key="online_school_2025_12_17", path="Online Data Export 17 Dec_SCRUBBED.csv",
        columns={
            "trainee_number": "Trainee Number", "id_number": "ID Number", "persal_number": "PERSAL", "email": "Email",
            "course_name_raw": "Course Name", "approval_date_raw": "Earliest Approval Date",
            "start_date_raw": "Start Date", "end_date_raw": "End Date", "profession_raw": "Profession",
            "district_raw": "District", "sub_district_raw": "sub-District",
            "facility_raw": "Location Name", "facility_other_raw": "Other Location",
        },
        date_format="%d %B %Y", completion_rule="end_date", delivery_method="Online",
    ),
    Source(
        key="inperson_webinar_2026_01_27", path="Capturing Tool_V1c V2-27 January 2026_SCRUBBED.xlsx",
        sheets=("Capturer1", "Capturer2", "Capturer3"),
        columns={
            "id_number": "ID number", "persal_number": "Persal/Employee number", "email": "Email Address",
            "course_name_raw": "Course Name", "delivery_method_raw": "Method of Delivery (Training modality)",
            "start_date_raw": "Start date (YYYY/MM/DD)", "end_date_raw": "End date (YYYY/MM/DD)",
            "booking_status_raw": "Booking/enrollment status", "attendance_status_raw": "Attendance status",
            "profession_raw": "Profession", "professional_category_raw": "Professional Category",
            "district_raw": "District", "sub_district_raw": "Sub-structure/ sub-district", "facility_raw": "Facility",
            "facility_other_raw": "If Facility not on the list, please provide details (Facility name/work location)",
        },
        completion_rule="attendance",
    ),
    _chw("chw_west_coast_2025_10", "CHW Training Attendance-West Coast-Oct 2025_SCRUBBED.xlsx"),
    _chw("chw_kess_2025_12", "CHW Training Attendance_KESS_December 2025_SCRUBBED.xlsx"),
    _chw("chw_witzenberg_2025_07_12", "WITZENBERG - July-Dec 2025_SCRUBBED.xlsx"),
)

LOOKUP_COURSES = Source(
    key="lookup_courses", path="Course and Facility Look Ups.xlsx", sheets=("LU_Courses",),
    extra_required=("CourseName", "CourseCode", "CourseGroup", "CourseStatus"),
)
LOOKUP_FACILITIES = Source(
    key="lookup_facilities", path="Course and Facility Look Ups.xlsx", sheets=("LU_Facility",),
    extra_required=("FacilityName", "FacilityReportingName", "FacilityCode", "HealthDistrict", "HealthSubdistrict"),
)
ALL_SOURCES = TRAINING_SOURCES + (LOOKUP_COURSES, LOOKUP_FACILITIES)
