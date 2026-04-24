#!/usr/bin/env python3
"""Build a local HTML report from Jobcan staff man-hour details."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from extract_staff_names import GET_RECORD_URL, build_params, normalize_text
from fetch_jobcan_man_hours import (
    ADMIN_URL,
    ATTENDANCE_URL,
    DEFAULT_BROWSER,
    MAN_HOUR_URL,
    build_search_period,
    find_group_id,
    get_request,
    login_to_jobcan_id,
)
from login_jobcan import SIGN_IN_URL, load_config


BASE_URL = "https://ssl.jobcan.jp"
COLORS = [
    "#2f80ed",
    "#27ae60",
    "#f2994a",
    "#eb5757",
    "#9b51e0",
    "#56ccf2",
    "#f2c94c",
    "#6fcf97",
]
REPORT_FONT_FAMILY = '"Noto Sans JP", "Noto Sans", sans-serif'
SVG_FONT_FAMILY = "Noto Sans JP, Noto Sans, sans-serif"


def progress(message: str) -> None:
    print(f"[progress] {message}", file=sys.stderr, flush=True)


@dataclass
class StaffLink:
    name: str
    href: str
    employee_id: str


@dataclass
class DailyRecord:
    date: str
    total_working_hours: str
    total_man_hours: str
    unix_time: str
    detail_html: str = ""
    detail_text: str = ""


@dataclass
class StaffReport:
    name: str
    employee_id: str
    detail_url: str
    chart_svg: str
    chart_items: list[tuple[str, int]] = field(default_factory=list)
    days: list[DailyRecord] = field(default_factory=list)


class StaffLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[StaffLink] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        href = attr_map.get("href", "")
        if "selected_employee_id=" not in href:
            return
        self._href = html.unescape(href)
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        name = normalize_text(" ".join(self._parts))
        match = re.search(r"selected_employee_id=(\d+)", self._href)
        if name and match:
            self.links.append(StaffLink(name=name, href=self._href, employee_id=match.group(1)))
        self._href = None
        self._parts = []


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def text_from_html(fragment: str) -> str:
    parser = TextCollector()
    parser.feed(fragment)
    return normalize_text(" ".join(parser.parts))


def clean_detail_text(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"^×\s*", "", text)
    text = re.sub(r"^\d{4}/\d{2}/\d{2}\([^)]*\)\s*", "", text)
    text = re.sub(r"実労働時間＝\d{2}:\d{2}\s*", "", text)
    text = text.replace("No プロジェクト タスク 工数(時間)", "")
    return normalize_text(text)


def man_hours_match(total_working_hours: str, total_man_hours: str) -> bool:
    if total_working_hours == "00:00" and total_man_hours == "入力がありません":
        return True
    return total_working_hours == total_man_hours


def weekend_row_class(date_text: str) -> str:
    normalized = date_text.replace(" ", "")
    if "(土)" in normalized:
        return "saturday"
    if "(日)" in normalized:
        return "sunday"
    return ""


def js_var_json(page: str, var_name: str) -> Any:
    match = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*(.*?);", page, flags=re.DOTALL)
    if not match:
        return {}
    return json.loads(match.group(1))


def parse_staff_links(record_json: dict[str, Any]) -> list[StaffLink]:
    parser = StaffLinkParser()
    parser.feed(str(record_json.get("detail", "")))
    return parser.links


def parse_daily_records(page: str) -> list[DailyRecord]:
    table_match = re.search(
        r"(<table class='man-hour-table'>.*?<th>日付</th>.*?</table>)",
        page,
        flags=re.DOTALL,
    )
    if not table_match:
        return []

    records: list[DailyRecord] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), flags=re.DOTALL):
        if "<th>" in row:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL)
        if len(cells) < 4:
            continue
        onclick = re.search(r"openEditWindow\((\d+),\s*(\d+)\)", cells[3])
        records.append(
            DailyRecord(
                date=text_from_html(cells[0]),
                total_working_hours=text_from_html(cells[1]),
                total_man_hours=text_from_html(cells[2]),
                unix_time=onclick.group(1) if onclick else "",
            )
        )
    return records


def graph_items(page: str) -> list[tuple[str, int]]:
    graph_data = js_var_json(page, "graphData")
    project_master = js_var_json(page, "projectMaster")
    task_master = js_var_json(page, "taskMaster")

    items: list[tuple[str, int]] = []
    for project_id, tasks in graph_data.items():
        project_name = project_master.get(str(project_id), {}).get("project_name", project_id)
        for task_id, minutes in tasks.items():
            task_name = task_master.get(str(task_id), {}).get("task_name", task_id)
            items.append((f"{project_name} - {task_name}", int(minutes)))
    return items


def pie_slice_path(cx: float, cy: float, r: float, start: float, end: float) -> str:
    x1 = cx + r * math.cos(start)
    y1 = cy + r * math.sin(start)
    x2 = cx + r * math.cos(end)
    y2 = cy + r * math.sin(end)
    large = 1 if end - start > math.pi else 0
    return f"M {cx:.3f} {cy:.3f} L {x1:.3f} {y1:.3f} A {r:.3f} {r:.3f} 0 {large} 1 {x2:.3f} {y2:.3f} Z"


def build_chart_svg(items: list[tuple[str, int]], title: str) -> str:
    total = sum(minutes for _, minutes in items)
    width = 760
    height = max(300, 130 + len(items) * 28)
    cx = 150
    cy = 150
    r = 110

    if total <= 0:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="220" '
            'viewBox="0 0 760 220"><text x="24" y="42" font-size="20">'
            f"{html.escape(title)}</text><text x=\"24\" y=\"86\">グラフデータがありません</text></svg>"
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="34" font-size="19" font-family="{SVG_FONT_FAMILY}">{html.escape(title)}</text>',
    ]
    start = -math.pi / 2
    for index, (label, minutes) in enumerate(items):
        end = start + (minutes / total) * math.pi * 2
        color = COLORS[index % len(COLORS)]
        if minutes == total:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        else:
            parts.append(f'<path d="{pie_slice_path(cx, cy, r, start, end)}" fill="{color}"/>')
        start = end

    legend_x = 310
    legend_y = 86
    for index, (label, minutes) in enumerate(items):
        color = COLORS[index % len(COLORS)]
        hours = minutes // 60
        mins = minutes % 60
        pct = minutes / total * 100
        y = legend_y + index * 28
        parts.append(f'<rect x="{legend_x}" y="{y - 13}" width="16" height="16" fill="{color}"/>')
        parts.append(
            f'<text x="{legend_x + 24}" y="{y}" font-size="13" font-family="{SVG_FONT_FAMILY}">'
            f"{html.escape(label)}: {hours:02d}:{mins:02d} ({pct:.1f}%)</text>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ン_-]+", "_", value).strip("_") or "staff"


def report_html_filename(config: dict[str, Any], group_name: str) -> str:
    year = config.get("year")
    month = int(config["month"])
    year_text = str(year) if year is not None else "unknown-year"
    return f"{year_text}_{month:02d}_{safe_filename(group_name)}.html"


def report_output_stem(config: dict[str, Any], group_name: str) -> str:
    year = config.get("year")
    month = int(config["month"])
    year_text = str(year) if year is not None else "unknown-year"
    return f"{year_text}_{month:02d}_{safe_filename(group_name)}"


def create_session(config: dict[str, Any], timeout: float, browser: str | None = None):
    opener = login_to_jobcan_id(config, timeout, browser)
    with opener.open(get_request(ATTENDANCE_URL, SIGN_IN_URL), timeout=timeout) as response:
        response.read()
        attendance_url = response.geturl()
    with opener.open(get_request(ADMIN_URL, attendance_url), timeout=timeout) as response:
        response.read()
        admin_url = response.geturl()
    with opener.open(get_request(MAN_HOUR_URL, admin_url), timeout=timeout) as response:
        man_hour_html = response.read()
        man_hour_url = response.geturl()
    return opener, admin_url, man_hour_url, man_hour_html


def fetch_record_json(opener, man_hour_url: str, config: dict[str, Any], group_id: str, timeout: float) -> dict[str, Any]:
    params = build_params(config, group_id)
    url = f"{GET_RECORD_URL}?{urlencode(params, doseq=True)}"
    with opener.open(get_request(url, man_hour_url), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def fetch_staff_report(
    opener,
    staff: StaffLink,
    charts_dir: Path,
    charts_dir_name: str,
    timeout: float,
    staff_index: int,
    staff_total: int,
) -> StaffReport:
    progress(f"({staff_index}/{staff_total}) {staff.name}: fetching staff detail page")
    detail_url = urljoin(BASE_URL, staff.href)
    with opener.open(get_request(detail_url, MAN_HOUR_URL), timeout=timeout) as response:
        detail_page = response.read().decode("utf-8", errors="replace")

    items = graph_items(detail_page)
    chart_svg = build_chart_svg(items, staff.name)
    chart_name = f"{safe_filename(staff.name)}_{staff.employee_id}.svg"
    chart_path = charts_dir / chart_name
    chart_path.write_text(chart_svg, encoding="utf-8")

    report = StaffReport(
        name=staff.name,
        employee_id=staff.employee_id,
        detail_url=detail_url,
        chart_svg=f"{charts_dir_name}/{chart_name}",
        chart_items=items,
        days=parse_daily_records(detail_page),
    )

    detail_targets = [day for day in report.days if day.unix_time]
    progress(
        f"({staff_index}/{staff_total}) {staff.name}: "
        f"parsed {len(report.days)} days, fetching {len(detail_targets)} daily details"
    )
    fetched_details = 0
    for day in report.days:
        if not day.unix_time:
            continue
        edit_url = (
            f"{MAN_HOUR_URL}/get-man-hour-data-for-edit/"
            f"unix_time/{day.unix_time}/selected_employee_id/{staff.employee_id}"
        )
        with opener.open(get_request(edit_url, detail_url), timeout=timeout) as response:
            edit_json = json.loads(response.read().decode("utf-8", errors="replace"))
        day.detail_html = str(edit_json.get("html", ""))
        day.detail_text = clean_detail_text(text_from_html(day.detail_html))
        fetched_details += 1
        if fetched_details == len(detail_targets) or fetched_details % 5 == 0:
            progress(
                f"({staff_index}/{staff_total}) {staff.name}: "
                f"daily details {fetched_details}/{len(detail_targets)}"
            )

    progress(f"({staff_index}/{staff_total}) {staff.name}: done")
    return report


def build_html(reports: list[StaffReport], period_text: str) -> str:
    nav_items: list[str] = []
    sections: list[str] = []
    for report in reports:
        section_id = f"staff-{safe_filename(report.name)}-{report.employee_id}"
        mismatch_count = sum(
            1 for day in report.days if not man_hours_match(day.total_working_hours, day.total_man_hours)
        )
        rows = []
        for day in report.days:
            classes = []
            weekend_class = weekend_row_class(day.date)
            if weekend_class:
                classes.append(weekend_class)
            if not man_hours_match(day.total_working_hours, day.total_man_hours):
                classes.append("mismatch")
            row_class = f' class="{" ".join(classes)}"' if classes else ""
            rows.append(
                f"<tr{row_class}>"
                f"<td>{html.escape(day.date)}</td>"
                f"<td>{html.escape(day.total_working_hours)}</td>"
                f"<td>{html.escape(day.total_man_hours)}</td>"
                f"<td><pre class=\"detail-pre\">{html.escape(day.detail_text)}</pre></td>"
                "</tr>"
            )
        nav_items.append(
            f"""
<button class="staff-link" data-target="{html.escape(section_id)}" type="button">
  <span class="staff-link-name">{html.escape(report.name)}</span>
  <span class="staff-link-meta">{len(report.days)}日 / 差分 {mismatch_count}件</span>
</button>
"""
        )
        sections.append(
            f"""
<section class="staff-section" id="{html.escape(section_id)}">
  <div class="staff-header">
    <div>
      <h2>{html.escape(report.name)}</h2>
      <p class="staff-meta">社員ID: {html.escape(report.employee_id)} / 差分: {mismatch_count}件</p>
    </div>
  </div>
  <div class="chart"><img src="{html.escape(report.chart_svg)}" alt="{html.escape(report.name)} 円グラフ"></div>
  <table>
    <thead><tr><th>日付</th><th>総労働時間</th><th>工数合計</th><th>詳細ボタン結果</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
"""
        )

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>Jobcan 工数詳細レポート</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{ --border: #d0d7de; --panel: #f6f8fa; --accent: #2f80ed; --text-subtle: #667085; }}
    body {{ font-family: {REPORT_FONT_FAMILY}; font-size: 14px; line-height: 1.45; margin: 24px; color: #222; }}
    h1 {{ margin-bottom: 4px; font-size: 24px; }}
    h2 {{ margin: 0; font-size: 20px; }}
    p {{ margin: 0; }}
    .layout {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 24px; align-items: start; margin-top: 24px; }}
    .sidebar {{ position: sticky; top: 24px; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: #fff; }}
    .sidebar-header {{ padding: 14px 16px; background: var(--panel); font-size: 13px; font-weight: 700; border-bottom: 1px solid var(--border); }}
    .staff-nav {{ display: grid; }}
    .staff-link {{ text-align: left; border: 0; border-bottom: 1px solid var(--border); background: #fff; padding: 13px 16px; cursor: pointer; }}
    .staff-link:last-child {{ border-bottom: 0; }}
    .staff-link:hover {{ background: #eef6ff; }}
    .staff-link.active {{ background: #e8f1ff; box-shadow: inset 3px 0 0 var(--accent); }}
    .staff-link-name {{ display: block; font-size: 14px; font-weight: 700; }}
    .staff-link-meta {{ display: block; margin-top: 4px; color: var(--text-subtle); font-size: 12px; }}
    .content {{ min-width: 0; }}
    .staff-section {{ display: none; }}
    .staff-section.active {{ display: block; }}
    .staff-header {{ margin-bottom: 16px; }}
    .staff-meta {{ margin-top: 6px; color: var(--text-subtle); }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 14px; }}
    th, td {{ border: 1px solid #d0d7de; padding: 7px 8px; vertical-align: top; font-size: 13px; }}
    th {{ background: #f6f8fa; font-size: 12px; }}
    pre {{ white-space: pre-wrap; margin: 8px 0 0; font-family: inherit; }}
    .detail-pre {{ white-space: pre-wrap; margin: 0; font-family: inherit; }}
    tr.saturday td {{ background: #dceeff; }}
    tr.sunday td {{ background: #ffe0e0; }}
    tr.mismatch td {{ background: #fff3cd; }}
    .chart img {{ max-width: 760px; width: 100%; height: auto; border: 1px solid #d0d7de; }}
    @media (max-width: 960px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; }}
    }}
  </style>
</head>
<body>
  <h1>Jobcan 工数詳細レポート</h1>
  <p>対象期間: {html.escape(period_text)}</p>
  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar-header">ユーザー一覧</div>
      <div class="staff-nav">
        {''.join(nav_items)}
      </div>
    </aside>
    <main class="content">
      {''.join(sections)}
    </main>
  </div>
  <script>
    const links = Array.from(document.querySelectorAll('.staff-link'));
    const sections = Array.from(document.querySelectorAll('.staff-section'));
    const showSection = (id) => {{
      links.forEach((link) => link.classList.toggle('active', link.dataset.target === id));
      sections.forEach((section) => section.classList.toggle('active', section.id === id));
    }};
    links.forEach((link) => {{
      link.addEventListener('click', () => showSection(link.dataset.target));
    }});
    if (links.length > 0) {{
      showSection(links[0].dataset.target);
    }}
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local Jobcan staff man-hour HTML report.")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--group-name")
    parser.add_argument("--output-dir", type=Path, default=Path("jobcan_staff_report"))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--browser",
        choices=["edge", "chrome", "firefox"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        progress(f"Loading config: {args.config}")
        config = load_config(args.config)
        group_name = args.group_name or config.get("group_name")
        if not group_name:
            raise ValueError("config.json に group_name を設定するか、--group-name を指定してください。")
        progress(f"Target group: {group_name}")
        progress(f"Preparing output directory: {args.output_dir}")
        args.output_dir.mkdir(exist_ok=True)
        output_stem = report_output_stem(config, group_name)
        charts_dir_name = f"{output_stem}_charts"
        charts_dir = args.output_dir / charts_dir_name
        charts_dir.mkdir(exist_ok=True)

        progress("Logging in to Jobcan and opening man-hour page")
        opener, _admin_url, man_hour_url, man_hour_html = create_session(config, args.timeout, args.browser)
        progress("Man-hour page fetched. Resolving target group id")
        group_id = find_group_id(man_hour_html, group_name)
        period = build_search_period(int(config["month"]), config.get("year"))
        period_text = (
            f"{period.from_year:04d}-{period.from_month:02d}-{period.from_day:02d} "
            f"to {period.to_year:04d}-{period.to_month:02d}-{period.to_day:02d}"
        )
        progress(f"Target period: {period_text}")

        progress("Fetching staff list")
        record_json = fetch_record_json(opener, man_hour_url, config, group_id, args.timeout)
        (args.output_dir / "record.json").write_text(
            json.dumps(record_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        progress(f"Wrote staff list JSON: {args.output_dir / 'record.json'}")

        staff_links = parse_staff_links(record_json)
        progress(f"Fetching staff report data: {len(staff_links)} staff")
        reports = []
        for index, staff in enumerate(staff_links, start=1):
            reports.append(
                fetch_staff_report(
                    opener,
                    staff,
                    charts_dir,
                    charts_dir_name,
                    args.timeout,
                    index,
                    len(staff_links),
                )
            )

        progress("Building HTML report")
        report_html = build_html(reports, period_text)
        output_html = args.output_dir / f"{output_stem}.html"
        output_html.write_text(report_html, encoding="utf-8")
        progress(f"Wrote HTML report: {output_html}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Config or parsing error: {exc}", file=sys.stderr)
        return 1
    except HTTPError as exc:
        print(f"HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Request failed: {exc.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Request timed out.", file=sys.stderr)
        return 1

    print(f"Period: {period_text}", file=sys.stderr)
    print(f"Staff count: {len(reports)}", file=sys.stderr)
    print(f"Wrote {output_html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
