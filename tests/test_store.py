import json
import tempfile
import unittest
from pathlib import Path

from inflammation_claims.errors import ConcurrencyConflict, ContractViolation, IdempotencyConflict
from inflammation_claims.store import EventStore

from scenarios import build_registry, freeze_default_cohort


def _event(event_id="evt-1", version=1, **overrides):
    event = {
        "event_id": event_id,
        "event_type": "COHORT_FROZEN",
        "aggregate_type": "cohort_snapshot",
        "aggregate_id": "study-x",
        "occurred_at": "2026-09-25T10:00:00+08:00",
        "version": version,
        "payload": {},
    }
    event.update(overrides)
    return event


class EventStoreTests(unittest.TestCase):
    def test_append_validates_contract(self):
        store = EventStore()
        with self.assertRaises(ContractViolation):
            store.append(_event(occurred_at="2026-09-25T10:00:00"))

    def test_same_event_id_same_body_is_idempotent(self):
        store = EventStore()
        first = store.append(_event())
        second = store.append(_event(occurred_at="2026-09-25T11:00:00+08:00"))
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, len(store.all_events()))

    def test_same_event_id_different_body_conflicts(self):
        store = EventStore()
        store.append(_event())
        with self.assertRaises(IdempotencyConflict):
            store.append(_event(payload={"slice_hash": "changed"}))

    def test_version_must_be_continuous(self):
        store = EventStore()
        store.append(_event(version=1))
        with self.assertRaises(ConcurrencyConflict):
            store.append(_event(event_id="evt-2", version=3))

    def test_expected_version_optimistic_concurrency(self):
        store = EventStore()
        store.append(_event(version=1))
        with self.assertRaises(ConcurrencyConflict):
            store.append(_event(event_id="evt-2", version=2), expected_version=0)

    def test_jsonl_log_is_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            registry = build_registry(path)
            freeze_default_cohort(registry, "study-persist")
            del registry
            replayed = build_registry(path)
            self.assertIn("study-persist", replayed.cohorts)
            self.assertEqual(1, replayed.store.version("cohort_snapshot", "study-persist"))
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len(lines))


if __name__ == "__main__":
    unittest.main()
