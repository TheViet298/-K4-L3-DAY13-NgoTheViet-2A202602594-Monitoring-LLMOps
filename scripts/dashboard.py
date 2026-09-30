from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "dashboard.yaml"
LOGS = ROOT / "data" / "logs.jsonl"
COLORS = ("#176b5b", "#d57936", "#377ca5", "#9c4d68")
CHART_WIDTH = 700
CHART_HEIGHT = 220


def read_records(path: Path = LOGS) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("ts"), str):
                    continue
                try:
                    stamp = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                record["_time"] = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                records.append(record)
    except FileNotFoundError:
        pass
    return records


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(percentile / 100 * len(ordered) + 0.5) - 1))
    return ordered[index]


def _number(record: dict[str, Any], field: str) -> float | None:
    value = record.get(field)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_dashboard(records: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    end = current.astimezone(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=59)
    contract = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["dashboard"]
    selected = [record for record in records if start <= record.get("_time", start - timedelta(days=1)) <= end + timedelta(minutes=1)]
    minutes = [start + timedelta(minutes=index) for index in range(60)]
    groups: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for record in selected:
        minute = record["_time"].replace(second=0, microsecond=0)
        if start <= minute <= end:
            groups[minute].append(record)
    batches = [groups[minute] for minute in minutes]
    panels: list[dict[str, Any]] = []

    for spec in contract["panels"]:
        panel_id = spec["id"]
        series: dict[str, list[float | None]] = {}
        summary: list[tuple[str, str]] = []
        thresholds = [{"label": spec["threshold"]["aggregation"], "value": float(spec["threshold"]["value"])}]

        if panel_id == "latency":
            series = {
                label: [_percentile([v for row in batch if (v := _number(row, field)) is not None and row.get("event") == "response_sent"], pct) for batch in batches]
                for label, field, pct in (("P50", "latency_ms", 50), ("P95", "latency_ms", 95), ("P99", "latency_ms", 99), ("TTFT P95", "ttft_ms", 95))
            }
            responses = [row for row in selected if row.get("event") == "response_sent"]
            for label, field, pct in (("P50", "latency_ms", 50), ("P95", "latency_ms", 95), ("P99", "latency_ms", 99), ("TTFT P95", "ttft_ms", 95)):
                value = _percentile([v for row in responses if (v := _number(row, field)) is not None], pct)
                summary.append((label, "n/a" if value is None else f"{value:.0f} ms"))
        elif panel_id == "traffic":
            series = {"Requests/min": [float(sum(row.get("event") == "request_received" for row in batch)) for batch in batches]}
            count = sum(row.get("event") == "request_received" for row in selected)
            summary = [("Requests", str(count)), ("Average/min", f"{count / 60:.2f}")]
        elif panel_id == "errors":
            series = {
                "Error rate %": [
                    100 * sum(row.get("event") == "request_failed" for row in batch) / max(1, sum(row.get("event") == "request_received" for row in batch))
                    if any(row.get("event") == "request_received" for row in batch) else None
                    for batch in batches
                ],
                "Retrieval success %": [
                    100 * sum(row.get("tool_success") is True for row in batch if isinstance(row.get("tool_success"), bool)) / sum(isinstance(row.get("tool_success"), bool) for row in batch)
                    if any(isinstance(row.get("tool_success"), bool) for row in batch) else None
                    for batch in batches
                ],
            }
            requests = sum(row.get("event") == "request_received" for row in selected)
            failures = sum(row.get("event") == "request_failed" for row in selected)
            tool_rows = [row for row in selected if isinstance(row.get("tool_success"), bool)]
            summary = [("Error rate", f"{100 * failures / requests:.2f}%" if requests else "n/a"), ("Retrieval success", f"{100 * sum(row['tool_success'] for row in tool_rows) / len(tool_rows):.2f}%" if tool_rows else "n/a")]
            thresholds.append({"label": "retrieval success floor", "value": 90.0})
        elif panel_id == "cost":
            totals = [sum(_number(row, "cost_usd") or 0 for row in batch if row.get("event") == "response_sent") for batch in batches]
            running = 0.0
            cumulative = []
            for value in totals:
                running += value
                cumulative.append(running)
            series = {"Cumulative USD": cumulative}
            summary = [("60m total", f"${sum(totals):.4f}")]
        elif panel_id == "tokens":
            cumulative: dict[str, list[float]] = {"Input cumulative": [], "Output cumulative": []}
            input_total = output_total = 0.0
            for batch in batches:
                input_total += sum(_number(row, "tokens_in") or 0 for row in batch if row.get("event") == "response_sent")
                output_total += sum(_number(row, "tokens_out") or 0 for row in batch if row.get("event") == "response_sent")
                cumulative["Input cumulative"].append(input_total)
                cumulative["Output cumulative"].append(output_total)
            series = cumulative
            summary = [("Input tokens", f"{input_total:.0f}"), ("Output tokens", f"{output_total:.0f}")]
        elif panel_id == "quality":
            series = {"Mean score": [mean(scores) if (scores := [v for row in batch if row.get("event") == "response_sent" and (v := _number(row, "quality_score")) is not None]) else None for batch in batches]}
            scores = [v for row in selected if row.get("event") == "response_sent" and (v := _number(row, "quality_score")) is not None]
            summary = [("Mean quality", f"{mean(scores):.3f}" if scores else "n/a")]

        panels.append({"id": panel_id, "title": spec["title"], "unit": spec["unit"], "series": series, "thresholds": thresholds, "summary": summary})
    return {"title": contract["title"], "start": start, "end": end, "refresh": contract["refresh_seconds"], "panels": panels, "records": len(selected)}


def _chart(series: dict[str, list[float | None]], thresholds: list[dict[str, Any]], start: datetime, end: datetime) -> str:
    left, top, width, height = 52, 12, CHART_WIDTH - 68, CHART_HEIGHT - 48
    values = [value for points in series.values() for value in points if value is not None]
    values.extend(float(item["value"]) for item in thresholds)
    low = min(0.0, min(values, default=0.0))
    high = max(values, default=1.0)
    high = high + (high - low) * 0.08 if high != low else high + 1
    span = high - low or 1

    def y(value: float) -> float:
        return top + height - ((value - low) / span) * height

    svg = [f'<svg class="plot" viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" aria-label="{start:%H:%M} to {end:%H:%M} UTC">']
    for step in range(5):
        value = low + span * step / 4
        position = y(value)
        svg.append(f'<line class="gridline" x1="{left}" y1="{position:.1f}" x2="{left + width}" y2="{position:.1f}"/><text class="axis" x="{left - 7}" y="{position + 4:.1f}" text-anchor="end">{value:.1f}</text>')
    for index, label in ((0, start.strftime("%H:%M")), (30, (start + timedelta(minutes=30)).strftime("%H:%M")), (59, end.strftime("%H:%M"))):
        position = left + width * index / 59
        svg.append(f'<text class="axis" x="{position:.1f}" y="{CHART_HEIGHT - 9}" text-anchor="middle">{label}</text>')
    for index, threshold in enumerate(thresholds):
        position = y(float(threshold["value"]))
        color = "#bd503b" if index == 0 else "#bf7a27"
        svg.append(f'<line class="limit" stroke="{color}" x1="{left}" y1="{position:.1f}" x2="{left + width}" y2="{position:.1f}"/><text class="limit-label" fill="{color}" x="{left + width - 3}" y="{position - 4:.1f}" text-anchor="end">{escape(threshold["label"])} {threshold["value"]:g}</text>')
    for color_index, (label, points) in enumerate(series.items()):
        commands = []
        active = False
        for index, value in enumerate(points):
            if value is None:
                active = False
                continue
            x = left + width * index / max(1, len(points) - 1)
            commands.append(("L" if active else "M") + f"{x:.1f},{y(float(value)):.1f}")
            active = True
        color = COLORS[color_index % len(COLORS)]
        svg.append(f'<path d="{" ".join(commands)}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><title>{escape(label)}</title></path>')
    svg.append("</svg>")
    legend = "".join(f'<span><i style="--color:{COLORS[index % len(COLORS)]}"></i>{escape(label)}</span>' for index, label in enumerate(series))
    return "".join(svg) + f'<div class="legend">{legend}</div>'


def render(data: dict[str, Any]) -> str:
    sections = []
    for panel in data["panels"]:
        stats = "".join(f'<div><span>{escape(label)}</span><strong>{escape(value)}</strong></div>' for label, value in panel["summary"])
        sections.append(f'<section class="panel"><div class="panelhead"><div><small>{escape(panel["id"].upper())}</small><h2>{escape(panel["title"])}</h2></div><span class="unit">{escape(panel["unit"])}</span></div>{_chart(panel["series"],panel["thresholds"],data["start"],data["end"])}<div class="stats">{stats}</div></section>')
    start, end = data["start"].strftime("%Y-%m-%d %H:%M UTC"), data["end"].strftime("%Y-%m-%d %H:%M UTC")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="{data['refresh']}"><title>{escape(data['title'])}</title><style>
:root{{--ink:#182a24;--muted:#66766f;--paper:#eff3ef;--panel:#fff;--line:#dce5df;--green:#176b5b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 "Aptos","Segoe UI",sans-serif}}header{{background:#173e35;color:#f5fbf7;padding:22px max(22px,calc((100vw - 1440px)/2));display:flex;justify-content:space-between;align-items:end;gap:20px}}header h1{{font:600 25px/1.2 Georgia,serif;margin:4px 0 0;letter-spacing:0}}.kicker{{color:#b7d0c3;font-size:10px;font-weight:700;text-transform:uppercase}}.window{{text-align:right;color:#d6e5dd;font-size:12px;font-variant-numeric:tabular-nums}}main{{max-width:1440px;margin:auto;padding:18px 22px}}.toolbar{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin:0 0 14px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:15px 17px 12px;min-width:0;box-shadow:0 2px 8px #173e350a}}.panelhead{{display:flex;justify-content:space-between;gap:8px}}.panelhead small{{color:var(--green);font-size:10px;font-weight:700}}h2{{font-size:16px;line-height:1.25;margin:2px 0 0;letter-spacing:0}}.unit{{font-size:11px;color:var(--muted);white-space:nowrap}}.plot{{display:block;width:100%;height:auto;margin-top:8px}}.gridline{{stroke:#e8eeea;stroke-width:1}}.axis{{fill:#718079;font-size:10px}}.limit{{stroke-dasharray:5 4;stroke-width:1.5}}.limit-label{{font-size:9px}}.legend{{display:flex;gap:13px;flex-wrap:wrap;min-height:18px;color:var(--muted);font-size:10px}}.legend i{{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--color);margin-right:5px}}.stats{{display:flex;gap:20px;flex-wrap:wrap;border-top:1px solid #edf1ee;padding-top:10px;margin-top:7px}}.stats div{{display:grid;color:var(--muted);font-size:10px}}.stats strong{{font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums}}@media(max-width:760px){{header{{align-items:start;flex-direction:column}}.window{{text-align:left}}main{{padding:12px}}.grid{{grid-template-columns:1fr}}}}
</style></head><body><header><div><div class="kicker">K4-L3B / Observability</div><h1>{escape(data['title'])}</h1></div><div class="window">{start} — {end}<br>Rolling 60 minutes · refresh {data['refresh']} seconds · UTC</div></header><main><div class="toolbar"><span>Source: <code>data/logs.jsonl</code></span><span>{data['records']} structured records</span></div><div class="grid">{"".join(sections)}</div></main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = render(build_dashboard(read_records())).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the six-panel monitoring dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    server = ThreadingHTTPServer((options.host, options.port), Handler)
    print(f"Dashboard available at http://{options.host}:{options.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
