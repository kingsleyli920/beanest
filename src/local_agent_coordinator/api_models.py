"""Pay-per-use API model partners: a host coding CLI pointed at a model endpoint.

An API model partner is the user's own endpoint, key and prices run through a
host CLI Beanest already drives (Claude Code against an Anthropic-compatible
endpoint today). Beanest stores only where the key lives, never the key: it is
read when a call starts and handed to that one child process.

Vendor names appear only in PRESETS. Dispatch, accounting and caps read the
saved configuration and never branch on who the vendor is.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import workload
from .quota_gate import RESERVE_PERCENT


# How each host CLI is pointed at an endpoint. A future OpenAI-compatible host
# (Codex with a custom model provider) adds one row here and its own launch.
HOSTS = {
    "claude": {"protocol": "anthropic", "base_url_variable": "ANTHROPIC_BASE_URL",
               "token_variable": "ANTHROPIC_AUTH_TOKEN"},
}
KEY_KINDS = ("env_file", "keychain")
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
CAP_FIELDS = ("daily", "monthly")
# RESERVE_PERCENT is the subscription reserve's line: with this much of a cap
# left, the partner gets work only when nobody else can take it.
MAX_MODELS = 16
MAX_WINDOWS = 8
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_KEY_BYTES = 4096
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,199}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")


class ApiModelError(ValueError):
    """Stable code; never carries a key, a key file's content or a path."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Editable starting points. Prices are per 1M tokens as published by the
# vendor when this preset was written; the user's saved numbers are what count.
PRESETS = (
    {"id": "deepseek", "name": "DeepSeek", "host": "claude",
     "base_url": "https://api.deepseek.com/anthropic",
     "docs_url": "https://api-docs.deepseek.com/quick_start/pricing",
     "key": {"kind": "env_file", "path": "", "variable": "DEEPSEEK_API_KEY"},
     "currency": "USD", "timezone": "UTC",
     # Peak hours on weekdays; every other hour is off-peak at half price.
     "peak_windows": [{"start": "01:00", "end": "04:00", "days": [0, 1, 2, 3, 4]},
                      {"start": "06:00", "end": "10:00", "days": [0, 1, 2, 3, 4]}],
     "default_model": "deepseek-v4-pro",
     "models": [
         {"id": "deepseek-v4-pro", "input": 0.66, "output": 1.98, "cache_read": 0.022, "cache_write": 0.66,
          "peak": {"input": 1.32, "output": 3.96, "cache_read": 0.044, "cache_write": 1.32}},
         {"id": "deepseek-flash", "input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": 0.15,
          "peak": {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.3}},
     ],
     "caps": {"daily": 5.0, "monthly": 50.0}},
    {"id": "custom_anthropic", "name": "自定义 Anthropic 兼容接口", "host": "claude",
     "base_url": "", "docs_url": None,
     "key": {"kind": "env_file", "path": "", "variable": ""},
     "currency": "USD", "timezone": "UTC", "peak_windows": [], "default_model": "",
     "models": [], "caps": {"daily": None, "monthly": None}},
)


def presets() -> list[dict]:
    return json.loads(json.dumps(PRESETS))


def base_url(value) -> str:
    """https only, a host, no credentials, query, fragment, trailing slash or /v1.

    The host CLI appends its own API path (/v1/messages), so a base URL that
    already ends in a slash or /v1 sends every request to the wrong place.
    """
    if not isinstance(value, str) or not 8 < len(value) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ApiModelError("invalid_api_base_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ApiModelError("invalid_api_base_url") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value
            or value.endswith("/") or re.search(r"/v1\Z", parsed.path, re.IGNORECASE)
            or port is not None and not 0 < port < 65536):
        raise ApiModelError("invalid_api_base_url")
    return value


def key_reference(value) -> dict:
    """Where the key lives. Holding the key itself here is refused outright."""
    if not isinstance(value, dict) or value.get("kind") not in KEY_KINDS:
        raise ApiModelError("invalid_api_key_reference")
    if value["kind"] == "env_file":
        if set(value) != {"kind", "path", "variable"}:
            raise ApiModelError("invalid_api_key_reference")
        path, variable = value["path"], value["variable"]
        if (not isinstance(path, str) or not path.startswith("/") or len(path) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or not isinstance(variable, str) or not _VARIABLE.fullmatch(variable)):
            raise ApiModelError("invalid_api_key_reference")
        return {"kind": "env_file", "path": path, "variable": variable}
    if set(value) - {"kind", "service", "account"} or "service" not in value:
        raise ApiModelError("invalid_api_key_reference")
    service, account = value["service"], value.get("account")
    for text in (service, account) if account is not None else (service,):
        if not isinstance(text, str) or not _LABEL.fullmatch(text) or text != text.strip():
            raise ApiModelError("invalid_api_key_reference")
    return {"kind": "keychain", "service": service, "account": account}


def _price(value, *, required=True):
    if value is None and not required:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1_000_000:
        raise ApiModelError("invalid_api_prices")
    return float(value)


def _prices(value):
    if not isinstance(value, dict) or set(value) != set(PRICE_FIELDS):
        raise ApiModelError("invalid_api_prices")
    return {field: _price(value[field]) for field in PRICE_FIELDS}


def _model(value):
    if not isinstance(value, dict) or set(value) - {*PRICE_FIELDS, "id", "peak"} or not {*PRICE_FIELDS, "id"} <= set(value):
        raise ApiModelError("invalid_api_models")
    if not isinstance(value["id"], str) or not _MODEL.fullmatch(value["id"]):
        raise ApiModelError("invalid_api_models")
    result = {"id": value["id"], **_prices({field: value[field] for field in PRICE_FIELDS})}
    if value.get("peak") is not None:
        result["peak"] = _prices(value["peak"])
    return result


def _clock_minutes(text):
    match = _TIME.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ApiModelError("invalid_api_peak_windows")
    return int(match[1]) * 60 + int(match[2])


def _window(value):
    if not isinstance(value, dict) or set(value) - {"start", "end", "days"} or not {"start", "end"} <= set(value):
        raise ApiModelError("invalid_api_peak_windows")
    if _clock_minutes(value["start"]) == _clock_minutes(value["end"]):
        raise ApiModelError("invalid_api_peak_windows")
    days = value.get("days", list(range(7)))
    if (not isinstance(days, list) or not days or len(set(days)) != len(days)
            or any(type(day) is not int or not 0 <= day <= 6 for day in days)):
        raise ApiModelError("invalid_api_peak_windows")
    return {"start": value["start"], "end": value["end"], "days": sorted(days)}


def _cap(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1_000_000_000:
        raise ApiModelError("invalid_api_caps")
    return float(value)


def config(value) -> dict:
    """The saved, canonical form of one API model partner; it never holds a key."""
    fields = {"host", "base_url", "key", "currency", "timezone", "peak_windows", "default_model", "models", "caps"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ApiModelError("invalid_api_model")
    if value["host"] not in HOSTS:
        raise ApiModelError("unsupported_api_host")
    if not isinstance(value["currency"], str) or not _CURRENCY.fullmatch(value["currency"]):
        raise ApiModelError("invalid_api_prices")
    timezone(value["timezone"])
    models = value["models"]
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise ApiModelError("invalid_api_models")
    models = [_model(item) for item in models]
    if len({item["id"] for item in models}) != len(models) or value["default_model"] not in {item["id"] for item in models}:
        raise ApiModelError("invalid_api_models")
    windows = value["peak_windows"]
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ApiModelError("invalid_api_peak_windows")
    caps = value["caps"]
    if not isinstance(caps, dict) or set(caps) != set(CAP_FIELDS):
        raise ApiModelError("invalid_api_caps")
    return {"host": value["host"], "base_url": base_url(value["base_url"]), "key": key_reference(value["key"]),
            "currency": value["currency"], "timezone": value["timezone"],
            "peak_windows": [_window(item) for item in windows], "default_model": value["default_model"],
            "models": models, "caps": {field: _cap(caps[field]) for field in CAP_FIELDS}}


def timezone(name):
    if not isinstance(name, str) or not 0 < len(name) <= 64 or not re.fullmatch(r"[A-Za-z0-9_+/-]+", name):
        raise ApiModelError("invalid_api_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ApiModelError("invalid_api_timezone") from None


# --- the key, read only when a call or an explicit check needs it ---------

def _clean_key(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8", "replace")) > MAX_KEY_BYTES
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ApiModelError("api_key_unavailable")
    return value


def _env_file_value(path, variable):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        raise ApiModelError("api_key_unavailable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENV_FILE_BYTES:
            raise ApiModelError("api_key_unavailable")
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="strict") as stream:
            descriptor = -1
            text = stream.read(MAX_ENV_FILE_BYTES + 1)
    except (OSError, UnicodeError):
        raise ApiModelError("api_key_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, raw = line.partition("=")
        if not separator or name.strip() != variable:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        else:
            raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        found = raw  # the last assignment wins, as a shell would read it
    return _clean_key(found)


def _keychain_value(service, account, timeout=10):
    command = ["/usr/bin/security", "find-generic-password", "-s", service]
    if account is not None:
        command += ["-a", account]
    command.append("-w")
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                   check=False, env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")})
    except (OSError, subprocess.TimeoutExpired):
        raise ApiModelError("api_key_unavailable") from None
    if completed.returncode != 0 or len(completed.stdout) > MAX_KEY_BYTES + 2:
        raise ApiModelError("api_key_unavailable")
    try:
        return _clean_key(completed.stdout.decode("utf-8").rstrip("\n"))
    except UnicodeError:
        raise ApiModelError("api_key_unavailable") from None


def read_key(reference) -> str:
    """The key value, for one child's environment; never stored or logged."""
    reference = key_reference(reference)
    if reference["kind"] == "env_file":
        return _env_file_value(reference["path"], reference["variable"])
    return _keychain_value(reference["service"], reference["account"])


# --- prices and spend ------------------------------------------------------

def _in_window(window, moment):
    minute = moment.hour * 60 + moment.minute
    start, end = _clock_minutes(window["start"]), _clock_minutes(window["end"])
    if start < end:
        return start <= minute < end and moment.weekday() in window["days"]
    # Wraps past midnight: the early hours belong to the day the window began.
    if minute >= start:
        return moment.weekday() in window["days"]
    return minute < end and (moment - timedelta(days=1)).weekday() in window["days"]


def price_for(settings, model_id, at) -> tuple[dict, bool]:
    """(per-1M prices, peak?) for this model at this moment; unknown models cost their default's price."""
    models = {item["id"]: item for item in settings["models"]}
    model = models.get(model_id) or models[settings["default_model"]]
    moment = datetime.fromtimestamp(at, timezone(settings["timezone"]))
    peak = "peak" in model and any(_in_window(window, moment) for window in settings["peak_windows"])
    return (model["peak"] if peak else {field: model[field] for field in PRICE_FIELDS}), peak


def call_cost(settings, model_id, usage, at, *, previous=None, resumed=False):
    """What one call cost in the partner's currency, or None when usage cannot tell."""
    counts = workload.token_counts(settings["host"], usage, previous=previous, resumed=resumed)
    if counts is None:
        return None
    prices, _ = price_for(settings, model_id, at)
    return (counts["input"] * prices["input"] + counts["output"] * prices["output"]
            + counts["cache_read"] * prices["cache_read"] + counts["cache_write"] * prices["cache_write"]) / 1_000_000


def period_keys(settings, at):
    moment = datetime.fromtimestamp(at, timezone(settings["timezone"]))
    return moment.strftime("%Y-%m-%d"), moment.strftime("%Y-%m")


_LEDGER_ROW = {"day", "day_spent", "month", "month_spent", "month_calls", "month_unpriced_calls", "currency"}


class SpendLedger:
    """Day and month spend per partner, in one small private file.

    A new day or month starts from zero the first time it is read or written;
    nothing has to run at midnight.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "api-spend.json"

    def _load(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ApiModelError("api_spend_state_corrupt")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                data = json.load(stream)
        except (OSError, ValueError, UnicodeError):
            raise ApiModelError("api_spend_state_corrupt") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("partners"), dict):
            raise ApiModelError("api_spend_state_corrupt")
        for row in data["partners"].values():
            if (not isinstance(row, dict) or set(row) != _LEDGER_ROW
                    or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0
                           for key in ("day_spent", "month_spent"))
                    or any(type(row[key]) is not int or row[key] < 0 for key in ("month_calls", "month_unpriced_calls"))):
                raise ApiModelError("api_spend_state_corrupt")
        return data["partners"]

    def _save(self, partners):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".api-spend-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "partners": partners}, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _current(row, settings, at):
        day, month = period_keys(settings, at)
        row = dict(row or {"day": day, "day_spent": 0.0, "month": month, "month_spent": 0.0,
                           "month_calls": 0, "month_unpriced_calls": 0, "currency": settings["currency"]})
        if row["month"] != month:
            row.update(month=month, month_spent=0.0, month_calls=0, month_unpriced_calls=0)
        if row["day"] != day:
            row.update(day=day, day_spent=0.0)
        return row

    def record(self, partner_id, settings, cost, at):
        """Add one finished call; a call whose cost is unknown still counts as a call."""
        partners = self._load()
        row = self._current(partners.get(partner_id), settings, at)
        row["month_calls"] += 1
        if cost is None:
            row["month_unpriced_calls"] += 1
        else:
            row["day_spent"] += cost
            row["month_spent"] += cost
        partners[partner_id] = row
        self._save(partners)
        return row

    def snapshot(self, partner_id, settings, at) -> dict:
        row = self._current(self._load().get(partner_id), settings, at)
        caps = settings["caps"]
        spent = {"daily": row["day_spent"], "monthly": row["month_spent"]}
        capped = [name for name in CAP_FIELDS if caps[name] is not None and spent[name] >= caps[name]]
        lefts = [max(0.0, (caps[name] - spent[name]) * 100 / caps[name]) for name in CAP_FIELDS if caps[name] is not None]
        # Rounded before comparing, so 0.3 of 2.0 left reads as the 15% it is.
        left = round(min(lefts), 6) if lefts else None
        return {"currency": row["currency"], "day": row["day"], "day_spent": round(row["day_spent"], 6),
                "month": row["month"], "month_spent": round(row["month_spent"], 6),
                "month_calls": row["month_calls"], "month_unpriced_calls": row["month_unpriced_calls"],
                "caps": dict(caps), "capped": capped,
                "left_percent": None if left is None else round(left, 1),
                "state": "capped" if capped else "near_cap" if left is not None and left <= RESERVE_PERCENT else "available"}
