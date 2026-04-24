#!/usr/bin/env python3
"""Log in to Jobcan manually and save the man-hour search result HTML."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request

from login_jobcan import (
    DEFAULT_BROWSER,
    DEFAULT_USER_AGENT,
    SIGN_IN_URL,
    create_authenticated_opener,
    load_config,
)


BASE_URL = "https://ssl.jobcan.jp"
ATTENDANCE_URL = f"{BASE_URL}/jbcoauth/login"
ADMIN_URL = f"{BASE_URL}/jbcoauth/admin/login"
MAN_HOUR_URL = f"{BASE_URL}/client/man-hour-manage"
@dataclass(frozen=True)
class SearchPeriod:
    from_year: int
    from_month: int
    from_day: int
    to_year: int
    to_month: int
    to_day: int


class GroupOptionParser(HTMLParser):
    def __init__(self, target_group_name: str) -> None:
        super().__init__()
        self.target_group_name = unicodedata.normalize("NFKC", target_group_name)
        self.group_id: str | None = None
        self.options: list[tuple[str, str]] = []
        self._inside_group_select = False
        self._current_option_value: str | None = None
        self._current_option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "select" and attr_map.get("name") == "group_id":
            self._inside_group_select = True
            return

        if tag == "option" and self._inside_group_select:
            self._current_option_value = attr_map.get("value")
            self._current_option_text = []

    def handle_data(self, data: str) -> None:
        if self._inside_group_select and self._current_option_value is not None:
            self._current_option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._inside_group_select:
            text = unicodedata.normalize("NFKC", "".join(self._current_option_text).strip())
            if self._current_option_value is not None:
                self.options.append((self._current_option_value, text))
            if self.target_group_name in text:
                self.group_id = self._current_option_value
            self._current_option_value = None
            self._current_option_text = []
            return

        if tag == "select" and self._inside_group_select:
            self._inside_group_select = False


def post_request(url: str, form_data: dict[str, Any], referer: str) -> Request:
    encoded = urlencode(form_data, doseq=True).encode("utf-8")
    parsed_url = urlparse(url)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return Request(
        url,
        data=encoded,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
        },
    )


def get_request(url: str, referer: str | None = None) -> Request:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer
    return Request(url, headers=headers)


def build_search_period(
    month: int,
    year: int | None = None,
    today: date | None = None,
) -> SearchPeriod:
    if month < 1 or month > 12:
        raise ValueError("config.json の month は 1 から 12 の範囲で指定してください。")

    today = today or date.today()
    if year is None:
        year = today.year
        if month > today.month:
            year -= 1

    from_month = month - 1
    from_year = year
    if from_month == 0:
        from_month = 12
        from_year -= 1

    return SearchPeriod(
        from_year=from_year,
        from_month=from_month,
        from_day=21,
        to_year=year,
        to_month=month,
        to_day=20,
    )


def login_to_jobcan_id(config: dict[str, Any], timeout: float, browser: str | None = None):
    opener, _current_url = create_authenticated_opener(
        config=config,
        timeout=timeout,
        user_agent=DEFAULT_USER_AGENT,
        browser=browser,
    )
    return opener


def find_group_id(html: bytes, group_name: str) -> str:
    parser = GroupOptionParser(group_name)
    parser.feed(html.decode("utf-8", errors="replace"))
    if not parser.group_id:
        candidates = ", ".join(f"{value}:{text}" for value, text in parser.options)
        raise ValueError(
            f"指定グループ '{group_name}' を含む候補が見つかりませんでした。"
            f" 候補: {candidates or '(group_id候補なし)'}"
        )
    return parser.group_id


def fetch_search_result(
    config: dict[str, Any],
    group_name: str,
    timeout: float,
    browser: str | None = None,
) -> tuple[str, bytes, SearchPeriod, str]:
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
    period = build_search_period(int(config["month"]), config.get("year"))
    today = date.today()

    search_form = {
        "search_type": "month",
        "day_year": today.year,
        "day_month": today.month,
        "day_day": today.day,
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

    with opener.open(post_request(MAN_HOUR_URL, search_form, man_hour_url), timeout=timeout) as response:
        body = response.read()
        final_url = response.geturl()

    return final_url, body, period, group_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log in to Jobcan manually, search man-hours, and save the response HTML."
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--group-name")
    parser.add_argument("-o", "--output", type=Path, default=Path("jobcan_man_hour_search.html"))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--browser",
        choices=["edge", "chrome", "firefox"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config)
        if "month" not in config:
            raise ValueError("config.json に month を設定してください。")
        group_name = args.group_name or config.get("group_name")
        if not group_name:
            raise ValueError("config.json に group_name を設定するか、--group-name を指定してください。")
        final_url, body, period, group_id = fetch_search_result(
            config,
            group_name=group_name,
            timeout=args.timeout,
            browser=args.browser,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Config or parsing error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Browser error: {exc}", file=sys.stderr)
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

    args.output.write_bytes(body)
    print(f"Final URL: {final_url}", file=sys.stderr)
    print(
        "Period: "
        f"{period.from_year:04d}-{period.from_month:02d}-{period.from_day:02d} "
        f"to {period.to_year:04d}-{period.to_month:02d}-{period.to_day:02d}",
        file=sys.stderr,
    )
    print(f"Group: {group_name} (group_id={group_id})", file=sys.stderr)
    print(f"Wrote {len(body)} bytes to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
