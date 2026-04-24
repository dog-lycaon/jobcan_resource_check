#!/usr/bin/env python3
"""Extract staff names from Jobcan man-hour search results."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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


GET_RECORD_URL = f"{MAN_HOUR_URL}/get-record"


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.text_parts.append(text)


class StaffLinkTextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._in_link = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attr_map = dict(attrs)
            href = attr_map.get("href", "")
            if "selected_employee_id=" in href:
                self._in_link = True
                self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            text = data.strip()
            if text:
                self._parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            text = normalize_text(" ".join(self._parts))
            if text:
                self.links.append(text)
            self._in_link = False
            self._parts = []


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def strip_tags(fragment: str) -> str:
    parser = TextCollector()
    parser.feed(fragment)
    return normalize_text(" ".join(parser.text_parts))


def build_params(config: dict[str, Any], group_id: str) -> dict[str, Any]:
    period = build_search_period(int(config["month"]), config.get("year"))
    return {
        "module": "client",
        "controller": "man-hour-manage",
        "action": "index",
        "search_type": "month",
        "day_year": period.to_year,
        "day_month": period.to_month,
        "day_day": period.to_day,
        "from_year": period.from_year,
        "from_month": period.from_month,
        "from_day": period.from_day,
        "to_year": period.to_year,
        "to_month": period.to_month,
        "to_day": period.to_day,
        "group_id": group_id,
        "group_where_type": "both",
        "work_kind[]": ["0", "-1", "-1", "-1", "-1", "-1", "-1", "-1"],
    }


def fetch_record_json(
    config: dict[str, Any],
    group_name: str,
    timeout: float,
    browser: str | None = None,
) -> dict[str, Any]:
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

    group_id = find_group_id(man_hour_html, group_name)
    params = build_params(config, group_id)
    url = f"{GET_RECORD_URL}?{urlencode(params, doseq=True)}"

    with opener.open(get_request(url, man_hour_url), timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")

    return json.loads(body)


def extract_names(record_json: dict[str, Any]) -> list[str]:
    link_parser = StaffLinkTextCollector()
    link_parser.feed(str(record_json.get("detail", "")))
    candidates = link_parser.links

    names: list[str] = []
    seen: set[str] = set()
    excluded = {
        "詳細",
        "合計",
        "総労働時間",
        "工数合計",
        "所属グループ名",
        "姓 名",
        "プロジェクト",
        "タスク",
    }

    for candidate in candidates:
        candidate = normalize_text(candidate)
        if not candidate or candidate in excluded:
            continue
        if any(marker in candidate for marker in ("->", ":", "：", "合計", "時間", "分", "工数")):
            continue
        if re.fullmatch(r"[\d:.,/\-]+", candidate):
            continue
        if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            names.append(candidate)

    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print staff names from Jobcan man-hour records.")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--group-name")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--browser",
        choices=["edge", "chrome", "firefox"],
    )
    parser.add_argument(
        "--save-json",
        type=Path,
        default=Path("jobcan_man_hour_record.json"),
        help="Save the raw get-record JSON response. Default: jobcan_man_hour_record.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config)
        group_name = args.group_name or config.get("group_name")
        if not group_name:
            raise ValueError("config.json に group_name を設定するか、--group-name を指定してください。")
        record_json = fetch_record_json(config, group_name, args.timeout, args.browser)
        args.save_json.write_text(
            json.dumps(record_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        names = extract_names(record_json)
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

    for name in names:
        print(name)

    print(f"Count: {len(names)}", file=sys.stderr)
    print(f"Wrote raw JSON to {args.save_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
