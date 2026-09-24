"""Small, auditable public development corpus collector (never a benchmark)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

DEFAULT_LISTING = "https://www.ccgp.gov.cn/cggg/dfgg/zbgg/"
USER_AGENT = "BidIntelResearch/0.1 (student development corpus; max 1 request/second)"
ATTACHMENT_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".txt", ".png", ".jpg", ".jpeg"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def allowed_url(url: str, hosts: set[str]) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 80, 443}
            and any(host == allowed or host.endswith("." + allowed) for allowed in hosts)
        )
    except ValueError:
        return False


def discover_notice_urls(content: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    return list(dict.fromkeys(
        urldefrag(urljoin(base_url, anchor["href"]))[0]
        for anchor in soup.find_all("a", href=True)
        if re.search(
            r"/cggg/(?:dfgg|zygg)/(?:zbgg|cjgg)/\d{6}/t\d{8}_\d+\.s?html?$",
            urlsplit(urljoin(base_url, anchor["href"])).path,
        )
    ))


def discover_attachment_urls(content: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    urls = []
    for anchor in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, anchor["href"]))[0]
        suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
        label = anchor.get_text(" ", strip=True)
        if suffix in ATTACHMENT_SUFFIXES or re.search(r"\.(?:pdf|docx?|xlsx?|zip)\b", label, re.IGNORECASE):
            urls.append(url)
    return list(dict.fromkeys(urls))


class CorpusCollector:
    def __init__(
        self,
        output_dir: Path,
        *,
        client: httpx.Client | None = None,
        allowed_hosts: set[str] | None = None,
        max_bytes: int = 20 * 1024 * 1024,
        interval: float = 1.0,
        sleeper=time.sleep,
        clock=time.monotonic,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None
        self.allowed_hosts = allowed_hosts or {"ccgp.gov.cn"}
        self.max_bytes = max_bytes
        self.interval = max(1.0, interval)
        self.sleeper, self.clock = sleeper, clock
        self.last_request: float | None = None
        self.robots: dict[str, RobotFileParser | bool] = {}
        self.blocked_hosts: set[str] = set()
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        event = {"timestamp_utc": utc_now(), **event}
        self.events.append(event)
        with (self.output_dir / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _request(self, url: str, *, kind: str) -> tuple[bytes, str, int] | None:
        original_url = url
        for _ in range(5):
            if not allowed_url(url, self.allowed_hosts):
                self.record({"kind": kind, "url": original_url, "target_url": url, "status": "blocked_origin"})
                return None
            host = urlsplit(url).hostname or ""
            if host in self.blocked_hosts:
                self.record({"kind": kind, "url": url, "status": "host_previously_blocked"})
                return None
            if self.last_request is not None:
                self.sleeper(max(0, self.interval - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            try:
                with self.client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                    status = response.status_code
                    if status in {401, 403, 429}:
                        self.blocked_hosts.add(host)
                    if response.is_redirect:
                        self.record({"kind": kind, "url": url, "status": status, "location": response.headers.get("location")})
                        target = urljoin(url, response.headers.get("location", ""))
                        if kind != "robots" and not self._robots_allowed(target):
                            self.record({"kind": kind, "url": target, "status": "robots_denied"})
                            return None
                        url = target
                        continue
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            self.record({"kind": kind, "url": url, "status": "size_limit", "bytes": size})
                            return None
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    self.record({
                        "kind": kind, "url": url, "status": status, "bytes": size,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content_type": response.headers.get("content-type", ""),
                    })
                    return content, url, status
            except (httpx.HTTPError, OSError) as exc:
                self.record({"kind": kind, "url": url, "status": "request_failed", "error": type(exc).__name__})
                return None
        self.record({"kind": kind, "url": original_url, "status": "redirect_limit"})
        return None

    def _robots_allowed(self, url: str) -> bool:
        if not allowed_url(url, self.allowed_hosts):
            return False
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots:
            response = self._request(origin + "/robots.txt", kind="robots")
            if response is None:
                self.robots[origin] = False
            else:
                content, _, status = response
                if status in {404, 410}:
                    self.robots[origin] = True
                elif status == 200:
                    # An HTML login/error page is not a usable robots policy.
                    text = content.decode("utf-8", errors="replace")
                    if "<html" in text.lower() or "<!doctype" in text.lower():
                        self.robots[origin] = False
                    else:
                        policy = RobotFileParser()
                        policy.parse(text.splitlines())
                        self.robots[origin] = policy
                else:
                    self.robots[origin] = False
        policy = self.robots[origin]
        if isinstance(policy, bool):
            return policy
        delay = policy.crawl_delay(USER_AGENT) or policy.crawl_delay("*")
        if delay:
            self.interval = max(self.interval, float(delay))
        return policy.can_fetch(USER_AGENT, url)

    def fetch(self, url: str, *, kind: str) -> tuple[bytes, str] | None:
        if not allowed_url(url, self.allowed_hosts):
            self.record({"kind": kind, "url": url, "status": "blocked_origin"})
            return None
        if not self._robots_allowed(url):
            self.record({"kind": kind, "url": url, "status": "robots_denied"})
            return None
        response = self._request(url, kind=kind)
        if response and response[2] == 200:
            return response[0], response[1]
        return None

    def collect(self, urls: list[str], *, listings: list[str] | None = None, limit: int = 30) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("Development collection limit must be between 1 and 100")
        started_at = utc_now()
        candidates = list(urls)
        for listing in listings or []:
            response = self.fetch(listing, kind="listing")
            if response:
                candidates.extend(discover_notice_urls(*response))
        notices = []
        for url in dict.fromkeys(candidates):
            if len(notices) >= limit:
                break
            response = self.fetch(url, kind="notice")
            if not response:
                continue
            content, final_url = response
            soup = BeautifulSoup(content, "html.parser")
            if not soup.find("html") or not soup.find("title"):
                self.record({"kind": "notice", "url": url, "status": "not_html_notice"})
                continue
            notice_id = hashlib.sha256(url.encode()).hexdigest()[:16]
            directory = self.output_dir / "notices" / notice_id
            directory.mkdir(parents=True, exist_ok=False)
            html_path = directory / f"{notice_id}.html"
            with html_path.open("xb") as stream:
                stream.write(content)
            files = [{
                "url": final_url, "path": html_path.relative_to(self.output_dir).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "kind": "notice",
            }]
            for attachment_url in discover_attachment_urls(content, final_url)[:10]:
                attachment = self.fetch(attachment_url, kind="attachment")
                if not attachment:
                    continue
                attachment_content, attachment_final_url = attachment
                suffix = Path(unquote(urlsplit(attachment_final_url).path)).suffix.lower()
                if suffix not in ATTACHMENT_SUFFIXES:
                    self.record({"kind": "attachment", "url": attachment_url, "status": "unknown_extension"})
                    continue
                digest = hashlib.sha256(attachment_content).hexdigest()
                attachment_path = directory / f"{notice_id}_attachment_{digest[:16]}{suffix}"
                if not attachment_path.exists():
                    with attachment_path.open("xb") as stream:
                        stream.write(attachment_content)
                files.append({
                    "url": attachment_final_url,
                    "path": attachment_path.relative_to(self.output_dir).as_posix(),
                    "sha256": digest, "bytes": len(attachment_content), "kind": "attachment",
                })
            notices.append({
                "id": notice_id, "source_url": url, "retrieved_at_utc": utc_now(),
                "title": soup.title.get_text(" ", strip=True), "files": files,
            })
        manifest = {
            "schema_version": 1, "purpose": "public_development_only_not_official_benchmark",
            "started_at_utc": started_at, "completed_at_utc": utc_now(),
            "requested_limit": limit, "notice_count": len(notices),
            "allowed_hosts": sorted(self.allowed_hosts), "rate_seconds": self.interval,
            "max_file_bytes": self.max_bytes, "notices": notices,
            "requests_log": "requests.jsonl",
        }
        with (self.output_dir / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        if self.owns_client:
            self.client.close()
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".data/development"))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--url-file", type=Path)
    parser.add_argument("--listing", action="append")
    parser.add_argument("--allow-host", action="append", default=[])
    args = parser.parse_args()
    urls = args.url
    if args.url_file:
        urls.extend(line.strip() for line in args.url_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))
    run_dir = args.output / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    collector = CorpusCollector(run_dir, allowed_hosts={"ccgp.gov.cn", *args.allow_host})
    manifest = collector.collect(urls, listings=args.listing if args.listing is not None else ([] if urls else [DEFAULT_LISTING]), limit=args.limit)
    print(json.dumps({"manifest": str(run_dir / "manifest.json"), "notices_downloaded": manifest["notice_count"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
