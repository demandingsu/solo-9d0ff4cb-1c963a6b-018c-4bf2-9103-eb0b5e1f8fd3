"""API 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    status: str = Field("active", description="active 有效 / revoked 已撤回")
    revoked_at: str | None = Field(None, description="撤回时间；未撤回为 null")
    revoked_by_replacement_id: int | None = Field(
        None, description="执行撤回的替换编号；未撤回为 null"
    )
    segments: list[SegmentOut]


class CandidateScopeMixin(BaseModel):
    candidate_report_ids: list[int] | None = Field(
        None,
        max_length=10,
        description="圈定的可撤回有效旧补报号（同一质检号下的补报号），最多 10 份；"
        "省略时自动圈定新近的 10 份。圈定范围外的有效补报一律保留。",
    )

    @model_validator(mode="after")
    def _validate_candidates(self):
        if self.candidate_report_ids is not None:
            if len(set(self.candidate_report_ids)) != len(self.candidate_report_ids):
                raise ValueError("圈定的补报号不得重复")
            if any(report_id <= 0 for report_id in self.candidate_report_ids):
                raise ValueError("补报号必须为正整数")
        return self


class ReplacementPlanIn(CandidateScopeMixin):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="复测表（拟提交的新补报）的全部厚度区间计数。",
    )


class ReplacementBasis(BaseModel):
    """试算依据：总片数与当时的全部有效补报号。

    提交替换时把这些值回传，可保证落库检查针对的就是试算时的依据；
    期间若有新补报被接收或总片数被更正，整次替换拒收。
    """

    total_pieces: int = Field(..., ge=0, description="试算时的总片数")
    active_report_ids: list[int] = Field(
        ..., description="试算时的全部有效补报号，升序"
    )


class ReplacementIn(CandidateScopeMixin):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="复测表（新补报）的全部厚度区间计数。",
    )
    expected: ReplacementBasis | None = Field(
        None,
        description="试算时取得的依据；提供后落库时逐项核对，不同则整次替换拒收。",
    )


class ReplacementCandidate(BaseModel):
    report_id: int
    created_at: str
    segments: list[SegmentOut]


class ReplacementPlan(BaseModel):
    inspection_id: int
    total_pieces: int
    basis: ReplacementBasis
    candidates: list[ReplacementCandidate] = Field(
        ..., description="圈定的可撤回有效旧补报，最多 10 份，新近优先"
    )
    revoked_report_ids: list[int] | None = Field(
        ..., description="使新旧计数相容且撤回份数最少所要撤回的补报号；无解为 null"
    )
    solvable: bool


class ReplacementOut(BaseModel):
    replacement_id: int
    inspection_id: int
    created_at: str
    report: CountReportOut = Field(..., description="本次入库的复测表")
    revoked_report_ids: list[int] = Field(
        ..., description="本次一并撤回的旧补报号，升序"
    )
    basis: ReplacementBasis = Field(..., description="生效时依据的总片数与有效补报号")


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="全部补报批次数，含已撤回")
    segment_count: int = Field(..., description="全部厚度区间条数，含已撤回")
    active_report_count: int = Field(..., description="当前有效补报批次数")
    revoked_report_count: int = Field(..., description="已撤回补报批次数")
    active_segment_count: int = Field(..., description="当前有效厚度区间条数")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut] = Field(
        ..., description="补报历史，含已撤回补报及其撤回状态"
    )
    replacements: list[ReplacementOut] = Field(..., description="替换历史")


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")
