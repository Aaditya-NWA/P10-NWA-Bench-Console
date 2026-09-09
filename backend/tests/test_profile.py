#  Throttle profile sequencer — the safety-critical part of the app.
#  These cover the three properties main.py documents as deliberate:
#    * ramps are driven by WALL-CLOCK time, not tick counting
#    * the `finally` cut to THR_MIN runs on EVERY exit path
#    * a manual throttle command takes over without being stomped
#  All three are invisible in normal operation and would break
#  silently in a refactor — which is exactly why they're pinned here.

import asyncio

import pytest

import main
from conftest import FakeSerial

# Captured before any patching. The fixture below replaces
# asyncio.sleep module-wide, so a fake sleep that yields via
# asyncio.sleep(0) would otherwise call itself forever.
_real_sleep = asyncio.sleep


class FakeClock:
    """Controllable monotonic clock plus a sleep that advances it.

    The ramp is time-driven, so with real time a 10-second ramp would
    take ten seconds to test. Advancing a fake clock inside the patched
    sleep lets the same code run to completion instantly, while still
    exercising the real elapsed-time arithmetic rather than a stub.
    """

    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float):
        self.t += seconds
        await _real_sleep(0)            # Yield so cancellation still lands


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(main.time, "monotonic", c.monotonic)
    monkeypatch.setattr(main.asyncio, "sleep", c.sleep)
    return c


def test_ramp_reaches_target_and_takes_the_right_wall_clock_time(clock, fake_serial):
    """A 1000→2000µs ramp at 100µs/s must take 10 s and end exactly on target."""
    started = clock.t
    result = asyncio.run(main._ramp_to(2000, ramp_rate=100.0, from_us=1000))

    assert result == 2000
    assert fake_serial.throttle_commands[-1] == 2000
    elapsed = clock.t - started
    # One tick of slack: the loop can only notice it has arrived on a
    # tick boundary.
    assert 10.0 <= elapsed <= 10.0 + main.PROFILE_TICK_S * 2


def test_ramp_is_monotonic_and_never_overshoots(clock, fake_serial):
    asyncio.run(main._ramp_to(1800, ramp_rate=200.0, from_us=1200))
    commands = fake_serial.throttle_commands

    assert commands == sorted(commands), "ramp must not move backwards"
    assert max(commands) == 1800, "ramp must not overshoot its target"
    assert commands[0] >= 1200


def test_ramp_down_is_monotonic(clock, fake_serial):
    asyncio.run(main._ramp_to(1100, ramp_rate=300.0, from_us=1900))
    commands = fake_serial.throttle_commands

    assert commands == sorted(commands, reverse=True)
    assert min(commands) == 1100


def test_ramp_is_driven_by_elapsed_time_not_tick_count(clock, fake_serial):
    """A stalled event loop must not stretch the ramp.

    This is the property the wall-clock design exists for: if the loop
    is starved mid-ramp, the ramp should still finish at the right
    moment rather than holding the motor at an intermediate throttle
    for as many extra ticks as it lost.
    """
    real_sleep = clock.sleep
    calls = {"n": 0}

    async def stalling_sleep(seconds):
        calls["n"] += 1
        # Simulate the loop being blocked for a full second on one tick.
        await real_sleep(1.0 if calls["n"] == 3 else seconds)

    main.asyncio.sleep = stalling_sleep
    asyncio.run(main._ramp_to(2000, ramp_rate=100.0, from_us=1000))

    commands = fake_serial.throttle_commands
    assert commands[-1] == 2000
    # A tick-counting ramp would have issued ~200 commands; an
    # elapsed-time one skips ahead through the stall instead.
    assert len(commands) < 200


def test_ramp_raises_when_the_link_drops(clock):
    main.serial_conn = FakeSerial(fail_writes=True)
    main.serial_status.update(connected=True, port="TEST")

    with pytest.raises(RuntimeError, match="serial link lost"):
        asyncio.run(main._ramp_to(1500, ramp_rate=100.0, from_us=1000))


def test_zero_length_ramp_is_a_no_op(clock, fake_serial):
    assert asyncio.run(main._ramp_to(1400, ramp_rate=100.0, from_us=1400)) == 1400
    assert fake_serial.throttle_commands == []


def test_completed_profile_ends_at_idle(clock, fake_serial):
    steps = [{"us": 1400, "hold_s": 2.0}]
    asyncio.run(main._run_profile(steps, ramp_rate=400.0, operator="testop"))

    assert main.profile_state["phase"] == "done"
    assert main.profile_state["running"] is False
    assert fake_serial.throttle_commands[-1] == main.THR_MIN


def test_profile_cuts_to_idle_when_a_step_raises(clock, fake_serial, monkeypatch):
    """An unexpected exception mid-profile must still drop the throttle."""
    async def boom(*args, **kwargs):
        raise ValueError("something went wrong mid-ramp")

    monkeypatch.setattr(main, "_ramp_to", boom)
    asyncio.run(main._run_profile([{"us": 1500, "hold_s": 1.0}], 400.0, "testop"))

    assert main.profile_state["phase"] == "error"
    assert main.profile_state["running"] is False
    assert fake_serial.throttle_commands[-1] == main.THR_MIN


def test_profile_cuts_to_idle_when_the_serial_link_drops(clock):
    conn = FakeSerial()
    main.serial_conn = conn
    main.serial_status.update(connected=True, port="TEST")

    async def scenario():
        task = asyncio.ensure_future(
            main._run_profile([{"us": 2000, "hold_s": 5.0}], 50.0, "testop"))
        for _ in range(5):
            await _real_sleep(0)
        conn.fail_writes = True          # Cable yanked mid-ramp
        await task

    asyncio.run(scenario())

    assert main.profile_state["phase"] == "error"
    assert main.profile_state["running"] is False


def test_operator_stop_cuts_to_idle(clock, fake_serial):
    async def scenario():
        main.profile_task = asyncio.ensure_future(
            main._run_profile([{"us": 2000, "hold_s": 30.0}], 20.0, "testop"))
        for _ in range(5):
            await _real_sleep(0)
        await main.abort_profile(cut=True)

    asyncio.run(scenario())

    assert main.profile_state["phase"] == "aborted"
    assert main.profile_state["running"] is False
    assert fake_serial.throttle_commands[-1] == main.THR_MIN


def test_takeover_suppresses_the_safety_cut(clock, fake_serial):
    """takeover=True must NOT stomp the value the operator is applying.

    The whole point of the flag: the sequencer's own cut would
    otherwise immediately overwrite the throttle the operator just
    reached for.
    """
    async def scenario():
        main.profile_task = asyncio.ensure_future(
            main._run_profile([{"us": 2000, "hold_s": 30.0}], 20.0, "testop"))
        for _ in range(5):
            await _real_sleep(0)
        await main.abort_profile(takeover=True)

    asyncio.run(scenario())

    assert main.profile_state["phase"] == "aborted"
    assert "Manual throttle took over" in main.profile_state["message"]
    assert fake_serial.throttle_commands[-1] != main.THR_MIN, \
        "takeover must not force the throttle back to idle"


def test_normal_abort_does_cut(clock, fake_serial):
    async def scenario():
        main.profile_task = asyncio.ensure_future(
            main._run_profile([{"us": 2000, "hold_s": 30.0}], 20.0, "testop"))
        for _ in range(5):
            await _real_sleep(0)
        await main.abort_profile(cut=True, takeover=False)

    asyncio.run(scenario())
    assert fake_serial.throttle_commands[-1] == main.THR_MIN


def test_abort_on_no_running_profile_is_harmless(clock):
    asyncio.run(main.abort_profile(cut=True))     # Must not raise


def test_profile_ramps_from_the_current_throttle_not_from_idle(clock, fake_serial):
    """Starting a profile at 1500µs must not snap the ESC to idle first.

    Regression: _run_profile seeded its ramp with a hardcoded THR_MIN,
    so its first write was ~1000µs — an abrupt deceleration nothing in
    the profile asked for, which showed up as a spike in the run's data.
    """
    main.write_throttle_us(1500)                  # Operator was already at half throttle
    fake_serial.writes.clear()

    asyncio.run(main._run_profile([{"us": 1600, "hold_s": 1.0}], 400.0, "testop"))

    commands = fake_serial.throttle_commands
    assert commands, "profile should have commanded something"
    assert commands[0] >= 1500, \
        f"first profile command was {commands[0]}µs — it dropped to idle before ramping"


@pytest.mark.parametrize("payload, expected", [
    ({"steps": []},                                     "At least one step"),
    ({"steps": [{"us": 2500, "hold_s": 1}]},            "outside"),
    ({"steps": [{"us": 900, "hold_s": 1}]},             "outside"),
    ({"steps": [{"us": 1500, "hold_s": 0}]},            "hold time"),
    ({"steps": [{"us": 1500, "hold_s": 1}], "ramp_rate_us_per_s": 0}, "Ramp rate"),
    ({"steps": [{"us": "abc", "hold_s": 1}]},           "numeric"),
])
def test_out_of_range_steps_are_rejected_not_clamped(payload, expected):
    """An operator who typed 2500 meant something — running 2000 would corrupt their test."""
    steps, rate, error = main._validate_profile(payload)
    assert error is not None
    assert expected in error
    assert steps == []


def test_valid_profile_passes_validation():
    steps, rate, error = main._validate_profile(
        {"steps": [{"us": 1200, "hold_s": 5}, {"us": 1400, "hold_s": 3}],
         "ramp_rate_us_per_s": 150})
    assert error is None
    assert steps == [{"us": 1200, "hold_s": 5.0}, {"us": 1400, "hold_s": 3.0}]
    assert rate == 150.0
