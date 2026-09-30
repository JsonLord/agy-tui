"""
P5 failover-state persistence tests — no network, temp dirs only.

Contract coverage (tests/test_agy_failover_state.py):

* persist_failover_events validates EVERY record against the mirrored
  schema rules and appends (existing records preserved); the atomic
  write leaves the merged array as the file's full content and no
  temp file behind;
* every schema violation is rejected with a ValueError naming the
  offending record + field: role outside the enum, primary_attempts
  5, session_id without the ^ses_ prefix, a missing required key and
  an extra key (additionalProperties=false);
* load_failover_events round-trips; merge=False replaces instead of
  appending; a validation failure never touches the file (no partial
  write);
* the MIRRORED_* constants in app.orchestration.agy_failover_state
  match the REAL control-plane schema file (injectable path; skipped
  gracefully when the control plane is absent).
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.orchestration.agy_failover_state import (
    DEFAULT_STATE_PATH,
    MIRRORED_ALLOWED_FIELDS,
    MIRRORED_ADDITIONAL_PROPERTIES,
    MIRRORED_FALLBACK_COUNT_MIN,
    MIRRORED_PRIMARY_ATTEMPTS_RANGE,
    MIRRORED_REQUIRED_FIELDS,
    MIRRORED_ROLE_ENUM,
    MIRRORED_SES_PATTERN,
    SCHEMA_PATH,
    load_failover_events,
    persist_failover_events,
    validate_failover_record,
)


def valid_record(**overrides) -> dict:
    """A schema-valid dispatch failover record (all 9 required keys)."""
    record = {
        "role": "dispatch",
        "primary_model": "primary-dispatch-model",
        "fallback_model": "fallback-dispatch-model",
        "active_model": "fallback-dispatch-model",
        "primary_attempts": 4,
        "fallback_reason": "rate_limit_exhausted",
        "session_id": "ses_valid000000000000001",
        "fallback_count": 1,
        "parent_session_id": None,
    }
    record.update(overrides)
    return record


class TestPersistAndMerge(unittest.TestCase):
    def test_persist_validates_appends_and_preserves_existing(self):
        """merge=True: existing records survive; file == merged array."""
        first = valid_record(session_id="ses_first00000000000001")
        second = valid_record(
            session_id="ses_second0000000000001",
            fallback_reason="provider_overload",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"

            # Bootstrap the file with one record.
            returned = persist_failover_events(path, [first])
            self.assertEqual(returned, path)

            # Append a second one.
            persist_failover_events(path, [second])

            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk, [first, second])
            self.assertEqual(load_failover_events(path), on_disk)
            # Atomic write left no temp file behind.
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_validation_failure_never_writes_partial_file(self):
        """A bad new record leaves the previous file byte-identical."""
        good = valid_record()
        bad = valid_record(role="lead")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            persist_failover_events(path, [good])
            before = path.read_text(encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                persist_failover_events(path, [good, bad])
            self.assertIn("[1]", str(caught.exception))
            self.assertIn("role", str(caught.exception))

            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_optional_schema_fields_are_accepted(self):
        """probe_derived (bool) + ts (str) are allowed properties."""
        record = valid_record(probe_derived=False,
                              ts="2026-09-27T12:00:00+02:00")
        validated = validate_failover_record(record)
        self.assertEqual(validated, record)

    def test_parent_session_id_may_be_a_ses_string(self):
        """parent_session_id: null OR ^ses_ string both validate."""
        parent = "ses_parent0000000000001"
        record = valid_record(parent_session_id=parent)
        self.assertEqual(
            validate_failover_record(record)["parent_session_id"], parent
        )
        with self.assertRaises(ValueError) as caught:
            validate_failover_record(
                valid_record(parent_session_id="s1_parent"), index=7
            )
        self.assertIn("[7]", str(caught.exception))
        self.assertIn("parent_session_id", str(caught.exception))


class TestSchemaViolations(unittest.TestCase):
    def test_each_schema_violation_names_the_offending_field(self):
        """Parametrized per contract: 5 violation classes (+extras)."""
        violations = {
            "role_not_in_enum": (
                valid_record(role="lead"),
                "[0] field 'role'",
            ),
            "role_supervisor_enum_ok_signal": (
                valid_record(role="supervisor", primary_model="p",
                             fallback_model="f", active_model="f"),
                None,  # sanity: sibling enum member must PASS
            ),
            "primary_attempts_5": (
                valid_record(primary_attempts=5),
                "[0] field 'primary_attempts'",
            ),
            "primary_attempts_zero": (
                valid_record(primary_attempts=0),
                "[0] field 'primary_attempts'",
            ),
            "session_id_without_ses_prefix": (
                valid_record(session_id="abc123"),
                "[0] field 'session_id'",
            ),
            "missing_required_key": (
                {key: value for key, value in valid_record().items()
                 if key != "session_id"},
                "[0] field 'session_id'",
            ),
            "extra_key_rejected": (
                valid_record(extra_key="surprise"),
                "[0] field 'extra_key'",
            ),
            "fallback_reason_out_of_enum": (
                valid_record(fallback_reason="test_failure_is_not_a_reason"),
                "[0] field 'fallback_reason'",
            ),
            "fallback_count_below_minimum": (
                valid_record(fallback_count=0),
                "[0] field 'fallback_count'",
            ),
            "non_object_record": (
                "not a record",
                "[0] field '<record>'",
            ),
        }
        for name, (record, fragment) in violations.items():
            with self.subTest(case=name):
                if fragment is None:
                    validate_failover_record(record)  # must NOT raise
                    continue
                with self.assertRaises(ValueError) as caught:
                    validate_failover_record(record)
                self.assertIn(fragment, str(caught.exception))


class TestLoadAndReplace(unittest.TestCase):
    def test_load_missing_file_is_empty_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.json"
            self.assertEqual(load_failover_events(path), [])

    def test_merge_false_replaces_the_file(self):
        old = valid_record(session_id="ses_old00000000000000001")
        new = valid_record(session_id="ses_new00000000000000001")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            persist_failover_events(path, [old])
            persist_failover_events(path, [new], merge=False)
            self.assertEqual(load_failover_events(path), [new])

    def test_non_array_state_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"role": "dispatch"}\n', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_failover_events(path)
            self.assertIn("JSON ARRAY", str(caught.exception))


class TestMirrorMatchesControlPlaneSchema(unittest.TestCase):
    """The mirrored constants == the real schema file (injectable)."""

    def test_default_state_path_is_the_control_plane_file(self):
        """The default path is pinned to the control-plane location —
        resolved from the configurable state root (P20: no baked host
        path), never a hardcoded ``/home/...``."""
        from app import runtime_config

        expected_state = Path(runtime_config.state_root())
        self.assertEqual(
            str(DEFAULT_STATE_PATH),
            str(expected_state / "autopilot" / "agy-failover-state.json"),
        )
        self.assertEqual(
            str(SCHEMA_PATH),
            str(expected_state / "autopilot" / "agy-failover-state.schema.json"),
        )
        # Portability contract (P20): the default FOLLOWS the
        # configurable state root — set AGY_STATE_ROOT and reload, the
        # module default re-resolves (no hardcoded path).
        import importlib
        import os as _os

        from app.orchestration import agy_failover_state as afs

        old = _os.environ.get("AGY_STATE_ROOT")
        _os.environ["AGY_STATE_ROOT"] = "/tmp/p20-fs-override"
        try:
            importlib.reload(afs)
            self.assertEqual(
                str(afs.DEFAULT_STATE_PATH),
                "/tmp/p20-fs-override/autopilot/agy-failover-state.json",
            )
        finally:
            if old is None:
                _os.environ.pop("AGY_STATE_ROOT", None)
            else:
                _os.environ["AGY_STATE_ROOT"] = old
            importlib.reload(afs)

    def _load_schema(self, path: Path):
        if not path.exists():
            self.skipTest(
                f"control-plane schema absent at {path} (skip gracefully)"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_mirror_matches_default_control_plane_schema(self):
        schema = self._load_schema(SCHEMA_PATH)
        self._compare(schema)

    def test_mirror_matches_injectable_schema_copy(self):
        """Same check against an injected copy (path injectability)."""
        original = self._load_schema(SCHEMA_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = Path(tmp) / "agy-failover-state.schema.json"
            copy_path.write_text(
                json.dumps(original), encoding="utf-8"
            )
            schema = json.loads(copy_path.read_text(encoding="utf-8"))
            self._compare(schema)

    def _compare(self, schema: dict) -> None:
        items = schema["items"]

        self.assertEqual(tuple(items["required"]), MIRRORED_REQUIRED_FIELDS)
        self.assertEqual(
            frozenset(items["properties"]), MIRRORED_ALLOWED_FIELDS
        )
        self.assertEqual(
            tuple(items["properties"]["role"]["enum"]),
            MIRRORED_ROLE_ENUM,
        )
        attempts = items["properties"]["primary_attempts"]
        self.assertEqual(
            (attempts["minimum"], attempts["maximum"]),
            MIRRORED_PRIMARY_ATTEMPTS_RANGE,
        )
        self.assertEqual(
            items["properties"]["session_id"]["pattern"],
            MIRRORED_SES_PATTERN.pattern,
        )
        self.assertEqual(
            items["properties"]["parent_session_id"]["pattern"],
            MIRRORED_SES_PATTERN.pattern,
        )
        self.assertEqual(
            items["properties"]["fallback_count"]["minimum"],
            MIRRORED_FALLBACK_COUNT_MIN,
        )
        self.assertIs(items["additionalProperties"], False)
        self.assertIs(MIRRORED_ADDITIONAL_PROPERTIES, False)
        # fallback_reason enum equality with failover.FAILOVER_REASONS
        # (imported by the module — single source of truth).
        from app.orchestration.failover import FAILOVER_REASONS

        self.assertEqual(
            list(items["properties"]["fallback_reason"]["enum"]),
            list(FAILOVER_REASONS),
        )


if __name__ == "__main__":
    unittest.main()