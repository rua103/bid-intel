"""Run a bounded, model-free smoke validation and emit machine-readable evidence.

This script is intentionally independent from the application entry points. It
records timings and failures without collecting environment variables or API keys.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
OUT = BACKEND / ".data" / "resource-limited-validation.json"


def validate_demo_api(api_base: str) -> dict[str, object]:
    """Exercise the synthetic dataset through the live FastAPI process."""
    import httpx

    started = time.perf_counter()
    record: dict[str, object] = {"label": "offline demo API workflow", "status": "failed"}
    try:
        with httpx.Client(base_url=api_base, timeout=10) as client:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    if client.get("/api/v1/health").is_success:
                        break
                except httpx.HTTPError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("backend health check timed out")

            datasets = client.get("/api/v1/datasets")
            datasets.raise_for_status()
            data = datasets.json()
            if isinstance(data, dict):
                data = data.get("datasets", data.get("items", []))
            demo = next((row for row in data if "离线演示样例" in str(row)), None)
            dataset_id = (demo or {}).get("dataset_id") or (demo or {}).get("id")
            if not dataset_id:
                raise RuntimeError("offline synthetic dataset was not listed")
            headers = {"X-Dataset-ID": str(dataset_id)}
            organizations = client.get("/api/v1/organizations", headers=headers)
            organizations.raise_for_status()
            orgs = organizations.json()
            orgs = orgs.get("organizations", orgs) if isinstance(orgs, dict) else orgs
            by_name = {row["canonical_name"]: row["id"] for row in orgs}
            buyers = [row for row in by_name if "公共服务中心" in row]
            suppliers = [row for row in by_name if "打印科技" in row]
            if not buyers or not suppliers:
                raise RuntimeError("expected synthetic organizations were not found")
            buyer_id, supplier_id = by_name[buyers[0]], by_name[suppliers[0]]
            calls = [
                ("GET", f"/api/v1/analytics/buyers/{buyer_id}/awardees", None),
                ("GET", f"/api/v1/analytics/buyers/{buyer_id}/bidders", None),
                ("GET", f"/api/v1/analytics/suppliers/{supplier_id}/co-bidders", None),
                ("POST", "/api/v1/analytics/common-buyers", {"organization_ids": [supplier_id, buyer_id]}),
                ("POST", "/api/v1/analytics/common-projects", {"organization_ids": [supplier_id, buyer_id]}),
            ]
            statuses = []
            for method, path, body in calls:
                response = client.request(method, path, headers=headers, json=body)
                response.raise_for_status()
                statuses.append(response.status_code)

            items_response = client.get("/api/v1/items", headers=headers)
            items_response.raise_for_status()
            items = items_response.json()
            if not items:
                raise RuntimeError("synthetic dataset contained no items")
            detail = client.get(f"/api/v1/notices/{items[0]['notice_id']}", headers=headers)
            detail.raise_for_status()
            evidence_status = "available" if any(
                item.get("source_evidence") and item.get("source_location")
                for item in detail.json().get("items", [])
            ) else "missing"

            # This checks list availability only, not pause/resume or job recovery.
            jobs = client.get("/api/v1/jobs")
            jobs.raise_for_status()
            record.update({
                "five_query_http_statuses": statuses,
                "evidence_detail": evidence_status,
                "jobs_list_http_status": jobs.status_code,
                "notice_detail_http_status": detail.status_code,
                "job_pause_resume_verified": False,
                "dataset_id": str(dataset_id),
            })
            if statuses != [200] * 5 or evidence_status != "available":
                raise RuntimeError("query or evidence-detail assertion failed")
            record["status"] = "passed"
    except Exception as exc:  # noqa: BLE001 - evidence report must preserve exact blocker
        record["failure_reason"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return record


def _output_text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def run(
    label: str, command: list[str], cwd: Path, timeout: int = 180,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env={**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        elapsed = round(time.perf_counter() - started, 3)
        return {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "elapsed_seconds": elapsed,
            "returncode": proc.returncode,
            "status": "passed" if proc.returncode == 0 else "failed",
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "returncode": None,
            "status": "timeout",
            "stdout_tail": _output_text(exc.stdout)[-2000:],
            "stderr_tail": _output_text(exc.stderr)[-2000:],
        }
    except OSError as exc:
        return {
            "label": label, "command": command, "cwd": str(cwd),
            "status": "failed", "failure_reason": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def main() -> int:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    records = [
        run("backend attachment/query regression", [sys.executable, "-m", "pytest", "-q", "tests/test_attachment_readiness.py", "tests/test_jobs.py"], BACKEND),
        run("backend ruff", [sys.executable, "-m", "ruff", "check", "app", "tests"], BACKEND),
        ({
            "label": "frontend production build",
            "status": "blocked",
            "failure_reason": "Node/npm is not installed in the Python-only validation image",
        } if npm is None else run("frontend production build", [npm, "run", "build"], ROOT / "frontend", timeout=240)),
    ]
    with tempfile.TemporaryDirectory(prefix="bid-intel-validation-") as directory:
        validation_env = {
            **os.environ, "EXTRACTION_MODE": "rules", "ANALYTICS_BACKEND": "sqlite",
            "DATABASE_PATH": str(Path(directory) / "validation.sqlite"),
            "AUTH_ENABLED": "false", "MODEL_BASE_URL": "", "MODEL_API_KEY": "", "MODEL_NAME": "",
        }
        records.append(run(
            "offline seed", [sys.executable, "-m", "app.demo_seed"], BACKEND, env=validation_env,
        ))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=validation_env,
        )
        try:
            records.append(validate_demo_api(f"http://127.0.0.1:{port}"))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    payload = {
        "schema_version": 1,
        "verification_mode": "resource-limited approximation",
        "runtime": {"host_os": os.name, "python": sys.version.split()[0]},
        "limits": {
            "enforced_by_script": False,
            "verification": "external container CPU/memory limits must be recorded by the caller",
        },
        "records": records,
        "api_key_logged": False,
        "holdout_modified": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep console output valid even in a legacy Windows GBK terminal.
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if all(row["status"] == "passed" for row in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
