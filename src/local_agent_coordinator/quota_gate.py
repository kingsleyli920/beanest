"""Known subscription limits, bound to a confirmed login without storing email.

This journal is separate from user enable/disable preferences. An unknown
balance is usable but never advertised as a positive quota observation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile


STALE_SECONDS = 90
_ADAPTERS = {"codex", "claude"}


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 32503680000


class QuotaGate:
    def __init__(self, root, clock):
        self.root, self.clock = Path(root), clock
        self.path = self.root / "quota-gates.json"
        self.salt, self.blocks = secrets.token_hex(32), {}
        self.unbound = {}
        self.rechecks = {}
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("quota_state_corrupt")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = None
                data = json.load(stream)
            if (not isinstance(data, dict) or set(data) not in ({"version", "salt", "blocks"}, {"version", "salt", "blocks", "rechecks"}) or data["version"] != 1
                    or not isinstance(data["salt"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["salt"])
                    or not isinstance(data["blocks"], dict) or set(data["blocks"]) - _ADAPTERS):
                raise ValueError("quota_state_corrupt")
            for row in data["blocks"].values():
                if (not isinstance(row, dict) or set(row) != {"identity", "reason", "source", "observed_at", "resets_at"}
                        or not isinstance(row["identity"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["identity"])
                        or row["reason"] not in {"quota_exhausted", "subscription_limit_reached"}
                        or row["source"] not in {"codex_shared_pool", "claude_shared_pool", "claude_cli_limit", "codex_cli_limit"}
                        or not _timestamp(row["observed_at"])
                        or row["resets_at"] is not None and not _timestamp(row["resets_at"])):
                    raise ValueError("quota_state_corrupt")
            self.salt, self.blocks = data["salt"], data["blocks"]
            rechecks = data.get("rechecks", {})
            if not isinstance(rechecks, dict) or set(rechecks) - _ADAPTERS:
                raise ValueError("quota_state_corrupt")
            for row in rechecks.values():
                if (not isinstance(row, dict) or set(row) != {"identity", "observed_at"}
                        or not isinstance(row["identity"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["identity"])
                        or not _timestamp(row["observed_at"])):
                    raise ValueError("quota_state_corrupt")
            self.rechecks = rechecks
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _save(self):
        descriptor, name = tempfile.mkstemp(prefix=".quota-gates-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "salt": self.salt, "blocks": self.blocks, "rechecks": self.rechecks}, stream,
                          separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def identity(self, account):
        if (not isinstance(account, dict) or account.get("auth_method") not in {"chatgpt", "claude.ai"}
                or not isinstance(account.get("email"), str) or not account["email"]):
            return None
        # The private per-install salt prevents an exported fingerprint from
        # being a plain, globally comparable email hash. Never export this file.
        text = account["auth_method"] + ":" + account["email"].strip().casefold()
        return hmac.new(bytes.fromhex(self.salt), text.encode(), hashlib.sha256).hexdigest()

    def _shared_windows(self, adapter, account):
        quota = account.get("quota", {})
        source, names = {
            "codex": ("codex_app_server_account_rate_limits", {"codex:primary", "codex:secondary"}),
            "claude": ("claude_sdk_get_usage", {"claude:five_hour", "claude:seven_day"}),
        }.get(adapter, (None, set()))
        if (quota.get("state") != "available" or quota.get("stale") is not False
                or quota.get("source") != source or source is None):
            return []
        now = self.clock()
        # Claude's is_active picks its single-value headline; it does not
        # decide whether an account-wide meter applies.
        return [row for row in quota.get("windows", []) if isinstance(row, dict)
                and row.get("name") in names
                and (adapter != "claude" or row.get("scope") == "provider_account")
                and type(row.get("used_percent")) in (int, float) and 0 <= row["used_percent"] <= 100
                and (row.get("resets_at") is None or _timestamp(row["resets_at"]) and row["resets_at"] > now)]

    def observe_account(self, adapter, account):
        identity = self.identity(account)
        if identity is None or account.get("stale") is not False:
            return
        previous = self.blocks.get(adapter)
        changed = previous is not None and previous["identity"] != identity
        windows = self._shared_windows(adapter, account)
        exhausted = [row for row in windows if row["used_percent"] >= 100]
        if exhausted:
            resets = [row["resets_at"] for row in exhausted if row["resets_at"] is not None]
            # All exhausted shared windows must reset, so retain the latest.
            self.blocks[adapter] = {"identity": identity, "reason": "quota_exhausted",
                "source": adapter + "_shared_pool", "observed_at": self.clock(),
                "resets_at": max(resets) if len(resets) == len(exhausted) else None}
            self._save()
        elif changed or windows:
            if previous is not None:
                self.blocks.pop(adapter, None)
                self._save()
        self.unbound.pop(adapter, None)
        recheck = self.rechecks.get(adapter)
        if recheck and (recheck["identity"] != identity or windows):
            self.rechecks.pop(adapter, None)
            self._save()

    def record_limit(self, adapter, account, *, bound_identity):
        identity = self.identity(account)
        if adapter not in _ADAPTERS or identity is None or identity != bound_identity or account.get("stale") is not False:
            self.unbound[adapter] = self.clock()
            return False
        quota = account.get("quota", {})
        shared = self._shared_windows(adapter, account)
        if any(row["used_percent"] >= 100 for row in shared):
            self.observe_account(adapter, account)
            return True
        if (adapter == "claude" and shared and all(row["used_percent"] < 100 for row in shared)
                and any(isinstance(row, dict) and row.get("scope") == "model"
                        and type(row.get("used_percent")) in (int, float) and row["used_percent"] == 100
                        for row in quota.get("windows", []))):
            # A generic native weekly error may refer to this model pool.
            # The task still fails, but that is not evidence to block every
            # other model sharing the same login.
            return False
        self.blocks[adapter] = {"identity": identity, "reason": "subscription_limit_reached",
            "source": adapter + "_cli_limit", "observed_at": self.clock(), "resets_at": None}
        self.rechecks.pop(adapter, None)
        self._save()
        return True

    def request_recheck(self, adapter, account):
        if not self.snapshot(adapter, account)["can_request_recheck"]:
            raise ValueError("availability_recheck_unavailable")
        self.blocks.pop(adapter)
        self.rechecks[adapter] = {"identity": self.identity(account), "observed_at": self.clock()}
        self._save()
        return {"kind": "quota_recheck_requested", "adapter": adapter, "observed_at": self.clock()}

    def snapshot(self, adapter, account):
        base = {"state": "unknown", "selectable": True, "reason": "quota_unknown",
                "source": account.get("source"), "observed_at": account.get("checked_at"),
                "identity_checked_at": account.get("checked_at"),
                "resets_at": None, "stale": account.get("stale", True), "scope": "default_cli_login",
                "can_request_recheck": False}
        if adapter not in _ADAPTERS:
            # Kimi exposes read-only usage, but not the stable account identity
            # needed to persist or clear an account-bound dispatch block.
            return base if adapter == "kimi" else {**base, "reason": "adapter_unsupported"}
        identity, block = self.identity(account), self.blocks.get(adapter)
        if block and identity == block["identity"]:
            expired = block["resets_at"] is not None and block["resets_at"] <= self.clock()
            return {**base, **{key: block[key] for key in ("reason", "source", "observed_at", "resets_at")},
                    "state": "unknown" if expired else "blocked", "selectable": expired,
                    "reason": "quota_reset_due" if expired else block["reason"],
                    "stale": base["stale"] or self.clock() - block["observed_at"] > STALE_SECONDS,
                    "can_request_recheck": (adapter == "claude" and not expired and account.get("stale") is False
                                            and block["reason"] == "subscription_limit_reached")}
        if block and identity is None:
            # A failed identity read after restart cannot bypass a previously
            # confirmed limit. We are blocked pending identity, not asserting
            # that the still-unknown current login is the exhausted account.
            return {**base, "state": "blocked", "selectable": False, "reason": "account_identity_unknown",
                    "source": block["source"], "observed_at": block["observed_at"], "resets_at": block["resets_at"]}
        if adapter in self.unbound:
            return {**base, "reason": "account_identity_unknown"}
        windows = self._shared_windows(adapter, account)
        if identity and windows and all(row["used_percent"] < 100 for row in windows):
            return {**base, "state": "available", "reason": None, "source": adapter + "_shared_pool",
                    "observed_at": account["quota"].get("checked_at"), "stale": False}
        if identity and self.rechecks.get(adapter, {}).get("identity") == identity:
            return {**base, "reason": "user_recheck_requested", "source": "user_recheck_requested",
                    "observed_at": self.rechecks[adapter]["observed_at"]}
        if account.get("state") == "error":
            return {**base, "reason": "account_check_failed"}
        return base
