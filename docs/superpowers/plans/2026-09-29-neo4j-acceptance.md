# Neo4j 实际运行与验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立 Neo4j dataset 中导入 24 条 reviewed Gold，实际执行五个关系场景，并用 SQLite 结果逐项验收，同时让 Neo4j 不可用时产生明确错误。

**Architecture:** SQLite 仍是关系查询的对照源；Gold 先导入临时 SQLite 验证库，再通过 `sync_to_neo4j` 写入稳定命名的 Neo4j dataset。新增一个可复现的验收脚本输出导入统计、五场景逐项差异和测试证据；应用层不改 `analytics.py` 或前端展示。

**Tech Stack:** Python 3.11-3.13, FastAPI project, `neo4j` Python driver 5.x, Neo4j Community Docker image, pytest, Ruff, Markdown/JSON evidence.

**Spec:** 用户提供的“Neo4j 实际运行与验收”任务清单。

## Global Constraints

- 不修改 `backend/app/analytics.py`。
- 不修改 Gold 文件、前端查询展示和场景三/五金额逻辑。
- 必须覆盖多包、`unknown`、`nonwinner`、两方组合和三方组合。
- Neo4j 不可用时只能明确报错或显式回退 SQLite，不能静默返回空数组。
- 真实 Neo4j 集成测试不能用 `skipif` 永久跳过。

### Task 1: Confirm Gold input and Neo4j runtime

**Files:**
- Inspect: `backend/.data/annotation-tasks/**/gold*.json`, `pilot-b.ready/**`, `annotator-b.ready/**`
- Create: `backend/neo4j/docker-compose.yml`
- Create: `backend/neo4j/README.md`

**Interfaces:**
- Produces a verified Gold path containing 24 reviewed notices and a repeatable Neo4j startup command.

- [ ] Locate the updated 24-notice reviewed Gold and verify its status and count without editing it.
- [ ] Add a pinned Neo4j Community compose service exposing Bolt `7687` and HTTP `7474`, with a named volume and healthcheck.
- [ ] Install the existing optional dependency with `python -m pip install -e ".[graph,dev]"`.
- [ ] Start Neo4j and record the exact command, image, URI, database, and credentials in the verification report.

### Task 2: Add failing tests for explicit Neo4j failures and live integration

**Files:**
- Modify: `backend/tests/test_graph.py`
- Modify: `backend/app/graph.py` only if the failing tests expose missing error translation

**Interfaces:**
- `create_driver`/`sync_to_neo4j`/`query_neo4j` raise a non-empty `RuntimeError` for an unavailable endpoint.
- A live test executes against the configured endpoint and fails with an actionable message when the endpoint is absent instead of being skipped.

- [ ] Replace the current `skipif` live test with a required integration test that reads `BIDINTEL_TEST_NEO4J_URI` (defaulting to the compose Bolt URI) and uses `pytest.fail` with startup instructions when unavailable.
- [ ] Add a test that an unreachable Bolt endpoint raises `RuntimeError` containing the URI and connection cause, never returning an empty scene result.
- [ ] Run the new tests before implementation and confirm the expected failures.

### Task 3: Implement Gold-to-Neo4j five-scene validation

**Files:**
- Create: `backend/scripts/validate_neo4j_gold.py`
- Modify: `backend/app/graph_cli.py` only for explicit connection error reporting and validation command wiring if needed
- Test: `backend/tests/test_graph.py`

**Interfaces:**
- `validate_neo4j_gold.run(gold_path, driver, dataset, database, ...) -> dict` returns import counts, per-scene checked/passed/failed counts, mismatches, and normalized SQLite/Neo4j result pairs.
- The script creates a fresh SQLite database from reviewed Gold, calls `sync_to_neo4j`, executes all five scenes, and compares normalized JSON values.

- [ ] Reuse `validate_gold_queries` Gold import helpers without changing Gold or analytics semantics.
- [ ] Add deterministic two-party and three-party selections from real Gold, including explicit `unknown` and `nonwinner` cases and a multi-package project.
- [ ] Normalize decimal strings and order-only differences before comparison; preserve field-level mismatch details.
- [ ] Run the script against the live Neo4j dataset and save a JSON evidence report under `docs/benchmarks/`.
- [ ] Run the failing tests from Task 2 and make them pass with the smallest implementation.

### Task 4: Document runtime and acceptance evidence

**Files:**
- Create: `docs/NEO4J_VALIDATION.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/GAP_ANALYSIS.md`

**Interfaces:**
- Documentation names exact startup/install/import/query/test commands and includes captured counts and comparison outcome.

- [ ] Document Docker/Neo4j startup, credentials, database, dataset name, Gold SHA-256, and import node/relationship counts.
- [ ] Document actual five-scene results and SQLite comparison totals, including the multi-package/unknown/nonwinner/two-party/three-party cases.
- [ ] Update CHANGELOG with the date and verified scope.
- [ ] Close GAP 2.3 only for the verified scope and retain explicit limits.

### Task 5: Full verification

**Files:**
- Inspect: all changed files and `git diff --check`

- [ ] Run targeted graph tests with live Neo4j environment.
- [ ] Run the complete backend test suite and Ruff.
- [ ] Re-run the Gold validation script and verify the saved report has zero mismatches.
- [ ] Check `git diff --check`, review protected files, and report any remaining warnings or limitations.
