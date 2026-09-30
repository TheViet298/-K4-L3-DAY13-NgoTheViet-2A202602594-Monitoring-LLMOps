from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_validator(config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_dashboard.py"),
            "--config",
            str(config_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_repository_dashboard_contract_is_valid() -> None:
    result = run_validator(REPO_ROOT / "config" / "dashboard.yaml")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "6/6 panel" in result.stdout


def test_retrieval_success_uses_success_and_failure_events() -> None:
    payload = yaml.safe_load(
        (REPO_ROOT / "config" / "dashboard.yaml").read_text(encoding="utf-8")
    )
    errors_panel = next(
        panel for panel in payload["dashboard"]["panels"] if panel["id"] == "errors"
    )

    assert {"response_sent", "request_failed"} <= set(errors_panel["events"])
    assert "tool_success == true" in errors_panel["query"]
    assert "tool_success != null" in errors_panel["query"]


def test_runtime_dashboard_renders_six_panels_and_retrieval_success() -> None:
    from datetime import datetime, timezone

    from scripts.dashboard import build_dashboard, render

    timestamp = datetime(2026, 9, 30, 10, 29, tzinfo=timezone.utc)
    records = [
        {"_time": timestamp, "event": "request_received"},
        {
            "_time": timestamp,
            "event": "response_sent",
            "latency_ms": 3100,
            "ttft_ms": 80,
            "cost_usd": 0.01,
            "tokens_in": 20,
            "tokens_out": 30,
            "quality_score": 0.8,
            "tool_success": True,
        },
        {"_time": timestamp, "event": "request_received"},
        {"_time": timestamp, "event": "request_failed", "tool_success": False},
    ]

    data = build_dashboard(records, now=datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc))
    error_panel = next(panel for panel in data["panels"] if panel["id"] == "errors")
    page = render(data)

    assert len(data["panels"]) == 6
    assert error_panel["summary"] == [
        ("Error rate", "50.00%"),
        ("Retrieval success", "50.00%"),
    ]
    assert page.count('<section class="panel"') == 6
    assert "refresh 30 seconds" in page


def test_validator_rejects_panel_without_threshold(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (REPO_ROOT / "config" / "dashboard.yaml").read_text(encoding="utf-8")
    )
    del payload["dashboard"]["panels"][0]["threshold"]
    invalid_config = tmp_path / "dashboard.yaml"
    invalid_config.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result = run_validator(invalid_config)

    assert result.returncode == 1
    assert "latency.threshold" in result.stdout


def test_validator_rejects_panel_without_query_example(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (REPO_ROOT / "config" / "dashboard.yaml").read_text(encoding="utf-8")
    )
    payload["dashboard"]["panels"][0].pop("query", None)
    invalid_config = tmp_path / "dashboard.yaml"
    invalid_config.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result = run_validator(invalid_config)

    assert result.returncode == 1
    assert "latency.query" in result.stdout
