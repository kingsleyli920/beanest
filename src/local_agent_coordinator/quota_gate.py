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
# A provider may say the limit was hit without saying when it ends. A small
# real request then is the only way to learn it has ended.
PROBE_SECONDS = 3600
# Below this much left in any shared window, an account is kept for when
# nobody else can do the step, so its owner still has some for themselves.
RESERVE_PERCENT = 15
# A low reading stays useful for a while: usage only grows until it resets.
RESERVE_FRESH_SECONDS = 1800
_ADAPTERS = {"codex", "claude", "kimi"}


# A gate key is the account scope: the bare adapter for the shared local CLI
# login, or adapter:profile for a profile's own Beanest-created CLI home.
_SCOPE = re.compile(r"(codex|claude|kimi)(?::[a-z][a-z0-9_-]{0,31})?")


def _scoped(keys):
    return all(isinstance(key, str) and _SCOPE.fullmatch(key) for key in keys)


def scope_adapter(scope):
    return scope.split(":", 1)[0] if isinstance(scope, str) else scope


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 32503680000


class QuotaGate:
    def __init__(self, root, clock):
        self.root, self.clock = Path(root), clock
        self.path = self.root / "quota-gates.json"
        self.salt, self.blocks = secrets.token_hex(32), {}
        self.unbound = {}
        self.probed = {}
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
                    or not isinstance(data["blocks"], dict) or not _scoped(data["blocks"])):
                raise ValueError("quota_state_corrupt")
            for row in data["blocks"].values():
                if (not isinstance(row, dict) or set(row) != {"identity", "reason", "source", "observed_at", "resets_at"}
                        or not isinstance(row["identity"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["identity"])
                        or row["reason"] not in {"quota_exhausted", "subscription_limit_reached"}
                        or row["source"] not in {"codex_shared_pool", "claude_shared_pool", "kimi_shared_pool",
                                                 "claude_cli_limit", "codex_cli_limit", "kimi_cli_limit"}
                        or not _timestamp(row["observed_at"])
                        or row["resets_at"] is not None and not _timestamp(row["resets_at"])):
                    raise ValueError("quota_state_corrupt")
            self.salt, self.blocks = data["salt"], data["blocks"]
            rechecks = data.get("rechecks", {})
            if not isinstance(rechecks, dict) or not _scoped(rechecks):
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

    def fingerprint(self, text):
        # The private per-install salt prevents an exported fingerprint from
        # being a plain, globally comparable hash. Never export this file.
        return hmac.new(bytes.fromhex(self.salt), text.encode(), hashlib.sha256).hexdigest()

    def identity(self, account):
        if not isinstance(account, dict):
            return None
        if account.get("auth_method") == "kimi-code":
            # Kimi logins may have no email; the account refresh keeps only a
            # salted fingerprint of the provider's opaque user id.
            key = account.get("account_key")
            return key if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) else None
        if (account.get("auth_method") not in {"chatgpt", "claude.ai"}
                or not isinstance(account.get("email"), str) or not account["email"]):
            return None
        return self.fingerprint(account["auth_method"] + ":" + account["email"].strip().casefold())

    def _shared_windows(self, scope, account, *, max_age=None):
        adapter = scope_adapter(scope)
        quota = account.get("quota", {})
        source, names = {
            "codex": ("codex_app_server_account_rate_limits", {"codex:primary", "codex:secondary"}),
            "claude": ("claude_sdk_get_usage", {"claude:five_hour", "claude:seven_day"}),
            # Only Kimi's billing-cycle meters can lift a monthly refusal. The
            # installed CLI reports just the 5-hour window, which says nothing
            # about the month, so a recorded refusal stays until a monthly
            # meter shows room or the user asks for a recheck.
            "kimi": ("kimi_code_web_oauth_usage", {"kimi:summary", "kimi:monthTotal", "kimi:monthCode"}),
        }.get(adapter, (None, set()))
        now = self.clock()
        fresh = (quota.get("stale") is False if max_age is None
                 else _timestamp(quota.get("checked_at")) and 0 <= now - quota["checked_at"] <= max_age)
        if quota.get("state") != "available" or not fresh or quota.get("source") != source or source is None:
            return []
        # Claude's is_active picks its single-value headline; it does not
        # decide whether an account-wide meter applies.
        return [row for row in quota.get("windows", []) if isinstance(row, dict)
                and row.get("name") in names
                and (adapter != "claude" or row.get("scope") == "provider_account")
                and type(row.get("used_percent")) in (int, float) and 0 <= row["used_percent"] <= 100
                and (row.get("resets_at") is None or _timestamp(row["resets_at"]) and row["resets_at"] > now)]

    def reserve_left(self, scope, account):
        """The least left in a shared window when it is at or below the reserve, else None."""
        if not isinstance(account, dict) or self.identity(account) is None:
            return None
        left = [100 - row["used_percent"] for row in self._shared_windows(scope, account, max_age=RESERVE_FRESH_SECONDS)]
        return round(min(left)) if left and min(left) <= RESERVE_PERCENT else None

    def observe_account(self, scope, account):
        adapter, identity = scope, self.identity(account)
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
                "source": scope_adapter(adapter) + "_shared_pool", "observed_at": self.clock(),
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

    def record_limit(self, scope, account, *, bound_identity):
        adapter, identity = scope, self.identity(account)
        if scope_adapter(adapter) not in _ADAPTERS or not _scoped([adapter]) or identity is None or identity != bound_identity or account.get("stale") is not False:
            self.unbound[adapter] = self.clock()
            return False
        quota = account.get("quota", {})
        shared = self._shared_windows(adapter, account)
        if any(row["used_percent"] >= 100 for row in shared):
            self.observe_account(adapter, account)
            return True
        if (scope_adapter(adapter) == "claude" and shared and all(row["used_percent"] < 100 for row in shared)
                and any(isinstance(row, dict) and row.get("scope") == "model"
                        and type(row.get("used_percent")) in (int, float) and row["used_percent"] == 100
                        for row in quota.get("windows", []))):
            # A generic native weekly error may refer to this model pool.
            # The task still fails, but that is not evidence to block every
            # other model sharing the same login.
            return False
        self.blocks[adapter] = {"identity": identity, "reason": "subscription_limit_reached",
            "source": scope_adapter(adapter) + "_cli_limit", "observed_at": self.clock(), "resets_at": None}
        self.rechecks.pop(adapter, None)
        self._save()
        return True

    def probe_due(self, scope):
        block = self.blocks.get(scope)
        return (block is not None and block["resets_at"] is None and block["reason"] == "subscription_limit_reached"
                and self.clock() - self.probed.get(scope, -PROBE_SECONDS) >= PROBE_SECONDS)

    def note_probe(self, scope):
        self.probed[scope] = self.clock()

    def release_after_probe(self, scope):
        """A real request succeeded, which is better evidence than any recorded limit."""
        if self.blocks.pop(scope, None) is not None:
            self._save()

    def request_recheck(self, scope, account):
        if not self.snapshot(scope, account)["can_request_recheck"]:
            raise ValueError("availability_recheck_unavailable")
        self.blocks.pop(scope)
        self.rechecks[scope] = {"identity": self.identity(account), "observed_at": self.clock()}
        self._save()
        return {"kind": "quota_recheck_requested", "adapter": scope_adapter(scope), "observed_at": self.clock()}

    def snapshot(self, scope, account):
        adapter = scope_adapter(scope)
        base = {"state": "unknown", "selectable": True, "reason": "quota_unknown",
                "source": account.get("source"), "observed_at": account.get("checked_at"),
                "identity_checked_at": account.get("checked_at"),
                "resets_at": None, "stale": account.get("stale", True),
                "scope": "default_cli_login" if scope == adapter else "profile_cli_home",
                "can_request_recheck": False}
        if adapter not in _ADAPTERS:
            return {**base, "reason": "adapter_unsupported"}
        identity, block = self.identity(account), self.blocks.get(scope)
        if block and identity == block["identity"]:
            expired = block["resets_at"] is not None and block["resets_at"] <= self.clock()
            return {**base, **{key: block[key] for key in ("reason", "source", "observed_at", "resets_at")},
                    "state": "unknown" if expired else "blocked", "selectable": expired,
                    "reason": "quota_reset_due" if expired else block["reason"],
                    "stale": base["stale"] or self.clock() - block["observed_at"] > STALE_SECONDS,
                    "can_request_recheck": (adapter in {"claude", "kimi"} and not expired and account.get("stale") is False
                                            and block["reason"] == "subscription_limit_reached")}
        if block and identity is None:
            # A failed identity read after restart cannot bypass a previously
            # confirmed limit. We are blocked pending identity, not asserting
            # that the still-unknown current login is the exhausted account.
            return {**base, "state": "blocked", "selectable": False, "reason": "account_identity_unknown",
                    "source": block["source"], "observed_at": block["observed_at"], "resets_at": block["resets_at"]}
        if scope in self.unbound:
            return {**base, "reason": "account_identity_unknown"}
        windows = self._shared_windows(scope, account)
        if identity and windows and all(row["used_percent"] < 100 for row in windows):
            return {**base, "state": "available", "reason": None, "source": adapter + "_shared_pool",
                    "observed_at": account["quota"].get("checked_at"), "stale": False}
        if identity and self.rechecks.get(scope, {}).get("identity") == identity:
            return {**base, "reason": "user_recheck_requested", "source": "user_recheck_requested",
                    "observed_at": self.rechecks[scope]["observed_at"]}
        if account.get("state") == "error":
            return {**base, "reason": "account_check_failed"}
        return base
