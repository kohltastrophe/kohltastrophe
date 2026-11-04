#!/usr/bin/env python3
"""Generate tiered Sponsors wall and platform CTA SVGs with inlined base64 avatars.

Reads GitHub Sponsors via GraphQL and merges Ko-fi / manual sponsors from `.github/sponsors.json`
and the orphan `badges` branch (`sponsors.json`). GitHub and Ko-fi handles auto-resolve from
`.github/FUNDING.yml` or `GITHUB_REPOSITORY_OWNER`. See `SPONSORS.md` for setup instructions.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAPHQL_URL = "https://api.github.com/graphql"
USER_AGENT = "github-sponsors-svg/1.0"
DEFAULT_FUNDING_PATH = Path(".github/FUNDING.yml")
DEFAULT_EXTRA_PATH = Path(".github/sponsors.json")
DEFAULT_KOFI_URL = "https://ko-fi.com"
FEATURED_THRESHOLD = 25
ACTIVE_WINDOW_DAYS = 35

# Default Ko-fi membership tier name mappings (overrideable via `kofi_tiers` in .github/sponsors.json)
KOFI_TIER_PRICES: dict[str, int] = {
    "$1 a month": 1,
    "$5 a month": 5,
    "$25 a month": 25,
    "supporter": 1,
    "backer": 5,
    "featured": 25,
    "featured sponsor": 25,
}

EMAIL_DOMAINS_IGNORE = frozenset({
    "gmail",
    "googlemail",
    "yahoo",
    "outlook",
    "hotmail",
    "icloud",
    "proton",
    "protonmail",
    "aol",
    "live",
    "msn",
    "kofi",
    "ko-fi",
})

ANONYMOUS_NAMES = frozenset({
    "anonymous",
    "someone",
    "supporter",
    "a supporter",
    "anon",
    "unknown",
    "private",
    "n/a",
    "none",
})

ALLOWED_SPONSOR_HOSTS = frozenset({
    "github.com",
    "www.github.com",
    "roblox.com",
    "www.roblox.com",
    "ko-fi.com",
    "www.ko-fi.com",
})

KOFI_TEST_TRANSACTION_ID = "00000000-1111-2222-3333-444444444444"
KOFI_TEST_EMAIL = "jo.example@example.com"
KOFI_TEST_EMAIL_SHA256 = hashlib.sha256(KOFI_TEST_EMAIL.encode("utf-8")).hexdigest()

GRAPHQL_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: false) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        tier {
          name
          monthlyPriceInDollars
          isOneTime
        }
        sponsorEntity {
          ... on User {
            login
            name
            avatarUrl(size: 160)
            url
          }
          ... on Organization {
            login
            name
            avatarUrl(size: 160)
            url
          }
        }
      }
    }
  }
}
"""

GRAPHQL_USER_ONLY_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: false) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        tier {
          name
          monthlyPriceInDollars
          isOneTime
        }
        sponsorEntity {
          ... on User {
            login
            name
            avatarUrl(size: 160)
            url
          }
        }
      }
    }
  }
}
"""

SVG_STYLE = """  <style>
    .font {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .tier-heading {
      fill: #656d76;
      font-size: 12px;
      font-weight: 500;
    }
    .sponsor-name {
      fill: #1f2328;
      font-size: 12px;
      font-weight: 500;
    }
    .sponsor-login {
      fill: #656d76;
      font-size: 11px;
      font-weight: 400;
    }
    .avatar-ring {
      fill: none;
      stroke: #d0d7de;
      stroke-width: 1.25;
    }
    .avatar-ring-featured {
      fill: none;
      stroke: #bf3989;
      stroke-width: 1.5;
      stroke-opacity: 0.55;
    }
    .avatar-fallback {
      fill: #f6f8fa;
    }
    .avatar-initial {
      fill: #57606a;
      font-weight: 600;
    }
    .cta-card {
      fill: #f6f8fa;
      stroke: #d0d7de;
      stroke-width: 1;
    }
    .cta-title {
      fill: #1f2328;
      font-size: 13px;
      font-weight: 600;
    }
    .cta-sub {
      fill: #656d76;
      font-size: 11.5px;
      font-weight: 400;
    }
    .cta-slot-github {
      fill: #bf3989;
      fill-opacity: 0.1;
      stroke: #bf3989;
      stroke-width: 1.25;
      stroke-dasharray: 3 3;
    }
    .cta-icon-github {
      fill: #bf3989;
    }
    .cta-pill-github {
      fill: #bf3989;
      fill-opacity: 0.1;
      stroke: #bf3989;
      stroke-width: 1;
      stroke-opacity: 0.35;
    }
    .cta-pill-text-github {
      fill: #bf3989;
      font-size: 12px;
      font-weight: 600;
    }
    .cta-slot-kofi {
      fill: #e04f4c;
      fill-opacity: 0.1;
      stroke: #e04f4c;
      stroke-width: 1.25;
      stroke-dasharray: 3 3;
    }
    .cta-icon-kofi {
      fill: #e04f4c;
    }
    .cta-pill-kofi {
      fill: #e04f4c;
      fill-opacity: 0.1;
      stroke: #e04f4c;
      stroke-width: 1;
      stroke-opacity: 0.35;
    }
    .cta-pill-text-kofi {
      fill: #e04f4c;
      font-size: 12px;
      font-weight: 600;
    }
    @media (prefers-color-scheme: dark) {
      .tier-heading { fill: #8b949e; }
      .sponsor-name { fill: #e6edf3; }
      .sponsor-login { fill: #8b949e; }
      .avatar-ring { stroke: #30363d; }
      .avatar-ring-featured { stroke: #db61a2; stroke-opacity: 0.65; }
      .avatar-fallback { fill: #161b22; }
      .avatar-initial { fill: #8b949e; }
      .cta-card { fill: #161b22; stroke: #30363d; }
      .cta-title { fill: #e6edf3; }
      .cta-sub { fill: #8b949e; }
      .cta-slot-github { fill: #db61a2; fill-opacity: 0.14; stroke: #db61a2; }
      .cta-icon-github { fill: #db61a2; }
      .cta-pill-github { fill: #db61a2; fill-opacity: 0.14; stroke: #db61a2; stroke-opacity: 0.45; }
      .cta-pill-text-github { fill: #db61a2; }
      .cta-slot-kofi { fill: #ff7875; fill-opacity: 0.14; stroke: #ff7875; }
      .cta-icon-kofi { fill: #ff7875; }
      .cta-pill-kofi { fill: #ff7875; fill-opacity: 0.14; stroke: #ff7875; stroke-opacity: 0.45; }
      .cta-pill-text-kofi { fill: #ff7875; }
    }
  </style>"""


@dataclass(frozen=True)
class Sponsor:
    login: str
    name: str
    avatar_url: str
    url: str
    monthly_dollars: int
    is_one_time: bool
    avatar_data_uri: str = ""
    platform: str = "github"


@dataclass(frozen=True)
class SponsorsConfig:
    featured_threshold: int = FEATURED_THRESHOLD
    active_window_days: int = ACTIVE_WINDOW_DAYS
    kofi_tiers: Mapping[str, int] = field(default_factory=lambda: dict(KOFI_TIER_PRICES))
    allowed_hosts: frozenset[str] = ALLOWED_SPONSOR_HOSTS


def _expand_allowed_hosts(hosts: Iterable[object]) -> frozenset[str]:
    merged = set(ALLOWED_SPONSOR_HOSTS)
    for raw in hosts:
        host = str(raw or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        if host:
            merged.add(host)
            if not host.startswith("www."):
                merged.add(f"www.{host}")
    return frozenset(merged)


def load_sponsors_config(
    config_path: Path | None = None,
    *,
    featured_threshold: int | None = None,
    active_window_days: int | None = None,
) -> SponsorsConfig:
    """Load tier/window/host settings from sponsors.json, environment variables, and explicit overrides."""
    resolved_threshold = FEATURED_THRESHOLD
    resolved_window = ACTIVE_WINDOW_DAYS
    merged_tiers = dict(KOFI_TIER_PRICES)
    merged_hosts = ALLOWED_SPONSOR_HOSTS

    if config_path is not None and config_path.is_file():
        raw_doc = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(raw_doc, dict):
            if raw_doc.get("featured_threshold") is not None:
                resolved_threshold = int(raw_doc["featured_threshold"])
            if raw_doc.get("active_window_days") is not None:
                resolved_window = int(raw_doc["active_window_days"])
            raw_tiers = raw_doc.get("kofi_tiers")
            if isinstance(raw_tiers, dict):
                for k, v in raw_tiers.items():
                    merged_tiers[str(k).strip().lower()] = int(float(v))
            raw_hosts = raw_doc.get("allowed_hosts")
            if isinstance(raw_hosts, list):
                merged_hosts = _expand_allowed_hosts(raw_hosts)

    env_threshold = os.environ.get("SPONSORS_FEATURED_THRESHOLD", "").strip()
    if env_threshold:
        resolved_threshold = int(env_threshold)
    env_window = os.environ.get("SPONSORS_ACTIVE_WINDOW_DAYS", "").strip()
    if env_window:
        resolved_window = int(env_window)
    env_tiers = os.environ.get("SPONSORS_KOFI_TIERS", "").strip()
    if env_tiers:
        parsed_tiers = json.loads(env_tiers)
        if isinstance(parsed_tiers, dict):
            for k, v in parsed_tiers.items():
                merged_tiers[str(k).strip().lower()] = int(float(v))
    env_hosts = os.environ.get("SPONSORS_ALLOWED_HOSTS", "").strip()
    if env_hosts:
        merged_hosts = merged_hosts | _expand_allowed_hosts(env_hosts.split(","))

    if featured_threshold is not None:
        resolved_threshold = featured_threshold
    if active_window_days is not None:
        resolved_window = active_window_days

    return SponsorsConfig(
        featured_threshold=resolved_threshold,
        active_window_days=resolved_window,
        kofi_tiers=merged_tiers,
        allowed_hosts=merged_hosts,
    )


def _detect_github_owner_from_git(repo_dir: Path | None = None) -> str:
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir,
            timeout=10,
            check=True,
            capture_output=True,
            text=True,
        )
        remote_url = res.stdout.strip()
        match = re.search(r"github\.com[:/]([^/\s]+)/", remote_url, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""


def load_funding_defaults(
    funding_path: Path = DEFAULT_FUNDING_PATH,
    repo_dir: Path | None = None,
) -> tuple[str, str]:
    """Return (github_login, kofi_handle) from environment, .github/FUNDING.yml, or git remote."""
    env_gh = os.environ.get("GITHUB_LOGIN", "").strip()
    env_kofi = os.environ.get("KOFI_HANDLE", "").strip()

    env_owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not env_owner:
        repo_env = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" in repo_env:
            env_owner = repo_env.split("/", 1)[0].strip()

    gh_login = ""
    kofi_handle = ""

    if funding_path.is_file():
        for raw_line in funding_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip().strip("[]").split(",", 1)[0].strip().strip("'\"")
            if key == "github" and val and not gh_login:
                gh_login = val
            elif key == "ko_fi" and val and not kofi_handle:
                kofi_handle = val

    resolved_gh = (
        env_gh
        or gh_login
        or _detect_github_owner_from_git(repo_dir or funding_path.parent.parent)
        or env_owner
    )
    resolved_kofi = env_kofi or kofi_handle or resolved_gh
    return resolved_gh, resolved_kofi


def kofi_profile_url(kofi_handle: str = "") -> str:
    clean = kofi_handle.strip().strip("/")
    if clean.startswith("https://") or clean.startswith("http://"):
        return clean
    return f"https://ko-fi.com/{clean}" if clean else DEFAULT_KOFI_URL


def resolve_token() -> str:
    for env_key in ("SPONSORS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
    try:
        res = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def fetch_avatar_data_uri(url: str, size: int = 128) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    if parsed.netloc.endswith("githubusercontent.com") or parsed.netloc == "github.com":
        query["s" if parsed.netloc.endswith("githubusercontent.com") else "size"] = str(size)
    sized_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )
    req = urllib.request.Request(sized_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/png"
            encoded = base64.b64encode(resp.read()).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except Exception as exc:
        print(f"warning: failed to fetch avatar {sized_url}: {exc}", file=sys.stderr)
        return ""


def sort_sponsors(sponsors: Sequence[Sponsor]) -> list[Sponsor]:
    return sorted(
        sponsors,
        key=lambda s: (
            0 if not s.is_one_time else 1,
            -s.monthly_dollars,
            s.login.lower(),
            s.name.lower(),
        ),
    )


def has_recurring_sponsors(sponsors: Sequence[Sponsor]) -> bool:
    return any(not s.is_one_time and s.monthly_dollars > 0 for s in sponsors)


def _node_to_sponsor(
    node: dict[str, object],
    *,
    featured_threshold: int = FEATURED_THRESHOLD,
) -> Sponsor | None:
    entity = node.get("sponsorEntity")
    if not isinstance(entity, dict):
        return None
    sponsor_login = str(entity.get("login") or "").strip()
    if not sponsor_login:
        return None

    raw_tier = node.get("tier")
    if not isinstance(raw_tier, dict):
        print(
            f"warning: GitHub GraphQL returned tier=null for @{sponsor_login}; "
            "configure SPONSORS_TOKEN with read:user scope to read tier pricing",
            file=sys.stderr,
        )
        monthly = 0
        is_one_time = True
    else:
        monthly = int(raw_tier.get("monthlyPriceInDollars") or 0)
        is_one_time = bool(raw_tier.get("isOneTime") or False)

    avatar_url = str(entity.get("avatarUrl") or "")
    avatar_size = 128 if (not is_one_time and monthly >= featured_threshold) else 96
    return Sponsor(
        login=sponsor_login,
        name=str(entity.get("name") or sponsor_login).strip(),
        avatar_url=avatar_url,
        url=str(entity.get("url") or f"https://github.com/{sponsor_login}"),
        monthly_dollars=monthly,
        is_one_time=is_one_time,
        avatar_data_uri=fetch_avatar_data_uri(avatar_url, size=avatar_size),
        platform="github",
    )


def _is_insufficient_scopes(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(err, dict) and err.get("type") == "INSUFFICIENT_SCOPES"
        for err in errors
    )


def _post_graphql(
    query: str,
    variables: dict[str, object],
    token: str,
) -> dict[str, object]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body if isinstance(body, dict) else {}


def fetch_sponsors(
    login: str,
    token: str,
    *,
    featured_threshold: int = FEATURED_THRESHOLD,
) -> list[Sponsor]:
    clean_login = login.strip()
    if not clean_login:
        raise RuntimeError(
            "No GitHub username configured. Set `github:` in .github/FUNDING.yml, "
            "pass `--login`, or set GITHUB_LOGIN."
        )
    if not token:
        raise RuntimeError(
            "No GitHub token found. Set SPONSORS_TOKEN / GITHUB_TOKEN or sign in with `gh auth login`."
        )

    sponsors: list[Sponsor] = []
    cursor: str | None = None
    query = GRAPHQL_QUERY

    while True:
        variables: dict[str, object] = {"login": clean_login, "cursor": cursor}
        body = _post_graphql(query, variables, token)
        if "errors" in body and query == GRAPHQL_QUERY and _is_insufficient_scopes(body["errors"]):
            query = GRAPHQL_USER_ONLY_QUERY
            body = _post_graphql(query, variables, token)

        if "errors" in body:
            raise RuntimeError(f"GitHub GraphQL error: {body['errors']}")

        data = body.get("data")
        user_data = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user_data, dict):
            raise RuntimeError(f"GitHub user '{clean_login}' not found.")

        conn = user_data.get("sponsorshipsAsMaintainer")
        if not isinstance(conn, dict):
            break
        for node in conn.get("nodes") or []:
            if isinstance(node, dict):
                sponsor = _node_to_sponsor(node, featured_threshold=featured_threshold)
                if sponsor is not None:
                    sponsors.append(sponsor)

        page_info = conn.get("pageInfo")
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            break
        end_cursor = page_info.get("endCursor")
        cursor = str(end_cursor) if end_cursor else None

    return sort_sponsors(sponsors)


def resolve_kofi_tier_dollars(
    tier_name: str,
    amount: object,
    *,
    kofi_tiers: Mapping[str, int] | None = None,
) -> int:
    tiers = kofi_tiers if kofi_tiers is not None else KOFI_TIER_PRICES
    clean_tier = tier_name.strip().lower()
    if clean_tier in tiers:
        return tiers[clean_tier]
    match = re.search(r"\$\s*(\d+(?:\.\d+)?)", tier_name)
    if match:
        return int(float(match.group(1)))
    try:
        return int(float(str(amount or 0)))
    except ValueError:
        return 0


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _is_expired(expires_at: str, now: datetime | None = None) -> bool:
    dt = _parse_iso_datetime(expires_at)
    if dt is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return dt <= ref


def _normalize_platform(raw: object) -> str:
    clean = str(raw or "kofi").strip().lower()
    return "kofi" if clean in ("kofi", "ko-fi", "ko_fi") else clean


def _sanitize_sponsor_url(
    url: str,
    platform: str = "kofi",
    login: str = "",
    *,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    allowed_hosts: AbstractSet[str] = ALLOWED_SPONSOR_HOSTS,
) -> str:
    fallback = (
        f"https://github.com/{urllib.parse.quote(login)}"
        if platform == "github" and login
        else default_kofi_url
    )
    raw = url.strip()
    if not raw:
        return fallback
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return fallback
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        return fallback
    if parsed.hostname.lower() not in allowed_hosts:
        return fallback
    return raw


def _parse_extra_entries(raw: object) -> list[dict[str, object]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        items = raw.get("sponsors")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _is_anonymous_name(name: str) -> bool:
    cleaned = name.strip().lower()
    return not cleaned or cleaned in ANONYMOUS_NAMES


def _record_priority(
    item: dict[str, object],
    *,
    kofi_tiers: Mapping[str, int] | None = None,
) -> tuple[int, int]:
    one_time = bool(item.get("is_one_time", False))
    dollars = resolve_kofi_tier_dollars(
        str(item.get("tier_name") or item.get("tier") or ""),
        item.get("monthly_dollars") or item.get("amount") or 0,
        kofi_tiers=kofi_tiers,
    )
    return (1 if not one_time else 0, dollars)


def _collect_active_records(
    entries: Iterable[dict[str, object]],
    *,
    now: datetime | None = None,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    config: SponsorsConfig | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    """Filter out expired or anonymous records and deduplicate by (platform, login)."""
    cfg = config or SponsorsConfig()
    active: dict[tuple[str, str], dict[str, object]] = {}
    for item in entries:
        expires_at = str(item.get("expires_at") or "").strip()
        if expires_at and _is_expired(expires_at, now=now):
            continue
        platform = _normalize_platform(item.get("platform"))
        login = str(item.get("login") or item.get("handle") or item.get("name") or "").strip()
        if not login:
            continue
        url = _sanitize_sponsor_url(
            str(item.get("url") or ""),
            platform=platform,
            login=login,
            default_kofi_url=default_kofi_url,
            allowed_hosts=cfg.allowed_hosts,
        )
        if _is_anonymous_name(login) and url == default_kofi_url:
            continue

        key = (platform, login.lower())
        prev = active.get(key)
        if prev is None or _record_priority(item, kofi_tiers=cfg.kofi_tiers) >= _record_priority(
            prev, kofi_tiers=cfg.kofi_tiers
        ):
            active[key] = item
    return active


def _sort_extra_records(records: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        records,
        key=lambda r: (
            0 if not bool(r.get("is_one_time", False)) else 1,
            -int(float(str(r.get("monthly_dollars") or 0))),
            str(r.get("login") or "").lower(),
        ),
    )


def _record_to_sponsor(
    item: dict[str, object],
    *,
    default_kofi_url: str,
    cfg: SponsorsConfig,
) -> Sponsor:
    platform = _normalize_platform(item.get("platform"))
    login = str(item.get("login") or item.get("handle") or item.get("name") or "").strip()
    name = str(item.get("name") or login).strip()
    tier_name = str(item.get("tier_name") or item.get("tier") or "").strip()
    monthly = resolve_kofi_tier_dollars(
        tier_name,
        item.get("monthly_dollars") or item.get("amount") or 0,
        kofi_tiers=cfg.kofi_tiers,
    )
    is_one_time = bool(item.get("is_one_time", False))
    avatar_url = str(item.get("avatar_url") or "").strip()
    url = _sanitize_sponsor_url(
        str(item.get("url") or ""),
        platform=platform,
        login=login,
        default_kofi_url=default_kofi_url,
        allowed_hosts=cfg.allowed_hosts,
    )
    avatar_size = 128 if (not is_one_time and monthly >= cfg.featured_threshold) else 96
    avatar_data_uri = (
        str(item.get("avatar_data_uri") or "").strip()
        or fetch_avatar_data_uri(avatar_url, size=avatar_size)
    )
    return Sponsor(
        login=login,
        name=name,
        avatar_url=avatar_url,
        url=url,
        monthly_dollars=monthly,
        is_one_time=is_one_time,
        avatar_data_uri=avatar_data_uri,
        platform=platform,
    )


def load_extra_sponsors(
    extra_path: Path | None = None,
    extra_env: str | None = None,
    *,
    extra_paths: Sequence[Path] | None = None,
    now: datetime | None = None,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    config: SponsorsConfig | None = None,
) -> list[Sponsor]:
    raw_entries: list[dict[str, object]] = []
    paths_to_load: list[Path] = []
    if extra_path is not None:
        paths_to_load.append(extra_path)
    for p in extra_paths or ():
        if p not in paths_to_load:
            paths_to_load.append(p)

    cfg = config or load_sponsors_config(paths_to_load[0] if paths_to_load else None)

    for path in paths_to_load:
        if path.is_file():
            raw_entries.extend(_parse_extra_entries(json.loads(path.read_text(encoding="utf-8"))))

    env_payload = (extra_env if extra_env is not None else os.environ.get("EXTRA_SPONSORS_JSON", "")).strip()
    if env_payload and env_payload != "null":
        raw_entries.extend(_parse_extra_entries(json.loads(env_payload)))

    active_records = _collect_active_records(
        raw_entries, now=now, default_kofi_url=default_kofi_url, config=cfg
    )
    return [
        _record_to_sponsor(item, default_kofi_url=default_kofi_url, cfg=cfg)
        for item in active_records.values()
    ]


def _resolve_roblox_profile(username: str | None = None, user_id: str | None = None) -> tuple[str, str, str] | None:
    resolved_id = user_id
    resolved_login = username or ""
    try:
        if not resolved_id and username:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/usernames/users",
                data=json.dumps({"usernames": [username], "excludeBannedUsers": True}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data") or []
                if not data:
                    return None
                resolved_id = str(data[0].get("id") or "")
                resolved_login = str(data[0].get("name") or username)
        elif resolved_id and not resolved_login:
            req = urllib.request.Request(
                f"https://users.roblox.com/v1/users/{resolved_id}",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                resolved_login = str(info.get("name") or f"roblox-{resolved_id}")

        if not resolved_id:
            return None

        thumb_url = (
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={urllib.parse.quote(resolved_id)}&size=150x150&format=Png&isCircular=false"
        )
        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        avatar_url = ""
        with urllib.request.urlopen(req, timeout=10) as resp:
            tdata = json.loads(resp.read().decode("utf-8")).get("data") or []
            if tdata:
                avatar_url = str(tdata[0].get("imageUrl") or "")

        profile_url = f"https://www.roblox.com/users/{resolved_id}/profile"
        return resolved_login or f"roblox-{resolved_id}", avatar_url, profile_url
    except Exception as exc:
        print(f"warning: failed to resolve Roblox profile ({username or user_id}): {exc}", file=sys.stderr)
        return None


def _probe_gravatar_url(email_sha256: str) -> str:
    clean_hash = email_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean_hash):
        return ""
    url = f"https://www.gravatar.com/avatar/{clean_hash}?s=160&d=404"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if 200 <= resp.status < 300:
                return url
    except Exception:
        pass
    return ""


def _extract_github_handle(combined: str) -> str:
    gh_url_match = re.search(
        r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)(?=[/?#\s)\].,;:!]|$)",
        combined,
        re.IGNORECASE,
    )
    if gh_url_match and gh_url_match.group(1).lower() not in ("sponsors", "orgs", "settings", "topics"):
        return gh_url_match.group(1)

    gh_tag_match = re.search(
        r"(?:^|[\s(\[])(?:github\s*:\s*@?|gh\s*:\s*@?|@)([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)\b",
        combined,
        re.IGNORECASE,
    )
    if gh_tag_match:
        candidate = gh_tag_match.group(1)
        if candidate.lower() not in EMAIL_DOMAINS_IGNORE:
            return candidate
    return ""


def _extract_roblox_tag(combined: str) -> tuple[str, str]:
    rblx_url_match = re.search(
        r"(?:https?://)?(?:www\.)?roblox\.com/users/(\d+)",
        combined,
        re.IGNORECASE,
    )
    if rblx_url_match:
        return "", rblx_url_match.group(1)

    rblx_tag_match = re.search(
        r"(?:^|[\s(\[])(?:roblox|rblx)\s*:\s*@?([A-Za-z0-9_]{3,20})\b",
        combined,
        re.IGNORECASE,
    )
    if rblx_tag_match:
        return rblx_tag_match.group(1), ""
    return "", ""


def _clean_display_name(from_name: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", from_name)
    cleaned = re.sub(
        r"\(\s*(?:github\s*:\s*@?|gh\s*:\s*@?|roblox\s*:\s*@?|rblx\s*:\s*@?|@)[A-Za-z0-9_-]+\s*\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?:^|[\s(\[])(?:github\s*:\s*@?|gh\s*:\s*@?|roblox\s*:\s*@?|rblx\s*:\s*@?|@)[A-Za-z0-9_-]+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" -·()[]")


def extract_sponsor_identity(
    from_name: str,
    message: str,
    *,
    email: str = "",
    email_sha256: str = "",
    network_lookup: bool = True,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    allowed_hosts: AbstractSet[str] = ALLOWED_SPONSOR_HOSTS,
) -> tuple[str, str, str, str]:
    """Return (login, display_name, avatar_url, profile_url) from Ko-fi supporter fields."""
    combined = f"{from_name}\n{message}"
    gh_user = _extract_github_handle(combined)
    rblx_user, rblx_id = _extract_roblox_tag(combined)
    clean_display = _clean_display_name(from_name)

    login = clean_display
    display_name = clean_display
    avatar_url = ""
    profile_url = default_kofi_url

    if gh_user:
        login = gh_user
        if _is_anonymous_name(clean_display) or clean_display.lower() == gh_user.lower():
            display_name = gh_user
        avatar_url = f"https://github.com/{urllib.parse.quote(gh_user)}.png?size=160"
        profile_url = f"https://github.com/{urllib.parse.quote(gh_user)}"
    elif rblx_user or rblx_id:
        resolved = (
            _resolve_roblox_profile(username=rblx_user or None, user_id=rblx_id or None)
            if network_lookup
            else None
        )
        if resolved:
            login, avatar_url, profile_url = resolved
        else:
            login = rblx_user or f"roblox-{rblx_id}"
            if rblx_id:
                profile_url = f"https://www.roblox.com/users/{rblx_id}/profile"
        if _is_anonymous_name(clean_display):
            display_name = login
    elif _is_anonymous_name(clean_display):
        return "", "", "", default_kofi_url

    if not avatar_url and network_lookup:
        digest = email_sha256.strip().lower()
        if not digest and email.strip():
            digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
        if digest:
            avatar_url = _probe_gravatar_url(digest)

    sanitized_url = _sanitize_sponsor_url(
        profile_url,
        platform="kofi",
        login=login,
        default_kofi_url=default_kofi_url,
        allowed_hosts=allowed_hosts,
    )
    return login, display_name, avatar_url, sanitized_url


def _write_sponsors_doc(sponsors_path: Path, sorted_records: list[dict[str, object]]) -> None:
    top_level: dict[str, object] = {}
    if sponsors_path.is_file():
        raw_doc = json.loads(sponsors_path.read_text(encoding="utf-8"))
        if isinstance(raw_doc, dict):
            top_level = {k: v for k, v in raw_doc.items() if k != "sponsors"}
    doc = {**top_level, "sponsors": sorted_records}
    formatted = json.dumps(doc, indent=2) + "\n"
    sponsors_path.parent.mkdir(parents=True, exist_ok=True)
    if not sponsors_path.is_file() or sponsors_path.read_text(encoding="utf-8") != formatted:
        sponsors_path.write_text(formatted, encoding="utf-8")


def prune_extra_sponsors_file(
    sponsors_path: Path,
    *,
    now: datetime | None = None,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    config: SponsorsConfig | None = None,
) -> list[dict[str, object]]:
    """Remove expired or anonymous records from sponsors_path and write back if changed."""
    if not sponsors_path.is_file():
        return []

    cfg = config or load_sponsors_config(sponsors_path)
    raw_doc = json.loads(sponsors_path.read_text(encoding="utf-8"))
    active_by_key = _collect_active_records(
        _parse_extra_entries(raw_doc),
        now=now,
        default_kofi_url=default_kofi_url,
        config=cfg,
    )
    sorted_records = _sort_extra_records(active_by_key.values())
    _write_sponsors_doc(sponsors_path, sorted_records)
    return sorted_records


def _find_matching_kofi_record(
    active_by_key: dict[tuple[str, str], dict[str, object]],
    login: str,
    email_sha256: str,
) -> tuple[tuple[str, str], dict[str, object] | None]:
    direct_key = ("kofi", login.lower())
    if direct_key in active_by_key:
        return direct_key, active_by_key[direct_key]
    if email_sha256:
        for existing_key, entry in active_by_key.items():
            if existing_key[0] == "kofi" and str(entry.get("email_sha256") or "").lower() == email_sha256:
                return existing_key, entry
    return direct_key, None


def _merge_prev_kofi_record(
    prev: dict[str, object],
    *,
    login: str,
    display_name: str,
    dollars: int,
    is_one_time: bool,
    tier_name: str,
    avatar_url: str,
    profile_url: str,
    expires_dt: datetime,
    default_kofi_url: str,
    cfg: SponsorsConfig,
) -> tuple[str, str, int, bool, str, str, str, str]:
    prev_login = str(prev.get("login") or login).strip()
    if profile_url == default_kofi_url and prev_login.lower() != login.lower():
        # Matched via email_sha256 on a renewal without social tags in the renewal message
        login = prev_login

    prev_one_time = bool(prev.get("is_one_time", False))
    prev_dollars = resolve_kofi_tier_dollars(
        str(prev.get("tier_name") or prev.get("tier") or ""),
        prev.get("monthly_dollars") or prev.get("amount") or 0,
        kofi_tiers=cfg.kofi_tiers,
    )
    prev_expires = _parse_iso_datetime(str(prev.get("expires_at") or ""))
    if prev_expires and prev_expires > expires_dt:
        expires_dt = prev_expires

    if not prev_one_time and is_one_time:
        is_one_time = False
        dollars = max(prev_dollars, 1)
        if not tier_name and prev.get("tier_name"):
            tier_name = str(prev["tier_name"])
    elif prev_one_time and is_one_time:
        dollars = prev_dollars + dollars

    if not avatar_url and prev.get("avatar_url"):
        avatar_url = str(prev["avatar_url"])
    prev_url = _sanitize_sponsor_url(
        str(prev.get("url") or ""),
        platform="kofi",
        login=login,
        default_kofi_url=default_kofi_url,
        allowed_hosts=cfg.allowed_hosts,
    )
    if profile_url == default_kofi_url and prev_url != default_kofi_url:
        profile_url = prev_url
    prev_name = str(prev.get("name") or "").strip()
    if prev_name and display_name.lower() == login.lower() and prev_name.lower() != login.lower():
        display_name = prev_name

    return (
        login,
        display_name,
        dollars,
        is_one_time,
        tier_name,
        avatar_url,
        profile_url,
        expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _parse_kofi_event_fields(
    payload: dict[str, object],
    *,
    kofi_tiers: Mapping[str, int] | None = None,
) -> tuple[str, str, str, int, bool] | None:
    if payload.get("is_public") is False:
        print("Skipping non-public Ko-fi event", file=sys.stderr)
        return None

    tx_id = str(payload.get("kofi_transaction_id") or "").strip()
    message_id = str(payload.get("message_id") or "").strip()
    email = str(payload.get("email") or "").strip()
    email_sha256 = str(payload.get("email_sha256") or "").strip().lower()
    if not email_sha256 and email:
        email_sha256 = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()

    if tx_id == KOFI_TEST_TRANSACTION_ID or email_sha256 == KOFI_TEST_EMAIL_SHA256:
        print("Skipping Ko-fi test webhook payload", file=sys.stderr)
        return None

    tier_name = str(payload.get("tier_name") or "").strip()
    amount = payload.get("amount") or payload.get("monthly_dollars") or 0
    dollars = resolve_kofi_tier_dollars(tier_name, amount, kofi_tiers=kofi_tiers)
    if dollars < 1:
        print(f"Skipping Ko-fi event with amount < $1 ({amount})", file=sys.stderr)
        return None

    event_type = str(payload.get("type") or "").strip().lower()
    is_one_time = not (
        bool(payload.get("is_subscription_payment")) or bool(tier_name) or event_type == "subscription"
    )
    return (tx_id or message_id, email_sha256, tier_name, dollars, is_one_time)


def ingest_kofi_payload(
    payload: dict[str, object],
    sponsors_path: Path,
    *,
    now: datetime | None = None,
    network_lookup: bool = True,
    default_kofi_url: str = DEFAULT_KOFI_URL,
    config: SponsorsConfig | None = None,
) -> bool:
    """Ingest a Ko-fi webhook payload into sponsors.json. Returns True if updated."""
    if isinstance(payload.get("kofi"), dict):
        payload = payload["kofi"]  # type: ignore[assignment]

    cfg = config or load_sponsors_config(sponsors_path)
    parsed = _parse_kofi_event_fields(payload, kofi_tiers=cfg.kofi_tiers)
    if parsed is None:
        return False
    event_id, email_sha256, tier_name, dollars, is_one_time = parsed

    login, display_name, avatar_url, profile_url = extract_sponsor_identity(
        str(payload.get("from_name") or payload.get("name") or "").strip(),
        str(payload.get("message") or "").strip(),
        email_sha256=email_sha256,
        network_lookup=network_lookup,
        default_kofi_url=default_kofi_url,
        allowed_hosts=cfg.allowed_hosts,
    )
    if not login:
        print("Skipping anonymous/unidentified Ko-fi event", file=sys.stderr)
        return False

    ref_now = now or datetime.now(timezone.utc)
    event_dt = _parse_iso_datetime(str(payload.get("timestamp") or "")) or ref_now
    expires_dt = max(event_dt, ref_now) + timedelta(days=cfg.active_window_days)
    expires_at = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    existing_entries = prune_extra_sponsors_file(
        sponsors_path, now=ref_now, default_kofi_url=default_kofi_url, config=cfg
    )
    active_by_key = _collect_active_records(
        existing_entries, now=ref_now, default_kofi_url=default_kofi_url, config=cfg
    )

    matched_key, prev = _find_matching_kofi_record(active_by_key, login, email_sha256)
    prev_event_ids = (
        [str(x) for x in prev["event_ids"] if x]  # type: ignore[index]
        if prev and isinstance(prev.get("event_ids"), list)
        else []
    )
    if event_id and event_id in prev_event_ids:
        print(f"Skipping duplicate Ko-fi event ({event_id})", file=sys.stderr)
        return False

    if prev:
        active_by_key.pop(matched_key, None)
        email_sha256 = email_sha256 or str(prev.get("email_sha256") or "").strip().lower()
        login, display_name, dollars, is_one_time, tier_name, avatar_url, profile_url, expires_at = (
            _merge_prev_kofi_record(
                prev,
                login=login,
                display_name=display_name,
                dollars=dollars,
                is_one_time=is_one_time,
                tier_name=tier_name,
                avatar_url=avatar_url,
                profile_url=profile_url,
                expires_dt=expires_dt,
                default_kofi_url=default_kofi_url,
                cfg=cfg,
            )
        )

    new_record: dict[str, object] = {
        "platform": "kofi",
        "login": login,
        "name": display_name,
        "monthly_dollars": dollars,
        "is_one_time": is_one_time,
        "url": profile_url,
        "expires_at": expires_at,
    }
    if tier_name:
        new_record["tier_name"] = tier_name
    if avatar_url:
        new_record["avatar_url"] = avatar_url
    if email_sha256:
        new_record["email_sha256"] = email_sha256
    updated_event_ids = [*prev_event_ids, *([event_id] if event_id else [])][-20:]
    if updated_event_ids:
        new_record["event_ids"] = updated_event_ids

    active_by_key[("kofi", login.lower())] = new_record
    sorted_records = _sort_extra_records(active_by_key.values())
    _write_sponsors_doc(sponsors_path, sorted_records)
    return True


def _canonical_sponsor_key(sponsor: Sponsor) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(sponsor.url)
        host = (parsed.hostname or "").lower()
        path_parts = [p for p in parsed.path.split("/") if p]
        if host in ("github.com", "www.github.com") and path_parts:
            return ("github", path_parts[0].lower())
        if host in ("roblox.com", "www.roblox.com") and len(path_parts) >= 2 and path_parts[0].lower() == "users":
            return ("roblox", path_parts[1].lower())
    except ValueError:
        pass
    return ("login", sponsor.login.lower())


def _combine_sponsor_pair(
    existing: Sponsor,
    incoming: Sponsor,
    *,
    featured_threshold: int = FEATURED_THRESHOLD,
) -> Sponsor:
    cross_platform = existing.platform.lower() != incoming.platform.lower()
    if not existing.is_one_time and not incoming.is_one_time:
        is_one_time = False
        monthly_dollars = (
            existing.monthly_dollars + incoming.monthly_dollars
            if cross_platform
            else max(existing.monthly_dollars, incoming.monthly_dollars)
        )
    elif not existing.is_one_time:
        is_one_time = False
        monthly_dollars = existing.monthly_dollars
    elif not incoming.is_one_time:
        is_one_time = False
        monthly_dollars = incoming.monthly_dollars
    else:
        is_one_time = True
        monthly_dollars = (
            existing.monthly_dollars + incoming.monthly_dollars
            if cross_platform
            else max(existing.monthly_dollars, incoming.monthly_dollars)
        )

    primary = (
        existing
        if existing.platform == "github" or (incoming.platform != "github" and not existing.is_one_time)
        else incoming
    )
    secondary = incoming if primary is existing else existing

    name = (
        primary.name
        if primary.name.lower() != primary.login.lower() or secondary.name.lower() == secondary.login.lower()
        else secondary.name
    )
    primary_is_generic_kofi = urllib.parse.urlsplit(primary.url).hostname in ("ko-fi.com", "www.ko-fi.com")
    url = secondary.url if primary_is_generic_kofi and secondary.url else primary.url
    avatar_url = primary.avatar_url or secondary.avatar_url
    avatar_data_uri = primary.avatar_data_uri or secondary.avatar_data_uri
    if not is_one_time and monthly_dollars >= featured_threshold and avatar_url and not avatar_data_uri:
        avatar_data_uri = fetch_avatar_data_uri(avatar_url, size=128)

    return Sponsor(
        login=primary.login,
        name=name,
        avatar_url=avatar_url,
        url=url,
        monthly_dollars=monthly_dollars,
        is_one_time=is_one_time,
        avatar_data_uri=avatar_data_uri,
        platform=primary.platform,
    )


def merge_sponsors(
    *groups: Sequence[Sponsor],
    featured_threshold: int = FEATURED_THRESHOLD,
) -> list[Sponsor]:
    merged: dict[tuple[str, str], Sponsor] = {}
    for group in groups:
        for sponsor in group:
            key = _canonical_sponsor_key(sponsor)
            existing = merged.get(key)
            merged[key] = (
                sponsor
                if existing is None
                else _combine_sponsor_pair(
                    existing, sponsor, featured_threshold=featured_threshold
                )
            )
    return sort_sponsors(list(merged.values()))


def truncate_label(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def render_style() -> str:
    return SVG_STYLE


def render_platform_card_svg(platform: str, *, has_monthly_sponsors: bool = False) -> str:
    if has_monthly_sponsors:
        width, height, cx, cy = 304, 56, 28, 28
        pill_x, pill_y, pill_w = 222, 15, 70
        title_y, sub_y, pill_text_y = 24.0, 40.0, 32.2
    else:
        width, height, cx, cy = 340, 60, 30, 30
        pill_x, pill_y, pill_w = 256, 17, 72
        title_y, sub_y, pill_text_y = 25.5, 42.0, 34.2

    pill_center_x = pill_x + pill_w / 2

    if platform == "github":
        aria_label = "Sponsor on GitHub Sponsors"
        title = "GitHub Sponsors" if has_monthly_sponsors else "Become the first sponsor"
        subtitle = "Monthly & one-time tiers" if has_monthly_sponsors else "Get featured across every repo"
        pill_label = "Sponsor"
        theme = "github"
        heart_path = (
            "M7.655 14.916v-.001h-.002l-.006-.003-.018-.01a22.066 22.066 0 0 1-3.744-2.584"
            "C2.045 10.731 0 8.35 0 5.5 0 2.836 2.086 1 4.25 1 5.797 1 7.153 1.802 8 3.02"
            " 8.847 1.802 10.203 1 11.75 1 13.914 1 16 2.836 16 5.5c0 2.85-2.044 5.231-3.886 6.818"
            "a22.094 22.094 0 0 1-3.433 2.414 7.152 7.152 0 0 1-.31.17l-.018.01-.008.004a.75.75 0 0 1-.69 0Z"
        )
        icon_el = (
            f'  <path class="cta-icon-{theme}" '
            f'transform="translate({cx - 6.8:.1f}, {cy - 6.5:.1f}) scale(0.85)" d="{heart_path}" />'
        )
    elif platform == "kofi":
        aria_label = "Support on Ko-fi"
        title = "Ko-fi" if has_monthly_sponsors else "Become the first on Ko-fi"
        subtitle = "0% fee tips & memberships" if has_monthly_sponsors else "0% fee · tag @github to list avatar"
        pill_label = "Support"
        theme = "kofi"
        cup_path = (
            "M1 3.5A1 1 0 0 1 2 2.5h9.5a1 1 0 0 1 1 1V4.5h1.25a2.75 2.75 0 0 1 0 5.5H12.4"
            "A4.5 4.5 0 0 1 8 13.75H5.5A4.5 4.5 0 0 1 1 9.25V3.5Z"
            "M12.5 6v2.5h1.25a1.25 1.25 0 0 0 0-2.5H12.5Z"
            "M6.75 10.9c.38-.24 2.85-1.85 2.85-3.7 0-1.05-.8-1.82-1.75-1.82-.48 0-.86.24-1.1.6"
            "-.24-.36-.62-.6-1.1-.6-.95 0-1.75.77-1.75 1.82 0 1.85 2.47 3.46 2.85 3.7Z"
        )
        icon_el = (
            f'  <path class="cta-icon-{theme}" fill-rule="evenodd" '
            f'transform="translate({cx - 6.75:.2f}, {cy - 8.125:.3f})" d="{cup_path}" />'
        )
    else:
        raise ValueError(f"Unsupported platform '{platform}'")

    text_x = cx + 28
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{html.escape(aria_label)}">',
        SVG_STYLE,
        f'  <rect class="cta-card" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" />',
        f'  <circle class="cta-slot-{theme}" cx="{cx}" cy="{cy}" r="16" pathLength="96" />',
        icon_el,
        f'  <text class="font cta-title" x="{text_x}" y="{title_y:g}">{html.escape(title)}</text>',
        f'  <text class="font cta-sub" x="{text_x}" y="{sub_y:g}">{html.escape(subtitle)}</text>',
        f'  <rect class="cta-pill-{theme}" x="{pill_x}" y="{pill_y}" width="{pill_w}" height="26" rx="13" />',
        f'  <text class="font cta-pill-text-{theme}" x="{pill_center_x:.1f}" y="{pill_text_y}" text-anchor="middle">{html.escape(pill_label)}</text>',
        "</svg>",
        "",
    ]
    return "\n".join(lines)


def render_empty_svg() -> str:
    # Sits inline inside the same <p align="center"> as the platform CTA cards so it adds 0px height when empty.
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" width="1" height="1" role="img" aria-label="Sponsors"></svg>\n'


def render_tier_section(
    sponsors: list[Sponsor],
    *,
    start_y: int,
    width: int,
    avatar_size: int,
    cell_w: int,
    cell_h: int,
    cols: int,
    featured: bool,
    heading: str | None,
    clip_prefix: str,
) -> tuple[list[str], list[str], int]:
    defs: list[str] = []
    body: list[str] = []
    y = start_y

    if heading:
        body.append(
            f'  <text class="font tier-heading" x="{width / 2:.1f}" y="{y + 14}" text-anchor="middle">{html.escape(heading)}</text>'
        )
        y += 28

    rows = math.ceil(len(sponsors) / cols)
    r = avatar_size / 2

    for row_idx in range(rows):
        row_items = sponsors[row_idx * cols : (row_idx + 1) * cols]
        row_start_x = (width - len(row_items) * cell_w) / 2

        for col_idx, sponsor in enumerate(row_items):
            idx = row_idx * cols + col_idx
            clip_id = f"{clip_prefix}-{idx}"
            cx = row_start_x + col_idx * cell_w + cell_w / 2
            cy = y + r

            defs.append(
                f'    <clipPath id="{clip_id}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" /></clipPath>'
            )

            esc_login = html.escape(sponsor.login)
            esc_name = html.escape(sponsor.name)
            esc_url = html.escape(sponsor.url)
            tooltip = (
                f"{esc_name} (Ko-fi)"
                if sponsor.platform == "kofi" and sponsor.login.lower() == sponsor.name.lower()
                else f"{esc_name} (@{esc_login})"
            )

            body.append(f'  <a href="{esc_url}" target="_blank" rel="noopener noreferrer">')
            body.append(f"    <title>{tooltip}</title>")

            if sponsor.avatar_data_uri:
                body.append(
                    f'    <image x="{cx - r:.1f}" y="{cy - r:.1f}" width="{avatar_size}" height="{avatar_size}" href="{sponsor.avatar_data_uri}" clip-path="url(#{clip_id})" />'
                )
            else:
                initial = html.escape((sponsor.name or sponsor.login)[:1].upper())
                font_px = int(avatar_size * 0.4)
                body.append(
                    f'    <circle class="avatar-fallback" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" />'
                )
                body.append(
                    f'    <text class="font avatar-initial" x="{cx:.1f}" y="{cy + font_px * 0.35:.1f}" font-size="{font_px}" text-anchor="middle">{initial}</text>'
                )

            ring_class = "avatar-ring-featured" if featured else "avatar-ring"
            body.append(
                f'    <circle class="{ring_class}" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" />'
            )

            label_text = truncate_label(
                sponsor.name if featured else sponsor.login,
                14 if featured else 12,
            )
            label_class = "sponsor-name" if featured else "sponsor-login"
            body.append(
                f'    <text class="font {label_class}" x="{cx:.1f}" y="{cy + r + 16:.1f}" text-anchor="middle">{html.escape(label_text)}</text>'
            )
            body.append("  </a>")

        y += cell_h

    return defs, body, y


def render_sponsors_svg(
    sponsors: list[Sponsor],
    *,
    featured_threshold: int = FEATURED_THRESHOLD,
) -> str:
    if not sponsors:
        return render_empty_svg()

    featured = [
        s for s in sponsors if not s.is_one_time and s.monthly_dollars >= featured_threshold
    ]
    standard = [
        s for s in sponsors if s.is_one_time or s.monthly_dollars < featured_threshold
    ]

    feat_cell_w, feat_cols = 96, 7
    std_cell_w, std_cols = 76, 9

    both_tiers = bool(featured and standard)
    content_w = max(
        min(len(featured), feat_cols) * feat_cell_w if featured else 0,
        min(len(standard), std_cols) * std_cell_w if standard else 0,
        160 if both_tiers else 0,
    )
    # Minimum width of 680 ensures sponsors.svg always wraps onto its own centered row
    # below the two platform cards inside a single <p align="center"> block.
    width = max(content_w + 8, 680)

    defs: list[str] = []
    body: list[str] = []
    y = 18

    if featured:
        f_defs, f_body, y = render_tier_section(
            featured,
            start_y=y,
            width=width,
            avatar_size=64,
            cell_w=feat_cell_w,
            cell_h=92,
            cols=feat_cols,
            featured=True,
            heading="Featured sponsors" if both_tiers else None,
            clip_prefix="feat",
        )
        defs.extend(f_defs)
        body.extend(f_body)
        if standard:
            y += 10

    if standard:
        s_defs, s_body, y = render_tier_section(
            standard,
            start_y=y,
            width=width,
            avatar_size=46,
            cell_w=std_cell_w,
            cell_h=74,
            cols=std_cols,
            featured=False,
            heading="Sponsors" if both_tiers else None,
            clip_prefix="std",
        )
        defs.extend(s_defs)
        body.extend(s_body)

    height = y + 4
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="Sponsors">',
        SVG_STYLE,
    ]
    if defs:
        lines.append("  <defs>")
        lines.extend(defs)
        lines.append("  </defs>")
    lines.extend(body)
    lines.append("</svg>")
    lines.append("")
    return "\n".join(lines)


def _run_git(
    args: Sequence[str],
    repo_dir: Path,
    *,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        timeout=60,
        input=input_text,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def git_identity_env(repo_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        if _run_git(["var", f"GIT_{role}_IDENT"], repo_dir, check=False).returncode != 0:
            env.setdefault(f"GIT_{role}_NAME", "github-actions[bot]")
            env.setdefault(
                f"GIT_{role}_EMAIL",
                "41898282+github-actions[bot]@users.noreply.github.com",
            )
    return env


def sync_branch_ref(branch: str, repo_dir: Path, *, fetch_remote: bool = False) -> None:
    ref = f"refs/heads/{branch}"
    local_exists = _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_dir, check=False).returncode == 0
    if fetch_remote or not local_exists:
        if _run_git(["ls-remote", "--exit-code", "--heads", "origin", branch], repo_dir, check=False).returncode == 0:
            _run_git(["fetch", "--no-tags", "origin", f"+{ref}:{ref}"], repo_dir)


def read_branch_file(branch: str, filename: str, repo_dir: Path) -> str | None:
    res = _run_git(["show", f"refs/heads/{branch}:{filename}"], repo_dir, check=False)
    return res.stdout if res.returncode == 0 else None


def _build_branch_tree(svg_paths: Sequence[Path], ref: str, repo_dir: Path) -> str:
    updated_names: set[str] = set()
    entry_lines: list[str] = []
    for svg_path in svg_paths:
        blob_sha = _run_git(["hash-object", "-w", str(svg_path)], repo_dir).stdout.strip()
        entry_lines.append(f"100644 blob {blob_sha}\t{svg_path.name}")
        updated_names.add(svg_path.name)

    existing_tree = _run_git(["ls-tree", ref], repo_dir, check=False)
    if existing_tree.returncode == 0:
        for line in existing_tree.stdout.splitlines():
            name = line.split("\t", 1)[1] if "\t" in line else ""
            if name and name not in updated_names:
                entry_lines.append(line)

    return _run_git(["mktree"], repo_dir, input_text="\n".join(entry_lines) + "\n").stdout.strip()


def publish_to_branch(
    svg_paths: Sequence[Path],
    branch: str,
    push: bool,
    repo_dir: Path,
    *,
    already_synced: bool = False,
) -> None:
    ref = f"refs/heads/{branch}"
    if not already_synced:
        sync_branch_ref(branch, repo_dir, fetch_remote=push)

    tree_sha = _build_branch_tree(svg_paths, ref, repo_dir)
    tip_res = _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_dir, check=False)
    tip = tip_res.stdout.strip() if tip_res.returncode == 0 else None
    if tip:
        tip_tree = _run_git(["rev-parse", f"{tip}^{{tree}}"], repo_dir).stdout.strip()
        has_parent = _run_git(["rev-parse", "--verify", "--quiet", f"{tip}^"], repo_dir, check=False).returncode == 0
        if tip_tree == tree_sha and not has_parent:
            print(f"Branch '{branch}' is already up to date ({tip[:7]})")
            if push:
                _run_git(["push", "--force", "origin", f"{ref}:{ref}"], repo_dir)
            return

    names_label = ", ".join(p.name for p in svg_paths)
    commit_sha = _run_git(
        ["commit-tree", tree_sha, "-m", f"chore: update {names_label}"],
        repo_dir,
        env=git_identity_env(repo_dir),
    ).stdout.strip()

    _run_git(["update-ref", ref, commit_sha], repo_dir)
    print(f"Updated branch '{branch}' ({commit_sha[:7]}) with {names_label}")

    if push:
        _run_git(["push", "--force", "origin", f"{ref}:{ref}"], repo_dir)
        print(f"Pushed '{branch}' to origin")


def build_demo_sponsors() -> list[Sponsor]:
    return [
        Sponsor(
            login="ada",
            name="Ada Lovelace",
            avatar_url="",
            url="https://github.com/ada",
            monthly_dollars=25,
            is_one_time=False,
            platform="github",
        ),
        Sponsor(
            login="grace",
            name="Grace Hopper",
            avatar_url="",
            url="https://ko-fi.com/grace",
            monthly_dollars=25,
            is_one_time=False,
            platform="kofi",
        ),
        Sponsor(
            login="linus",
            name="Linus Torvalds",
            avatar_url="",
            url="https://github.com/linus",
            monthly_dollars=5,
            is_one_time=False,
            platform="github",
        ),
        Sponsor(
            login="margaret",
            name="Margaret Hamilton",
            avatar_url="",
            url="https://ko-fi.com/margaret",
            monthly_dollars=5,
            is_one_time=False,
            platform="kofi",
        ),
        Sponsor(
            login="ken",
            name="Ken Thompson",
            avatar_url="",
            url="https://ko-fi.com/ken",
            monthly_dollars=1,
            is_one_time=True,
            platform="kofi",
        ),
    ]


def _parse_cli_args(default_gh: str, default_kofi: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate tiered Sponsors wall and platform CTA SVGs.")
    parser.add_argument(
        "--login",
        default=default_gh,
        help=f"GitHub username to query (default: {default_gh or 'from .github/FUNDING.yml'})",
    )
    parser.add_argument(
        "--kofi",
        default=default_kofi,
        help=f"Ko-fi username or URL for fallback links (default: {default_kofi or 'from .github/FUNDING.yml'})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="sponsors.svg",
        help="Output SVG file path for the unified sponsors wall (default: sponsors.svg)",
    )
    parser.add_argument(
        "--extra",
        type=Path,
        default=DEFAULT_EXTRA_PATH,
        help="Path to JSON file with extra/Ko-fi sponsors and config (default: .github/sponsors.json)",
    )
    parser.add_argument(
        "--featured-threshold",
        type=int,
        default=None,
        help=f"Monthly dollar threshold for Featured sponsors tier (default: {FEATURED_THRESHOLD})",
    )
    parser.add_argument(
        "--active-window-days",
        type=int,
        default=None,
        help=f"Rolling active window in days for Ko-fi records (default: {ACTIVE_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--ingest-kofi",
        help="JSON string (or @filepath) of a Ko-fi webhook payload to ingest before rendering",
    )
    parser.add_argument(
        "--branch",
        help="Commit the generated SVGs and state onto the given orphan Git branch (e.g. badges)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Force-push --branch to origin after updating ref",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Render a demo multi-tier SVG fixture instead of querying GitHub",
    )
    return parser.parse_args()


def main() -> int:
    repo_dir = Path(__file__).resolve().parent.parent
    default_gh, default_kofi = load_funding_defaults(repo_dir / DEFAULT_FUNDING_PATH, repo_dir=repo_dir)
    args = _parse_cli_args(default_gh, default_kofi)
    default_kofi_url = kofi_profile_url(args.kofi)
    cfg = load_sponsors_config(
        args.extra,
        featured_threshold=args.featured_threshold,
        active_window_days=args.active_window_days,
    )

    out_path = Path(args.output)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        branch_state_path: Path | None = None
        if args.branch:
            sync_branch_ref(args.branch, repo_dir, fetch_remote=args.push)
            branch_state_path = Path(tmp_dir) / "sponsors.json"
            existing_branch_json = read_branch_file(args.branch, "sponsors.json", repo_dir)
            branch_state_path.write_text(
                existing_branch_json if existing_branch_json is not None else '{\n  "sponsors": []\n}\n',
                encoding="utf-8",
            )
            prune_extra_sponsors_file(
                branch_state_path, default_kofi_url=default_kofi_url, config=cfg
            )
        elif args.extra.is_file():
            prune_extra_sponsors_file(args.extra, default_kofi_url=default_kofi_url, config=cfg)

        ingest_target = branch_state_path if branch_state_path is not None else args.extra
        kofi_raw = (args.ingest_kofi or os.environ.get("KOFI_WEBHOOK_PAYLOAD", "")).strip()
        if kofi_raw and kofi_raw != "null":
            kofi_text = Path(kofi_raw[1:]).read_text(encoding="utf-8") if kofi_raw.startswith("@") else kofi_raw
            kofi_payload = json.loads(kofi_text)
            if isinstance(kofi_payload, dict):
                ingest_kofi_payload(
                    kofi_payload, ingest_target, default_kofi_url=default_kofi_url, config=cfg
                )

        extra_sources = [args.extra, *([branch_state_path] if branch_state_path is not None else [])]
        extra_sponsors = load_extra_sponsors(
            extra_paths=extra_sources, default_kofi_url=default_kofi_url, config=cfg
        )
        base_sponsors = (
            build_demo_sponsors()
            if args.demo
            else fetch_sponsors(args.login, resolve_token(), featured_threshold=cfg.featured_threshold)
        )
        sponsors = merge_sponsors(
            base_sponsors, extra_sponsors, featured_threshold=cfg.featured_threshold
        )

        has_monthly = has_recurring_sponsors(sponsors)
        out_path.write_text(
            render_sponsors_svg(sponsors, featured_threshold=cfg.featured_threshold),
            encoding="utf-8",
        )

        github_card_path = out_dir / "sponsor-github.svg"
        kofi_card_path = out_dir / "sponsor-kofi.svg"
        github_card_path.write_text(
            render_platform_card_svg("github", has_monthly_sponsors=has_monthly),
            encoding="utf-8",
        )
        kofi_card_path.write_text(
            render_platform_card_svg("kofi", has_monthly_sponsors=has_monthly),
            encoding="utf-8",
        )

        print(
            f"Wrote {out_path}, {github_card_path}, {kofi_card_path} "
            f"({len(sponsors)} public sponsor(s), recurring={has_monthly})"
        )

        if args.branch and branch_state_path is not None:
            publish_to_branch(
                [out_path, github_card_path, kofi_card_path, branch_state_path],
                args.branch,
                args.push,
                repo_dir,
                already_synced=True,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
