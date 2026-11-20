"""替换（纸厚复测）相关测试：最少撤回、并列规则、原子性、试算期间补报。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import MAX_CANDIDATES, create_app  # noqa: E402
from app.replacement import choose_revocations  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces, note=None):
    response = client.post(
        "/inspections", json={"total_pieces": total_pieces, "note": note}
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_report(client, segments, inspection_id=1):
    response = client.post(
        f"/inspections/{inspection_id}/reports", json={"segments": segments}
    )
    assert response.status_code == 201, response.text
    return response.json()


def plan(client, segments, inspection_id=1):
    response = client.post(
        f"/inspections/{inspection_id}/replacement-plan", json={"segments": segments}
    )
    assert response.status_code == 200, response.text
    return response.json()


def replace(client, segments, inspection_id=1, expected=None, candidate_report_ids=None):
    body = {"segments": segments}
    if expected is not None:
        body["expected"] = expected
    if candidate_report_ids is not None:
        body["candidate_report_ids"] = candidate_report_ids
    return client.post(f"/inspections/{inspection_id}/replacements", json=body)


def test_plan_chooses_minimum_revocations(client):
    make_inspection(client, 100)
    # 三份互相相容的旧补报：撤回 0 份即可与新表相容（新表落在空档里）。
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 5}])  # 2
    add_report(client, [{"lo": 20, "hi": 30, "count": 5}])  # 3

    result = plan(client, [{"lo": 90, "hi": 100, "count": 10}])
    assert result["solvable"] is True
    assert result["revoked_report_ids"] == []
    assert [c["report_id"] for c in result["candidates"]] == [1, 2, 3]


def test_plan_revokes_only_conflicting_reports(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1 相容
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    add_report(client, [{"lo": 20, "hi": 30, "count": 5}])  # 3 相容

    # 复测表说 [10,20) 只有 3 片，与 2 号矛盾；撤 2 号一份即可。
    result = plan(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert result["revoked_report_ids"] == [2]
    assert result["solvable"] is True


def test_plan_reports_unsolvable(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    # 复测表自身要求区间内 200 片，超过总片数；撤哪一份都无法相容。
    result = plan(client, [{"lo": 0, "hi": 10, "count": 200}])
    assert result["solvable"] is False
    assert result["revoked_report_ids"] is None


def test_tie_break_prefers_larger_report_numbers(client):
    make_inspection(client, 100)
    # 复测表 [0,20)=10；旧表 1 号 [0,5)=6、2 号 [5,10)=6。
    # 两份都保留时 [0,10) 至少 12 片 > 复测表的 10 片，必撤一份；
    # 撤 1 号或撤 2 号都能相容，并列时取编号较大的一组 => 撤 2 号。
    add_report(client, [{"lo": 0, "hi": 5, "count": 6}])  # 1
    add_report(client, [{"lo": 5, "hi": 10, "count": 6}])  # 2

    result = plan(client, [{"lo": 0, "hi": 20, "count": 10}])
    assert result["solvable"] is True
    assert result["revoked_report_ids"] == [2]


def test_tie_break_groups_compared_descending(client):
    make_inspection(client, 100)
    # 1、2、3 三份各自与复测表冲突，撤任意两份都能相容；
    # 三份两两组合按“报告号从大到小排列”比较，最大的是 (3,2)。
    add_report(client, [{"lo": 0, "hi": 5, "count": 6}])  # 1
    add_report(client, [{"lo": 5, "hi": 10, "count": 6}])  # 2
    add_report(client, [{"lo": 10, "hi": 15, "count": 6}])  # 3

    # 复测表 [0,15)=10，三份旧表合计 18 片，至少撤两份；
    # 撤 (1,2)、(1,3)、(2,3) 都相容，按降序比较 (3,2) 最大。
    result = plan(client, [{"lo": 0, "hi": 15, "count": 10}])
    assert result["revoked_report_ids"] == [2, 3]


def test_replacement_is_atomic_and_returns_revoked_ids(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    before = client.get("/inspections/1").json()

    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revoked_report_ids"] == [2]
    assert body["report"]["id"] == 3
    assert body["report"]["status"] == "active"

    detail = client.get("/inspections/1").json()
    assert detail["report_count"] == 3
    assert detail["active_report_count"] == 2
    assert detail["revoked_report_count"] == 1
    statuses = {r["id"]: r["status"] for r in detail["reports"]}
    assert statuses == {1: "active", 2: "revoked", 3: "active"}
    assert len(detail["replacements"]) == 1
    assert detail["replacements"][0]["revoked_report_ids"] == [2]

    # 撤回状态带时间与执行替换编号
    revoked = next(r for r in detail["reports"] if r["id"] == 2)
    assert revoked["revoked_at"]
    assert revoked["revoked_by_replacement_id"] == detail["replacements"][0]["replacement_id"]

    # 原报表仍在历史中（区间完整）
    assert [s["count"] for s in revoked["segments"]] == [6]
    assert before["reports"][0]["segments"] == detail["reports"][0]["segments"]


def test_unsolvable_replacement_rejected_atomically(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])
    before = client.get("/inspections/1").json()

    response = replace(client, [{"lo": 0, "hi": 10, "count": 200}])
    assert response.status_code == 409
    assert "拒收" in response.json()["detail"]
    assert client.get("/inspections/1").json() == before


def test_revoked_reports_no_longer_constrain_future_writes(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2
    assert replace(client, [{"lo": 10, "hi": 20, "count": 3}]).status_code == 201

    # 已撤回的 2 号不再约束后续补报：再报 [10,20)=3 可以接收。
    add_report(client, [{"lo": 10, "hi": 20, "count": 3}])

    # 撤回 2 号后，总片数可以缩小到 3（只需 >= 新表与 1 号之和）。
    assert client.patch("/inspections/1", json={"total_pieces": 10}).status_code == 200

    # 上下界只按有效补报计算：[10,20) 恰好是复测表与后来补报的 3 片。
    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 10, "qualified_hi": 20}
    ).json()
    assert bounds["min_count"] == 3
    assert bounds["max_count"] == 3

    # 而历史里旧表仍可查，且撤回状态仍在。
    history = client.get(
        "/inspections/1/reports", params={"include_revoked": True}
    ).json()
    assert {r["id"]: r["status"] for r in history}[2] == "revoked"
    active = client.get(
        "/inspections/1/reports", params={"include_revoked": "false"}
    ).json()
    assert all(r["status"] == "active" for r in active)


def test_expected_basis_mismatch_rejected(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    basis = plan(client, [{"lo": 10, "hi": 20, "count": 3}])["basis"]
    assert basis == {"total_pieces": 100, "active_report_ids": [1]}

    # 试算之后又来了一份相容补报 => 依据不再一致，整次替换拒收。
    add_report(client, [{"lo": 20, "hi": 30, "count": 4}])  # 2
    before = client.get("/inspections/1").json()
    response = replace(
        client, [{"lo": 10, "hi": 20, "count": 3}], expected=basis
    )
    assert response.status_code == 409
    assert "依据" in response.json()["detail"]
    assert client.get("/inspections/1").json() == before


def test_expected_basis_total_mismatch_rejected(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])
    basis = plan(client, [{"lo": 10, "hi": 20, "count": 3}])["basis"]

    assert client.patch("/inspections/1", json={"total_pieces": 101}).status_code == 200
    before = client.get("/inspections/1").json()
    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}], expected=basis)
    assert response.status_code == 409
    assert client.get("/inspections/1").json() == before


def test_expected_basis_matching_is_accepted(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])
    planned = plan(client, [{"lo": 10, "hi": 20, "count": 3}])
    response = replace(
        client, [{"lo": 10, "hi": 20, "count": 3}], expected=planned["basis"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["revoked_report_ids"] == []


def test_reports_accepted_during_trial_period(client):
    """试算不加锁：试算期间补报与总片数更正照常接收，落库时按依据核对。"""
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    planned = plan(client, [{"lo": 10, "hi": 20, "count": 3}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = [
            pool.submit(
                add_report, client, [{"lo": 20, "hi": 30, "count": 4}]
            ),
            pool.submit(
                client.patch, "/inspections/1", json={"total_pieces": 100}
            ),
        ]
        for future in writes:
            future.result()
    # 试算期间接收了新补报，但结果仍全部可见、可用。
    detail = client.get("/inspections/1").json()
    assert detail["active_report_count"] == 2

    response = replace(
        client, [{"lo": 10, "hi": 20, "count": 3}], expected=planned["basis"]
    )
    assert response.status_code == 409
    # 不带依据时按落库时刻的实际有效补报重新选方案（这里 0 撤回即可）。
    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert response.status_code == 201, response.text


def test_candidate_scope_limited_to_ten_newest(client):
    make_inspection(client, 1000)
    for index in range(12):
        add_report(client, [{"lo": index * 10, "hi": index * 10 + 5, "count": 1}])
    result = plan(client, [{"lo": 900, "hi": 1000, "count": 1}])
    candidate_ids = [c["report_id"] for c in result["candidates"]]
    assert len(candidate_ids) == MAX_CANDIDATES
    # 新近优先：最旧的 1、2 号不参与撤回选择，圈定为 3..12。
    assert candidate_ids == list(range(3, 13))


def test_explicit_candidate_scope(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    add_report(client, [{"lo": 20, "hi": 30, "count": 5}])  # 3

    response = client.post(
        "/inspections/1/replacement-plan",
        json={
            "segments": [{"lo": 10, "hi": 20, "count": 3}],
            "candidate_report_ids": [2],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [c["report_id"] for c in body["candidates"]] == [2]
    assert body["revoked_report_ids"] == [2]

    # 圈定范围外的 2 号仍冲突 => 无解
    response = client.post(
        "/inspections/1/replacement-plan",
        json={
            "segments": [{"lo": 10, "hi": 20, "count": 3}],
            "candidate_report_ids": [1, 3],
        },
    )
    assert response.status_code == 200
    assert response.json()["solvable"] is False

    # 圈定不存在的补报号 => 拒绝
    response = client.post(
        "/inspections/1/replacement-plan",
        json={
            "segments": [{"lo": 10, "hi": 20, "count": 3}],
            "candidate_report_ids": [99],
        },
    )
    assert response.status_code == 409

    ok = replace(
        client,
        [{"lo": 10, "hi": 20, "count": 3}],
        candidate_report_ids=[2],
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["revoked_report_ids"] == [2]


def test_candidate_scope_validation(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])
    payload = {
        "segments": [{"lo": 10, "hi": 20, "count": 3}],
        "candidate_report_ids": list(range(1, 12)),
    }
    assert client.post("/inspections/1/replacement-plan", json=payload).status_code == 422
    payload["candidate_report_ids"] = [1, 1]
    assert client.post("/inspections/1/replacement-plan", json=payload).status_code == 422


def test_out_of_scope_reports_are_always_kept(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    # 直接调用算法层：保留约束（1 号与复测表）始终参与，范围内只圈了 2 号。
    revoked = choose_revocations(100, [(0, 10, 5), (10, 20, 3)], [(2, [(10, 20, 6)])])
    assert revoked == [2]


def test_choose_revocations_empty_candidates():
    assert choose_revocations(10, [(0, 10, 10)], []) == []
    assert choose_revocations(10, [(0, 10, 99)], []) is None


def test_choose_revocations_prefers_larger_ids_on_tie():
    kept = [(0, 20, 10)]
    candidates = [
        (1, [(0, 5, 6)]),
        (2, [(5, 10, 6)]),
        (3, [(10, 15, 6)]),
    ]
    # 总 10 片、复测表 [0,20)=10，三份旧表各 6 片：至少要撤两份。
    assert choose_revocations(100, kept, candidates) == [2, 3]


def test_replacement_requires_valid_segments(client):
    make_inspection(client, 10)
    assert replace(client, []).status_code == 422
    assert replace(client, [{"lo": 5, "hi": 5, "count": 1}]).status_code == 422
    assert replace(client, [{"lo": 0, "hi": 5, "count": -1}]).status_code == 422


def test_replacement_missing_inspection(client):
    response = replace(client, [{"lo": 0, "hi": 10, "count": 1}], inspection_id=99)
    assert response.status_code == 404
    assert (
        client.post(
            "/inspections/99/replacement-plan",
            json={"segments": [{"lo": 0, "hi": 10, "count": 1}]},
        ).status_code
        == 404
    )
    assert client.get("/inspections/99/replacements").status_code == 404


def test_plan_basis_is_internally_consistent_under_concurrent_writes(client):
    """试算不加锁，但一次响应里的总片数与有效补报必须来自同一快照。"""
    make_inspection(client, 500)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1

    def writer(index):
        return client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 100 + index * 10, "hi": 105 + index * 10, "count": 1}]},
        ).status_code

    def reader(_):
        response = client.post(
            "/inspections/1/replacement-plan",
            json={"segments": [{"lo": 10, "hi": 20, "count": 3}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        basis = body["basis"]
        assert set(basis["active_report_ids"]) >= {
            candidate["report_id"] for candidate in body["candidates"]
        }
        assert body["total_pieces"] == basis["total_pieces"]
        return response.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(writer, index) for index in range(8)]
        futures += [pool.submit(reader, index) for index in range(16)]
        results = [future.result() for future in futures]

    assert results.count(201) == 8
    assert all(status in (200, 201) for status in results)


def test_report_with_multiple_segments_is_revoked_as_a_whole(client):
    make_inspection(client, 100)
    # 1 号含两个区间；其中只有一条与复测表冲突，但撤回必须整份进行。
    add_report(
        client,
        [
            {"lo": 0, "hi": 10, "count": 5},
            {"lo": 10, "hi": 20, "count": 6},
        ],
    )  # 1
    add_report(client, [{"lo": 20, "hi": 30, "count": 5}])  # 2

    result = plan(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert result["revoked_report_ids"] == [1]

    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert response.status_code == 201
    detail = client.get("/inspections/1").json()
    assert {r["id"]: r["status"] for r in detail["reports"]}[1] == "revoked"
    # 撤回后 [0,10) 不再有 5 片的约束，可以放到 0 片。
    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
    ).json()
    assert bounds["min_count"] == 0


def test_openapi_documents_replacement(client):
    schema = client.get("/openapi.json").json()
    assert "/inspections/{inspection_id}/replacements" in schema["paths"]
    assert "/inspections/{inspection_id}/replacement-plan" in schema["paths"]
    assert client.get("/docs").status_code == 200


def test_replacement_rolls_back_when_revocation_fails(client, monkeypatch):
    """新表入库与旧表撤回必须一并生效：撤回失败则新表也不得留下。"""
    from app import db as db_module

    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    before = client.get("/inspections/1").json()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟撤回阶段故障")

    monkeypatch.setattr(db_module, "revoke_reports", boom)
    with pytest.raises(RuntimeError):
        replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    monkeypatch.undo()

    after = client.get("/inspections/1").json()
    assert after == before  # 复测表、替换记录、撤回全部未落库
    assert after["report_count"] == 2
    assert after["replacements"] == []

    # 故障排除后重试可以正常完成。
    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert response.status_code == 201
    assert response.json()["revoked_report_ids"] == [2]


def test_concurrent_replacements_are_serialized(client):
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    add_report(client, [{"lo": 20, "hi": 30, "count": 6}])  # 3 冲突

    # 两张复测表都在同一试算依据下试算；串行化写入后只有先到者仍然对得上依据。
    basis = plan(
        client,
        [
            {"lo": 10, "hi": 20, "count": 3},
            {"lo": 20, "hi": 30, "count": 3},
        ],
    )["basis"]

    def do_replace(count):
        return replace(
            client,
            [
                {"lo": 10, "hi": 20, "count": count},
                {"lo": 20, "hi": 30, "count": count},
            ],
            expected=basis,
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(do_replace, [3, 4]))

    assert statuses == [201, 409]
    detail = client.get("/inspections/1").json()
    assert len(detail["replacements"]) == 1
    assert detail["replacements"][0]["revoked_report_ids"] == [2, 3]
    assert detail["active_report_count"] == 2  # 1 号与复测表
    # 落库后有效计数必须相容。
    assert (
        client.get(
            "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 100}
        ).status_code
        == 200
    )


def test_report_between_plan_and_commit_is_reconsidered_without_basis(client):
    """不带 expected 时，落库按当时实际有效补报重新试算，不丢期间补报。"""
    make_inspection(client, 100)
    add_report(client, [{"lo": 0, "hi": 10, "count": 5}])  # 1
    add_report(client, [{"lo": 10, "hi": 20, "count": 6}])  # 2 冲突
    plan(client, [{"lo": 10, "hi": 20, "count": 3}])

    # 试算后、提交前又接收了一份相容补报。
    add_report(client, [{"lo": 30, "hi": 40, "count": 4}])  # 3

    response = replace(client, [{"lo": 10, "hi": 20, "count": 3}])
    assert response.status_code == 201
    body = response.json()
    assert body["revoked_report_ids"] == [2]
    # 期间新增的 3 号保留，未被误撤回。
    detail = client.get("/inspections/1").json()
    assert {r["id"]: r["status"] for r in detail["reports"]}[3] == "active"
    assert body["basis"]["active_report_ids"] == [1, 2, 3]
