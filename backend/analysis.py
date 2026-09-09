# Tab 2 backend: offline test-data analysis.
#
# Compares NW rig test logs against benchmark ("tito") reference data.
# It detects steady-state points in the rig log, then for every
# benchmark RPM point LINEARLY INTERPOLATES the tested rig's
# thrust/torque at that exact RPM rather than picking the nearest
# measured point — so both sides are compared at the same RPM.
#
# Also renders matplotlib charts (base64 PNG, reused for the browser
# preview and the Excel export) and builds the workbook.
#
# Entirely separate from main.py's serial code — no effect on Tab 1.

import io
import re
import json
import base64

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage

from constants import DIR_CCW, DIR_CW

router = APIRouter(prefix="/analysis", tags=["analysis"])

G = 9.80665                      # 1 kgf = 9.80665 N

EXCEL_EXTENSIONS = (".xlsx", ".xls")
STABLE_TRUE_VALUES = ("true", "1", "yes")

MIN_USABLE_RPM = 50              # Below this the rig is idling, not producing data
SEGMENT_RPM_JUMP = 30            # An rpm step larger than this starts a new plateau
MIN_PLATEAU_SAMPLES = 5          # A sustained plateau, not a transient spike
MIN_PLATEAUS_TO_PREFER = 3       # Below this, fall back to every grouped point
MIN_INTERP_POINTS = 2

# A benchmark thrust column with no unit in its header: these rigs
# report either grams-force (values in the thousands) or already-newtons
# (tens to low hundreds).
GF_MAGNITUDE_THRESHOLD = 500

METRIC_THRUST = "thrust"
METRIC_TORQUE = "torque"

EXPORT_FILENAME = "NWA-Bench-Console_Analysis.xlsx"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Percent-error shading thresholds, in whole percent
PCT_GOOD = 10
PCT_MARGINAL = 25


# ================================================================
# FILE READING + COLUMN AUTO-DETECTION
#
# Different rigs name their columns differently ("rpm" vs
# "Motor_Optical_Speed_RPM" vs "Powertrain 1 - rotation speed (rpm)"),
# so columns are keyword-matched rather than pinned to one schema.
# ================================================================

def _find_col(columns, keywords):
    for c in columns:
        low = str(c).lower()
        for kw in keywords:
            if kw in low:
                return c
    return None


def _read_any(filename: str, raw: bytes) -> pd.DataFrame:
    buf = io.BytesIO(raw)
    if filename.lower().endswith(EXCEL_EXTENSIONS):
        return pd.read_excel(buf)
    return pd.read_csv(buf)


def parse_tested(filename: str, raw: bytes) -> pd.DataFrame:
    """An NW rig log as steady-state (rpm, torque_Nm, thrust_N) points,
    ramp-up leg only, sorted by rpm."""
    df = _read_any(filename, raw)

    rpm_col = _find_col(df.columns, ["rpm"])
    torque_col = _find_col(df.columns, ["torque"])
    thrust_col = _find_col(df.columns, ["thrust"])
    stable_col = _find_col(df.columns, ["stable"])

    if not (rpm_col and torque_col and thrust_col):
        raise ValueError(
            f"'{filename}': couldn't find rpm/torque/thrust columns "
            f"(saw: {list(df.columns)})"
        )

    df = df.rename(columns={rpm_col: "rpm", torque_col: "torque_Nm", thrust_col: "thrust_N"})

    if stable_col:
        df = df[df[stable_col].astype(str).str.strip().str.lower().isin(STABLE_TRUE_VALUES)]

    # Coerced and WRITTEN BACK. Only the rpm filter used to be coerced,
    # inside the mask expression, so a file whose numbers arrive as
    # strings (quoted CSV fields, a stray unit suffix, an Excel column
    # typed as text) passed the row filter and then blew up on
    # df["rpm"].round(-1), or produced a string-concatenation "mean".
    df = df.copy()
    for col in ("rpm", "torque_Nm", "thrust_N"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["rpm", "torque_Nm", "thrust_N"])

    df = df[df["rpm"] > MIN_USABLE_RPM].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"'{filename}': no usable rows found (rpm > {MIN_USABLE_RPM})")

    # Consecutive samples group into plateaus, split wherever rpm jumps.
    r = df["rpm"].round(-1)
    df["grp"] = (r.diff().abs() > SEGMENT_RPM_JUMP).cumsum()
    g = (
        df.groupby("grp")
        .agg(n=("rpm", "size"), rpm=("rpm", "mean"),
             torque_Nm=("torque_Nm", "mean"), thrust_N=("thrust_N", "mean"))
        .reset_index(drop=True)
    )

    # Sustained plateaus are preferred when the log is dense enough to
    # have them — that's what filters out transient spikes. Sparser
    # continuous-ramp logs rarely form them, hence the fallback.
    plateau = g[g["n"] >= MIN_PLATEAU_SAMPLES].reset_index(drop=True)
    g_use = plateau if len(plateau) >= MIN_PLATEAUS_TO_PREFER else g

    peak_idx = g_use["rpm"].idxmax()
    ramp_up = g_use.loc[:peak_idx].sort_values("rpm").reset_index(drop=True)

    # Interpolation needs strictly-increasing, de-duplicated x-values.
    # Repeated rpm bins are rare but happen at very low throttle.
    ramp_up = (
        ramp_up.groupby("rpm", as_index=False)
        .agg(torque_Nm=("torque_Nm", "mean"), thrust_N=("thrust_N", "mean"))
        .sort_values("rpm")
        .reset_index(drop=True)
    )

    if ramp_up.empty or len(ramp_up) < MIN_INTERP_POINTS:
        raise ValueError(f"'{filename}': not enough ramp-up points to interpolate against")
    return ramp_up


def parse_benchmark(filename: str, raw: bytes) -> pd.DataFrame:
    """A benchmark ("tito") reference file as (rpm, torque_Nm, thrust_N,
    thrust_gf), auto-detecting columns and thrust units."""
    df = _read_any(filename, raw)

    rpm_col = _find_col(df.columns, ["rpm", "speed"])
    torque_col = _find_col(df.columns, ["torque"])
    thrust_col = _find_col(df.columns, ["thrust", "force"])

    if not (rpm_col and torque_col and thrust_col):
        raise ValueError(
            f"'{filename}': couldn't find rpm/torque/thrust columns "
            f"(saw: {list(df.columns)})"
        )

    df = df.rename(columns={rpm_col: "rpm", torque_col: "torque_Nm", thrust_col: "thrust_raw"})
    df["rpm"] = pd.to_numeric(df["rpm"], errors="coerce")
    df["torque_Nm"] = pd.to_numeric(df["torque_Nm"], errors="coerce")
    df["thrust_raw"] = pd.to_numeric(df["thrust_raw"], errors="coerce")
    df = df[df["rpm"] > 0].dropna(subset=["rpm", "torque_Nm", "thrust_raw"])
    df = df.sort_values("rpm").reset_index(drop=True)
    if df.empty:
        raise ValueError(f"'{filename}': no usable rows found (rpm > 0)")

    low = thrust_col.lower()
    if "kgf" in low:
        df["thrust_N"] = df["thrust_raw"] * G
        df["thrust_gf"] = df["thrust_raw"] * 1000.0
    elif "gf" in low:
        df["thrust_N"] = df["thrust_raw"] * G / 1000.0
        df["thrust_gf"] = df["thrust_raw"]
    else:
        # No unit in the column header — infer it from the magnitude.
        if df["thrust_raw"].max() > GF_MAGNITUDE_THRESHOLD:
            df["thrust_N"] = df["thrust_raw"] * G / 1000.0
            df["thrust_gf"] = df["thrust_raw"]
        else:
            df["thrust_N"] = df["thrust_raw"]
            df["thrust_gf"] = df["thrust_raw"] * 1000.0 / G

    return df


# ================================================================
# MATCHING + TABLE
#
# Each benchmark RPM point is matched by interpolating the tested rig's
# torque/thrust AT THAT EXACT RPM from the two bracketing tested points,
# rather than picking whichever tested point is nearest. That removes
# RPM mismatch as a source of apparent error, so a reported % error is
# real deviation and not "we weren't measured at quite the same RPM".
#
# Where tito's RPM falls outside the tested range there is nothing to
# interpolate between: those rows are flagged out-of-range and report
# the closest achieved boundary value rather than extrapolating a linear
# trend past real data.
# ================================================================

def interp_match(tested: pd.DataFrame, target_rpm: float):
    rpm_arr = tested["rpm"].to_numpy()
    torque_arr = tested["torque_Nm"].to_numpy()
    thrust_arr = tested["thrust_N"].to_numpy()

    lo, hi = float(rpm_arr.min()), float(rpm_arr.max())
    in_range = lo <= target_rpm <= hi

    # np.interp clamps to the boundary value outside the known x-range —
    # exactly the "closest achieved point" behaviour out-of-range rows
    # want, with no fabricated extrapolation.
    t_torque = float(np.interp(target_rpm, rpm_arr, torque_arr))
    t_thrust = float(np.interp(target_rpm, rpm_arr, thrust_arr))
    matched_rpm = target_rpm if in_range else (lo if target_rpm < lo else hi)

    return matched_rpm, t_torque, t_thrust, in_range


def _r(value, digits):
    """Round for JSON output, mapping NaN/inf to None.

    json.dumps() emits bare NaN / Infinity tokens, which are NOT valid
    JSON — JSON.parse() rejects the whole response, so one
    divide-by-zero row would blank the entire analysis tab rather than
    one cell. None serialises to null, which the frontend renders as an
    empty cell.
    """
    if value is None:
        return None
    v = float(value)
    if not np.isfinite(v):
        return None
    return round(v, digits)


def build_table(tito: pd.DataFrame, tested: pd.DataFrame):
    rows = []
    for _, r in tito.iterrows():
        rpm_ref = float(r["rpm"])
        t_rpm, t_torque, t_thrust, in_range = interp_match(tested, rpm_ref)

        # 0 by construction for in-range rows, since the interpolation is
        # exactly at tito's RPM. For out-of-range rows it says how far
        # the closest achievable point falls short.
        rpm_err_pct = (t_rpm - rpm_ref) / rpm_ref * 100 if rpm_ref else np.nan

        thrust_ref = float(r["thrust_N"])
        thrust_err_pct = (t_thrust - thrust_ref) / thrust_ref * 100 if thrust_ref else np.nan
        torque_ref = float(r["torque_Nm"])
        torque_dev = t_torque - torque_ref
        torque_err_pct = torque_dev / torque_ref * 100 if torque_ref else np.nan

        rows.append({
            "tito_rpm": _r(rpm_ref, 1),
            "tested_rpm": _r(t_rpm, 1),
            "rpm_pct_error": _r(rpm_err_pct, 2),
            "tito_thrust_N": _r(thrust_ref, 3),
            "tito_thrust_gf": _r(r["thrust_gf"], 1),
            "tested_thrust_N": _r(t_thrust, 3),
            "thrust_pct_error": _r(thrust_err_pct, 2),
            "tito_torque_Nm": _r(torque_ref, 4),
            "tested_torque_Nm": _r(t_torque, 4),
            "torque_deviation_Nm": _r(torque_dev, 4),
            "torque_pct_error": _r(torque_err_pct, 2),
            "in_range": bool(in_range),
        })
    return rows


def guess_label(test_name: str, bench_name: str) -> str:
    m = re.search(r"(2[0-9]|3[0-9])\s*[\-_ ]?\s*(?:in|inch)?", test_name)
    size = m.group(1) if m else None
    direction = None
    for name in (test_name, bench_name):
        if re.search(r"ccw", name, re.I):
            direction = DIR_CCW
            break
        if re.search(r"\bcw\b", name, re.I):
            direction = DIR_CW
            break
    parts = []
    if size:
        parts.append(f'{size}"')
    if direction:
        parts.append(direction)
    return " ".join(parts) if parts else test_name


# ================================================================
# CHART RENDERING — reused for the browser preview and the Excel export
# ================================================================

TITO_COLOR = "#1F4E78"
TEST_COLOR = "#D9534F"


def render_chart_png(tito: pd.DataFrame, tested: pd.DataFrame, metric: str, title: str) -> bytes:
    ycol = "thrust_N" if metric == METRIC_THRUST else "torque_Nm"
    ylabel = "Thrust (N)" if metric == METRIC_THRUST else "Torque (Nm)"

    fig, ax = plt.subplots(figsize=(6.2, 4.2))
    ax.plot(tito["rpm"], tito[ycol], "-o", color=TITO_COLOR, label="Benchmark (tito)", linewidth=2, markersize=4)
    ax.plot(tested["rpm"], tested[ycol], "-o", color=TEST_COLOR, label="Tested (NW rig)", linewidth=2, markersize=4)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("RPM")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def _compute_pairs(test_files, benchmark_files, pairs_json: str):
    """Parse the uploaded files and the pairing spec into results."""
    pair_list = json.loads(pairs_json)  # [{test_index, benchmark_index, label?}, ...]

    test_bytes = [await f.read() for f in test_files]
    bench_bytes = [await f.read() for f in benchmark_files]

    results = []
    for p in pair_list:
        ti, bi = p["test_index"], p["benchmark_index"]

        # More than defensive tidiness: the frontend uses -1 as its "no
        # benchmark matched" sentinel, and Python's negative indexing
        # would silently select the LAST uploaded benchmark file and
        # report a confident comparison against the wrong propeller.
        if not (0 <= ti < len(test_files)) or not (0 <= bi < len(benchmark_files)):
            results.append({
                "label": p.get("label") or f"pair {ti}->{bi}",
                "test_filename": "", "benchmark_filename": "",
                "rows": [], "summary": None,
                "thrust_chart_png": None, "torque_chart_png": None,
                "error": (f"Invalid file pairing (test #{ti}, benchmark #{bi}) — "
                          f"got {len(test_files)} test and {len(benchmark_files)} benchmark files"),
            })
            continue

        t_name = test_files[ti].filename
        b_name = benchmark_files[bi].filename
        label = p.get("label") or guess_label(t_name, b_name)

        entry = {"label": label, "test_filename": t_name, "benchmark_filename": b_name}
        try:
            tested = parse_tested(t_name, test_bytes[ti])
            tito = parse_benchmark(b_name, bench_bytes[bi])
            rows = build_table(tito, tested)
            in_range_rows = [r for r in rows if r["in_range"]]

            # A reference point at zero thrust/torque divides by zero;
            # _r() maps those to None, and abs(None) would raise.
            thrust_errs = [abs(r["thrust_pct_error"]) for r in in_range_rows
                           if r["thrust_pct_error"] is not None]
            torque_errs = [abs(r["torque_pct_error"]) for r in in_range_rows
                           if r["torque_pct_error"] is not None]

            summary = {
                "points_compared": len(in_range_rows),
                "avg_thrust_pct_error": (
                    round(float(np.mean(thrust_errs)), 2) if thrust_errs else None
                ),
                "avg_torque_pct_error": (
                    round(float(np.mean(torque_errs)), 2) if torque_errs else None
                ),
                "max_tested_rpm": round(float(tested["rpm"].max()), 1),
                "max_tito_rpm": round(float(tito["rpm"].max()), 1),
            }

            thrust_png = base64.b64encode(
                render_chart_png(tito, tested, METRIC_THRUST, f"{label} — Thrust vs RPM")
            ).decode("ascii")
            torque_png = base64.b64encode(
                render_chart_png(tito, tested, METRIC_TORQUE, f"{label} — Torque vs RPM")
            ).decode("ascii")

            entry.update({
                "rows": rows,
                "summary": summary,
                "thrust_chart_png": thrust_png,
                "torque_chart_png": torque_png,
                "error": None,
            })
        except Exception as e:
            entry.update({"rows": [], "summary": None,
                          "thrust_chart_png": None, "torque_chart_png": None,
                          "error": str(e)})
        results.append(entry)

    return results


@router.post("/run")
async def run_analysis(
    test_files: list[UploadFile] = File(...),
    benchmark_files: list[UploadFile] = File(...),
    pairs: str = Form(...),
):
    results = await _compute_pairs(test_files, benchmark_files, pairs)
    return {"pairs": results}


# ── Excel export: one sheet per pair, plus a sheet of charts ──────
FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT, bold=True, size=13, color="1F4E78")
SUB_FONT = Font(name=FONT, italic=True, size=9, color="666666")
CELL_FONT = Font(name=FONT, size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

GREEN = PatternFill("solid", fgColor="C6EFCE")
YELLOW = PatternFill("solid", fgColor="FFEB9C")
RED = PatternFill("solid", fgColor="FFC7CE")


def _err_fill(v):
    if v is None:
        return None
    av = abs(v)
    if av <= PCT_GOOD:
        return GREEN
    elif av <= PCT_MARGINAL:
        return YELLOW
    return RED


COL_HEADERS = ["Tito RPM", "Tested RPM\n(interpolated at)", "RPM\n% Error",
               "Tito Thrust\n(N)", "Tito Thrust\n(gf)", "Tested Thrust\n(N)", "Thrust\n% Error",
               "Tito Torque\n(Nm)", "Tested Torque\n(Nm)", "Torque\nDeviation (Nm)", "Torque\n% Error",
               "Within Tested\nRPM Range"]

ROW_KEYS = ["tito_rpm", "tested_rpm", "rpm_pct_error", "tito_thrust_N", "tito_thrust_gf",
            "tested_thrust_N", "thrust_pct_error", "tito_torque_Nm", "tested_torque_Nm",
            "torque_deviation_Nm", "torque_pct_error", "in_range"]

PCT_KEYS = {"rpm_pct_error", "thrust_pct_error", "torque_pct_error"}

MAX_SHEET_NAME_LEN = 31          # Excel's own limit
CHART_ROW_SPACING = 24           # Rows between stacked chart images


def _sheet_name(label: str, used: set) -> str:
    """An Excel-legal, unique sheet name: ≤31 chars, none of []:*?/\\"""
    clean = re.sub(r'[\[\]\:\*\?/\\]', "", label).strip() or "Sheet"
    clean = clean[:MAX_SHEET_NAME_LEN]
    base, i = clean, 1
    while clean in used:
        suffix = f" ({i})"
        clean = base[: MAX_SHEET_NAME_LEN - len(suffix)] + suffix
        i += 1
    used.add(clean)
    return clean


def build_workbook(results) -> io.BytesIO:
    wb = Workbook()
    wb.remove(wb.active)
    used_names = set()

    for entry in results:
        if entry.get("error"):
            ws = wb.create_sheet(_sheet_name(entry["label"], used_names))
            ws["A1"] = f'{entry["label"]} — could not be analysed'
            ws["A1"].font = TITLE_FONT
            ws["A2"] = entry["error"]
            ws["A2"].font = SUB_FONT
            continue

        ws = wb.create_sheet(_sheet_name(entry["label"], used_names))
        ws.merge_cells("A1:L1")
        ws["A1"] = f'{entry["label"]} — Tested vs Benchmark'
        ws["A1"].font = TITLE_FONT
        ws.merge_cells("A2:L2")
        ws["A2"] = (f'Test file: {entry["test_filename"]}  |  Benchmark file: {entry["benchmark_filename"]}  |  '
                    "Tested values are linearly interpolated at each Tito RPM; out-of-range rows show the "
                    "closest achieved test point instead.")
        ws["A2"].font = SUB_FONT

        header_row = 4
        for j, h in enumerate(COL_HEADERS, start=1):
            c = ws.cell(row=header_row, column=j, value=h)
            c.font = HEADER_FONT
            c.fill = HEADER_FILL
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = BORDER
        ws.row_dimensions[header_row].height = 30

        for i, row in enumerate(entry["rows"]):
            r = header_row + 1 + i
            for j, key in enumerate(ROW_KEYS, start=1):
                val = row[key]
                if key == "in_range":
                    val = "Yes" if val else "No (extrapolated match)"
                c = ws.cell(row=r, column=j, value=val)
                c.font = CELL_FONT
                c.border = BORDER
                c.alignment = Alignment(horizontal="center")
                if key in PCT_KEYS:
                    c.number_format = "0.00"
                    fill = _err_fill(row[key])
                    if fill:
                        c.fill = fill
                elif key in ("tito_thrust_N", "tested_thrust_N", "tito_torque_Nm",
                             "tested_torque_Nm", "torque_deviation_Nm"):
                    c.number_format = "0.000"
                elif key == "tito_thrust_gf":
                    c.number_format = "0.0"

        widths = [10, 14, 9, 11, 11, 11, 9, 11, 12, 13, 10, 14]
        for j, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = "A5"

    ok_results = [e for e in results if not e.get("error")]
    if ok_results:
        ws = wb.create_sheet("Charts")
        ws["A1"] = "Thrust & Torque vs RPM — Tested vs Benchmark"
        ws["A1"].font = TITLE_FONT
        row_cursor = 3
        for entry in ok_results:
            ws.cell(row=row_cursor, column=1, value=entry["label"]).font = Font(name=FONT, bold=True, size=11)
            row_cursor += 1
            thrust_img = XLImage(io.BytesIO(base64.b64decode(entry["thrust_chart_png"])))
            torque_img = XLImage(io.BytesIO(base64.b64decode(entry["torque_chart_png"])))
            ws.add_image(thrust_img, f"A{row_cursor}")
            ws.add_image(torque_img, f"J{row_cursor}")
            row_cursor += CHART_ROW_SPACING     # Clears the ~4.2in-tall image

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


@router.post("/export-excel")
async def export_excel(
    test_files: list[UploadFile] = File(...),
    benchmark_files: list[UploadFile] = File(...),
    pairs: str = Form(...),
):
    results = await _compute_pairs(test_files, benchmark_files, pairs)
    buffer = build_workbook(results)
    return StreamingResponse(
        buffer,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"'},
    )