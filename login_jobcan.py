#!/usr/bin/env python3
"""Authenticate to Jobcan in a real browser and reuse the session in urllib."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.firefox.service import Service as FirefoxService


BASE_URL = "https://id.jobcan.jp"
SIGN_IN_URL = f"{BASE_URL}/users/sign_in"
ATTENDANCE_URL = "https://ssl.jobcan.jp/jbcoauth/login"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; python-jobcan-login/1.0)"
SUPPORTED_BROWSERS = ("chrome", "edge", "firefox")


class BrowserDriverConfig(NamedTuple):
    webdriver_class: Any
    options_class: Any
    service_class: Any


BROWSER_DRIVER_CONFIGS = {
    "chrome": BrowserDriverConfig(webdriver.Chrome, webdriver.ChromeOptions, ChromeService),
    "edge": BrowserDriverConfig(webdriver.Edge, webdriver.EdgeOptions, EdgeService),
    "firefox": BrowserDriverConfig(webdriver.Firefox, webdriver.FirefoxOptions, FirefoxService),
}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def paths_from_config(config: dict[str, Any], key: str) -> dict[str, Path]:
    raw_paths = config.get(key, {})
    return {
        browser: Path(str(raw_path))
        for browser, raw_path in raw_paths.items()
        if browser in SUPPORTED_BROWSERS and raw_path
    }


def browser_paths_from_config(config: dict[str, Any]) -> dict[str, Path]:
    return paths_from_config(config, "browser_paths")


def driver_paths_from_config(config: dict[str, Any]) -> dict[str, Path]:
    return paths_from_config(config, "driver_paths")


def resolve_browser_choice(config: dict[str, Any], browser: str | None = None) -> tuple[str, Path | None]:
    configured_browser_paths = browser_paths_from_config(config)
    if browser:
        requested = browser.lower()
        configured_path = configured_browser_paths.get(requested)
        if configured_path and not configured_path.exists():
            raise ValueError(f"config.json の browser_paths.{requested} が存在しません: {configured_path}")
        return requested, configured_path

    for candidate, path in configured_browser_paths.items():
        if path.exists():
            return candidate, path

    raise ValueError(
        "config.json に有効な browser_paths がありません。"
        " chrome / edge / firefox のいずれか1つを設定してください。"
    )


def resolve_driver_path(config: dict[str, Any], browser: str) -> Path | None:
    configured_driver_paths = driver_paths_from_config(config)
    driver_path = configured_driver_paths.get(browser)
    if driver_path and not driver_path.exists():
        raise ValueError(f"config.json の driver_paths.{browser} が存在しません: {driver_path}")
    return driver_path


def make_request(
    url: str,
    user_agent: str,
    data: bytes | None = None,
    referer: str | None = None,
) -> Request:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = BASE_URL
    return Request(url, data=data, headers=headers)


@contextmanager
def path_without(entries: list[Path]):
    original_path = os.environ.get("PATH", "")
    blocked = {str(entry).lower() for entry in entries}
    filtered = [
        item for item in original_path.split(os.pathsep)
        if item and item.lower() not in blocked
    ]
    os.environ["PATH"] = os.pathsep.join(filtered)
    try:
        yield
    finally:
        os.environ["PATH"] = original_path


def create_webdriver(browser: str, binary_path: Path | None = None, driver_path: Path | None = None):
    browser = browser.lower()
    browser_config = BROWSER_DRIVER_CONFIGS.get(browser)
    if not browser_config:
        raise ValueError(f"Unsupported browser: {browser}")

    options = browser_config.options_class()
    if binary_path:
        options.binary_location = str(binary_path)
    if driver_path:
        try:
            return browser_config.webdriver_class(
                service=browser_config.service_class(executable_path=str(driver_path)),
                options=options,
            )
        except WebDriverException:
            pass
    with path_without([driver_path.parent] if driver_path else []):
        return browser_config.webdriver_class(options=options)


def webdriver_cookie_to_cookiejar(cookie: dict[str, Any]) -> http.cookiejar.Cookie:
    domain = cookie["domain"]
    return http.cookiejar.Cookie(
        version=0,
        name=cookie["name"],
        value=cookie["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=cookie.get("path", "/"),
        path_specified=True,
        secure=bool(cookie.get("secure")),
        expires=cookie.get("expiry"),
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": cookie.get("httpOnly")},
        rfc2109=False,
    )


def merge_driver_cookies(
    cookie_jar: http.cookiejar.CookieJar,
    driver,
    seen: set[tuple[str, str, str]] | None = None,
) -> set[tuple[str, str, str]]:
    seen = seen or set()
    for cookie in driver.get_cookies():
        key = (cookie["domain"], cookie["path"], cookie["name"])
        if key in seen:
            continue
        seen.add(key)
        cookie_jar.set_cookie(webdriver_cookie_to_cookiejar(cookie))
    return seen


def login_completed(driver) -> bool:
    current_url = driver.current_url
    parsed = urlparse(current_url)
    if not parsed.netloc.endswith("jobcan.jp"):
        return False
    if parsed.netloc == "id.jobcan.jp" and parsed.path.startswith("/users/sign_in"):
        return False
    if not any(cookie.get("domain", "").endswith("jobcan.jp") for cookie in driver.get_cookies()):
        return False
    if parsed.netloc == "id.jobcan.jp":
        return bool(driver.find_elements(By.CSS_SELECTOR, "a[href*='ssl.jobcan.jp/jbcoauth/login']"))
    return True


def navigate_to_attendance(driver, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_url = driver.current_url
        parsed = urlparse(current_url)
        if parsed.netloc == "ssl.jobcan.jp":
            return current_url

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='ssl.jobcan.jp/jbcoauth/login']")
        if links:
            href = links[0].get_attribute("href")
            if href:
                driver.get(href)
                time.sleep(2.0)
                return driver.current_url
        time.sleep(1.0)

    raise TimeoutError(f"勤怠画面への遷移を {int(timeout)} 秒待ちましたが検知できませんでした。")


def wait_for_manual_login(driver, timeout: float) -> None:
    login_timeout = max(timeout, 300.0)
    print("ブラウザでJobcanにログインしてください。完了を自動検知して続行します。", file=sys.stderr)
    deadline = time.monotonic() + login_timeout
    while time.monotonic() < deadline:
        try:
            if login_completed(driver):
                return
        except WebDriverException as exc:
            raise ValueError("ブラウザが閉じられたため、ログイン状態を確認できませんでした。") from exc
        time.sleep(1.0)
    raise TimeoutError(f"手動ログインの完了を {int(login_timeout)} 秒待ちましたが検知できませんでした。")


def create_authenticated_opener(
    config: dict[str, Any],
    timeout: float,
    user_agent: str = DEFAULT_USER_AGENT,
    browser: str | None = None,
) -> tuple[Any, str]:
    browser_name, browser_path = resolve_browser_choice(config, browser)
    driver_path = resolve_driver_path(config, browser_name)
    try:
        driver = create_webdriver(browser_name, browser_path, driver_path)
    except WebDriverException as exc:
        raise RuntimeError(f"{browser_name} を起動できませんでした: {exc}") from exc

    try:
        cookie_jar = http.cookiejar.CookieJar()
        seen_cookies: set[tuple[str, str, str]] = set()
        driver.get(SIGN_IN_URL)
        wait_for_manual_login(driver, timeout)
        seen_cookies = merge_driver_cookies(cookie_jar, driver, seen_cookies)
        current_url = navigate_to_attendance(driver, timeout)
        merge_driver_cookies(cookie_jar, driver, seen_cookies)
    finally:
        driver.quit()

    if len(cookie_jar) == 0:
        raise ValueError("ログイン後の Cookie を取得できませんでした。")

    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [("User-Agent", user_agent)]
    return opener, current_url


def login(config: dict[str, Any], timeout: float, user_agent: str, browser: str | None) -> tuple[str, bytes]:
    opener, final_url = create_authenticated_opener(
        config=config,
        timeout=timeout,
        user_agent=user_agent,
        browser=browser,
    )
    with opener.open(make_request(final_url, user_agent), timeout=timeout) as response:
        return response.geturl(), response.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a browser for manual Jobcan login and print the fetched HTML."
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config)
        final_url, body = login(config, args.timeout, args.user_agent, args.browser)
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

    print(f"Final URL: {final_url}", file=sys.stderr)
    if args.output:
        args.output.write_bytes(body)
        print(f"Wrote {len(body)} bytes to {args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
