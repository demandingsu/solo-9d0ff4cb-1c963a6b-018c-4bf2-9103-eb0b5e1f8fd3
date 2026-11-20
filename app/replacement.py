"""在圈定的旧补报里选出撤回份数最少的相容方案。

规则
----
* 一份补报里的所有区间必须整体保留或整体撤回，不能只撤回其中一条。
* 保留的旧补报加上复测表必须与总片数相容。
* 撤回份数相同时，把各组要撤回的报告号从大到小排列，逐位比较，取第一处
  编号较大的那一组（即字典序最大）。

算法
----
候选最多 10 份，直接按撤回份数 k = 0, 1, … 从小到大枚举组合，第一个找到
相容方案的 k 即最少撤回份数。同一 k 内按“报告号降序排列后字典序最大”的
顺序枚举，遇到第一个可行组合即为该 k 下的最优解，因此总枚举量不超过
2**10，且不需要依赖相容性单调的假设（撤回更少不等于更难相容）。
"""

from __future__ import annotations

from itertools import combinations
from typing import Sequence

from .solver import Constraint, InconsistentCounts, check_consistent


def _descending_desc(ids: Sequence[int]) -> tuple[int, ...]:
    """报告号从大到小排列，用于并列时的字典序比较。"""
    return tuple(sorted(ids, reverse=True))


def choose_revocations(
    total_pieces: int,
    kept_constraints: Sequence[Constraint],
    candidates: Sequence[tuple[int, Sequence[Constraint]]],
) -> list[int] | None:
    """返回最少撤回的补报号（升序）；无解返回 ``None``。

    ``kept_constraints`` 是圈定范围之外的有效补报与复测表的约束；
    ``candidates`` 是 ``(报告号, 该报告的约束)``，按报告号升序。
    """
    ordered = sorted(candidates, key=lambda item: item[0])
    all_ids = [report_id for report_id, _ in ordered]

    # 同一 k 内按“撤回集合降序排列后的字典序”从大到小尝试。
    for k in range(len(ordered) + 1):
        groups = sorted(
            combinations(all_ids, k), key=_descending_desc, reverse=True
        )
        for revoke in groups:
            revoked = set(revoke)
            constraints = list(kept_constraints)
            for report_id, report_constraints in ordered:
                if report_id not in revoked:
                    constraints.extend(report_constraints)
            try:
                check_consistent(total_pieces, constraints)
            except InconsistentCounts:
                continue
            return sorted(revoke)
    return None
