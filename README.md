# NetSapiens AA Digit-Press Report

A standalone CLI tool that pulls Call Detail Records (CDRs) from a NetSapiens v2 API, identifies calls that interacted with an Auto Attendant, and reports which menu digit each caller pressed along with where the call routed.

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration with providers.toml](#configuration-with-providerstoml)
- [Token handling](#token-handling)
- [CLI reference](#cli-reference)
- [Output formats](#output-formats)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)
- [Background](#background)
- [License](#license)

## How it works

When a caller interacts with a NetSapiens Auto Attendant, the resulting CDR's `call-term-pre-routing-uri` field contains a value like:

```
Prompt_600001.Case_2
```

This tells you the prompt ID (`600001`) and the digit the caller pressed (`2`). The script:

1. Pulls CDRs for the requested date range, paginating until exhausted
2. Filters for CDRs whose pre-routing URI matches `Prompt_<id>.Case_<digit>`
3. Derives the AA owner's user ID by stripping the last two characters of the prompt ID (the prompt index)
4. Fetches the AA configuration from `/users/{aa_user}/autoattendants/{prompt_full}`, caching identical configs so duplicate lookups don't fire
5. Reads `option-{digit}` from the AA config to map the digit to its destination user and description
6. Outputs a row per AA digit press with timestamp, caller info, AA name, digit, destination, and the menu option description

## Requirements

- Python 3.11 or newer (recommended) — or Python 3.8–3.10 with `pip install tomli` for TOML profile support
- `pip install requests`

## Installation

```bash
git clone https://github.com/<your-user>/NetsapiensAutoAttendantReport.git
cd NetsapiensAutoAttendantReport
pip install requests
chmod +x aa_digit_report.py
```

On modern Ubuntu/Debian you may hit `error: externally-managed-environment` from pip. Either:

```bash
pip install requests --break-system-packages
```

…or use a virtualenv:

```bash
python3 -m venv ~/.venvs/ns
~/.venvs/ns/bin/pip install requests
# then call the script with that interpreter
~/.venvs/ns/bin/python aa_digit_report.py ...
```

## Quick start

The simplest one-shot invocation with everything on the command line:

```bash
python3 aa_digit_report.py \
  --api-base https://fqdn/ns-api/v2 \
  --domain MyDomain \
  --token nss_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --start 2026-05-20 \
  --end 2026-05-20
```

That prints a table of every AA digit press for May 20 to your terminal.

Add `--format csv > report.csv` or `--format json > report.json` to save structured output.

## Configuration with providers.toml

For repeat use — especially across multiple NetSapiens providers — define profiles once and select them with `--profile`.

### Setup

```bash
mkdir -p ~/.config/ns
chmod 700 ~/.config/ns
cp providers.toml.example ~/.config/ns/providers.toml
chmod 600 ~/.config/ns/providers.toml      # matters only if you inline tokens
```

Edit `~/.config/ns/providers.toml`:

```toml
default = "myprovider"

[myprovider]
api_base   = "https://fqdn/ns-api/v2"
domain     = "MyDomain"
token_file = "~/.config/ns/myprovider-token"
```

Drop the token in its own file so it can be chmod'd and rotated independently:

```bash
echo 'nss_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' > ~/.config/ns/myprovider-token
chmod 600 ~/.config/ns/myprovider-token
```

Now invocations get short:

```bash
python3 aa_digit_report.py --start 2026-05-20 --end 2026-05-20
```

No flags needed for base URL, domain, or token — the default profile supplies them.

### Multiple providers

```toml
default = "acme"

[acme]
api_base   = "https://fqdn/ns-api/v2"
domain     = "AcmeCorp"
token_file = "~/.config/ns/acme-token"
```

Switch between them:

```bash
python3 aa_digit_report.py --profile acme --start 2026-05-20
python3 aa_digit_report.py --list-profiles                       # show what's defined
NS_PROFILE=acme python3 aa_digit_report.py --start 2026-05-20    # select via env
```

### Config file location

Default: `~/.config/ns/providers.toml`

Override by exporting `XDG_CONFIG_HOME`; the script will then look for `$XDG_CONFIG_HOME/ns/providers.toml`.

## Token handling

Tokens are resolved in this order (highest priority first):

1. `--token <value>` — explicit CLI flag
2. `--token-file <path>` — file containing the token (one per line; `#` comments allowed)
3. Selected profile's `token_file`
4. Selected profile's inline `token`
5. `$NS_API_TOKEN` environment variable
6. Interactive `getpass` prompt — only if stdin is a TTY, so cron and pipes won't hang

For production use, `token_file` in a profile is usually cleanest — the file can be chmod 600'd and rotated without touching your config.

## CLI reference

| Flag | Description |
|------|-------------|
| `--start <date>` | Start datetime or `YYYY-MM-DD` (default: today) |
| `--end <date>` | End datetime or `YYYY-MM-DD` (default: today) |
| `--profile <name>` | Profile from `providers.toml` (overrides `$NS_PROFILE`) |
| `--list-profiles` | List configured profiles and exit |
| `--api-base <url>` | Override API base URL |
| `--domain <name>` | Override NetSapiens domain |
| `--token <value>` | Bearer token (overrides --token-file, profile, env) |
| `--token-file <path>` | Read bearer token from a file |
| `--format {table,csv,json}` | Output format (default: `table`) |
| `--inbound-only` | Skip CDRs that aren't inbound calls |
| `--help` | Show all flags |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `NS_API_BASE` | Default API base URL |
| `NS_DOMAIN` | Default NetSapiens domain |
| `NS_API_TOKEN` | Default bearer token |
| `NS_PROFILE` | Default profile name |
| `XDG_CONFIG_HOME` | Config file root (default: `~/.config`) |

### Date formats

Both `--start` and `--end` accept:
- `YYYY-MM-DD` — interpreted as `00:00:00` for `--start` and `23:59:59` for `--end`
- `YYYY-MM-DD HH:MM:SS` — full datetime for sub-day precision

`--start` and `--end` default independently to today's date, so omitting `--end` does **not** mean "through now." If `--start` is in the past, set `--end` explicitly.

## Output formats

### Table (default)

```
timestamp            caller_number   auto_attendant     digit  destination
-------------------  --------------  -----------------  -----  ------------------
2026-05-20 10:30:50  +13145753268    Business Hours AA  2      7002 Support RG
2026-05-20 11:15:22  +13125550199    Business Hours AA  1      7001 Sales RG
```

### CSV

```bash
python3 aa_digit_report.py --start 2026-05-20 --format csv > report.csv
```

Includes additional fields beyond the table view: `caller_user`, `called_user`, and `option_description`.

### JSON

```bash
python3 aa_digit_report.py --start 2026-05-20 --format json > report.json
```

Same schema as CSV, emitted as a JSON array — convenient for piping into `jq` or feeding downstream tools.

## Examples

### Single day report

```bash
python3 aa_digit_report.py --profile acme --start 2026-05-20 --end 2026-05-20
```

### Date range with CSV export

```bash
python3 aa_digit_report.py --profile acme \
  --start 2026-05-15 --end 2026-05-21 \
  --format csv > weekly.csv
```

### One-off provider without a profile

```bash
python3 aa_digit_report.py \
  --api-base https://fqdn/ns-api/v2 \
  --domain MyDomain \
  --token-file /tmp/example-token \
  --start 2026-05-20 --end 2026-05-20
```

### Filter to a specific digit with `jq`

```bash
python3 aa_digit_report.py --start 2026-05-20 --end 2026-05-20 --format json | \
  jq '.[] | select(.digit == "9")'
```

### Daily cron job

```cron
# /etc/cron.d/aa-digit-report
0 1 * * * support /usr/local/bin/python3 /opt/scripts/aa_digit_report.py \
  --profile acme \
  --start $(date -d yesterday +\%Y-\%m-\%d) \
  --end   $(date -d yesterday +\%Y-\%m-\%d) \
  --format csv > /var/log/aa-reports/$(date +\%Y-\%m-\%d).csv
```

(`%` must be backslash-escaped inside crontab files.)

## Troubleshooting

**`error: no API token`** — none of the token sources resolved a token. Provide `--token`, `--token-file`, set up a profile, export `$NS_API_TOKEN`, or run from a terminal to be prompted.

**`error: api_base and domain are required`** — you haven't supplied them via CLI flags, a profile, env vars, or by editing the defaults in the script.

**`error: cannot read token file <path>`** — the path is wrong or permissions block reading. Confirm with `cat <path>`.

**`warn: AA lookup failed for Prompt_NNNNNN: 404`** — the prompt ID parsed correctly but the AA config isn't reachable. Usually means the AA was deleted, renumbered, or the token's scope doesn't include that user.

**`(no AA digit presses found)`** — no CDRs in the range had a `Prompt_*.Case_*` pre-routing URI. Confirm the date range is correct and that calls actually reached the AA during that window. Try a wider range first to validate the script can see your CDRs at all.

**Profile file ignored silently** — confirm the path with `python3 -c "import os; print(os.path.expanduser('~/.config/ns/providers.toml'))"` and that `--list-profiles` shows your entries. If the file exists but the script warns about a missing TOML parser, install with `pip install tomli` (Python ≤3.10 only).

**Stale or missing recent CDRs** — NetSapiens typically reflects CDRs within a minute or two of call completion, but very recent calls may not appear yet. Try again after a brief delay.

## Background

This script was extracted from an n8n workflow used for routing diagnostics. The NetSapiens AA pre-routing URI convention (`Prompt_<id>.Case_<digit>`) and the prompt-ID-to-AA-owner derivation (strip last two chars) follow NetSapiens platform behavior validated against SiPbx v44.

If you want to extend the script — say, to publish results to a dashboard, write into PostgreSQL, or chart digit-press distribution over time — the rows emitted by `--format json` are the cleanest integration point.

## License

Released under the [MIT License](LICENSE). See `LICENSE` for full terms.
