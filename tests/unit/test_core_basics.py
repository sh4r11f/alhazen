"""Clock, rng discipline, event schema/bus, and the trial vocabulary."""

from __future__ import annotations

import pytest

from alhazen.core.clock import MonotonicClock
from alhazen.core.events import RESERVED_EVENTS, Event, EventBus, EventSchema
from alhazen.core.rng import STREAMS, resolve_seed, spawn_streams
from alhazen.core.trial import ABORTED, PAUSED, CircleRegion, TrialContext, outcomes
from alhazen.testing import EventCollector, FakeClock
from support import SCREEN


class TestClock:
    def test_monotonic_starts_near_zero(self):
        clock = MonotonicClock()
        assert 0 <= clock.now() < 1.0

    def test_fake_clock_advances(self):
        clock = FakeClock()
        clock.advance(2.5)
        assert clock.now() == 2.5


class TestRng:
    def test_resolve_seed_respects_given(self):
        assert resolve_seed(42) == 42

    def test_resolve_seed_generates_concrete(self):
        seed = resolve_seed(None)
        assert isinstance(seed, int)

    def test_streams_reproducible_and_independent(self):
        a, b = spawn_streams(123), spawn_streams(123)
        for name in STREAMS:
            assert a[name].random() == b[name].random()
        c = spawn_streams(123)
        draws = {name: c[name].random() for name in STREAMS}
        assert len(set(draws.values())) == len(STREAMS)


class TestEventSchema:
    def test_reserved_plus_declared(self):
        schema = EventSchema(("STIM_ON",))
        assert schema.all_names == RESERVED_EVENTS | {"STIM_ON"}
        assert schema.validate("STIM_ON") == "STIM_ON"
        assert schema.validate("TRIAL_START") == "TRIAL_START"

    def test_undeclared_event_rejected(self):
        schema = EventSchema(())
        with pytest.raises(ValueError, match="never declared"):
            schema.validate("STIM_ON")

    @pytest.mark.parametrize("bad", ["stim_on", "Stim_On", "1STIM", "STIM ON"])
    def test_bad_names_rejected(self, bad):
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            EventSchema((bad,))

    def test_reserved_names_rejected(self):
        with pytest.raises(ValueError, match="reserved"):
            EventSchema(("TRIAL_START",))

    def test_duplicates_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            EventSchema(("A_ON", "A_ON"))


class TestEventBus:
    def test_delivery_in_subscription_order(self):
        bus = EventBus()
        order = []
        bus.subscribe(lambda e: order.append("first"))
        bus.subscribe(lambda e: order.append("second"))
        bus.emit(Event(name="TRIAL_START", t=0.0, trial_index=1))
        assert order == ["first", "second"]

    def test_subscriber_errors_propagate(self):
        bus = EventBus()

        def broken(event):
            raise RuntimeError("sync line down")

        bus.subscribe(broken)
        with pytest.raises(RuntimeError, match="sync line down"):
            bus.emit(Event(name="TRIAL_START", t=0.0, trial_index=1))


class TestOutcomes:
    def test_declared_plus_reserved(self):
        outs = outcomes(
            CORRECT=dict(completed=True, success=True),
            FIX_BREAK=dict(completed=False),
        )
        assert outs.CORRECT.completed and outs.CORRECT.success
        assert not outs.FIX_BREAK.completed and outs.FIX_BREAK.success is None
        assert outs["PAUSED"] is PAUSED
        assert outs["ABORTED"] is ABORTED
        assert outs.names == {"CORRECT", "FIX_BREAK", "PAUSED", "ABORTED"}

    def test_reserved_names_rejected(self):
        with pytest.raises(ValueError, match="reserved"):
            outcomes(PAUSED=dict(completed=False))

    def test_missing_completed_rejected(self):
        with pytest.raises(ValueError, match="completed"):
            outcomes(CORRECT=dict(success=True))

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError, match="unknown keys"):
            outcomes(CORRECT=dict(completed=True, reward=2))

    def test_bad_name_rejected(self):
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            outcomes(correct=dict(completed=True))


class TestRegionsAndContext:
    def test_none_gaze_is_outside_every_region(self):
        region = CircleRegion(center=(0.0, 0.0), radius=100.0)
        assert not region.contains(None)
        assert region.contains((50.0, 50.0))
        assert not region.contains((100.0, 100.0))

    def test_emit_on_flip_queues_without_emitting(self):
        collector = EventCollector()
        ctx = TrialContext(
            clock=FakeClock(),
            screen=SCREEN,
            rng=None,  # type: ignore[arg-type]
            trial_index=1,
            params={},
        )
        ctx.emit_on_flip("STIM_ON", {"x": 1})
        assert ctx.pending_flip_events == [("STIM_ON", {"x": 1})]
        assert collector.events == []
