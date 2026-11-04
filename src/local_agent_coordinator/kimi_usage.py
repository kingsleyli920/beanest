"""Read Kimi's managed-account quota through an owned, local CLI service.

The CLI holds OAuth credentials. Beanest only uses its local HTTP bearer in
memory, never calls a model endpoint, and never adopts an existing web process.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import http.client
import json
import math
import re
import socket
import tempfile
import threading
import time
from urllib.parse import unquote


USAGE_URL = "https://www.kimi.com/code/console"
SOURCE = "kimi_code_web_oauth_usage"
# Cold CLI startup and OAuth refresh precede the native 8s usage fetch. This
# interactive deadline still bounds long provider refresh/lock contention.
DEFAULT_TIMEOUT = 30
_LIMIT = 64 * 1024
_BANNER_URL = re.compile(rb"http://127\.0\.0\.1:(\d+)/?#token=([A-Za-z0-9._~%+/=-]{16,512})(?=[\s\x1b])")
_AUTH = "/api/v1/auth"
_USAGE = "/api/v1/oauth/usage?provider=managed%3Akimi-code"
_PROFILE = "/api/v1/oauth/userinfo?provider=managed%3Akimi-code"


class UsageError(RuntimeError):
    def __init__(self, reason="provider_usage_unavailable", *, retryable=False):
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def _status_error(status):
    if status in (401, 403):
        return UsageError("provider_usage_auth_unconfirmed")
    if status == 429:
        return UsageError("provider_usage_rate_limited")
    if type(status) is int and 500 <= status <= 599:
        return UsageError("provider_usage_temporarily_unavailable", retryable=True)
    return UsageError()


def _reset(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = parsed.timestamp() if parsed.tzinfo is not None else None
        return stamp if stamp is not None and 0 <= stamp <= 32503680000 else None
    except (ValueError, OverflowError, OSError):
        return None


def _window(name, used, limit, minutes, reset):
    if (type(used) not in (int, float) or type(limit) not in (int, float)
            or not math.isfinite(used) or not math.isfinite(limit) or limit <= 0 or not 0 <= used <= limit):
        return None
    percent = used / limit * 100
    return {"name": name, "used_percent": percent, "remaining_percent": 100 - percent,
            "window_minutes": minutes, "resets_at": _reset(reset)}


def parse_quota(data):
    """Project actual provider windows; absent/invalid counters never become 0."""
    windows = []
    if isinstance(data, dict) and data.get("kind") == "ok":
        # Installed 0.34.0 returns normalized limits, not the newer quota.usages.
        limits = data.get("limits")
        if isinstance(limits, list):
            for row in limits[:16]:
                if not isinstance(row, dict) or not isinstance(row.get("window"), dict):
                    continue
                duration, unit = row["window"].get("duration"), row["window"].get("unit")
                if type(duration) is not int or not 1 <= duration <= 366 or unit not in {"minute", "hour", "day"}:
                    continue
                minutes = duration * {"minute": 1, "hour": 60, "day": 1440}[unit]
                suffix = {"minute": "m", "hour": "h", "day": "d"}[unit]
                window = _window("kimi:limit" + str(duration) + suffix, row.get("used"), row.get("limit"), minutes, row.get("reset_at"))
                if window:
                    windows.append(window)
        summary = data.get("summary")
        if isinstance(summary, dict):
            window = _window("kimi:summary", summary.get("used"), summary.get("limit"), None, summary.get("reset_at"))
            if window:
                windows.append(window)
        quota = data.get("quota")
        usages = quota.get("usages") if isinstance(quota, dict) else None
        if isinstance(usages, dict):
            for name, minutes in (("limit5h", 300), ("limit7d", 10080), ("monthTotal", None), ("monthCode", None)):
                row = usages.get(name)
                if isinstance(row, dict):
                    window = _window("kimi:" + name, row.get("usedRatio"), 1, minutes, row.get("resetAt"))
                    if window:
                        windows.append(window)
    # Never expose extra-usage money, opaque account IDs or provider messages.
    unique = {window["name"]: window for window in windows}
    return {"state": "available" if unique else "unavailable", "windows": list(unique.values()),
            "source": SOURCE, "scope": "provider_account", "usage_url": USAGE_URL,
            "reason": None if unique else "provider_usage_unavailable"}


class _OwnedConnection(http.client.HTTPConnection):
    """Keep the read socket abortable even after HTTP/1.0 detaches it."""
    def __init__(self, port, timeout):
        super().__init__("127.0.0.1", port, timeout=timeout)
        self._aborted = threading.Event()
        self._read_socket = None

    def connect(self):
        read_timeout = self.timeout
        self.timeout = min(read_timeout, 2)
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        self._read_socket = self.sock
        self.sock.settimeout(read_timeout)
        if self._aborted.is_set():
            self.abort()
            raise UsageError("provider_usage_transport_error")

    def abort(self):
        self._aborted.set()
        if self._read_socket is not None:
            try:
                self._read_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _get_json(port, token, path, timeout, *, connection=None):
    connection = connection or _OwnedConnection(port, timeout)
    response = None
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        body = response.read(_LIMIT + 1)
        if response.status == 404:
            raise UsageError("provider_quota_not_supported")
        if response.status != 200:
            raise _status_error(response.status)
        if len(body) > _LIMIT:
            raise UsageError("provider_usage_invalid_response")
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError):
            raise UsageError("provider_usage_invalid_response") from None
        if not isinstance(envelope, dict) or envelope.get("code") != 0 or not isinstance(envelope.get("data"), dict):
            raise UsageError("provider_usage_invalid_response")
        return envelope["data"]
    except (TimeoutError, socket.timeout):
        raise UsageError("account_check_timeout", retryable=True) from None
    except (OSError, http.client.HTTPException):
        raise UsageError("provider_usage_transport_error", retryable=True) from None
    finally:
        if response is not None:
            response.close()
        connection.close()


async def _owned_outcome(operation):
    # Python 3.14 logs exceptions from a cancelled shield even if joined later.
    # Carry the owned operation's error back to its caller as an outcome.
    try:
        return await operation, None
    except Exception as error:
        return None, error


async def _read_json(port, token, path, timeout):
    # Retry only this idempotent local read, once, within account_status's
    # overall deadline. Never retry auth refusal, throttling or malformed data.
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection = _OwnedConnection(port, remaining)
        task = asyncio.create_task(_owned_outcome(asyncio.to_thread(
            _get_json, port, token, path, remaining, connection=connection)))
        try:
            data, error = await asyncio.shield(task)
            if error is not None:
                raise error
            if path == _USAGE:
                if data.get("kind") == "error":
                    raise _status_error(data.get("status"))
                if data.get("kind") != "ok":
                    raise UsageError("provider_usage_invalid_response")
            return data
        except asyncio.CancelledError:
            connection.abort()
            # Cancellation of to_thread does not stop the socket read. Join it
            # before destroying its owned server, including repeated cancels.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
        except UsageError as error:
            if not error.retryable or attempt:
                raise
            await asyncio.sleep(.15)


async def _access_url(process, port):
    found = asyncio.get_running_loop().create_future()
    total = 0

    async def read(stream):
        nonlocal total
        buffer = b""
        while chunk := await stream.read(4096):
            total += len(chunk)
            if total > _LIMIT:
                if not found.done():
                    found.set_exception(UsageError())
                return
            buffer = (buffer + chunk)[-_LIMIT:]
            match = _BANNER_URL.search(buffer)
            if match and not found.done():
                token = unquote(match[2].decode("ascii"))
                if int(match[1]) != port or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,512}", token):
                    found.set_exception(UsageError())
                else:
                    found.set_result(token)

    readers = [asyncio.create_task(read(stream)) for stream in (process.stdout, process.stderr)]
    complete = asyncio.gather(*readers, return_exceptions=True)
    try:
        await asyncio.wait([found, complete], return_when=asyncio.FIRST_COMPLETED)
        if not found.done():
            raise UsageError()
        return found.result()
    finally:
        for reader in readers:
            reader.cancel()
        await complete


def _managed(auth):
    provider = auth.get("managed_provider")
    return isinstance(provider, dict) and provider.get("name") == "managed:kimi-code" and provider.get("status") == "authenticated"


def _display_identity(user):
    """Public account hints only; the complete phone never leaves this parser."""
    display = {}
    nickname = user.get("nickname")
    if isinstance(nickname, str) and nickname.strip() and len(nickname) <= 100 and nickname.isprintable():
        display["nickname"] = nickname.strip()
    phone = user.get("phone")
    if isinstance(phone, dict):
        number, country = phone.get("number"), phone.get("countryCode")
        country = str(country).lstrip("+") if type(country) in (str, int) else ""
        if (isinstance(number, str) and re.fullmatch(r"[+0-9* ()-]{7,40}", number)
                and re.fullmatch(r"[0-9]{1,4}", country)):
            digits = re.sub(r"[^0-9]", "", number)
            if 7 <= len(digits) <= 20 or 4 <= len(digits) < 7 and "*" in number:
                display["masked_phone"] = "+" + country + " **** " + digits[-4:]
    return display


def _profile(data):
    """Keep only display fields; the opaque identity is compared in memory."""
    from .adapters import _account_email
    user = data.get("userInfo") if isinstance(data, dict) and data.get("kind") == "ok" else None
    identity = user.get("userId") if isinstance(user, dict) else None
    if not isinstance(identity, str) or not 1 <= len(identity) <= 256 or not identity.isprintable():
        return None
    plan = user.get("userLevelName")
    if not isinstance(plan, str) or not plan.strip() or len(plan) > 80 or not plan.isprintable():
        plan = None
    return {"identity": identity, "email": _account_email(user.get("email")), "plan": plan,
            "display_identity": _display_identity(user)}


async def account_status(cwd, *, timeout=DEFAULT_TIMEOUT):
    from . import adapters
    from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
    process, owned = None, {}
    quota = {**parse_quota(None)}
    authenticated = False
    profile = None
    field_reasons = {"email": "profile_query_unavailable", "plan": "profile_query_unavailable"}
    reason = "auth_unconfirmed"
    started = time.monotonic()
    deadline = started + timeout
    phase = "startup"
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value
    async def read_profile(port, token):
        try:
            return _profile(await _read_json(port, token, _PROFILE, remaining()))
        except (UsageError, OSError, ValueError, RuntimeError, http.client.HTTPException):
            # Profile failure does not turn a separately valid quota into zero.
            return None
    # A neutral cwd avoids loading a user's project while inspecting an account.
    with tempfile.TemporaryDirectory(prefix="beanest-kimi-usage-") as directory:
        try:
            async with asyncio.timeout(timeout):
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                launch = asyncio.create_task(_owned_outcome(asyncio.create_subprocess_exec(adapters.find_kimi(), "web", "--no-open", "--host", "127.0.0.1", "--port", str(port),
                    cwd=directory, env=adapters._environment(), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=_LIMIT + 1)))
                try:
                    process, launch_error = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    async def finish_launch():
                        return await launch
                    (process, _), _ = await _settle_cleanup(finish_launch())
                    raise
                if launch_error is not None:
                    raise launch_error
                _remember(process.pid, owned, await asyncio.to_thread(_processes))
                phase = "banner"
                token = await _access_url(process, port)
                phase = "auth"
                first = await _read_json(port, token, _AUTH, remaining())
                authenticated = _managed(first)
                if authenticated:
                    reason = "provider_account_details_not_exposed"
                    phase = "profile"
                    first_profile = await read_profile(port, token)
                    phase = "usage"
                    data, usage_error = None, None
                    try:
                        data = await _read_json(port, token, _USAGE, remaining())
                    except UsageError as error:
                        usage_error = error.reason
                    phase = "second_profile"
                    second_profile = await read_profile(port, token)
                    phase = "second_auth"
                    second = await _read_json(port, token, _AUTH, remaining())
                    authenticated = _managed(second)
                    quota = parse_quota(data) if authenticated else parse_quota(None)
                    if authenticated and quota["state"] != "available":
                        quota["reason"] = usage_error or "provider_usage_invalid_response"
                    if usage_error:
                        phase = "usage"
                    reason = "provider_account_details_not_exposed" if authenticated else "auth_unconfirmed"
                    if authenticated and first_profile and second_profile:
                        if first_profile["identity"] != second_profile["identity"]:
                            quota = {**parse_quota(None), "reason": "account_changed"}
                            reason = "account_changed"
                            field_reasons = {"email": "account_changed", "plan": "account_changed"}
                        else:
                            profile = second_profile
                            field_reasons = {key: None if profile[key] else "provider_not_provided" for key in ("email", "plan")}
                            reason = None if all(profile[key] for key in ("email", "plan")) else "provider_account_details_not_exposed"
        except UsageError as error:
            quota.update(reason=error.reason)
        except TimeoutError:
            quota.update(reason="account_check_timeout")
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            quota.update(reason="provider_usage_unavailable")
        finally:
            async def cleanup():
                if process is None:
                    return True
                # The web server drains its local HTTP connections on SIGTERM;
                # give that normal shutdown a chance before fallback cleanup.
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(asyncio.shield(process.wait()), 3)
                    except asyncio.TimeoutError:
                        pass
                return await _cleanup(process, owned)
            clean, interrupted = await _settle_cleanup(cleanup())
            if interrupted:
                raise asyncio.CancelledError()
            if not clean:
                raise adapters.AccountTelemetryError("account_cleanup_unconfirmed")
    return {"state": ("ready" if quota["state"] == "available" else "partial") if authenticated else "unavailable", "email": profile["email"] if profile else None, "plan": profile["plan"] if profile else None,
            "field_reasons": field_reasons, "display_identity": profile["display_identity"] if profile else {},
            "auth_method": "kimi-code" if authenticated else None, "source": "kimi_managed_oauth",
            "scope": "default_cli_login", "reason": reason, "quota": {**quota,
                "diagnostics": {"phase": "complete" if quota["state"] == "available" else phase,
                    "elapsed_seconds": round(time.monotonic() - started, 3), "reason": quota["reason"]}}}
