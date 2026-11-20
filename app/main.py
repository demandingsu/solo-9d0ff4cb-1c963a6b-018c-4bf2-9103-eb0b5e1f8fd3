"""FastAPI 应用：纸样厚度计数核对。

* ``POST /inspections``                    登记一批纸样的总片数
* ``GET  /inspections``                    列出全部登记
* ``GET  /inspections/{id}``               查看登记、补报历史与替换历史
* ``PATCH /inspections/{id}``              更正总片数/备注（与有效计数冲突则拒收）
* ``POST /inspections/{id}/reports``       分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``       查看补报历史（含已撤回补报及撤回状态）
* ``GET  /inspections/{id}/bounds``        给定合格区间，查询合格片数最少/最多
* ``POST /inspections/{id}/replacement-plan``  试算替换：圈定旧补报并选出撤回最少的方案
* ``POST /inspections/{id}/replacements``  提交复测表，原子地入库新表并撤回旧表
* ``GET  /inspections/{id}/replacements``  查看替换历史
* ``GET  /health``                         健康检查

所有区间均为整数微米，含起点不含终点，即 ``[lo, hi)``，且 ``hi > lo``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import Connection

from . import db
from .replacement import choose_revocations
from .schemas import (
    BoundsOut,
    CountReportIn,
    CountReportOut,
    InspectionCreate,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdate,
    ReplacementBasis,
    ReplacementCandidate,
    ReplacementIn,
    ReplacementOut,
    ReplacementPlan,
    ReplacementPlanIn,
)
from .solver import InconsistentCounts, check_consistent, compute_bounds

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/inspection.db")

# 替换时最多圈定多少份有效旧补报参与“撤回/保留”的选择。
MAX_CANDIDATES = 10


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init(path)
        yield

    api = FastAPI(
        title="纸样厚度计数核对",
        description=(
            "登记总片数，分次补报互可重叠的厚度区间计数；"
            "按全部有效计数的交集计算合格片数的可达到上下界，不逐片展开。"
            "旧补报可能失准时，可提交纸厚复测表替换：圈定同号下最多 "
            f"{MAX_CANDIDATES} 份有效旧补报，在保留复测表与圈定范围外补报的前提下，"
            "选出使计数相容且撤回份数最少的方案，新表入库与旧表撤回一并生效。"
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    def get_conn():
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(connection: Connection):
        """只读事务：让一次响应里的多条查询看到同一快照。

        试算不加写锁（期间补报与总片数更正照常进行），但“读总片数 + 读有效
        补报”必须来自同一个快照，否则返回的试算依据会自相矛盾。
        """
        connection.execute("BEGIN")
        try:
            yield
        finally:
            connection.execute("ROLLBACK")

    def _require_inspection(connection: Connection, inspection_id: int):
        row = db.get_inspection(connection, inspection_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"登记 {inspection_id} 不存在",
            )
        return row

    def _summary(row) -> InspectionSummary:
        return InspectionSummary(
            id=row["id"],
            total_pieces=row["total_pieces"],
            note=row["note"],
            created_at=row["created_at"],
            report_count=row["report_count"],
            segment_count=row["segment_count"],
            active_report_count=row["active_report_count"],
            revoked_report_count=row["revoked_report_count"],
            active_segment_count=row["active_segment_count"],
        )

    def _scope_candidates(
        active_reports: list[dict], requested_ids: list[int] | None = None
    ) -> list[dict]:
        """圈定最多 10 份可撤回的有效旧补报。

        未指定时新近的报告优先进入圈定范围（报告号大者优先），再按报告号
        升序返回：这样既尽量保留原有报表，也让圈定结果稳定可复现。显式给出
        ``requested_ids`` 时以它为准，并校验每一份都是当前有效补报。圈定范围
        外的有效补报一律保留，不参与撤回。
        """
        if requested_ids is not None:
            by_id = {report["id"]: report for report in active_reports}
            missing = [report_id for report_id in requested_ids if report_id not in by_id]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"圈定的补报 {missing} 不是该登记下的有效补报"
                        "（可能已被撤回或不存在），整次操作拒绝"
                    ),
                )
            return sorted(
                (by_id[report_id] for report_id in requested_ids),
                key=lambda report: report["id"],
            )

        newest_first = sorted(
            active_reports, key=lambda report: report["id"], reverse=True
        )
        return sorted(newest_first[:MAX_CANDIDATES], key=lambda report: report["id"])

    def _plan(
        connection: Connection,
        inspection_id: int,
        segments: list[tuple[int, int, int]],
        candidate_ids_requested: list[int] | None = None,
    ) -> ReplacementPlan:
        """在当前有效补报上试算：不改动任何数据。"""
        row = _require_inspection(connection, inspection_id)
        total_pieces = int(row["total_pieces"])
        active_reports = db.load_active_reports(connection, inspection_id)
        candidates = _scope_candidates(active_reports, candidate_ids_requested)
        candidate_ids = {report["id"] for report in candidates}

        kept_constraints: list[tuple[int, int, int]] = list(segments)
        for report in active_reports:
            if report["id"] not in candidate_ids:
                kept_constraints.extend(
                    (s["lo"], s["hi"], s["count"]) for s in report["segments"]
                )

        candidate_constraints = [
            (
                report["id"],
                [(s["lo"], s["hi"], s["count"]) for s in report["segments"]],
            )
            for report in candidates
        ]
        revoked = choose_revocations(
            total_pieces, kept_constraints, candidate_constraints
        )
        return ReplacementPlan(
            inspection_id=inspection_id,
            total_pieces=total_pieces,
            basis=ReplacementBasis(
                total_pieces=total_pieces,
                active_report_ids=[report["id"] for report in active_reports],
            ),
            candidates=[
                ReplacementCandidate(
                    report_id=report["id"],
                    created_at=report["created_at"],
                    segments=report["segments"],
                )
                for report in candidates
            ],
            revoked_report_ids=revoked,
            solvable=revoked is not None,
        )

    def _replacement_model(item: dict) -> ReplacementOut:
        return ReplacementOut(
            replacement_id=item["id"],
            inspection_id=item["inspection_id"],
            created_at=item["created_at"],
            report=CountReportOut(**item["report"]),
            revoked_report_ids=item["revoked_report_ids"],
            basis=ReplacementBasis(
                total_pieces=item["total_pieces"],
                active_report_ids=item["basis_report_ids"],
            ),
        )

    def _replacement_out(
        connection: Connection, inspection_id: int, replacement_id: int
    ) -> ReplacementOut:
        """读回刚落库的替换，并复核替换后有效计数确实相容。"""
        with read_snapshot(connection):
            items = db.get_replacements(connection, inspection_id)
            item = next((row for row in items if row["id"] == replacement_id), None)
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="替换写入后未能读回",
                )
            try:
                check_consistent(
                    int(_require_inspection(connection, inspection_id)["total_pieces"]),
                    db.load_constraints(connection, inspection_id),
                )
            except InconsistentCounts as exc:  # pragma: no cover - 防御
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="替换后有效计数仍不相容，请检查数据",
                ) from exc
        return _replacement_model(item)

    @api.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    @api.post(
        "/inspections",
        response_model=InspectionSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["登记"],
        summary="登记一批纸样的总片数",
    )
    def create_inspection(
        payload: InspectionCreate, connection: Connection = Depends(get_conn)
    ) -> InspectionSummary:
        inspection_id = db.create_inspection(
            connection, payload.total_pieces, payload.note
        )
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.get(
        "/inspections",
        response_model=list[InspectionSummary],
        tags=["登记"],
        summary="列出全部登记",
    )
    def list_inspections(
        connection: Connection = Depends(get_conn),
    ) -> list[InspectionSummary]:
        with read_snapshot(connection):
            rows = db.list_inspections(connection)
        return [_summary(row) for row in rows]

    @api.get(
        "/inspections/{inspection_id}",
        response_model=InspectionDetail,
        tags=["登记"],
        summary="查看登记、补报历史与替换历史",
        description="补报历史包含已撤回的旧补报及其撤回状态。",
    )
    def get_inspection(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> InspectionDetail:
        with read_snapshot(connection):
            row = _require_inspection(connection, inspection_id)
            summary = _summary(row)
            reports = [
                CountReportOut(**r)
                for r in db.get_reports(connection, inspection_id)
            ]
            replacements = [
                _replacement_model(item)
                for item in db.get_replacements(connection, inspection_id)
            ]
        return InspectionDetail(
            **summary.model_dump(), reports=reports, replacements=replacements
        )

    @api.patch(
        "/inspections/{inspection_id}",
        response_model=InspectionSummary,
        tags=["登记"],
        summary="更正总片数或备注",
        description=(
            "若新的总片数与当前有效补报的计数不相容，整次更正被拒收，原登记不变；"
            "已撤回的旧补报不参与校验。"
        ),
    )
    def update_inspection(
        inspection_id: int,
        payload: InspectionUpdate,
        connection: Connection = Depends(get_conn),
    ) -> InspectionSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            new_total = (
                payload.total_pieces
                if "total_pieces" in payload.model_fields_set
                else int(row["total_pieces"])
            )
            new_note = payload.note if "note" in payload.model_fields_set else row["note"]
            if "total_pieces" in payload.model_fields_set:
                constraints = db.load_constraints(connection, inspection_id)
                try:
                    check_consistent(new_total, constraints)
                except InconsistentCounts:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"新总片数 {new_total} 与有效补报的计数不相容，"
                            "本次更正已拒收，原登记保持不变"
                        ),
                    )
            db.update_inspection(connection, inspection_id, new_total, new_note)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.post(
        "/inspections/{inspection_id}/reports",
        response_model=CountReportOut,
        status_code=status.HTTP_201_CREATED,
        tags=["补报"],
        summary="提交一批厚度区间计数",
        description=(
            "区间可以重叠。整份报告要么全部接收，要么在与有效计数"
            "（含本报告内其他区间；已撤回补报不参与）不相容时整份拒收；"
            "拒收不改变已有结果。"
        ),
    )
    def submit_report(
        inspection_id: int,
        payload: CountReportIn,
        connection: Connection = Depends(get_conn),
    ) -> CountReportOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            existing = db.load_constraints(connection, inspection_id)
            try:
                check_consistent(int(row["total_pieces"]), existing + segments)
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "补报与有效计数（或本报告内部）不相容，整份拒收；"
                        "已有结果保持不变"
                    ),
                ) from exc
            report_id = db.insert_report(connection, inspection_id, segments)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        report = db.get_report(connection, inspection_id, report_id)
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="补报写入后未能读回",
            )
        return CountReportOut(**report)

    @api.get(
        "/inspections/{inspection_id}/reports",
        response_model=list[CountReportOut],
        tags=["补报"],
        summary="查看补报历史",
        description="默认返回全部补报（含已撤回）；``include_revoked=false`` 只看有效补报。",
    )
    def list_reports(
        inspection_id: int,
        include_revoked: bool = Query(True, description="是否包含已撤回的旧补报"),
        connection: Connection = Depends(get_conn),
    ) -> list[CountReportOut]:
        with read_snapshot(connection):
            _require_inspection(connection, inspection_id)
            reports = db.get_reports(
                connection, inspection_id, include_revoked=include_revoked
            )
        return [CountReportOut(**r) for r in reports]

    @api.get(
        "/inspections/{inspection_id}/bounds",
        response_model=BoundsOut,
        tags=["查询"],
        summary="查询合格片数的最少/最多可达到值",
        description=(
            "合格区间为 [qualified_lo, qualified_hi)，整数微米，含起点不含终点。"
            "返回值是同时满足全部有效计数时，合格片数可同时达到的最小值和最大值；"
            "已撤回的旧补报不参与计算。不逐片展开，按端点划分的整数规划精确求解。"
        ),
    )
    def get_bounds(
        inspection_id: int,
        qualified_lo: int = Query(..., ge=0, description="合格区间起点，包含"),
        qualified_hi: int = Query(..., description="合格区间终点，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> BoundsOut:
        if qualified_hi <= qualified_lo:
            raise HTTPException(
                status_code=422,
                detail="qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）",
            )
        with read_snapshot(connection):
            row = _require_inspection(connection, inspection_id)
            total_pieces = int(row["total_pieces"])
            constraints = db.load_constraints(connection, inspection_id)
        try:
            min_count, max_count = compute_bounds(
                total_pieces, constraints, qualified_lo, qualified_hi
            )
        except InconsistentCounts as exc:
            # 正常写入路径会拒绝不相容数据；此处防御数据库被外部改写。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        return BoundsOut(
            inspection_id=inspection_id,
            total_pieces=total_pieces,
            qualified_lo=qualified_lo,
            qualified_hi=qualified_hi,
            min_count=min_count,
            max_count=max_count,
        )

    @api.post(
        "/inspections/{inspection_id}/replacement-plan",
        response_model=ReplacementPlan,
        tags=["替换"],
        summary="试算替换方案（不落库）",
        description=(
            "按复测表试算：圈定同号下最多 10 份有效旧补报（新近优先），"
            "保留复测表与圈定范围外的有效补报，返回使计数相容且撤回份数最少的"
            "撤回方案；并列时按撤回报告号从大到小排列后取编号较大的一组。"
            "无解时 ``solvable=false``、``revoked_report_ids=null``。"
            "试算不加写锁，期间补报与总片数更正照常进行；提交替换时可用返回的 "
            "``basis`` 要求落库依据与试算一致。"
        ),
    )
    def replacement_plan(
        inspection_id: int,
        payload: ReplacementPlanIn,
        connection: Connection = Depends(get_conn),
    ) -> ReplacementPlan:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]
        with read_snapshot(connection):
            return _plan(
                connection, inspection_id, segments, payload.candidate_report_ids
            )

    @api.get(
        "/inspections/{inspection_id}/replacements",
        response_model=list[ReplacementOut],
        tags=["替换"],
        summary="查看替换历史",
    )
    def list_replacements(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> list[ReplacementOut]:
        with read_snapshot(connection):
            _require_inspection(connection, inspection_id)
            items = db.get_replacements(connection, inspection_id)
        return [_replacement_model(item) for item in items]

    def _commit_replacement(
        inspection_id: int,
        payload: ReplacementIn,
        connection: Connection,
    ) -> ReplacementOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            active_reports = db.load_active_reports(connection, inspection_id)
            actual_basis = ReplacementBasis(
                total_pieces=int(row["total_pieces"]),
                active_report_ids=[report["id"] for report in active_reports],
            )
            if payload.expected is not None and (
                payload.expected.total_pieces != actual_basis.total_pieces
                or set(payload.expected.active_report_ids)
                != set(actual_basis.active_report_ids)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "落库时的总片数或有效补报与试算依据不同"
                        f"（试算 {payload.expected.total_pieces} 片/"
                        f"{len(payload.expected.active_report_ids)} 份有效补报，"
                        f"当前 {actual_basis.total_pieces} 片/"
                        f"{len(actual_basis.active_report_ids)} 份有效补报），"
                        "整次替换已拒收"
                    ),
                )

            plan = _plan(
                connection,
                inspection_id,
                segments,
                payload.candidate_report_ids,
            )
            if plan.revoked_report_ids is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "复测表与保留的补报无法使计数相容："
                        f"在圈定的 {len(plan.candidates)} 份有效旧补报内无解，"
                        "整次替换已拒收，原补报保持不变"
                    ),
                )

            created_at = db.now()
            report_id = db.insert_report(
                connection, inspection_id, segments, created_at=created_at
            )
            replacement_id = db.insert_replacement(
                connection,
                inspection_id,
                report_id,
                plan.revoked_report_ids,
                created_at,
                total_pieces=actual_basis.total_pieces,
                basis_report_ids=actual_basis.active_report_ids,
            )
            db.revoke_reports(
                connection,
                plan.revoked_report_ids,
                replacement_id,
                created_at,
            )
            # 提交前在同一事务内复核：入库的复测表 + 撤回后剩余的有效补报
            # 必须确实相容，否则整次回滚（正常路径不会触发，属防御性检查）。
            try:
                check_consistent(
                    int(row["total_pieces"]),
                    db.load_constraints(connection, inspection_id),
                )
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="替换后有效计数仍不相容，整次替换已拒收",
                ) from exc
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        result = _replacement_out(connection, inspection_id, replacement_id)
        return result

    @api.post(
        "/inspections/{inspection_id}/replacements",
        response_model=ReplacementOut,
        status_code=status.HTTP_201_CREATED,
        tags=["替换"],
        summary="提交复测表替换旧补报",
        description=(
            "按质检号提交纸厚复测表。圈定同号下最多 10 份有效旧补报（新近优先），"
            "保留复测表与圈定范围外的有效补报，选出使计数相容且撤回份数最少的方案；"
            "并列时按撤回报告号从大到小排列后取编号较大的一组。"
            "新表入库与旧表撤回在同一事务内一并生效，返回撤回的报告号。"
            "试算无解，或落库时发现总片数/有效补报与 ``expected``（试算依据）"
            "不同，整次替换拒收、不写入任何数据。"
        ),
    )
    def submit_replacement(
        inspection_id: int,
        payload: ReplacementIn,
        connection: Connection = Depends(get_conn),
    ) -> ReplacementOut:
        return _commit_replacement(inspection_id, payload, connection)

    return api


app = create_app()
