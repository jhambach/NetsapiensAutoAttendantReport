#!/usr/bin/env python3
"""
aa_digit_report.py — NetSapiens Auto-Attendant digit-press tracker.

Pulls CDRs from the NetSapiens v2 API for a date range, filters to calls
that hit an Auto Attendant, looks up the AA config to map the pressed
digit to that menu option's destination, and prints/exports a report.

Config & credential resolution priority (highest first):
  1. CLI flags: --api-base, --domain, --token, --token-file
  2. Selected profile from ~/.config/ns/providers.toml (via --profile,
     $NS_PROFILE, or 'default = "name"' in the TOML file)
  3. Environment: $NS_API_BASE, $NS_DOMAIN, $NS_API_TOKEN
  4. Script defaults (edit API_BASE / DOMAIN constants below)
For token entry only, an interactive getpass prompt is offered as a final
fallback when stdin is a TTY.

Requires: requests. Python 3.11+ has TOML built in; on 3.10 and below
also: pip install tomli

Usage:
    # Using a profile from ~/.config/ns/providers.toml
    python aa_digit_report.py --profile myprovider --start 2026-05-20

    # Inspect what's configured
    python aa_digit_report.py --list-profiles

    # No profile: explicit flags
    python aa_digit_report.py --api-base https://api.example.com/ns-api/v2 \\
                              --domain MyDomain --token-file ~/.config/ns/token \\
                              --start 2026-05-20 --end 2026-05-20 --format csv > report.csv
"""

import argparse
import csv
import getpass
import json
import os
import re
import sys
from datetime import date
from typing import Iterator, Optional

import requests

# TOML parser: stdlib on 3.11+, 'tomli' backport on 3.10 and below
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# Script-level defaults — edit these if you want one-provider hardcoding,
# otherwise leave as "{fill in}" and use profiles/env/CLI.
API_BASE_DEFAULT = os.environ.get("NS_API_BASE", "{fill in}")
DOMAIN_DEFAULT = os.environ.get("NS_DOMAIN", "{fill in}")
TOKEN_ENV = "NS_API_TOKEN"
PROFILE_ENV = "NS_PROFILE"
PAGE_SIZE = 100

# Matches NetSapiens AA pre-routing URIs like "Prompt_600001.Case_2"
PROMPT_RE = re.compile(r"^Prompt_(\d+)\.Case_(\d+)$")


# ---------- config / profiles ----------

def config_path() -> str:
    """Standard location: $XDG_CONFIG_HOME/ns/providers.toml, else ~/.config/ns/providers.toml."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.expanduser("~/.config")
    return os.path.join(base, "ns", "providers.toml")


def load_config() -> dict:
    """Load the TOML config file. Returns empty dict if the file is missing."""
    path = config_path()
    if not os.path.exists(path):
        return {}
    if tomllib is None:
        print(
            f"warn: {path} exists but no TOML parser is available "
            f"(need Python 3.11+ or 'pip install tomli'); ignoring config.",
            file=sys.stderr,
        )
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def pick_profile(config: dict, requested: Optional[str]) -> Optional[dict]:
    """
    Choose a profile by name. Falls back to $NS_PROFILE, then the 'default = "name"'
    key in the TOML file. Returns None if no profile is selected.
    """
    name = requested or os.environ.get(PROFILE_ENV) or config.get("default")
    if not name:
        return None
    profile = config.get(name)
    if not isinstance(profile, dict):
        print(f"error: profile '{name}' not found in {config_path()}", file=sys.stderr)
        return None
    return {**profile, "_name": name}


def list_profiles(config: dict) -> None:
    """Print available profiles in a human-readable form."""
    path = config_path()
    if not config:
        print(f"(no config at {path})")
        return
    names = sorted(k for k, v in config.items() if isinstance(v, dict))
    if not names:
        print(f"(no profiles defined in {path})")
        return
    default = config.get("default")
    print(f"profiles in {path}:")
    for n in names:
        marker = "  (default)" if n == default else ""
        prof = config[n]
        tok_src = (
            f"token_file = {prof['token_file']}" if "token_file" in prof
            else "token (inline)" if "token" in prof
            else "(no token source)"
        )
        print(f"  {n}{marker}")
        print(f"    api_base   = {prof.get('api_base', '(unset)')}")
        print(f"    domain     = {prof.get('domain', '(unset)')}")
        print(f"    {tok_src}")


# ---------- auth ----------

def resolve_token(
    cli_token: Optional[str],
    token_file: Optional[str],
    profile: Optional[dict],
) -> Optional[str]:
    """
    Token precedence: CLI --token > --token-file > profile.token_file > profile.token >
    $NS_API_TOKEN > interactive prompt (TTY only).
    """
    if cli_token:
        return cli_token.strip()

    # Fall back to profile's token_file if the flag wasn't given
    if token_file is None and profile and profile.get("token_file"):
        token_file = profile["token_file"]

    if token_file:
        path = os.path.expanduser(token_file)
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError as e:
            print(f"error: cannot read token file {path}: {e}", file=sys.stderr)
            return None

    if profile and profile.get("token"):
        return str(profile["token"]).strip()

    env_token = os.environ.get(TOKEN_ENV)
    if env_token:
        return env_token.strip()

    if sys.stdin.isatty():
        try:
            entered = getpass.getpass("NetSapiens API token: ").strip()
            return entered or None
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None

    return None


# ---------- API helpers ----------

def fetch_cdrs(
    session: requests.Session, api_base: str, domain: str, start: str, end: str,
) -> Iterator[dict]:
    """Yield every CDR in [start, end], paginating until exhausted."""
    url = f"{api_base}/domains/{domain}/cdrs"
    offset = 0
    while True:
        params = {
            "datetime-start": start,
            "datetime-end": end,
            "limit": PAGE_SIZE,
            "start": offset,
        }
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json() or []
        if not batch:
            return
        for cdr in batch:
            yield cdr
        if len(batch) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def fetch_aa(
    session: requests.Session, api_base: str, domain: str,
    aa_user: str, prompt_full: str, cache: dict,
) -> Optional[dict]:
    """Fetch AA config, cached per (user, prompt). Returns None on 404."""
    key = (aa_user, prompt_full)
    if key in cache:
        return cache[key]
    url = f"{api_base}/domains/{domain}/users/{aa_user}/autoattendants/{prompt_full}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        cache[key] = None
        return None
    r.raise_for_status()
    aa = r.json()
    cache[key] = aa
    return aa


# ---------- parsing / mapping ----------

def parse_prompt_uri(uri: str) -> Optional[dict]:
    """
    'Prompt_600001.Case_2' -> {aa_user: '6000', prompt_full: 'Prompt_600001', digit: '2'}
    Returns None if the URI doesn't match the AA pattern.
    """
    if not uri:
        return None
    m = PROMPT_RE.match(uri)
    if not m:
        return None
    prompt_id, digit = m.group(1), m.group(2)
    if len(prompt_id) < 3:
        return None
    return {
        "aa_user": prompt_id[:-2],
        "prompt_full": f"Prompt_{prompt_id}",
        "digit": digit,
    }


def resolve_destination(aa: dict, digit: str) -> tuple[Optional[str], Optional[str]]:
    """
    From an AA config + pressed digit, return (clean_description, destination_string).
    The description in AA designer looks like:
      'AA designer: press 2 for user 7002 (Support RG)'
    """
    if not isinstance(aa, dict):
        return None, None
    options = aa.get("auto-attendant") or {}
    opt = options.get(f"option-{digit}") or {}
    raw = opt.get("description") or ""
    desc = re.sub(r"^AA designer:\s*", "", raw).strip() or None
    dest_user = opt.get("destination-user")
    paren = re.search(r"\(([^)]+)\)\s*$", desc or "")
    friendly = paren.group(1) if paren else None
    if dest_user and friendly:
        destination = f"{dest_user} {friendly}"
    else:
        destination = dest_user or friendly or desc
    return desc, destination


# ---------- output formatters ----------

def emit_table(rows: list[dict]) -> None:
    if not rows:
        print("(no AA digit presses found)")
        return
    cols = ["timestamp", "caller_number", "auto_attendant", "digit", "destination"]
    widths = {c: max(len(c), max(len(str(r.get(c) or "")) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in cols))


def emit_csv(rows: list[dict]) -> None:
    if not rows:
        return
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)


def emit_json(rows: list[dict]) -> None:
    print(json.dumps(rows, indent=2, default=str))


# ---------- main ----------

def normalize_dt(s: str, end_of_day: bool = False) -> str:
    """Allow bare YYYY-MM-DD; fill the time component."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s} {'23:59:59' if end_of_day else '00:00:00'}"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    today = date.today().isoformat()
    ap.add_argument("--start", default=today, help="Start datetime or YYYY-MM-DD (default: today)")
    ap.add_argument("--end", default=today, help="End datetime or YYYY-MM-DD (default: today)")

    ap.add_argument("--profile", default=None,
                    help=f"Profile name from {config_path()} (overrides ${PROFILE_ENV})")
    ap.add_argument("--list-profiles", action="store_true",
                    help="List configured profiles and exit")

    ap.add_argument("--api-base", default=None, help="Override API base URL")
    ap.add_argument("--domain", default=None, help="Override NetSapiens domain")
    ap.add_argument("--token", default=None,
                    help="Bearer token (overrides --token-file, profile, and env var)")
    ap.add_argument("--token-file", default=None,
                    help="Path to a file containing the bearer token (one line; '#' comments ok)")

    ap.add_argument("--format", choices=["table", "csv", "json"], default="table")
    ap.add_argument("--inbound-only", action="store_true",
                    help="Skip non-inbound CDRs (matches the original n8n intent more reliably)")
    args = ap.parse_args()

    config = load_config()

    if args.list_profiles:
        list_profiles(config)
        return 0

    profile = pick_profile(config, args.profile)

    # Resolve api_base and domain: CLI > profile > env > default
    api_base = (
        args.api_base
        or (profile.get("api_base") if profile else None)
        or os.environ.get("NS_API_BASE")
        or API_BASE_DEFAULT
    )
    domain = (
        args.domain
        or (profile.get("domain") if profile else None)
        or os.environ.get("NS_DOMAIN")
        or DOMAIN_DEFAULT
    )

    if not api_base or api_base == "{fill in}" or not domain or domain == "{fill in}":
        print(
            "error: api_base and domain are required. Provide via --profile, "
            f"--api-base/--domain, $NS_API_BASE/$NS_DOMAIN, or edit {config_path()}.",
            file=sys.stderr,
        )
        return 2

    token = resolve_token(args.token, args.token_file, profile)
    if not token:
        print(
            f"error: no API token. Provide --token, --token-file, a profile with token/token_file, "
            f"${TOKEN_ENV}, or run from a terminal to be prompted.",
            file=sys.stderr,
        )
        return 2

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    })

    start = normalize_dt(args.start, end_of_day=False)
    end = normalize_dt(args.end, end_of_day=True)

    aa_cache: dict = {}
    rows: list[dict] = []
    seen_cdrs = 0
    aa_hits = 0

    profile_label = f" (profile: {profile['_name']})" if profile else ""
    print(f"# api_base={api_base} domain={domain}{profile_label}", file=sys.stderr)

    for cdr in fetch_cdrs(session, api_base, domain, start, end):
        seen_cdrs += 1
        uri = cdr.get("call-term-pre-routing-uri") or ""
        if "Prompt_" not in uri:
            continue
        if args.inbound_only:
            direction = str(cdr.get("call-direction") or "").lower()
            if direction not in ("inbound", "0", "1"):
                continue

        parsed = parse_prompt_uri(uri)
        if not parsed:
            continue

        try:
            aa = fetch_aa(session, api_base, domain,
                          parsed["aa_user"], parsed["prompt_full"], aa_cache)
        except requests.HTTPError as e:
            print(f"warn: AA lookup failed for {parsed['prompt_full']}: {e}", file=sys.stderr)
            continue

        if aa is None:
            continue

        desc, destination = resolve_destination(aa, parsed["digit"])
        rows.append({
            "timestamp": cdr.get("time-start") or cdr.get("call-start-datetime"),
            "caller_number": cdr.get("call-orig-caller-id"),
            "caller_user": cdr.get("call-orig-user"),
            "called_user": cdr.get("call-term-user"),
            "auto_attendant": aa.get("attendant-name") if isinstance(aa, dict) else None,
            "digit": parsed["digit"],
            "destination": destination,
            "option_description": desc,
        })
        aa_hits += 1

    print(f"# scanned {seen_cdrs} CDRs, matched {aa_hits} AA digit press(es), "
          f"{len(aa_cache)} AA config lookup(s)", file=sys.stderr)

    if args.format == "csv":
        emit_csv(rows)
    elif args.format == "json":
        emit_json(rows)
    else:
        emit_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
