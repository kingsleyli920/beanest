"""Private, explicit profiles for the two verified subscription CLI adapters.

A profile is a separate room participant, not a new model or a new account. Its
adapter and provider remain visible, and existing rooms keep their frozen specs.
Inspection never runs a CLI; only ``check`` performs a bounded local auth check.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable

from . import adapters
from .agent_registry import (AgentRegistry, AgentSpec, BEAN_STYLE_IDS,
                             TOKEN_FIELDS, default_bean_style, safe_agent_id, native_spec)
from .quota_gate import QuotaGate
from .native_setup import NATIVE_ADAPTERS, NativeLogin, native_adapter


TRUSTED_ADAPTERS = {item.id: item.provider for item in NATIVE_ADAPTERS}
CONNECTOR_TEMPLATES = (("kimi", "Kimi"), ("deepseek", "DeepSeek"), ("local_json", "兼容本机 CLI"))
CONNECTOR_ADAPTERS = frozenset(item[0] for item in CONNECTOR_TEMPLATES)
MAX_PROFILES = 32
MAX_CONFIG_BYTES = 128 * 1024
_DEFAULT_BEAN_STYLE = object()
_DEFAULT_CONNECTION = object()


def _connector(item):
    # Previously saved Kimi JSON profiles keep their exact executable contract.
    return item["adapter"] in CONNECTOR_ADAPTERS and (item["adapter"] not in TRUSTED_ADAPTERS or "connection" in item)
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?")
_PROFILE_KEYS = {"id", "name", "avatar", "adapter", "enabled", "builtin", "bean_style", "lifecycle_state"}
_LIFECYCLE_ACTIONS = {"archive": "archived", "restore": "active", "remove": "removed"}
_HEALTH_KEYS = {"state", "checked_at", "cli_available", "version", "auth", "reason",
                "cost", "subscription_remaining"}
_REASONS = {None, "not_checked", "cli_missing", "cli_check_failed", "auth_unconfirmed",
            "check_timeout", "check_failed", "connection_not_configured", "protocol_invalid",
            "connector_not_ready"}
ACCOUNT_STALE_SECONDS = 90
ACCOUNT_REFRESH_COALESCE_SECONDS = 55


def connection_templates() -> list[dict]:
    return [{"id": adapter, "name": name, "adapter": adapter,
             "protocol": adapters.JSON_CLI_PROTOCOL,
             "description": "保存为待配置工具；需本机连接器实现豆窝 JSON 协议，通过检查后才能参与。"}
            for adapter, name in CONNECTOR_TEMPLATES if adapter not in TRUSTED_ADAPTERS]


def connection_protocol() -> dict:
    return {"id": adapters.JSON_CLI_PROTOCOL,
            "transport": "stdin_stdout_json", "shell": False, "tools": False,
            "billing_modes": ["existing_subscription", "local"],
            "description": "只连接你信任的本机可执行程序；不是任意厂商 CLI 的通用参数。检查不能调用模型。"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class PartnerError(ValueError):
    """Stable error code; no local paths or subprocess output are exposed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unknown_health() -> dict:
    return {"state": "unknown", "checked_at": None, "cli_available": None,
            "version": None, "auth": {"method": None, "subscription_confirmed": None},
            "reason": "not_checked", "cost": None, "subscription_remaining": None}


def _unknown_account(adapter):
    supported = adapter in TRUSTED_ADAPTERS
    source = {"claude": "claude_auth_status", "codex": "codex_app_server", "kimi": "kimi_managed_oauth"}.get(adapter, "unsupported")
    return {"state": "not_checked" if supported else "unavailable", "email": None, "plan": None,
            "auth_method": None, "source": source, "scope": "default_cli_login",
            "checked_at": None, "stale": True, "reason": "not_checked" if supported else "account_adapter_unsupported",
            "shared_profile_ids": [],
            "quota": {"state": "not_checked" if supported else "unavailable", "windows": [],
                      "source": "codex_app_server_account_rate_limits" if adapter == "codex" else source,
                      "scope": "provider_account", "checked_at": None, "stale": True,
                      "reason": "not_checked" if supported else "account_adapter_unsupported",
                      **(_kimi_usage_metadata() if adapter == "kimi" else {})}}


def _kimi_usage_metadata():
    from .kimi_usage import USAGE_URL
    return {"usage_url": USAGE_URL}


def unavailable_discovery() -> dict:
    return {"state": "unavailable", "observed_at": None,
            "tools": [{"adapter": adapter, "name": name, "installed": None}
                      for adapter, name in ((item.id, item.name) for item in NATIVE_ADAPTERS)],
            "scope": "supported_cli_locations"}


class PartnerManager:
    def __init__(self, root: Path, base_registry: AgentRegistry, *,
                 clock: Callable[[], float] = time.time, startup_discovery: bool = False):
        if type(startup_discovery) is not bool:
            raise ValueError("startup_discovery_must_be_boolean")
        self.root = Path(root).absolute()
        self.base_registry = base_registry
        self.clock = clock
        self._checks = asyncio.Semaphore(2)
        self._check_locks: dict[str, asyncio.Lock] = {}
        # Identity observations stay in process memory. Profiles on disk contain
        # no email addresses, account payloads or inferred quota balances.
        self._account_cache: dict[str, dict] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._account_attempts: dict[str, float] = {}
        self._startup_discovery = startup_discovery
        self._setup_observations = {}
        self.native_login = NativeLogin(self.root, on_ready=self._login_ready)
        self._ensure_root()
        try:
            self.quota_gate = QuotaGate(self.root, clock)
        except (OSError, ValueError, TypeError):
            raise PartnerError("quota_state_corrupt") from None
        # Fail closed immediately, rather than silently resetting a bad config.
        with self._locked():
            self._load()
        self._discovery = self._discover() if startup_discovery else unavailable_discovery()

    def _discover(self) -> dict:
        result = unavailable_discovery()
        try:
            observed = self.clock()
            if type(observed) in (float, int) and math.isfinite(observed) and observed >= 0:
                result["observed_at"] = observed
        except Exception:
            pass
        for tool in result["tools"]:
            # The two resolvers only inspect supported executable locations.
            # No version, authentication, model, project or session is queried.
            resolver = native_adapter(tool["adapter"]).resolve
            try:
                resolver()
                tool["installed"] = True
            except RuntimeError:
                tool["installed"] = False
            except Exception:
                # Permission/filesystem failures are unknown, never absence.
                tool["installed"] = None
        if result["observed_at"] is not None and all(type(tool["installed"]) is bool for tool in result["tools"]):
            result["state"] = "completed"
        return result

    def discovery(self) -> dict:
        """Return the startup observation; inspection never repeats discovery."""
        return json.loads(json.dumps(self._discovery))

    def _login_ready(self, adapter):
        self._setup_observations[adapter] = "ready"

    async def refresh_setup(self):
        """Explicit no-inference checks; GET and installation scans stay passive."""
        installed = {item["adapter"]: item["installed"] for item in self._discovery["tools"]}
        if installed.get("kimi") is True:
            try:
                observation = await adapters.kimi_status(self.root)
                self._setup_observations["kimi"] = "ready" if observation["ready"] else "login_required"
            except (OSError, RuntimeError, asyncio.TimeoutError):
                self._setup_observations["kimi"] = "unknown"

    def supported_adapters(self):
        installed = {item["adapter"]: item["installed"] for item in self._discovery["tools"]}
        result = []
        for descriptor in NATIVE_ADAPTERS:
            state = ("not_installed" if installed.get(descriptor.id) is False else
                     self._setup_observations.get(descriptor.id, "unknown"))
            result.append({"id": descriptor.id, "name": descriptor.name,
                           "setup": {"state": state, "install_url": descriptor.install_url,
                                     "login_supported": descriptor.login_supported,
                                     "login_operation": self.native_login.snapshot(descriptor.id)}})
        return result

    def refresh_discovery(self) -> dict:
        """Refresh installation observations on an explicit application open.

        This never runs an executable, touches configuration bytes, or changes
        disabled preferences. GET inspection continues to return a snapshot.
        """
        self._discovery = self._discover()
        self._startup_discovery = True
        return self.discovery()

    def _installation(self, adapter: str, connection=None, *, connector=False) -> dict:
        if connector:
            if connection is None:
                return {"state": "not_configured"}
            try:
                adapters.find_json_cli(connection)
                return {"state": "found"}
            except (FileNotFoundError, RuntimeError):
                return {"state": "missing"}
            except OSError:
                return {"state": "unknown"}
        observed = next((tool["installed"] for tool in self._discovery["tools"]
                         if tool["adapter"] == adapter), None)
        result = {"state": "found" if observed is True else "missing" if observed is False else "unknown"}
        selection = adapters.cli_selection(adapter) if adapter in {"codex", "claude", "kimi"} else None
        if selection and selection["checked"] and (selection["skipped"] or selection["path"]):
            # Several installations may coexist; name the one in use and why
            # others were skipped instead of a generic login prompt.
            result.update(executable=selection["path"], skipped=selection["skipped"])
        return result

    def _ensure_root(self):
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise PartnerError("unsafe_partner_storage")
        try:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not self.root.is_dir():
                raise PartnerError("unsafe_partner_storage")
            self.root.chmod(0o700)
        except OSError:
            raise PartnerError("unsafe_partner_storage") from None

    @contextmanager
    def _locked(self):
        self._ensure_root()
        try:
            descriptor = os.open(self.root / ".profiles.lock",
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise PartnerError("unsafe_partner_storage")
            os.fchmod(descriptor, 0o600)
        except OSError:
            raise PartnerError("unsafe_partner_storage") from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _initial(self):
        return {"schema": 1, "profiles": [
            {"id": spec.id, "name": spec.name, "avatar": spec.avatar,
             "adapter": spec.adapter or spec.id, "bean_style": spec.bean_style,
             "enabled": True, "builtin": True, "lifecycle_state": "active"}
            for spec in (self.base_registry.get(key) for key in self.base_registry.ids())
        ], "checks": {}, "ignored_builtin_ids": [], "retired_profile_ids": []}

    def _load(self) -> dict:
        try:
            descriptor = os.open(self.root / "profiles.json", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return self._initial()
        except OSError:
            raise PartnerError("unsafe_partner_storage") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES or info.st_mode & 0o077:
                raise PartnerError("unsafe_partner_storage")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                data = json.load(stream, object_pairs_hook=_unique_object)
        except PartnerError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise PartnerError("invalid_partner_storage") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            for item in data["profiles"]:
                if (isinstance(item, dict) and set(item) - {"bean_style", "lifecycle_state", "connection"}
                        == _PROFILE_KEYS - {"bean_style", "lifecycle_state"}):
                    # Upgrade in memory only: read-only inspection must not
                    # rewrite configuration. The next explicit save includes it.
                    if "bean_style" not in item and isinstance(item["adapter"], str):
                        item["bean_style"] = default_bean_style(item["adapter"])
                    item.setdefault("lifecycle_state", "active")
        if isinstance(data, dict):
            data.setdefault("ignored_builtin_ids", [])
            data.setdefault("retired_profile_ids", [])
        self._validate(data)
        return data

    def _validate(self, data):
        try:
            if (not isinstance(data, dict) or set(data) != {"schema", "profiles", "checks", "ignored_builtin_ids", "retired_profile_ids"}
                    or type(data["schema"]) is not int or data["schema"] != 1
                    or not isinstance(data["profiles"], list)
                    or not 0 <= len(data["profiles"]) <= MAX_PROFILES
                    or not isinstance(data["checks"], dict)):
                raise ValueError
            ignored, retired = data["ignored_builtin_ids"], data["retired_profile_ids"]
            if (not isinstance(ignored, list) or any(not safe_agent_id(value) for value in ignored)
                    or len(set(ignored)) != len(ignored) or not set(ignored).issubset(self.base_registry.ids())
                    or not isinstance(retired, list) or any(not safe_agent_id(value) for value in retired)
                    or len(set(retired)) != len(retired) or set(retired) & set(self.base_registry.ids())):
                raise ValueError
            seen = set()
            for item in data["profiles"]:
                if (not isinstance(item, dict) or set(item) not in (_PROFILE_KEYS, _PROFILE_KEYS | {"connection"})
                        or not safe_agent_id(item["id"]) or item["id"] in seen
                        or not isinstance(item["bean_style"], str) or item["bean_style"] not in BEAN_STYLE_IDS
                        or not isinstance(item["lifecycle_state"], str)
                        or item["lifecycle_state"] not in {"active", "archived", "removed"}
                        or type(item["enabled"]) is not bool or type(item["builtin"]) is not bool):
                    raise ValueError
                seen.add(item["id"])
                base = self.base_registry.get(item["id"])
                if item["builtin"]:
                    if (base is None or item["name"] != base.name or item["avatar"] != base.avatar
                            or item["adapter"] != (base.adapter or base.id) or "connection" in item):
                        raise ValueError
                elif base is not None:
                    raise ValueError
                self._spec(item)
            if (not set(self.base_registry.ids()).issubset(seen | set(ignored))
                    or seen & (set(ignored) | set(retired)) or not set(data["checks"]).issubset(seen)):
                raise ValueError
            for health in data["checks"].values():
                self._validate_health(health)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PartnerError("invalid_partner_storage") from None

    @staticmethod
    def _validate_health(health):
        if (not isinstance(health, dict) or set(health) != _HEALTH_KEYS
                or health["state"] not in {"unknown", "ready", "unavailable"}
                or type(health["checked_at"]) not in (float, int)
                or not math.isfinite(health["checked_at"]) or health["checked_at"] < 0
                or type(health["cli_available"]) not in (bool, type(None))
                or (health["version"] is not None and (not isinstance(health["version"], str)
                    or not _VERSION.fullmatch(health["version"])))
                or not isinstance(health["auth"], dict)
                or set(health["auth"]) != {"method", "subscription_confirmed"}
                or health["auth"]["method"] not in {None, "chatgpt", "claude.ai", "unconfirmed", "local", "configured_cli", "kimi-code"}
                or type(health["auth"]["subscription_confirmed"]) not in (bool, type(None))
                or health["reason"] not in _REASONS
                or health["cost"] is not None or health["subscription_remaining"] is not None):
            raise ValueError("invalid_health")

    def _spec(self, item) -> AgentSpec:
        if item["builtin"]:
            spec = self.base_registry.get(item["id"])
            if spec is None:
                raise PartnerError("invalid_partner_storage")
            return replace(spec, bean_style=item["bean_style"])
        adapter = item["adapter"]
        if _connector(item):
            connection = item.get("connection")
            if connection is not None:
                try:
                    connection = adapters.json_cli_connection(connection)
                except ValueError:
                    raise PartnerError("invalid_partner_connection") from None
            invoke = partial(adapters.invoke_json_cli, connection)
            return AgentSpec(item["id"], item["name"], invoke,
                             usage_source="configured_cli", usage_fields=TOKEN_FIELDS,
                             avatar=item["avatar"], adapter=adapter, provider=adapter,
                             bean_style=item["bean_style"], invoke_structured=invoke)
        if "connection" in item:
            raise PartnerError("invalid_partner_connection")
        template = (self.base_registry.get(adapter) or native_spec(adapter)) if isinstance(adapter, str) else None
        if adapter not in TRUSTED_ADAPTERS or template is None:
            raise PartnerError("unsupported_partner_adapter")
        # Reuse the trusted invoke callable, never partial it with the alias ID.
        # The adapter still launches its own fresh provider session per room.
        return replace(template, id=item["id"], name=item["name"], avatar=item["avatar"],
                       adapter=adapter, provider=TRUSTED_ADAPTERS[adapter], bean_style=item["bean_style"])

    def _save(self, data):
        self._validate(data)
        descriptor, name = tempfile.mkstemp(prefix=".profiles-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "profiles.json")
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

    def _describe(self, item, data):
        result = self._spec(item).describe()
        installation = self._installation(item["adapter"], item.get("connection"), connector=_connector(item))
        health = data["checks"].get(item["id"], _unknown_health())
        connector = _connector(item)
        available = item["enabled"] and item["lifecycle_state"] == "active"
        if connector and item.get("connection") is None and item["id"] not in data["checks"]:
            health = {**_unknown_health(), "reason": "connection_not_configured"}
        result.update(adapter=item["adapter"], enabled=item["enabled"], builtin=item["builtin"],
                      lifecycle_state=item["lifecycle_state"],
                      health=health, installation=installation,
                      connection_state=("configured" if item.get("connection") else "not_configured")
                      if connector else "builtin",
                      selectable=(available and installation["state"] == "found" and health["state"] == "ready")
                      if connector else (available and (not self._startup_discovery
                                                              or installation["state"] == "found")))
        result["account"] = (_unknown_account("configured_cli") if connector else self._account_snapshot(item["adapter"], data))
        result["availability"] = self.quota_gate.snapshot(item["adapter"], result["account"])
        result["configuration_selectable"] = result["selectable"]
        result["selectable"] = result["selectable"] and result["availability"]["selectable"]
        if connector:
            result["connection"] = item.get("connection")
        # Return independent values; callers cannot edit the persisted structure.
        return json.loads(json.dumps(result))

    def _account_snapshot(self, adapter, data):
        account = json.loads(json.dumps(self._account_cache.get(adapter, _unknown_account(adapter))))
        account["shared_profile_ids"] = ([item["id"] for item in data["profiles"] if item["adapter"] == adapter]
                                         if adapter in TRUSTED_ADAPTERS else [])
        if account["checked_at"] is None and account["quota"]["checked_at"] is None:
            return account
        try:
            now = self.clock()
            valid_clock = type(now) in (int, float) and math.isfinite(now)
        except Exception:
            now, valid_clock = None, False
        for observation in (account, account["quota"]):
            checked = observation["checked_at"]
            if (not valid_clock or checked is None or now < checked
                    or now - checked > ACCOUNT_STALE_SECONDS):
                observation["stale"] = True
        return account

    async def refresh_account(self, partner_id, *, force=False):
        """Explicit no-inference refresh. GET inspection never starts a CLI."""
        profile = self.get(partner_id)
        adapter = profile["adapter"]
        if adapter not in TRUSTED_ADAPTERS or profile["connection_state"] != "builtin":
            raise PartnerError("account_adapter_unsupported")
        async with self._account_locks.setdefault(adapter, asyncio.Lock()), self._checks:
            checked_at = self.clock()
            if type(checked_at) not in (int, float) or not math.isfinite(checked_at) or checked_at < 0:
                raise PartnerError("invalid_check_clock")
            previous_attempt = self._account_attempts.get(adapter)
            if not force and previous_attempt is not None and 0 <= checked_at - previous_attempt <= ACCOUNT_REFRESH_COALESCE_SECONDS:
                return self.get(partner_id)
            previous = self._account_cache.get(adapter)
            try:
                from .kimi_usage import DEFAULT_TIMEOUT as kimi_timeout
                from .kimi_usage import SOURCE as kimi_usage_source
                observed = await adapters.account_status(adapter, self.root, timeout=kimi_timeout if adapter == "kimi" else 10)
                account = {key: observed[key] for key in ("state", "email", "plan", "auth_method", "source", "scope", "reason")}
                quota = {key: observed["quota"][key] for key in ("state", "windows", "source", "scope", "reason")}
                if adapter == "kimi":
                    reasons = observed.get("field_reasons", {})
                    account["field_reasons"] = {key: reasons.get(key) if isinstance(reasons, dict)
                        and reasons.get(key) in {None, "provider_not_provided", "profile_query_unavailable", "account_changed"}
                        else "profile_query_unavailable" for key in ("email", "plan")}
                    display = observed.get("display_identity", {})
                    account["display_identity"] = {}
                    if isinstance(display, dict):
                        phone, nickname = display.get("masked_phone"), display.get("nickname")
                        if isinstance(phone, str) and re.fullmatch(r"\+[0-9]{1,4} \*{4} [0-9]{4}", phone):
                            account["display_identity"]["masked_phone"] = phone
                        if isinstance(nickname, str) and nickname.strip() and len(nickname) <= 100 and nickname.isprintable():
                            account["display_identity"]["nickname"] = nickname.strip()
                    quota.update(_kimi_usage_metadata())
                    if isinstance(observed["quota"].get("diagnostics"), dict):
                        quota["diagnostics"] = {key: observed["quota"]["diagnostics"].get(key)
                            for key in ("phase", "elapsed_seconds", "reason")}
                account.update(checked_at=checked_at, stale=False, shared_profile_ids=[])
                quota.update(checked_at=checked_at, stale=False)
                identity = (account["email"], account["plan"], account["auth_method"])
                previous_identity = ((previous["email"], previous["plan"], previous["auth_method"])
                                     if previous else None)
                if (previous and identity == previous_identity and account["email"] is not None
                        and quota["state"] == "error" and quota["reason"] != "account_changed"):
                    quota = {**previous["quota"], "stale": True, "reason": quota["reason"]}
                account["quota"] = quota
                self._account_cache[adapter] = account
                self.quota_gate.observe_account(adapter, account)
                # A fresh managed usage read confirms the same login fact as
                # setup/login checks, without requiring another CLI process.
                if (adapter == "kimi" and account["state"] == "ready"
                        and account["auth_method"] == "kimi-code" and account["source"] == "kimi_managed_oauth"
                        and observed.get("stale") is not True and observed["quota"].get("stale") is not True
                        and quota["state"] == "available" and quota["source"] == kimi_usage_source
                        and quota["scope"] == "provider_account" and quota["reason"] is None
                        and isinstance(quota["windows"], list) and quota["windows"]
                        and all(isinstance(row, dict) and all(type(row.get(key)) in (int, float)
                            and math.isfinite(row[key]) and 0 <= row[key] <= 100
                            for key in ("used_percent", "remaining_percent"))
                            and math.isclose(row["used_percent"] + row["remaining_percent"], 100)
                            for row in quota["windows"])):
                    self._login_ready("kimi")
            except asyncio.CancelledError:
                if previous:
                    self._account_cache[adapter] = {**previous, "stale": True, "reason": "account_refresh_cancelled",
                                                   "quota": {**previous["quota"], "stale": True,
                                                             "reason": "account_refresh_cancelled"}}
                raise
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                reason = ("account_check_timeout" if isinstance(error, asyncio.TimeoutError) else
                          error.code if isinstance(error, adapters.AccountTelemetryError) else "account_check_failed")
                cached = json.loads(json.dumps(previous or _unknown_account(adapter)))
                cached.update(state="error", stale=True, reason=reason)
                cached["quota"].update(stale=True, reason=reason)
                self._account_cache[adapter] = cached
            finally:
                self._account_attempts[adapter] = checked_at
            return self.get(partner_id)

    async def refresh_availability(self, partner_ids=None):
        """Bounded explicit refresh, once per native default-login adapter."""
        selected = set(partner_ids) if partner_ids is not None else None
        chosen = {}
        for profile in self.profiles():
            spec = self.base_registry.get(profile["adapter"]) or native_spec(profile["adapter"])
            included = (profile["lifecycle_state"] == "active" and profile["enabled"]
                        if selected is None else profile["id"] in selected)
            if (included
                    and not _connector(profile) and spec is not None and spec.native_account_status):
                chosen.setdefault(profile["adapter"], profile["id"])
        await asyncio.gather(*(self.refresh_account(identifier) for identifier in chosen.values()))
        return self.profiles()

    async def record_subscription_limit(self, partner_id, bound_identity):
        profile = await self.refresh_account(partner_id, force=True)
        self.quota_gate.record_limit(profile["adapter"], profile["account"], bound_identity=bound_identity)

    def account_identity(self, partner_id):
        return self.quota_gate.identity(self.get(partner_id)["account"])

    def request_availability_recheck(self, partner_id):
        profile = self.get(partner_id)
        try:
            return self.quota_gate.request_recheck(profile["adapter"], profile["account"])
        except ValueError:
            raise PartnerError("availability_recheck_unavailable") from None

    def profiles(self) -> list[dict]:
        with self._locked():
            data = self._load()
            return [self._describe(item, data) for item in data["profiles"]]

    def get(self, partner_id: str) -> dict:
        for item in self.profiles():
            if item["id"] == partner_id:
                return item
        raise PartnerError("unknown_partner")

    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(item["id"] for item in self.profiles()
                     if item["enabled"] and item["lifecycle_state"] == "active")

    def available_ids(self) -> tuple[str, ...]:
        """Filter new goals by installation without rewriting saved preferences."""
        # Keep known identities in the catalog even when quota blocks dispatch;
        # otherwise a polling UI could silently shrink an already chosen team.
        return tuple(item["id"] for item in self.profiles() if item["configuration_selectable"])

    def registry(self) -> AgentRegistry:
        """Keep inactive identities for historical rooms; new rooms are filtered."""
        with self._locked():
            data = self._load()
            return AgentRegistry((self._spec(item) for item in data["profiles"]), allow_empty=True)

    def add(self, *, id: str, name: str, adapter: str, avatar: str = "",
            bean_style: object = _DEFAULT_BEAN_STYLE, connection=_DEFAULT_CONNECTION) -> dict:
        if not isinstance(adapter, str) or adapter not in TRUSTED_ADAPTERS.keys() | CONNECTOR_ADAPTERS:
            raise PartnerError("unsupported_partner_adapter")
        if connection is not _DEFAULT_CONNECTION and connection is not None:
            if adapter not in CONNECTOR_ADAPTERS:
                raise PartnerError("invalid_partner_connection")
            try:
                connection = adapters.json_cli_connection(connection)
            except ValueError:
                raise PartnerError("invalid_partner_connection") from None
        if bean_style is _DEFAULT_BEAN_STYLE:
            bean_style = default_bean_style(adapter)
        elif not isinstance(bean_style, str) or bean_style not in BEAN_STYLE_IDS:
            raise PartnerError("invalid_partner_profile")
        item = {"id": id, "name": name, "adapter": adapter, "avatar": avatar,
                "bean_style": bean_style,
                "enabled": True, "builtin": False, "lifecycle_state": "active"}
        if adapter in CONNECTOR_ADAPTERS and (adapter not in TRUSTED_ADAPTERS or connection is not _DEFAULT_CONNECTION):
            item["connection"] = None if connection is _DEFAULT_CONNECTION else connection
        try:
            self._spec(item)
        except (ValueError, TypeError):
            raise PartnerError("invalid_partner_profile") from None
        with self._locked():
            data = self._load()
            if id in data["retired_profile_ids"] or id in data["ignored_builtin_ids"]:
                raise PartnerError("partner_identity_conflict")
            for original in data["profiles"]:
                if original["id"] == id:
                    same = all(original[key] == item[key] for key in ("id", "name", "adapter", "avatar", "bean_style"))
                    same = same and original.get("connection") == item.get("connection")
                    if same:
                        return self._describe(original, data)
                    raise PartnerError("partner_identity_conflict")
            if len(data["profiles"]) >= MAX_PROFILES:
                raise PartnerError("partner_limit_reached")
            data["profiles"].append(item)
            self._save(data)
            return self._describe(item, data)

    def configure(self, partner_id: str, connection: dict) -> dict:
        """Fill a dormant template once; configured identities stay immutable.

        Existing rooms resolve frozen IDs. Replacing their executable would
        silently change who owns an old session, so a changed tool gets a new ID.
        """
        try:
            connection = adapters.json_cli_connection(connection)
        except ValueError:
            raise PartnerError("invalid_partner_connection") from None
        with self._locked():
            data = self._load()
            for item in data["profiles"]:
                if item["id"] == partner_id:
                    if item["builtin"] or not _connector(item):
                        raise PartnerError("unsupported_partner_adapter")
                    if item.get("connection") == connection:
                        return self._describe(item, data)
                    if item.get("connection") is not None:
                        raise PartnerError("partner_identity_conflict")
                    item["connection"] = connection
                    data["checks"].pop(partner_id, None)
                    self._save(data)
                    return self._describe(item, data)
        raise PartnerError("unknown_partner")

    def set_appearance(self, partner_id: str, bean_style: str) -> dict:
        """Change only presentation, preserving the existing partner identity."""
        if not isinstance(bean_style, str) or bean_style not in BEAN_STYLE_IDS:
            raise PartnerError("invalid_partner_profile")
        with self._locked():
            data = self._load()
            for item in data["profiles"]:
                if item["id"] == partner_id:
                    item["bean_style"] = bean_style
                    self._save(data)
                    return self._describe(item, data)
        raise PartnerError("unknown_partner")

    def set_enabled(self, partner_id: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise PartnerError("invalid_partner_enabled")
        with self._locked():
            data = self._load()
            for item in data["profiles"]:
                if item["id"] == partner_id:
                    if item["lifecycle_state"] != "active":
                        raise PartnerError("partner_inactive")
                    item["enabled"] = enabled
                    self._save(data)
                    return self._describe(item, data)
        raise PartnerError("unknown_partner")

    def set_lifecycle(self, partner_id: str, action: str) -> dict:
        """Hide or restore a profile without deleting its identity or settings."""
        if not isinstance(action, str) or action not in _LIFECYCLE_ACTIONS:
            raise PartnerError("invalid_lifecycle_action")
        state = _LIFECYCLE_ACTIONS[action]
        with self._locked():
            data = self._load()
            for item in data["profiles"]:
                if item["id"] == partner_id:
                    if item["lifecycle_state"] != state:
                        item["lifecycle_state"] = state
                        self._save(data)
                    return self._describe(item, data)
        raise PartnerError("unknown_partner")

    def remove_profile(self, partner_id: str) -> dict:
        """Delete Beanest configuration, not the CLI, login or historical rooms.

        A deleted custom identity cannot later acquire a different connector and
        inherit its old sessions. Built-in suppression is only a discovery
        preference; explicit restore_builtin recreates its known default spec.
        """
        if not safe_agent_id(partner_id):
            raise PartnerError("invalid_partner_profile")
        with self._locked():
            data = self._load()
            removed = next((item for item in data["profiles"] if item["id"] == partner_id), None)
            remaining = [item for item in data["profiles"] if item["id"] != partner_id]
            if len(remaining) != len(data["profiles"]):
                data["profiles"] = remaining
                data["checks"].pop(partner_id, None)
                preference = "ignored_builtin_ids" if self.base_registry.get(partner_id) else "retired_profile_ids"
                data[preference] = sorted(set(data[preference]) | {partner_id})
                self._save(data)
                if not any(item["adapter"] == removed["adapter"] for item in remaining):
                    self._account_cache.pop(removed["adapter"], None)
                    self._account_attempts.pop(removed["adapter"], None)
        # Adapter-wide observations remain only for surviving aliases. Never
        # alter the CLI login or the shared quota identity when removing a row.
        self._check_locks.pop(partner_id, None)
        return {"id": partner_id, "deleted": True}

    def restore_builtin(self, partner_id: str) -> dict:
        """Explicitly add a deleted built-in with its known default identity."""
        spec = self.base_registry.get(partner_id) if safe_agent_id(partner_id) else None
        if spec is None:
            raise PartnerError("unknown_partner")
        with self._locked():
            data = self._load()
            for item in data["profiles"]:
                if item["id"] == partner_id:
                    return self._describe(item, data)
            if len(data["profiles"]) >= MAX_PROFILES:
                raise PartnerError("partner_limit_reached")
            item = next(item for item in self._initial()["profiles"] if item["id"] == partner_id)
            data["profiles"].append(item)
            data["ignored_builtin_ids"] = [identifier for identifier in data["ignored_builtin_ids"] if identifier != partner_id]
            self._save(data)
            return self._describe(item, data)

    async def check(self, partner_id: str) -> dict:
        """Explicit no-inference check. Saved results are observations, not quotas."""
        profile = self.get(partner_id)
        adapter = profile["adapter"]
        if adapter not in TRUSTED_ADAPTERS.keys() | CONNECTOR_ADAPTERS:
            raise PartnerError("unsupported_partner_adapter")
        lock = self._check_locks.setdefault(partner_id, asyncio.Lock())
        async with lock, self._checks:
            health = _unknown_health()
            checked_at = self.clock()
            if type(checked_at) not in (float, int) or not math.isfinite(checked_at) or checked_at < 0:
                raise PartnerError("invalid_check_clock")
            health["checked_at"] = checked_at
            try:
                if _connector(profile):
                    connection = profile.get("connection")
                    if connection is None:
                        health.update(state="unavailable", reason="connection_not_configured")
                    else:
                        adapters.find_json_cli(connection)
                        health["cli_available"] = True
                        observation = await adapters.check_json_cli(connection, self.root)
                        local = observation["billing"] == "local"
                        health.update(state="ready" if observation["ready"] else "unavailable",
                                      reason=None if observation["ready"] else "connector_not_ready",
                                      version=observation["version"],
                                      auth={"method": "local" if local else "configured_cli",
                                            "subscription_confirmed": None})
                    return self._save_check(partner_id, health)
                if adapter == "kimi":
                    adapters.find_kimi()
                    await adapters.select_cli("kimi", self.root)
                    observation = await adapters.kimi_status(self.root)
                    health.update(state="ready" if observation["ready"] else "unavailable", cli_available=True,
                                  version=observation["version"], reason=None if observation["ready"] else "auth_unconfirmed",
                                  auth={"method": "kimi-code" if observation["ready"] else "unconfirmed",
                                        "subscription_confirmed": observation["ready"]})
                    self._setup_observations[adapter] = "ready" if observation["ready"] else "login_required"
                    return self._save_check(partner_id, health)
                await adapters.select_cli(adapter, self.root)
                executable = native_adapter(adapter).resolve()
                health["cli_available"] = True
                version_command = ([executable, "--version"] if adapter == "codex" else
                                   [executable, "--safe-mode", "--restricted", "--version"])
                lines, code = await adapters._run(version_command, "", self.root, 10, include_stderr=True)
                raw = b"\n".join(lines)
                version = _VERSION.search(raw.decode("utf-8", errors="replace")) if len(raw) <= 65536 else None
                if code or version is None:
                    health["state"] = "unknown"
                    health["reason"] = "cli_check_failed"
                else:
                    health["version"] = version.group(0)
                    command = ([executable, "login", "status"] if adapter == "codex" else
                               [executable, "--safe-mode", "--restricted", "auth", "status", "--json"])
                    lines, code = await adapters._run(command, "", self.root, 10, include_stderr=True)
                    raw = b"\n".join(lines)
                    if len(raw) > 65536:
                        raise RuntimeError
                    auth = adapters._authentication(adapter, code, raw.decode("utf-8", errors="replace"))
                    confirmed = auth.get("subscription_confirmed") is True
                    method = auth.get("authMethod")
                    health["auth"] = {"method": method if method in {"chatgpt", "claude.ai"} else "unconfirmed",
                                      "subscription_confirmed": confirmed}
                    health["state"] = "ready" if confirmed else "unavailable"
                    health["reason"] = None if confirmed else "auth_unconfirmed"
            except (FileNotFoundError, RuntimeError) as error:
                # find_codex uses RuntimeError for absence. Process/output failures
                # remain unknown whenever an executable was already found.
                missing = health["cli_available"] is None
                health.update(state="unavailable" if missing else "unknown",
                              reason="cli_missing" if missing else (
                                  "protocol_invalid" if str(error) == "connector_protocol_invalid" else "check_failed"))
                if missing:
                    health["cli_available"] = False
            except asyncio.TimeoutError:
                health.update(state="unknown", reason="check_timeout")
            except OSError:
                health.update(state="unknown", reason="check_failed")
            # CancelledError deliberately propagates after adapters._run finishes
            # process-group cleanup. It never overwrites a previous health check.
            return self._save_check(partner_id, health)

    def _save_check(self, partner_id, health):
        with self._locked():
            data = self._load()
            if not any(item["id"] == partner_id for item in data["profiles"]):
                raise PartnerError("unknown_partner")
            data["checks"][partner_id] = health
            self._save(data)
            item = next(item for item in data["profiles"] if item["id"] == partner_id)
            return self._describe(item, data)
