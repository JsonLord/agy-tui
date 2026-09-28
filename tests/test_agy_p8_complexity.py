"""P8 worker A — deterministic complexity classification tests (49-50).

49  every branch of classify_complexity with boundary values, purely
    deterministic (no LLM, no Laya, no network)
50  classification is a pure function: same inputs -> same output
    across repeated calls and argument evaluation order
"""

import unittest

from app.orchestration.checkpoint_planning.schemas import Complexity
from app.orchestration.checkpoint_planning.planner import classify_complexity


LONG = "x" * 80  # len >= 80 blocks TRIVIAL


class ClassifyComplexityTests(unittest.TestCase):
    # -- 49 ------------------------------------------------------------------
    def test_trivial_boundaries(self):
        self.assertEqual(
            classify_complexity(objective="fix typo", files_touched=0),
            Complexity.TRIVIAL,
        )
        self.assertEqual(
            classify_complexity(objective="fix typo", files_touched=1),
            Complexity.TRIVIAL,
        )
        # ui alone does not disqualify SMALL (files<=3, no public api);
        # public_api on one file jumps straight to FEATURE:
        self.assertEqual(
            classify_complexity(
                objective="fix typo", files_touched=1, ui_surface=True
            ),
            Complexity.SMALL,
        )
        self.assertEqual(
            classify_complexity(
                objective="fix typo", files_touched=1, public_api=True
            ),
            Complexity.FEATURE,
        )
        # long objective disqualifies TRIVIAL:
        self.assertEqual(
            classify_complexity(objective=LONG, files_touched=1),
            Complexity.SMALL,
        )
        # exactly-79 chars still TRIVIAL:
        self.assertEqual(
            classify_complexity(objective="y" * 79, files_touched=1),
            Complexity.TRIVIAL,
        )

    def test_small_boundaries(self):
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=2),
            Complexity.SMALL,
        )
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=3),
            Complexity.SMALL,
        )
        # ui alone still SMALL (<=3 files, no public api)
        self.assertEqual(
            classify_complexity(
                objective="do it", files_touched=3, ui_surface=True
            ),
            Complexity.SMALL,
        )

    def test_feature_boundaries(self):
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=4),
            Complexity.FEATURE,
        )
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=8),
            Complexity.FEATURE,
        )
        # public_api forces >= FEATURE even with few files:
        self.assertEqual(
            classify_complexity(
                objective=LONG, files_touched=2, public_api=True
            ),
            Complexity.FEATURE,
        )

    def test_large_feature(self):
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=9),
            Complexity.LARGE_FEATURE,
        )
        self.assertEqual(
            classify_complexity(objective="do it", files_touched=100),
            Complexity.LARGE_FEATURE,
        )

    def test_new_modules_does_not_change_buckets(self):
        # new_modules is accepted (contract signature) but the buckets are
        # defined on files/ui/api/objective-length only:
        self.assertEqual(
            classify_complexity(
                objective="do it", files_touched=1, new_modules=7
            ),
            Complexity.TRIVIAL,
        )

    def test_empty_objective_tolerated(self):
        self.assertEqual(
            classify_complexity(objective="", files_touched=0),
            Complexity.TRIVIAL,
        )

    # -- 50 ------------------------------------------------------------------
    def test_purity_and_repeat_stability(self):
        kwargs = dict(
            objective="ship the planner",
            files_touched=6,
            new_modules=1,
            ui_surface=True,
            public_api=False,
        )
        first = classify_complexity(**kwargs)
        for _ in range(5):
            self.assertEqual(classify_complexity(**kwargs), first)
        self.assertEqual(first, Complexity.FEATURE)

    def test_returns_enum_members_only(self):
        outcomes = set()
        for files in range(0, 12):
            for ui in (False, True):
                for api in (False, True):
                    outcomes.add(
                        classify_complexity(
                            objective="short goal", files_touched=files,
                            ui_surface=ui, public_api=api,
                        )
                    )
        self.assertTrue(outcomes.issubset(set(Complexity)))
        self.assertEqual(len(outcomes), 4)  # all four reachable


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
