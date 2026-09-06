#!/usr/bin/env python3
"""
CC Island bridge — macOS / Windows / Linux.

Reads the credentials your CLIs already wrote, queries the providers' own
usage/balance endpoints, and pushes one compact JSON line per update to the
StopWatch over BLE (Nordic UART Service).

Providers (payload v3, one line of JSON):
  c   Claude Code  {h,d,r}    GET api.anthropic.com/api/oauth/usage
  x   ChatGPT      {h,d,r}    GET chatgpt.com/backend-api/wham/usage
                               (credential: OpenAI Codex CLI's ~/.codex/auth.json)
  g   GLM          {h,d,r}    GET open.bigmodel.cn/api/monitor/usage/quota/limit
                               (community endpoint, see docs §4.1; z.ai for intl)
  ds  DeepSeek     {cur,bal}  GET api.deepseek.com/user/balance (official)

Recipes mirror ericjypark/codex-island. Platform notes
(docs/windows-port-design.zh-CN.md):
  - macOS:          Claude OAuth in Keychain ("Claude Code-credentials").
  - Windows/Linux:  Claude OAuth in ~/.claude/.credentials.json (same JSON).
  - GLM/DeepSeek:   API key discovery: CLI -> ~/.cc-island/config.json (the
                    dedicated store) -> provider env vars -> ~/.claude/settings.json
                    (Claude Code routed through the provider) -> ~/.codex/config.toml
                    (Codex routed through the provider).

No tokens, keys or logs ever leave this machine — only the computed numbers.

Usage:
    python codexisland_bridge.py              # human-readable table
    python codexisland_bridge.py --json       # full JSON (what compact() shrinks)
    python codexisland_bridge.py --ble 5      # BLE push loop, every 5 minutes
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_CODE_USER_AGENT = "claude-code/2.1.121"
HTTP_TIMEOUT = 15

# python.org's Python ships without a populated CA store (macOS), so the default
# context fails TLS verification. Prefer certifi's bundle; Windows loads its own
# system store either way.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# Optional endpoint overrides (--glm-endpoint / --ds-endpoint).
_GLM_ENDPOINT = None
_DS_ENDPOINT = None

# Runtime log sink (--log-file); None = stdout only.
_LOG_FILE = None


# --------------------------------------------------------------------------- #
# Logging / console helpers
# --------------------------------------------------------------------------- #
def set_log_file(path):
    global _LOG_FILE
    _LOG_FILE = path


def _log(msg):
    """print() that also appends to --log-file (rotated at 1 MB)."""
    print(msg, flush=True)
    if not _LOG_FILE:
        return
    try:
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > 1_000_000:
            os.replace(_LOG_FILE, _LOG_FILE + ".1")
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except OSError:
        pass


def _force_utf8_stdio():
    """Chinese Windows consoles default to GBK; the table glyphs need UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def _http(method, url, headers=None, body=None):
    """Return (status, parsed_json_or_None). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"_transport_error": str(e)}


def _window(used_percent, reset_at):
    """Normalize one window to {pct: 0-100 float, reset_at: epoch_s|None}."""
    return {"pct": round(max(0.0, min(100.0, float(used_percent))), 1),
            "reset_at": reset_at}


def _parse_reset(value):
    """reset_at may be epoch seconds or an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = int(value)
        # GLM's nextResetTime arrives in milliseconds; normalize to seconds
        # (ms-since-epoch crossed 10^12 in 2001, s-since-epoch hits it in 33658).
        if value > 10**12:
            value //= 1000
        return value
    # ISO string
    try:
        from datetime import datetime
        s = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# ChatGPT (usage credential from the OpenAI Codex CLI)
# --------------------------------------------------------------------------- #
def fetch_chatgpt():
    path = os.path.expanduser("~/.codex/auth.json")
    try:
        with open(path) as f:
            tokens = json.load(f).get("tokens") or {}
        token = tokens.get("access_token")
    except (OSError, json.JSONDecodeError):
        token = None
    if not token:
        return {"error": "no codex auth (codex login)"}

    status, obj = _http(
        "GET", "https://chatgpt.com/backend-api/wham/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status == 401:
        return {"error": "auth expired — codex login"}
    if status != 200 or not isinstance(obj, dict):
        return {"error": f"http {status}"}

    rl = obj.get("rate_limit") or {}

    def win(w):
        d = rl.get(w) or {}
        return _window(d.get("used_percent", 0), _parse_reset(d.get("reset_at")))

    return {
        "plan": obj.get("plan_type"),
        "five_hour": win("primary_window"),
        "weekly": win("secondary_window"),
    }


# --------------------------------------------------------------------------- #
# Claude — credential flow, split per platform
#   macOS:    Keychain "Claude Code-credentials" via /usr/bin/security
#   Win/Linux: ~/.claude/.credentials.json  (same claudeAiOauth JSON)
# --------------------------------------------------------------------------- #
CLAUDE_CRED_FILE = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def _security(args):
    try:
        out = subprocess.run(
            ["/usr/bin/security", *args],
            capture_output=True, text=True, timeout=10,
        )
        return out
    except (OSError, subprocess.TimeoutExpired):
        return None


def _claude_keychain_account():
    """Pull the account name from the `"acct"...="value"` metadata line."""
    out = _security(["find-generic-password", "-s", "Claude Code-credentials"])
    if not out or out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line.startswith('"acct"'):
            continue
        if "=" not in line:
            return None
        value = line.split("=", 1)[1]
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            inner = value[1:-1]
            return inner or None
    return None


def _read_claude_creds_keychain():
    account = _claude_keychain_account()
    if not account:
        return None
    out = _security([
        "find-generic-password", "-s", "Claude Code-credentials",
        "-a", account, "-w",
    ])
    if not out or out.returncode != 0:
        return None
    try:
        outer = json.loads(out.stdout.strip())
        oauth = outer.get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return None
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        return None
    return {"account": account, "oauth": oauth}


def _write_claude_creds_keychain(account, oauth):
    payload = json.dumps({"claudeAiOauth": oauth})
    out = _security([
        "add-generic-password", "-U",
        "-s", "Claude Code-credentials", "-a", account, "-w", payload,
    ])
    return bool(out and out.returncode == 0)


def _read_claude_creds():
    """Return {account, oauth} or None. account is only meaningful on macOS."""
    if sys.platform == "darwin":
        return _read_claude_creds_keychain()
    # Windows / Linux: plain file next to Claude Code's other state. It may be
    # open by a running Claude Code — a transient PermissionError (OSError)
    # just skips this round.
    try:
        with open(CLAUDE_CRED_FILE, encoding="utf-8") as f:
            outer = json.load(f)
        oauth = outer.get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        return None
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        return None
    return {"account": None, "oauth": oauth}


def _write_claude_creds(account, oauth):
    """Persist rotated tokens back so Claude Code itself doesn't break."""
    if sys.platform == "darwin":
        return _write_claude_creds_keychain(account, oauth)
    # Read-modify-write the whole file, keeping unknown top-level keys, via a
    # temp file + atomic replace to shrink the race with a running Claude Code.
    outer = {}
    try:
        with open(CLAUDE_CRED_FILE, encoding="utf-8") as f:
            outer = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    outer["claudeAiOauth"] = oauth
    tmp = CLAUDE_CRED_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(outer, f)
        os.replace(tmp, CLAUDE_CRED_FILE)
        return True
    except OSError:
        return False


def _refresh_claude(refresh_token):
    status, obj = _http(
        "POST", "https://platform.claude.com/v1/oauth/token",
        headers={"Content-Type": "application/json"},
        body={"grant_type": "refresh_token",
              "refresh_token": refresh_token,
              "client_id": CLAUDE_OAUTH_CLIENT_ID},
    )
    if status != 200 or not isinstance(obj, dict):
        return None
    if not obj.get("access_token") or not obj.get("refresh_token"):
        return None
    expires_in = obj.get("expires_in") or 28800
    return {
        "access_token": obj["access_token"],
        "refresh_token": obj["refresh_token"],
        "expires_at": int((time.time() + expires_in) * 1000),
    }


def _probe_claude(token, plan):
    """Single usage-endpoint probe. Returns ('ok', usage) or ('err', reason)."""
    status, obj = _http(
        "GET", "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_CODE_USER_AGENT,
        },
    )
    if status == 401:
        return "unauthorized", "unauthorized"
    if status == 403:
        return "scope", "re-login: claude /login"
    if status == 429:
        return "err", "rate limited"
    if status != 200 or not isinstance(obj, dict):
        return "err", f"http {status}"
    if isinstance(obj.get("error"), dict) and obj["error"].get("type") == "rate_limit_error":
        return "err", "rate limited"

    def win(key):
        d = obj.get(key) or {}
        raw = d.get("utilization", d.get("used_percent", 0)) or 0
        return _window(raw, _parse_reset(d.get("resets_at")))

    return "ok", {"plan": plan, "five_hour": win("five_hour"), "weekly": win("seven_day")}


def fetch_claude():
    last_error = "auth required — run claude"
    creds = _read_claude_creds()
    plan = (creds["oauth"].get("subscriptionType") if creds else None)

    # 1) env token (set by Claude Desktop for child procs; always fresh)
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        kind, val = _probe_claude(env_token, plan)
        if kind == "ok":
            return val
        if kind == "scope":
            last_error = val  # env scope-insufficient does NOT short-circuit
        elif kind == "err":
            last_error = val

    if creds:
        oauth = creds["oauth"]
        # 2) stored access token
        kind, val = _probe_claude(oauth["accessToken"], plan)
        if kind == "ok":
            return val
        if kind == "scope":
            return {"error": val}  # refresh can't fix a missing scope
        if kind == "err":
            last_error = val

        # 3) refresh + writeback, then retry
        refreshed = _refresh_claude(oauth["refreshToken"])
        if refreshed:
            oauth = dict(oauth)
            oauth["accessToken"] = refreshed["access_token"]
            oauth["refreshToken"] = refreshed["refresh_token"]
            oauth["expiresAt"] = refreshed["expires_at"]
            _write_claude_creds(creds["account"], oauth)
            kind, val = _probe_claude(refreshed["access_token"], plan)
            if kind == "ok":
                return val
            if kind == "scope":
                return {"error": val}
            if kind == "err":
                last_error = val

    return {"error": last_error}


# --------------------------------------------------------------------------- #
# GLM & DeepSeek — shared API-key discovery
#
# Users typically reach these providers by routing Claude Code or Codex
# through them, so the key is already on disk. Discovery order per provider:
#   0. CCISLAND_<NAME> env / --glm-key / --deepseek-key CLI override
#   1. provider's usual env names
#   2. ~/.claude/settings.json "env" block (ANTHROPIC_BASE_URL points at the
#      provider -> take ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY)
#   3. ~/.codex/config.toml [model_providers.*] with a matching base_url
#      (experimental_bearer_token, or env_key -> that env var)
# --------------------------------------------------------------------------- #
GLM_MONITOR_USAGE = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"   # mainland
GLM_MONITOR_USAGE_INTL = "https://api.z.ai/api/monitor/usage/quota/limit"      # z.ai
DS_BALANCE_URL = "https://api.deepseek.com/user/balance"

# Dedicated per-user config: keys live here instead of env vars or the CLI
# tools' config files. Missing file is fine; only non-empty fields apply.
APP_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".cc-island", "config.json")

_KEY_MATCHERS = {
    "glm": {
        "config_field": "glm_key",
        "envs": ("CCISLAND_GLM_KEY", "GLM_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY"),
        "urls": ("bigmodel.cn", "z.ai"),
        "anthropic_vars": ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    },
    "deepseek": {
        "config_field": "deepseek_key",
        "envs": ("CCISLAND_DEEPSEEK_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY"),
        "urls": ("deepseek.com",),
        "anthropic_vars": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    },
}

_GLM_CREDS = None   # cached (key, base_hint)
_DS_CREDS = None


def _load_claude_settings_env():
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("env") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_codex_providers():
    path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f).get("model_providers") or {}
    except Exception:  # noqa: BLE001 — missing file, <3.11, or bad TOML all disable this source
        return {}


_APP_CONFIG = None


def _load_app_config():
    """Cached read of ~/.cc-island/config.json (the dedicated key store)."""
    global _APP_CONFIG
    if _APP_CONFIG is None:
        try:
            with open(APP_CONFIG_FILE, encoding="utf-8") as f:
                _APP_CONFIG = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            _APP_CONFIG = {}
    return _APP_CONFIG


def _discover_key(matcher):
    """Return (key, base_hint) or (None, None). See module docstring for order."""
    # 1) the dedicated config store (~/.cc-island/config.json)
    val = _load_app_config().get(matcher["config_field"])
    if val:
        return val, None

    # 2) provider env vars (incl. the CCISLAND_* CLI-override names)
    for env in matcher["envs"]:
        val = os.environ.get(env)
        if val:
            return val, None

    env_block = _load_claude_settings_env()
    base = env_block.get("ANTHROPIC_BASE_URL", "") or ""
    if any(u in base for u in matcher["urls"]):
        for var in matcher["anthropic_vars"]:
            if env_block.get(var):
                return env_block[var], base

    for prov in _load_codex_providers().values():
        base = prov.get("base_url", "") or ""
        if any(u in base for u in matcher["urls"]):
            key = prov.get("experimental_bearer_token")
            if not key and prov.get("env_key"):
                key = os.environ.get(prov["env_key"])
            if key:
                return key, base
    return None, None


def _glm_creds():
    global _GLM_CREDS
    if _GLM_CREDS is None:
        _GLM_CREDS = _discover_key(_KEY_MATCHERS["glm"])
    return _GLM_CREDS


def _ds_creds():
    global _DS_CREDS
    if _DS_CREDS is None:
        _DS_CREDS = _discover_key(_KEY_MATCHERS["deepseek"])
    return _DS_CREDS


# --------------------------------------------------------------------------- #
# GLM usage (community endpoint — response shape documented in docs §4.1;
# the three assumptions to re-verify on first live run are marked below)
# --------------------------------------------------------------------------- #
def fetch_glm():
    key, base = _glm_creds()
    if not key:
        return {"error": "not configured"}

    url = _GLM_ENDPOINT or (
        GLM_MONITOR_USAGE_INTL if base and "z.ai" in base else GLM_MONITOR_USAGE
    )
    # Community scripts send the bare key; fall back to Bearer on 401.
    status, obj = _http("GET", url, headers={"Authorization": key})
    if status == 401:
        status, obj = _http("GET", url, headers={"Authorization": f"Bearer {key}"})
    if status == 401:
        return {"error": "invalid glm key"}
    if status != 200 or not isinstance(obj, dict):
        return {"error": f"http {status}"}
    if not obj.get("success"):
        return {"error": str(obj.get("msg") or "glm query failed")}

    d = obj.get("data") or {}
    limits = d.get("limits") or []
    tok = [x for x in limits if x.get("type") == "TOKENS_LIMIT"]
    # ASSUMPTION (verify live): entry 0 = 5h window, entry 1 = weekly. If
    # nextResetTime is present on both, the earlier reset must be the 5h one.
    if len(tok) >= 2 and tok[0].get("nextResetTime") and tok[1].get("nextResetTime"):
        r0 = _parse_reset(tok[0]["nextResetTime"])
        r1 = _parse_reset(tok[1]["nextResetTime"])
        if r0 and r1 and r1 < r0:
            tok = [tok[1], tok[0]]

    def win(x):
        if not x:
            return _window(0, None)
        return _window(x.get("percentage", 0) or 0,
                       _parse_reset(x.get("nextResetTime")))

    return {
        "plan": d.get("level"),
        "five_hour": win(tok[0] if tok else None),
        "weekly": win(tok[1] if len(tok) > 1 else None),
    }


# --------------------------------------------------------------------------- #
# DeepSeek balance (official endpoint, docs §5.1)
# --------------------------------------------------------------------------- #
def fetch_deepseek():
    key, _ = _ds_creds()
    if not key:
        return {"error": "not configured"}

    status, obj = _http("GET", _DS_ENDPOINT or DS_BALANCE_URL,
                        headers={"Authorization": f"Bearer {key}"})
    if status == 401:
        return {"error": "invalid deepseek key"}
    if status != 200 or not isinstance(obj, dict) or "balance_infos" not in obj:
        return {"error": f"http {status}"}
    infos = obj.get("balance_infos") or []
    if not infos:
        return {"error": "no balance info"}

    def fnum(v):
        try:
            return float(v)  # API amounts are strings
        except (TypeError, ValueError):
            return 0.0

    # Multi-currency accounts list one entry per currency; CNY first.
    infos = sorted(infos, key=lambda x: 0 if x.get("currency") == "CNY" else 1)
    main = infos[0]
    return {
        "currency": main.get("currency", "CNY"),
        "balance": fnum(main.get("total_balance")),
    }


# --------------------------------------------------------------------------- #
# Combine + render
# --------------------------------------------------------------------------- #
def collect():
    return {
        "ts": int(time.time()),
        "claude": fetch_claude(),
        "chatgpt": fetch_chatgpt(),
        "glm": fetch_glm(),
        "deepseek": fetch_deepseek(),
    }


_TITLES = {"claude": "Claude Code", "chatgpt": "ChatGPT", "glm": "GLM", "deepseek": "DeepSeek"}
_ORDER = ("claude", "chatgpt", "glm", "deepseek")


def _fmt_window(w):
    if not w:
        return "—"
    pct = w["pct"]
    if w["reset_at"]:
        mins = max(0, int((w["reset_at"] - time.time()) / 60))
        reset = f"resets in {mins // 60}h{mins % 60:02d}m"
    else:
        reset = "reset ?"
    bar_n = int(round(pct / 5))
    bar = "█" * bar_n + "░" * (20 - bar_n)
    return f"{pct:5.1f}%  {bar}  {reset}"


def _fmt_balance(p):
    sym = "¥" if p.get("currency", "CNY") == "CNY" else "$"
    return f"bal {sym}{p.get('balance', 0):.2f}"


def render(data):
    lines = []
    for name in _ORDER:
        p = data.get(name) or {}
        title = _TITLES[name]
        if "error" in p:
            lines.append(f"{title:12} ⚠ {p['error']}")
            continue
        if name == "deepseek":
            lines.append(f"{title:12} {_fmt_balance(p)}")
            continue
        plan = f" [{p['plan']}]" if p.get("plan") else ""
        lines.append(f"{title:12}{plan}")
        lines.append(f"   5h   {_fmt_window(p.get('five_hour'))}")
        lines.append(f"   7d   {_fmt_window(p.get('weekly'))}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# BLE push — send the compact payload to the StopWatch over Nordic UART Service
# --------------------------------------------------------------------------- #
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # host -> watch (usage JSON)
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # watch -> host (refresh request)
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_DEVICE_NAME = "CC Island"
MANUAL_REFRESH_MIN_GAP = 5  # seconds — throttle button-triggered refreshes
SCAN_TIMEOUT_S = 20
RECONNECT_DELAY_S = 3
MAX_STALE_PROVIDER_S = 6 * 60 * 60

# Window providers use {five_hour, weekly}; DeepSeek is the balance-row provider.
_ALL_PROVIDERS = ("claude", "chatgpt", "glm", "deepseek")


def _provider_ok(name, p):
    """Is this provider's reading good enough to cache as last-good?"""
    if "error" in p:
        return False
    if name == "deepseek":
        return "balance" in p
    return bool(p.get("five_hour") and p.get("weekly"))


def _win_pct(w):
    # Payload boundary: clamp defensively, the watch bar assumes 0..100.
    return int(round(min(100.0, max(0.0, w["pct"])))) if w else 0


def _reset_min(w):
    if not w or not w.get("reset_at"):
        return 0
    reset_at = w["reset_at"]
    if reset_at > 10**12:      # defensive: ms leaked past _parse_reset
        reset_at //= 1000
    return max(0, int((reset_at - time.time()) / 60))


def compact(data):
    """Short-key one-line JSON for the watch (payload v3).

    Window rows: {h,d,r}. The balance row: {cur,bal}. Providers that are
    unconfigured or failing with no cached fallback are omitted entirely, so
    the watch shows its "--" state.
    """
    def prov(p):
        return {
            "h": _win_pct(p.get("five_hour")),
            "d": _win_pct(p.get("weekly")),
            "r": _reset_min(p.get("five_hour")),
        }

    out = {"c": prov(data["claude"]), "x": prov(data["chatgpt"])}

    glm = data.get("glm") or {}
    if "error" not in glm:
        out["g"] = prov(glm)

    ds = data.get("deepseek") or {}
    if "error" not in ds:
        out["ds"] = {
            "cur": ds.get("currency", "CNY"),
            "bal": round(ds.get("balance", 0), 2),
        }

    return json.dumps(out, separators=(",", ":"))


async def ble_loop(interval_s):
    import asyncio
    from bleak import BleakClient, BleakScanner

    refresh = asyncio.Event()   # set when the watch's button asks for a refresh
    disconnected = asyncio.Event()
    last_push = [0.0]
    last_good = {}

    def remember_good(data):
        now = time.time()
        for name in _ALL_PROVIDERS:
            provider = data.get(name) or {}
            if _provider_ok(name, provider):
                cached = dict(provider)
                cached["_cached_at"] = now
                last_good[name] = cached

    def with_cached_windows(data):
        now = time.time()
        merged = dict(data)
        for name in _ALL_PROVIDERS:
            provider = dict(data.get(name) or {})
            cached = last_good.get(name)
            if "error" in provider and cached and now - cached.get("_cached_at", 0) <= MAX_STALE_PROVIDER_S:
                restored = {k: v for k, v in cached.items() if not k.startswith("_")}
                restored["stale"] = True
                merged[name] = restored
            else:
                merged[name] = provider
        return merged

    async def find_watch():
        target_uuid = NUS_SERVICE_UUID.lower()

        def match(device, adv):
            name = device.name or adv.local_name or ""
            service_uuids = [u.lower() for u in (adv.service_uuids or [])]
            return name == BLE_DEVICE_NAME or target_uuid in service_uuids

        dev = await BleakScanner.find_device_by_filter(match, timeout=SCAN_TIMEOUT_S)
        if dev:
            return dev

        # Diagnostic fallback: list visible named devices without failing the loop.
        try:
            seen = await BleakScanner.discover(timeout=5, return_adv=True)
            names = []
            for _, (device, adv) in seen.items():
                name = device.name or adv.local_name
                if name:
                    names.append(name)
            if names:
                _log("  visible BLE names: " + ", ".join(sorted(set(names))[:12]))
        except Exception as e:  # noqa: BLE001
            _log("  scan diagnostic failed: " + repr(e))
        return None

    async def connect_watch(dev):
        disconnected.clear()
        client = BleakClient(
            dev,
            disconnected_callback=lambda _client: disconnected.set(),
            services=[NUS_SERVICE_UUID],
            timeout=20,
        )
        await client.connect()
        _log(f"connected to {dev.address}")
        try:
            # Windows can return an incomplete GATT table right after the watch
            # (re)boots; force discovery and fail fast so the outer loop retries.
            nus = next((s for s in client.services
                        if s.uuid.lower().startswith(NUS_SERVICE_UUID[:8])), None)
            if nus is None or not nus.characteristics:
                raise RuntimeError("NUS service not discovered yet (transient)")
            await client.start_notify(NUS_TX_UUID, lambda _h, _d: refresh.set())
        except Exception as e:  # noqa: BLE001
            _log("  (button refresh unavailable: " + repr(e) + ")")
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        return client

    async def push(client, tag):
        data = collect()
        remember_good(data)
        payload = compact(with_cached_windows(data))
        try:
            await client.write_gatt_char(NUS_RX_UUID, (payload + "\n").encode(), response=True)
        except Exception:
            await asyncio.sleep(0.5)
            await client.write_gatt_char(NUS_RX_UUID, (payload + "\n").encode(), response=False)
        last_push[0] = time.time()
        _log(f"pushed ({tag}): {payload}")

    client = None
    while True:
        try:
            if client is None or not client.is_connected:
                _log(f"scanning for '{BLE_DEVICE_NAME}'...")
                dev = await find_watch()
                if not dev:
                    _log("  not found — is the CC Island app open on the watch? retrying")
                    await asyncio.sleep(5)
                    continue
                client = await connect_watch(dev)
                await push(client, "connect")

            # Wake on either the periodic timer or a button-triggered refresh.
            try:
                refresh_task = asyncio.create_task(refresh.wait())
                disconnect_task = asyncio.create_task(disconnected.wait())
                done, pending = await asyncio.wait(
                    {refresh_task, disconnect_task},
                    timeout=interval_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if disconnect_task in done:
                    _log("disconnected")
                    client = None
                    refresh.clear()
                    continue
                if refresh_task in done:
                    refresh.clear()
                    if time.time() - last_push[0] >= MANUAL_REFRESH_MIN_GAP:
                        await push(client, "button")
                    else:
                        _log("  refresh throttled (too soon)")
                else:
                    await push(client, "auto")
            except asyncio.TimeoutError:
                await push(client, "auto")
        except Exception as e:  # noqa: BLE001 — keep the loop alive across BLE hiccups
            _log("ble error: " + repr(e))
            try:
                if client:
                    await client.disconnect()
            except Exception:
                pass
            client = None
            refresh.clear()
            disconnected.clear()
            await asyncio.sleep(RECONNECT_DELAY_S)


def main():
    _force_utf8_stdio()

    ap = argparse.ArgumentParser(
        prog="codexisland_bridge",
        description="CC Island bridge — Claude/ChatGPT/GLM/DeepSeek usage & balance on the M5 StopWatch over BLE",
    )
    ap.add_argument("--ble", nargs="?", const=5.0, default=None, type=float, metavar="MIN",
                    help="push to the watch over BLE every MIN minutes (default: 5)")
    ap.add_argument("--json", action="store_true",
                    help="print the full collected JSON instead of a table")
    ap.add_argument("--log-file", metavar="PATH",
                    help="also append log output to PATH (rotated at 1 MB)")
    ap.add_argument("--glm-key", help="explicit GLM API key (skips key discovery)")
    ap.add_argument("--deepseek-key", help="explicit DeepSeek API key (skips key discovery)")
    ap.add_argument("--glm-endpoint", help="override the GLM usage endpoint URL")
    ap.add_argument("--ds-endpoint", help="override the DeepSeek balance endpoint URL")
    args = ap.parse_args()

    global _GLM_ENDPOINT, _DS_ENDPOINT
    if args.glm_endpoint:
        _GLM_ENDPOINT = args.glm_endpoint
    if args.ds_endpoint:
        _DS_ENDPOINT = args.ds_endpoint
    _GLM_ENDPOINT = _GLM_ENDPOINT or _load_app_config().get("glm_endpoint") or None
    _DS_ENDPOINT = _DS_ENDPOINT or _load_app_config().get("ds_endpoint") or None
    if args.glm_key:
        os.environ["CCISLAND_GLM_KEY"] = args.glm_key
    if args.deepseek_key:
        os.environ["CCISLAND_DEEPSEEK_KEY"] = args.deepseek_key
    if args.log_file:
        set_log_file(args.log_file)

    if args.ble:
        import asyncio
        _log(f"BLE push every {args.ble:g} min (Ctrl-C to stop)")
        asyncio.run(ble_loop(int(args.ble * 60)))
        return

    data = collect()
    if args.json:
        _log(json.dumps(data, indent=2))
    else:
        _log(render(data))


if __name__ == "__main__":
    main()
