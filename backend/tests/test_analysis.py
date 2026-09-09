#  Tab 2 analysis pipeline  (improvements.txt #31)
#  Stateless and hardware-independent, so it needs no fakes at all.
#  The two behaviours worth pinning are the ones a future refactor
#  would most plausibly get wrong:
#    * columns are found by KEYWORD, because different rig exports
#      name them differently ("rpm" vs "Motor_Optical_Speed_RPM" vs
#      "Powertrain 1 - rotation speed (rpm)")
#    * the tested rig is INTERPOLATED at each benchmark RPM rather
#      than nearest-neighbour matched, so both sides are compared at
#      the identical RPM instead of two slightly different ones

import io

import numpy as np
import pandas as pd
import pytest

import analysis


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


@pytest.mark.parametrize("columns", [
    ["rpm", "torque", "thrust"],
    ["RPM", "Torque (N·m)", "Thrust (N)"],                       # This console's own CSV export
    ["Motor_Optical_Speed_RPM", "Motor_Torque_Nm", "Thrust_N"],
    ["Powertrain 1 - rotation speed (rpm)",
     "Powertrain 1 - torque (N·m)",
     "Powertrain 1 - thrust (N)"],
])
def test_rig_log_columns_are_found_whatever_the_export_called_them(columns):
    rows = "\n".join(f"{2000 + i * 500},{0.5 + i * 0.2},{10 + i * 5}" for i in range(12))
    raw = csv_bytes(",".join(columns) + "\n" + rows)

    parsed = analysis.parse_tested("rig.csv", raw)

    assert list(parsed.columns) == ["rpm", "torque_Nm", "thrust_N"]
    assert len(parsed) > 0


def test_a_log_with_no_recognisable_columns_says_so_clearly():
    raw = csv_bytes("alpha,beta,gamma\n1,2,3\n")

    with pytest.raises(ValueError, match="couldn't find rpm/torque/thrust"):
        analysis.parse_tested("mystery.csv", raw)


def test_rows_below_the_rpm_floor_are_dropped():
    """Idle noise near zero rpm isn't a data point."""
    rows = "\n".join(f"{rpm},1.0,20.0" for rpm in [0, 10, 45, 3000, 3500, 4000, 4500])
    raw = csv_bytes("rpm,torque,thrust\n" + rows)

    parsed = analysis.parse_tested("rig.csv", raw)

    assert parsed["rpm"].min() > 50


def test_non_numeric_cells_are_coerced_and_dropped_not_crashed():
    """A rig export with a stray '---' row used to blow up downstream."""
    raw = csv_bytes(
        "rpm,torque,thrust\n"
        "3000,1.0,20.0\n"
        "---,---,---\n"
        "3500,1.2,24.0\n"
        "4000,1.4,28.0\n"
        "4500,1.6,32.0\n"
    )

    parsed = analysis.parse_tested("rig.csv", raw)
    assert parsed["rpm"].notna().all()
    assert len(parsed) > 0


def make_tested(points) -> pd.DataFrame:
    return pd.DataFrame(points, columns=["rpm", "torque_Nm", "thrust_N"])


def test_interpolation_hits_the_exact_value_between_two_samples():
    """A benchmark RPM between two rig samples must be interpolated.

    Nearest-neighbour would return one of the endpoints and quietly
    compare the two sides at different RPMs.
    """
    tested = make_tested([(4000, 1.0, 20.0), (6000, 2.0, 40.0)])

    matched_rpm, torque, thrust, in_range = analysis.interp_match(tested, 5000)

    assert in_range is True
    assert matched_rpm == 5000
    assert torque == pytest.approx(1.5)              # Exactly halfway
    assert thrust == pytest.approx(30.0)
    assert torque not in (1.0, 2.0), "this is nearest-neighbour, not interpolation"


def test_interpolation_returns_the_sample_value_at_an_exact_match():
    tested = make_tested([(4000, 1.0, 20.0), (6000, 2.0, 40.0)])
    _, torque, thrust, in_range = analysis.interp_match(tested, 6000)

    assert in_range is True
    assert torque == pytest.approx(2.0)
    assert thrust == pytest.approx(40.0)


@pytest.mark.parametrize("target, expected_rpm", [(1000, 4000), (9000, 6000)])
def test_out_of_range_targets_clamp_and_are_flagged_rather_than_extrapolated(target, expected_rpm):
    """No fabricated numbers beyond what the rig actually achieved."""
    tested = make_tested([(4000, 1.0, 20.0), (6000, 2.0, 40.0)])

    matched_rpm, torque, thrust, in_range = analysis.interp_match(tested, target)

    assert in_range is False, "an out-of-range point must be flagged"
    assert matched_rpm == expected_rpm
    assert 1.0 <= torque <= 2.0, "clamped to the measured range, not extrapolated"


def test_benchmark_thrust_in_grams_force_is_converted_to_newtons():
    raw = csv_bytes(
        "RPM,Torque (N.m),Thrust (gf)\n"
        "3000,0.5,1000\n4000,0.8,1800\n5000,1.2,2800\n"
    )

    parsed = analysis.parse_benchmark("tito.csv", raw)

    assert "thrust_N" in parsed.columns
    # 1000 gf = 1 kgf = 9.80665 N
    assert parsed["thrust_N"].iloc[0] == pytest.approx(9.80665, rel=1e-3)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_results_become_null_not_invalid_json(value):
    """json.dumps emits bare NaN/Infinity, which JSON.parse rejects.

    One divide-by-zero row would otherwise blank the entire analysis
    tab instead of just that cell.
    """
    assert analysis._r(value, 3) is None


def test_finite_results_round_normally():
    assert analysis._r(1.23456, 2) == 1.23
    assert analysis._r(None, 2) is None


def test_build_table_compares_both_sides_at_the_same_rpm():
    tito = pd.DataFrame({"rpm": [4000, 5000], "torque_Nm": [1.0, 1.5],
                         "thrust_N": [20.0, 30.0], "thrust_gf": [2039.0, 3058.0]})
    tested = make_tested([(3000, 0.5, 10.0), (4000, 1.1, 21.0),
                          (5000, 1.4, 29.0), (6000, 2.0, 40.0)])

    rows = analysis.build_table(tito, tested)

    assert len(rows) == 2
    for row, expected_rpm in zip(rows, [4000, 5000]):
        # Every row must be anchored to the benchmark's own RPM.
        assert expected_rpm in row.values(), f"row is not anchored to {expected_rpm} rpm: {row}"
