"""算法层交叉验证：最少撤回份数与并列取舍，用穷举子集独立对拍。"""

from __future__ import annotations

import random
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.replacement import choose_revocations  # noqa: E402
from app.solver import InconsistentCounts, check_consistent  # noqa: E402


def brute_choose(total, kept, candidates):
    """穷举全部撤回子集，按题面规则挑选。"""
    best = None
    for k in range(len(candidates) + 1):
        feasible = []
        for revoke in combinations(candidates, k):
            revoked = {report_id for report_id, _ in revoke}
            constraints = list(kept)
            for report_id, report_constraints in candidates:
                if report_id not in revoked:
                    constraints.extend(report_constraints)
            try:
                check_consistent(total, constraints)
            except InconsistentCounts:
                continue
            feasible.append(sorted(revoked))
        if not feasible:
            continue
        # 并列处理：撤回号降序排列，取字典序最大的一组。
        best = max(feasible, key=lambda ids: tuple(sorted(ids, reverse=True)))
        return best
    return best


def test_matches_brute_force_on_random_instances():
    rng = random.Random(20261120)
    for _ in range(150):
        total = rng.randint(0, 6)
        max_thickness = 5
        # 圈定范围外的保留约束（合法即可，不必保持“原本相容”）。
        kept = []
        for _ in range(rng.randint(0, 2)):
            lo = rng.randint(0, max_thickness - 1)
            hi = rng.randint(lo + 1, max_thickness)
            kept.append((lo, hi, rng.randint(0, total)))

        candidates = []
        for report_id in range(1, rng.randint(0, 5) + 1):
            segments = []
            for _ in range(rng.randint(1, 2)):
                lo = rng.randint(0, max_thickness - 1)
                hi = rng.randint(lo + 1, max_thickness)
                segments.append((lo, hi, rng.randint(0, total)))
            candidates.append((report_id, segments))

        expected = brute_choose(total, kept, candidates)
        actual = choose_revocations(total, kept, candidates)
        assert actual == expected, (total, kept, candidates, expected, actual)


def test_never_returns_more_than_ten_revocations():
    # 10 份候选都能撤，且撤光才相容时，最多撤回 10 份。
    candidates = [
        (report_id, [(0, 10, 10)]) for report_id in range(1, 11)
    ]
    # 保留任何一份都与“区间内 0 片”矛盾，只能全部撤回。
    assert choose_revocations(10, [(0, 10, 0)], candidates) == list(range(1, 11))
