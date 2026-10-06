"""请求迁移评估（Request Migration Assessment）引擎。

在已有分析（基线 / 候选两份 OpenAPI 文档包）上，上传一份**基线时期**的
NDJSON 调用记录，逐行把同一个具体请求分别对照两份文档的端点，评估这些
旧客户端请求在候选服务端上是否仍可接受：

* 请求行字段：``method`` / ``path`` / ``query`` / ``headers`` / ``cookies``
  及可选 ``request.media_type`` / ``request.body``；
* **兼容旧调用行**：接受与「调用覆盖」完全相同的 NDJSON 行（含
  ``response``），但本评估**只看请求**，``response`` 一律忽略；
* 同一具体路径在两侧分别做端点匹配（先精确路径、后唯一模板），并各自
  绑定**该侧自己的** path 参数——模板变量改名（``/pets/{id}`` →
  ``/pets/{petId}``）时绝不复用基线的参数绑定；
* 候选侧缺少端点为**确定失败**；候选侧多个模板同时命中为**待定**
  （多模板歧义，待人工核对），不做失败结论；
* 请求参数与请求体按与调用覆盖一致的**三态**校验（确定通过 / 确定失败 /
  待定），``pattern`` 等未支持约束按既有三态裁决；
* 逐行迁移裁决：

  === ================================= =========
  基线   候选        迁移裁决
  === ================================= =========
  pass  pass        ``unaffected``（双侧通过，未受影响）
  pass  fail        ``affected``（基线通过、候选失败，受影响）
  pass  pending     ``inconclusive``（待核）
  fail  *           ``not_applicable``（基线本就不接受，不适用）
  pending *         ``inconclusive``（基线能否通过无定论，待核）
  === ================================= =========

  另有行级 ``invalid``（行格式错误，两侧都无法评估）。

**注意：本评估不复用调用覆盖的结论（那是“响应状态码 × JSON 媒体类型”
覆盖格，含响应校验）**——它只复用同一套端点匹配与三态校验原语，对两侧
文档分别独立计算“请求是否被接受”，从而回答“旧请求迁移到候选后是否受
影响”。原覆盖结果与发布裁决均不受影响。

结果按端点（以**基线模板**为主键，端点缺失时标注候选侧情况）汇总受影响
请求与逐行原因；文档确定性序列化（排序键、行序固定）。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import coverage as cov
from .instance import TRI_FAIL, TRI_PASS, TRI_PENDING

SCHEMA_VERSION = "1.0"

# 逐行迁移裁决
AFFECTED = "affected"                 # 基线确定通过、候选确定失败
UNAFFECTED = "unaffected"             # 双侧确定通过
INCONCLUSIVE = "inconclusive"        # 待核
NOT_APPLICABLE = "not_applicable"    # 基线确定失败
INVALID = "invalid"                  # 行本身不是合法调用对象

MIGRATION_LABELS = {
    AFFECTED: "受影响",
    UNAFFECTED: "未受影响",
    INCONCLUSIVE: "待核",
    NOT_APPLICABLE: "不适用（基线失败）",
    INVALID: "行格式错误",
}

# 单侧请求结论（与调用覆盖三态一致，另加端点层面的缺失/歧义）
SIDE_MATCHED = "matched"             # 唯一命中端点
SIDE_MISSING = "endpoint_missing"    # 没有端点匹配
SIDE_AMBIGUOUS = "ambiguous"         # 多模板歧义


class MigrationError(ValueError):
    """上传内容整体无法用于迁移评估（例如没有任何可解析的行）。"""


@dataclass
class _SideVerdict:
    state: str                           # pass / fail / pending
    match: str                           # matched / endpoint_missing / ambiguous
    reasons: list
    operation: str | None = None
    matched_template: str | None = None

    def to_doc(self) -> dict:
        return {
            "state": self.state,
            "match": self.match,
            "operation": self.operation,
            "matched_template": self.matched_template,
            "reasons": self.reasons,
        }


# ============================================================ public API
def build_migration_assessment(pkg_baseline, pkg_candidate, raw: bytes,
                               source_file: str = "calls.ndjson") -> dict:
    """Evaluate baseline-era request calls against both document packages."""
    ctx_b = _SideContext(pkg_baseline)
    ctx_c = _SideContext(pkg_candidate)

    rows: list[dict] = []
    valid_calls: list[cov._Call] = []
    for line_no, raw_line in cov._iter_lines(raw):
        parsed = cov._parse_line(line_no, raw_line, require_response=False)
        if isinstance(parsed, dict) and "__error__" in parsed:
            rows.append({"line": line_no, "outcome": INVALID,
                         "method": None, "path": None,
                         "baseline": None, "candidate": None,
                         "reasons": [parsed["__error__"]]})
            continue
        valid_calls.append(parsed)

    if not valid_calls and not rows:
        raise MigrationError("NDJSON 为空或没有任何非空行")
    if not valid_calls:
        raise MigrationError(
            "NDJSON 中没有任何可解析的调用行："
            + "；".join(f"第{r['line']}行 {r['reasons'][0]}"
                        for r in rows[:5]))

    for call in valid_calls:
        rows.append(_evaluate_row(ctx_b, ctx_c, call))

    rows.sort(key=lambda r: r["line"])

    endpoints = _summarize_endpoints(rows)
    summary = _build_summary(rows, endpoints)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "OpenAPI Contract Evolution Workbench",
        "assessment": "request_migration",
        "source_file": source_file,
        "summary": summary,
        "endpoints": endpoints,
        "rows": rows,
    }


# ============================================================ side context
class _SideContext:
    """One side's schema graph + indexed operations (request-only checks)."""

    def __init__(self, pkg):
        self.graph = cov.SchemaGraph(pkg.files, pkg.entry)
        self.operations = cov._index_operations(self.graph)
        self.entry = pkg.entry

    def assess_request(self, call: cov._Call) -> _SideVerdict:
        op, template, reason = cov._match_operation(self.operations, call)
        if op is None:
            ambiguous = "同时命中" in reason
            return _SideVerdict(
                state=TRI_FAIL if not ambiguous else TRI_PENDING,
                match=SIDE_AMBIGUOUS if ambiguous else SIDE_MISSING,
                reasons=[reason])

        failures: list[str] = []
        undetermined: list[str] = []

        # 路径变量绑定只用**本侧**模板自己的占位符——变量改名时不复用
        # 另一侧的绑定（_template_match 以本侧 template 解析具体路径）。
        path_values = cov._template_match(template, call.path) or {}
        pf, pu = cov._check_parameters(
            self.graph, op, path_values, call, self.entry)
        failures.extend(pf)
        undetermined.extend(pu)

        self._check_request_body(op, call, failures, undetermined)

        state = TRI_FAIL if failures else (
            TRI_PENDING if undetermined else TRI_PASS)
        reasons = failures + undetermined
        return _SideVerdict(
            state=state, match=SIDE_MATCHED, reasons=reasons,
            operation=f"{op.method.upper()} {op.path}",
            matched_template=template)

    def _check_request_body(self, op, call, failures, undetermined) -> None:
        """请求体三态核对（镜像 coverage._evaluate 的请求体分支，不看响应）。"""
        req_site = op.body_site
        req_entity = cov._entity(call.request)
        if req_site is None:
            if req_entity is not None:
                failures.append("操作未声明 requestBody，但调用携带了请求体")
            return
        content = cov._content_map(req_site)
        required = bool(req_site[2].get("required"))
        if req_entity is None:
            if required:
                failures.append("requestBody 为必填，但调用未携带请求体")
            return
        if not content:
            failures.append("requestBody 未声明 content，但调用携带了请求体")
            return
        mt = cov._norm_mt(call.request.get("media_type"))
        if not mt:
            undetermined.append(
                "请求体未提供 media_type，无法判定 content 条目")
            return
        if not cov._is_json_mt(mt):
            # 非 JSON 请求媒体（如 XML / octet-stream / 表单）无法按 JSON
            # schema 三态核对——按既有“未支持约束”裁决为待定（待核），
            # 绝不静默当作通过。
            undetermined.append(
                f"请求媒体类型 {mt} 非 JSON，无法按 JSON schema 判定"
                "候选是否接受该请求体")
            return
        if mt not in {cov._norm_mt(k) for k in content}:
            failures.append(
                f"请求媒体类型 {mt} 未在 requestBody.content 中声明")
            return
        media = next(v for k, v in content.items() if cov._norm_mt(k) == mt)
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            node = cov._schema_node(self.graph, req_site, mt)
            if node.invalid or node.is_cycle:
                undetermined.append(
                    f"请求体 {mt} 的 schema 无法判定"
                    f"（{node.invalid or '循环引用'}）")
            else:
                cov._check_body(self.graph, f"请求体 {mt}", node,
                                call.request.get("body"),
                                failures, undetermined, self.entry)


# ============================================================ per-row eval
def _evaluate_row(ctx_b: _SideContext, ctx_c: _SideContext,
                  call: cov._Call) -> dict:
    base = {
        "line": call.line,
        "method": call.method.upper(),
        "path": call.path,
        "request_media_type": cov._norm_mt(call.request.get("media_type"))
        if cov._entity(call.request) is not None else None,
    }
    vb = ctx_b.assess_request(call)
    vc = ctx_c.assess_request(call)

    if vb.match == SIDE_AMBIGUOUS:
        outcome = INCONCLUSIVE
    elif vb.state == TRI_FAIL:
        outcome = NOT_APPLICABLE
    elif vb.state == TRI_PENDING:
        outcome = INCONCLUSIVE
    elif vc.state == TRI_PASS:
        outcome = UNAFFECTED
    elif vc.state == TRI_FAIL:
        outcome = AFFECTED
    else:
        outcome = INCONCLUSIVE

    return {
        **base,
        "outcome": outcome,
        "operation": vb.operation or vc.operation,
        "baseline": vb.to_doc(),
        "candidate": vc.to_doc(),
        "reasons": _row_reasons(outcome, vb, vc),
    }


def _row_reasons(outcome: str, vb: _SideVerdict, vc: _SideVerdict) -> list:
    """Human-readable per-line rationale (deterministic order)."""
    b_label = _state_text(vb)
    c_label = _state_text(vc)
    head = {
        AFFECTED: f"基线确定通过（{b_label}），候选确定失败（{c_label}）"
                  "→ 迁移受影响",
        UNAFFECTED: "双侧均确定通过 → 迁移未受影响",
        NOT_APPLICABLE: f"基线侧即确定失败（{b_label}）→ 该请求不属于"
                        "“基线接受、迁移后可能被拒”的评估范围，不适用",
        INCONCLUSIVE: "存在待定 / 歧义结论，迁移影响无法定论 → 待核",
        INVALID: "行格式错误",
    }[outcome]
    reasons = [head]
    for side_name, v in (("基线", vb), ("候选", vc)):
        for r in v.reasons:
            reasons.append(f"[{side_name}] {r}")
    return reasons


def _state_text(v: _SideVerdict) -> str:
    if v.match == SIDE_MISSING:
        return "无此端点"
    if v.match == SIDE_AMBIGUOUS:
        return "多模板歧义"
    return {TRI_PASS: "请求通过", TRI_FAIL: "请求失败",
            TRI_PENDING: "请求待定"}.get(v.state, v.state)


# ============================================================ endpoint summary
def _endpoint_key(row: dict) -> tuple[str, str, str]:
    """Group key: prefer baseline template (the contract being migrated from).

    Returns ``(method, operation_label, kind)`` where kind marks
    endpoint-removed rows so they group together even though the candidate
    has no operation.
    """
    b = row.get("baseline") or {}
    op = b.get("operation") or row.get("operation") or "—"
    try:
        method, _, template = op.partition(" ")
    except ValueError:
        method, template = "—", op
    return method or "—", template or "—", b.get("matched_template") or ""


def _summarize_endpoints(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for row in rows:
        if row["outcome"] == INVALID:
            continue
        method, template, b_tmpl = _endpoint_key(row)
        key = (method, template)
        g = groups.get(key)
        if g is None:
            b = row.get("baseline") or {}
            c = row.get("candidate") or {}
            g = {
                "method": method,
                "baseline_operation": b.get("operation"),
                "baseline_template": b.get("matched_template") or b_tmpl,
                "candidate_operation": c.get("operation"),
                "candidate_template": c.get("matched_template"),
                "candidate_endpoint_missing": False,
                "affected": [],
                "unaffected": [],
                "inconclusive": [],
                "not_applicable": [],
                "candidate_ambiguous": False,
            }
            groups[key] = g
        c = row.get("candidate") or {}
        if c.get("match") == SIDE_MISSING:
            g["candidate_endpoint_missing"] = True
        if c.get("match") == SIDE_AMBIGUOUS:
            g["candidate_ambiguous"] = True
        g[row["outcome"]].append(row["line"])

    out: list[dict] = []
    for (method, template), g in sorted(groups.items(),
                                       key=lambda kv: (kv[0][0], kv[0][1])):
        affected_lines = g["affected"]
        out.append({
            "method": method,
            "path": template,
            "baseline_operation": g["baseline_operation"],
            "baseline_template": g["baseline_template"],
            "candidate_operation": g["candidate_operation"],
            "candidate_template": g["candidate_template"],
            "candidate_endpoint_missing": g["candidate_endpoint_missing"],
            "candidate_ambiguous": g["candidate_ambiguous"],
            "affected_count": len(affected_lines),
            "unaffected_count": len(g["unaffected"]),
            "inconclusive_count": len(g["inconclusive"]),
            "not_applicable_count": len(g["not_applicable"]),
            "affected_lines": affected_lines,
            "unaffected_lines": g["unaffected"],
            "inconclusive_lines": g["inconclusive"],
            "not_applicable_lines": g["not_applicable"],
        })
    return out


def _build_summary(rows: list[dict], endpoints: list[dict]) -> dict:
    def count(outcome: str) -> int:
        return sum(1 for r in rows if r["outcome"] == outcome)

    affected = count(AFFECTED)
    unaffected = count(UNAFFECTED)
    inconclusive = count(INCONCLUSIVE)
    not_applicable = count(NOT_APPLICABLE)
    invalid = count(INVALID)
    return {
        "calls_total": len(rows),
        "calls_affected": affected,
        "calls_unaffected": unaffected,
        "calls_inconclusive": inconclusive,
        "calls_not_applicable": not_applicable,
        "calls_invalid": invalid,
        "endpoints_total": len(endpoints),
        "endpoints_with_affected": sum(
            1 for e in endpoints if e["affected_count"] > 0),
        "endpoints_missing_in_candidate": sum(
            1 for e in endpoints if e["candidate_endpoint_missing"]),
        "endpoints_ambiguous_in_candidate": sum(
            1 for e in endpoints if e["candidate_ambiguous"]),
    }
