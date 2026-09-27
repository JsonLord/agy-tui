"""
P4 session-capture tests — network-free, every seam injected.

Coverage (contract §5, first test file):

* capture_session: stdout match wins (source 'stdout'); stdout empty +
  known session -> 'known'; stdout empty + no known + read_back id ->
  'readback'; all none -> (None, 'none'). read_back is injectable —
  NO test touches the network (the default path is proven by patching
  the module-level ``read_back_session`` with a stub).
* read_back_session: parses a FAKE /api/session payload through the
  injected ``opener`` (transport stub) — newest-by-updated selected,
  deterministic id tie-break, agent filter, directory filter (the
  real ``location`` dict / ``directory`` / ``path`` row shapes),
  since_ms honored, empty -> None, network error -> None (never
  raises), shape-differs -> falls back to the /session view.
* TaskSessionLog: the per-task (session_id, source) recorder the live
  executor exposes on ``execute_task.session_log``.
"""

import json
import unittest
from unittest import mock

from app.orchestration.agy_session import (
    TaskSessionLog,
    capture_session,
    extract_session_from_stdout,
    read_back_session,
)
from app.orchestration import agy_session


def session_row(
    session_id: str,
    *,
    agent: str | None = "agy-dispatch",
    updated: int = 1_000_000,
    created: int = 900_000,
    directory: str | None = "/repo/e2e",
    directory_shape: str = "location-dict",
    with_time: bool = True,
) -> dict:
    """
    One row shaped like the REAL OpenCode views (verified live):
    /api/session carries ``location: {directory: ...}``; /session
    carries a flat ``directory`` (+ relative ``path``).
    """
    row: dict = {
        "id": session_id,
        "agent": agent,
        "model": {"id": "some-model", "providerID": "p", "variant": "default"},
        "title": "t",
    }
    if with_time:
        row["time"] = {"created": created, "updated": updated}
    if directory is not None:
        if directory_shape == "location-dict":
            row["location"] = {"directory": directory}
        elif directory_shape == "directory":
            row["directory"] = directory
        elif directory_shape == "location-str":
            row["location"] = directory
        elif directory_shape == "path":
            row["directory"] = directory
            row["path"] = directory.lstrip("/")
    return row


class RecordingReadBack:
    """Injectable capture-session read-back stub (records calls)."""

    def __init__(self, result: str | None = "ses_Stub1", raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


class TestCaptureSession(unittest.TestCase):
    def test_case1_stdout_match_wins(self):
        """stdout first: known session and read-back both ignored."""
        read_back = RecordingReadBack(result="ses_ShouldNotBeUsed")

        session_id, source = capture_session(
            "worker yapping\nSession ID: ses_FromStdout9\n",
            known_session="ses_Known",
            agent="agy-dispatch",
            directory="/repo/e2e",
            since_ms=1,
            read_back=read_back,
        )

        self.assertEqual((session_id, source), ("ses_FromStdout9", "stdout"))
        self.assertEqual(read_back.calls, [])  # never consulted

    def test_case2_known_session_second(self):
        """stdout empty + known session -> 'known', no read-back."""
        read_back = RecordingReadBack()

        session_id, source = capture_session(
            "",
            known_session="ses_Known2",
            agent="agy-dispatch",
            read_back=read_back,
        )

        self.assertEqual((session_id, source), ("ses_Known2", "known"))
        self.assertEqual(read_back.calls, [])

    def test_case3_readback_third(self):
        """stdout empty + no known -> read-back; keys forwarded."""
        read_back = RecordingReadBack(result="ses_Recovered7")

        session_id, source = capture_session(
            None,
            known_session=None,
            agent="agy-dispatch",
            directory="/repo/e2e",
            since_ms=4242,
            server="http://oc.test:4096",
            read_back=read_back,
        )

        self.assertEqual((session_id, source), ("ses_Recovered7", "readback"))
        self.assertEqual(len(read_back.calls), 1)
        self.assertEqual(read_back.calls[0]["agent"], "agy-dispatch")
        self.assertEqual(read_back.calls[0]["directory"], "/repo/e2e")
        self.assertEqual(read_back.calls[0]["since_ms"], 4242)
        self.assertEqual(read_back.calls[0]["server"], "http://oc.test:4096")

    def test_case4_all_none(self):
        """Nothing anywhere -> (None, 'none')."""
        session_id, source = capture_session(
            "no id in this output",
            known_session=None,
            agent="agy-dispatch",
            read_back=RecordingReadBack(result=None),
        )

        self.assertEqual((session_id, source), (None, "none"))

    def test_case4b_readback_exception_is_swallowed(self):
        """A raising read-back can never fail the execution path."""
        session_id, source = capture_session(
            "",
            known_session=None,
            agent="agy-dispatch",
            read_back=RecordingReadBack(raises=RuntimeError("boom")),
        )

        self.assertEqual((session_id, source), (None, "none"))

    def test_case5_default_readback_is_the_live_function(self):
        """read_back=None resolves to the module-level read_back_session
        (proved by patching the module attribute — still no network)."""
        with mock.patch.object(
            agy_session, "read_back_session", return_value="ses_Patched1"
        ) as patched:
            session_id, source = capture_session(
                "",
                known_session=None,
                agent="agy-dispatch",
                directory="/repo/e2e",
                since_ms=7,
            )

        self.assertEqual((session_id, source), ("ses_Patched1", "readback"))
        patched.assert_called_once()
        kwargs = patched.call_args.kwargs
        self.assertEqual(kwargs["agent"], "agy-dispatch")
        self.assertEqual(kwargs["directory"], "/repo/e2e")
        self.assertEqual(kwargs["since_ms"], 7)

    def test_stdout_extractor_shared_semantics(self):
        """Line pattern preferred, bare ses_ token still accepted."""
        self.assertEqual(
            extract_session_from_stdout("x\nSession ID: ses_Abc123\n"),
            "ses_Abc123",
        )
        self.assertEqual(
            extract_session_from_stdout("continued ses_XyZ789 tail"),
            "ses_XyZ789",
        )
        self.assertIsNone(extract_session_from_stdout(""))
        self.assertIsNone(extract_session_from_stdout(None))
        self.assertIsNone(extract_session_from_stdout("no id here"))


class TestReadBackSession(unittest.TestCase):
    @staticmethod
    def opener(payloads, *, ordered: bool = False):
        """
        Fake transport: a single payload for every URL, an exception
        (raised for every URL), or — with ``ordered=True`` — a list of
        payloads consumed one per attempted URL.
        """
        calls: list[str] = []

        class _ScriptedOpener:
            def __init__(self, log: list[str]) -> None:
                self.calls = log

            def __call__(self, url: str, timeout: float):
                self.calls.append(url)
                if isinstance(payloads, Exception):
                    raise payloads
                if ordered:
                    index = len(self.calls) - 1
                    if index >= len(payloads):
                        raise IndexError("no scripted page left")
                    return payloads[index]
                return payloads

        return _ScriptedOpener(calls)

    def test_newest_by_updated_selected(self):
        rows = [
            session_row("ses_Old1", updated=100),
            session_row("ses_Mid2", updated=500),
            session_row("ses_New3", updated=900),
            session_row("ses_NewestButOtherAgent", updated=5000,
                        agent="build"),
            session_row("ses_NewestButOtherDir", updated=6000,
                        directory="/somewhere/else"),
        ]
        opener = self.opener({"data": rows})

        picked = read_back_session(
            agent="agy-dispatch",
            directory="/repo/e2e",
            opener=opener,
        )

        self.assertEqual(picked, "ses_New3")
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(opener.calls[0].startswith(
            "http://127.0.0.1:4096/api/session"
        ))

    def test_tie_break_on_id_is_deterministic(self):
        rows = [
            session_row("ses_aaa", updated=700),
            session_row("ses_zzz", updated=700),
            session_row("ses_mmm", updated=700, agent="build"),
        ]
        opener = self.opener({"data": rows})

        first = read_back_session(agent="agy-dispatch", opener=opener)
        second = read_back_session(agent="agy-dispatch", opener=opener)

        self.assertEqual(first, "ses_zzz")  # documented rule: higher id
        self.assertEqual(first, second)

    def test_agent_filter_honored(self):
        rows = [
            session_row("ses_Other1", updated=900, agent="build"),
            session_row("ses_Other2", updated=800, agent=None),
        ]
        self.assertIsNone(
            read_back_session(
                agent="agy-dispatch", opener=self.opener({"data": rows})
            )
        )

    def test_directory_filter_all_real_row_shapes(self):
        """location dict (api view), flat directory, location str, path."""
        cases = [
            ("location-dict", "/repo/e2e"),
            ("directory", "/repo/e2e/"),
            ("location-str", "/repo/e2e"),
            ("path", "/repo/e2e"),
        ]
        for shape, row_dir in cases:
            with self.subTest(shape=shape):
                rows = [
                    session_row(
                        "ses_Match1", updated=500,
                        directory=row_dir, directory_shape=shape,
                    ),
                    session_row(
                        "ses_Other1", updated=400,
                        directory="/other/repo", directory_shape=shape,
                    ),
                ]
                opener = self.opener({"data": rows})
                picked = read_back_session(
                    agent="agy-dispatch",
                    directory="/repo/e2e",
                    opener=opener,
                )
                self.assertEqual(picked, "ses_Match1")

        # No directory -> the other-dir row is also eligible (newest wins).
        rows = [session_row("ses_Any1", updated=1, directory="/x")]
        self.assertEqual(
            read_back_session(
                agent="agy-dispatch", opener=self.opener({"data": rows})
            ),
            "ses_Any1",
        )

    def test_since_ms_honored(self):
        rows = [
            session_row("ses_Stale5", updated=900),
            session_row("ses_Fresh6", updated=1_500),
        ]
        opener = self.opener({"data": rows})

        picked = read_back_session(
            agent="agy-dispatch", since_ms=1_000, opener=opener
        )
        self.assertEqual(picked, "ses_Fresh6")

        self.assertIsNone(
            read_back_session(
                agent="agy-dispatch",
                since_ms=2_000,
                opener=self.opener({"data": rows}),
            )
        )

    def test_since_ms_excludes_rows_without_time(self):
        rows = [session_row("ses_NoTime", with_time=False)]
        self.assertIsNone(
            read_back_session(
                agent="agy-dispatch",
                since_ms=1,
                opener=self.opener({"data": rows}),
            )
        )
        # Without since_ms the same row is eligible.
        self.assertEqual(
            read_back_session(
                agent="agy-dispatch",
                opener=self.opener({"data": rows}),
            ),
            "ses_NoTime",
        )

    def test_empty_payload_returns_none(self):
        self.assertIsNone(
            read_back_session(
                agent="agy-dispatch", opener=self.opener({"data": []})
            )
        )
        self.assertIsNone(
            read_back_session(
                agent="agy-dispatch",
                opener=self.opener({"other": ["shape"]}),
            )
        )

    def test_network_error_never_raises(self):
        """Every endpoint failing -> None; no exception escapes."""
        for failure in (
            OSError("connection refused"),
            json.JSONDecodeError("Expecting value", "", 0),
            RuntimeError("weird transport"),
        ):
            with self.subTest(failure=type(failure).__name__):
                result = read_back_session(
                    agent="agy-dispatch",
                    opener=self.opener(failure),
                )
                self.assertIsNone(result)

    def test_shape_differs_falls_back_to_session_view(self):
        """Primary view unusable -> GET /session rows are consulted."""
        primary = {"unexpected": "shape"}  # dict without a row list
        fallback_rows = [
            session_row("ses_Fallback9", updated=700,
                        directory_shape="directory"),
        ]
        opener = self.opener([primary, fallback_rows], ordered=True)

        picked = read_back_session(
            agent="agy-dispatch", directory="/repo/e2e", opener=opener
        )

        self.assertEqual(picked, "ses_Fallback9")
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(opener.calls[0].startswith(
            "http://127.0.0.1:4096/api/session"
        ))
        self.assertTrue(opener.calls[1].startswith(
            "http://127.0.0.1:4096/session"
        ))

    def test_bare_list_payload_accepted(self):
        rows = [session_row("ses_Bare1", updated=10)]
        self.assertEqual(
            read_back_session(
                agent="agy-dispatch", opener=self.opener(rows)
            ),
            "ses_Bare1",
        )


class TestTaskSessionLog(unittest.TestCase):
    def test_record_and_read_back(self):
        log = TaskSessionLog()

        self.assertIsNone(log.record(None, "ses_x", "stdout"))
        self.assertIsNone(log.record("t1", None, "stdout"))

        entry = log.record("t1", "ses_one", "readback")
        self.assertEqual(
            entry,
            {"task_id": "t1", "session_id": "ses_one", "source": "readback"},
        )
        self.assertEqual(log.get("t1"), entry)
        self.assertEqual(log.session_id_for("t1"), "ses_one")
        self.assertEqual(log.source_for("t1"), "readback")
        self.assertEqual(log.source_for("unknown"), "none")
        self.assertIsNone(log.get("unknown"))
        self.assertEqual(log.records, {"t1": entry})
        self.assertEqual(log.sources, {"t1": "readback"})

        # Later capture for the SAME task is authoritative (the task's
        # current record fully replaces the previous one).
        log.record("t1", "ses_two", "known")
        self.assertEqual(log.session_id_for("t1"), "ses_two")
        self.assertEqual(log.source_for("t1"), "known")
        self.assertEqual(log.session_ids(), ["ses_two"])

    def test_session_ids_dedup_order(self):
        log = TaskSessionLog()
        log.record("t1", "ses_a", "stdout")
        log.record("t2", "ses_a", "known")
        log.record("t3", "ses_b", "readback")
        self.assertEqual(log.session_ids(), ["ses_a", "ses_b"])


if __name__ == "__main__":
    unittest.main()