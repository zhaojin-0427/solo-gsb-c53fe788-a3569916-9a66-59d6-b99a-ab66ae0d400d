"""请求迁移评估（Request Migration Assessment）引擎与 HTTP API 测试。"""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _zip_bytes(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, folder))
    return buf.getvalue()


def _ndjson(rows) -> bytes:
    return ("\n".join(
        r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        for r in rows)).encode()


@pytest.fixture(scope="module")
def analysis_id(client):
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", _zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", _zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


MIG = "/api/analyses/{aid}/migration/requests"


# ============================================================== example file
def test_example_requests_ndjson_end_to_end(client, analysis_id):
    with open("examples/requests-baseline.ndjson", "rb") as fh:
        r = client.post(
            MIG.format(aid=analysis_id),
            files={"file": ("requests.ndjson", fh.read(),
                            "application/x-ndjson")})
    assert r.status_code == 201, r.text
    doc = r.json()
    s = doc["summary"]
    # 枚举收窄 / 可选变必填 / 端点删除 = 3 行受影响；oneOf 一过一待定 = 待核
    assert s["calls_affected"] == 3
    assert s["calls_unaffected"] == 4
    assert s["calls_inconclusive"] == 1
    assert s["calls_not_applicable"] == 2
    by_line = {row["line"]: row for row in doc["rows"]}
    assert by_line[2]["outcome"] == "affected"          # enum 收窄
    assert by_line[4]["outcome"] == "affected"          # 新增必填 status
    assert by_line[7]["outcome"] == "affected"          # /legacy/echo 删除
    assert by_line[7]["candidate"]["match"] == \
        "endpoint_missing"
    assert by_line[8]["outcome"] == "inconclusive"      # oneOf 一过一待定
    assert by_line[8]["baseline"]["state"] == "pending"
    assert by_line[8]["candidate"]["state"] == "fail"
    # 汇总端点名：受影响端点（候选缺端点标记）
    eps = {e["path"]: e for e in doc["endpoints"]}
    assert eps["/legacy/echo"]["candidate_endpoint_missing"] is True
    assert 7 in eps["/legacy/echo"]["affected_lines"]


def test_legacy_calls_with_response_are_accepted_and_response_ignored(
        client, analysis_id):
    """旧调用覆盖 NDJSON（含 response）可直接上传；response 被忽略。"""
    with open("examples/calls-baseline.ndjson", "rb") as fh:
        r = client.post(
            MIG.format(aid=analysis_id),
            files={"file": ("calls.ndjson", fh.read(),
                            "application/x-ndjson")})
    assert r.status_code == 201, r.text
    doc = r.json()
    # 覆盖文件中第 10 行 response 599（覆盖校验会失败），但迁移评估只看
    # 请求——response 完全被忽略；该行在候选侧因 status 变必填而受影响
    by_line = {row["line"]: row for row in doc["rows"]}
    row10 = by_line[10]
    assert row10["baseline"]["state"] == "pass"
    assert "status" not in row10
    assert row10["candidate"]["state"] == "fail"
    assert row10["outcome"] == "affected"


# ============================================================== verdict rules
def _analysis(client, baseline: dict, candidate: dict | None = None) -> str:
    candidate = candidate or baseline
    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.json", json.dumps(baseline).encode(),
                         "application/json"),
            "candidate": ("c.json", json.dumps(candidate).encode(),
                          "application/json"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": paths}


def _upload(client, aid, rows):
    return client.post(
        MIG.format(aid=aid),
        files={"file": ("r.ndjson", _ndjson(rows),
                        "application/x-ndjson")})


def test_affected_only_when_baseline_pass_and_candidate_fail(client):
    b = _spec({"/p": {"get": {"responses": {"200": {"description": "o"}}}}})
    c = _spec({"/p": {"get": {"parameters": [
        {"name": "q", "in": "query", "required": True,
         "schema": {"type": "string"}}],
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    r = _upload(client, aid, [
        {"method": "GET", "path": "/p"},                          # b✓ c✗
        {"method": "GET", "path": "/p", "query": {"q": "x"}},     # ✓ ✓
    ])
    doc = r.json()
    assert [x["outcome"] for x in doc["rows"]] == \
        ["affected", "unaffected"]


def test_baseline_fail_is_not_applicable_even_when_candidate_fails(client):
    spec = _spec({"/p": {"get": {"parameters": [
        {"name": "q", "in": "query", "required": True,
         "schema": {"type": "string"}}],
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, spec, spec)
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/p"}]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "not_applicable"
    assert row["baseline"]["state"] == "fail"
    assert row["candidate"]["state"] == "fail"
    assert doc["summary"]["calls_affected"] == 0
    assert doc["summary"]["calls_not_applicable"] == 1


def test_candidate_missing_endpoint_is_definite_fail_and_affected(client):
    b = _spec({"/p": {"get": {"responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, _spec({}))
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/p"}]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "affected"
    assert row["candidate"]["state"] == "fail"
    assert row["candidate"]["match"] == "endpoint_missing"


def test_exact_path_without_method_is_missing_not_ambiguous(client):
    b = _spec({"/p": {"get": {"responses": {"200": {"description": "o"}}}}})
    c = _spec({"/p": {"post": {"responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/p"}]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "affected"
    assert row["candidate"]["match"] == "endpoint_missing"


def test_template_variable_renamed_does_not_reuse_baseline_binding(client):
    b = _spec({"/items/{id}": {"get": {"parameters": [
        {"name": "id", "in": "path", "required": True,
         "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "o"}}}}})
    c = _spec({"/items/{itemId}": {"get": {"parameters": [
        {"name": "itemId", "in": "path", "required": True,
         "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/items/5"}]).json()
    row = doc["rows"][0]
    assert row["baseline"]["matched_template"] == "/items/{id}"
    assert row["candidate"]["matched_template"] == "/items/{itemId}"
    assert row["outcome"] == "unaffected"


def test_template_variable_renamed_with_narrowed_type_is_affected(client):
    b = _spec({"/items/{id}": {"get": {"parameters": [
        {"name": "id", "in": "path", "required": True,
         "schema": {"type": "string"}}],
        "responses": {"200": {"description": "o"}}}}})
    c = _spec({"/items/{itemId}": {"get": {"parameters": [
        {"name": "itemId", "in": "path", "required": True,
         "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/items/abc"}]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pass"
    assert row["candidate"]["state"] == "fail"
    assert row["outcome"] == "affected"


def test_candidate_template_ambiguity_is_inconclusive(client):
    b = _spec({"/items/{id}": {"get": {"parameters": [
        {"name": "id", "in": "path", "required": True,
         "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "o"}}}}})
    c = _spec({
        "/items/{a}": {"get": {"responses": {"200": {"description": "o"}}}},
        "/items/{b}": {"get": {"responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    doc = _upload(client, aid,
                  [{"method": "GET", "path": "/items/5"}]).json()
    row = doc["rows"][0]
    assert row["outcome"] == "inconclusive"
    assert row["candidate"]["state"] == "pending"
    assert row["candidate"]["match"] == "ambiguous"
    ep = doc["endpoints"][0]
    assert ep["candidate_ambiguous"] is True


def test_unsupported_constraint_pending_on_baseline_is_inconclusive(client):
    """基线 oneOf 一过一待定（未支持 not）→ 不宣称受影响，待核。"""
    b = _spec({"/d": {"post": {"requestBody": {"required": True,
        "content": {"application/json": {"schema": {"oneOf": [
            {"type": "object"},
            {"type": "object", "not": {"type": "string"}}]}}}},
        "responses": {"200": {"description": "o"}}}}})
    c = _spec({"/d": {"post": {"requestBody": {"required": True,
        "content": {"application/json": {"schema": {"type": "string"}}}},
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, b, c)
    doc = _upload(client, aid, [{
        "method": "POST", "path": "/d",
        "request": {"media_type": "application/json", "body": {}}}]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pending"
    assert row["outcome"] == "inconclusive"
    assert "not" in row["baseline"]["reasons"][0]


def test_non_json_request_media_is_pending_not_pass(client):
    spec = _spec({"/x": {"post": {"requestBody": {
        "content": {"application/json": {"schema": {"type": "object"}}}},
        "responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, spec, spec)
    doc = _upload(client, aid, [{
        "method": "POST", "path": "/x",
        "request": {"media_type": "application/xml",
                    "body": "<a/>"}}]).json()
    row = doc["rows"][0]
    assert row["baseline"]["state"] == "pending"
    assert row["outcome"] == "inconclusive"


def test_invalid_rows_reported_separately(client):
    aid = _analysis(client,
                    _spec({"/p": {"get": {"responses":
                        {"200": {"description": "o"}}}}}))
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p"},
        {"path": "/p"},
        {"method": "WAT", "path": "/p"},
    ]).json()
    assert [x["outcome"] for x in doc["rows"]] == \
        ["unaffected", "invalid", "invalid"]
    assert doc["summary"]["calls_invalid"] == 2


def test_empty_ndjson_is_400(client):
    aid = _analysis(client,
                    _spec({"/p": {"get": {"responses":
                        {"200": {"description": "o"}}}}}))
    r = _upload(client, aid, [])
    assert r.status_code == 400


# ============================================================== persistence
def test_migration_persistence_get_delete_export(client, analysis_id):
    rows = [{"method": "GET", "path": "/pets",
             "query": {"status": "pending"}}]
    assert _upload(client, analysis_id, rows).status_code == 201

    got = client.get(MIG.format(aid=analysis_id))
    assert got.status_code == 200
    assert got.json()["summary"]["calls_affected"] == 1

    # 确定性导出：两次导出字节一致，带指纹头
    e1 = client.get(MIG.format(aid=analysis_id) + "/export")
    e2 = client.get(MIG.format(aid=analysis_id) + "/export")
    assert e1.content == e2.content
    assert e1.headers.get("X-Migration-Fingerprint")
    d1 = json.loads(e1.content)
    assert d1["assessment"] == "request_migration"

    # 覆盖接口可见迁移评估可用性
    cov = client.get(
        f"/api/analyses/{analysis_id}/coverage").json()
    assert cov["request_migration"]["available"] is True

    # 迁移评估嵌入总导出，但不影响覆盖/发布
    full = client.get(
        f"/api/analyses/{analysis_id}/export").json()
    assert full["request_migration"]["summary"]["calls_affected"] == 1

    assert client.delete(
        MIG.format(aid=analysis_id)).status_code == 200
    assert client.get(
        MIG.format(aid=analysis_id)).status_code == 404


def test_migration_does_not_mutate_coverage(client, analysis_id):
    """上传迁移评估不改变已保存的调用覆盖。"""
    cov_rows = [{"method": "GET", "path": "/pets",
                 "request": {},
                 "response": {"status": 200, "media_type":
                              "application/json", "body": []}}]
    cr = client.post(
        f"/api/analyses/{analysis_id}/coverage/baseline",
        files={"file": ("c.ndjson", _ndjson(cov_rows),
                        "application/x-ndjson")})
    assert cr.status_code == 201
    before = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline").json()

    _upload(client, analysis_id,
            [{"method": "GET", "path": "/pets",
              "query": {"status": "pending"}}])

    after = client.get(
        f"/api/analyses/{analysis_id}/coverage/baseline").json()
    assert after == before


def test_malformed_response_is_ignored_completely(client):
    """迁移评估忽略 response：即使 response 非对象 / status 非法也不判 invalid。"""
    spec = _spec({"/p": {"get": {"responses": {"200": {"description": "o"}}}}})
    aid = _analysis(client, spec, spec)
    doc = _upload(client, aid, [
        {"method": "GET", "path": "/p", "response": "not-an-object"},
        {"method": "GET", "path": "/p", "response": {"status": 599}},
        {"method": "GET", "path": "/p"},
    ]).json()
    assert [r["outcome"] for r in doc["rows"]] == \
        ["unaffected", "unaffected", "unaffected"]


def test_migration_requires_existing_analysis(client):
    r = client.post(
        MIG.format(aid="deadbeefdeadbeef"),
        files={"file": ("r.ndjson", _ndjson(
            [{"method": "GET", "path": "/p"}]),
            "application/x-ndjson")})
    assert r.status_code == 404
