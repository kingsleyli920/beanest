"""Bounded, subscription-only CLI adapters for the communication prototype.

Agents produce text; the coordinator owns all file writes. No shell is used to
launch the CLIs and no provider API credentials are inherited.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import weakref

from .message_capacity import MAX_PROMPT_BYTES

MAX_OUTPUT = 32 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
CODEX_APP = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')
CLAUDE_SYSTEM_PATHS = (Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude'))
# DeepSeek Harness installs from npm; the global prefixes Homebrew's and the system Node use.
DSH_SYSTEM_PATHS = (Path('/opt/homebrew/bin/dsh'), Path('/usr/local/bin/dsh'))
CLAUDE_SUBSCRIPTIONS = ('pro', 'max', 'ultra', 'team', 'enterprise')
CODE_MODE_DISABLED_NOTICE = (
    'Code Mode is unavailable because code-mode host is disabled. '
    'Code mode will fail closed; enable `features.code_mode_host` and '
    'install `codex-code-mode-host`.'
)
# Whole official notices only, built from observed Claude Code wording:
# session/weekly/N-hour windows, dated resets, any zone, spend limits and
# usage credits. Appended prose, an unobserved window name or "temporarily
# limiting requests (not your usage limit)" never match.
_CLAUDE_RESET = (r"resets (?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?:[1-9]|[12][0-9]|3[01]) at )?"
                 r"(?:[1-9]|1[0-2])(?::[0-5][0-9])?(?:am|pm)(?: \([A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}\))?")
_CLAUDE_WINDOW = r"(?:session|weekly|[1-9][0-9]?-hour)"
_CLAUDE_SUBSCRIPTION_LIMIT = re.compile(
    r"You've hit your " + _CLAUDE_WINDOW + r" limit · " + _CLAUDE_RESET
    + r"|You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit · your "
    + _CLAUDE_WINDOW + r" limit " + _CLAUDE_RESET
    + r"|You're out of usage credits\. (?:Switch to another model, or manage usage credits at "
      r"claude\.ai/settings/usage(?:\?from=[a-z_]{1,40})?, to continue\."
      r"|Run /usage-credits to keep using [A-Za-z0-9 .-]{1,40} or /model to switch models\.)"
)
_CODEX_DISABLED_FEATURES = ('shell_tool', 'apps', 'plugins', 'multi_agent', 'multi_agent_v2',
    'browser_use', 'browser_use_external', 'in_app_browser', 'computer_use', 'image_generation',
    'view_image', 'hooks', 'memories', 'skill_search', 'workspace_dependencies', 'code_mode',
    'code_mode_host', 'remote_plugin', 'sleep_tool')


def _find_cli(command: str, fallbacks: tuple[Path, ...]) -> str | None:
    """Locate an executable without running it or reading provider data."""
    on_path = shutil.which(command)
    candidates = ((Path(on_path),) if on_path else ()) + fallbacks
    for path in candidates:
        # is_file follows ordinary installation symlinks, while excluding
        # executable directories. Both PATH and fixed locations use this check.
        try:
            if path.is_file() and os.access(path, os.X_OK):
                # PATH may contain relative entries; execution uses a different
                # working directory, so preserve the discovered absolute path.
                return str(path.absolute())
        except OSError:
            # An inaccessible stale installation must not hide a later usable one.
            continue
    return None


def _install_locations(agent):
    return {'codex': (Path.home() / '.local/bin/codex', CODEX_APP),
            'claude': (Path.home() / '.local/bin/claude', *CLAUDE_SYSTEM_PATHS),
            'kimi': (Path.home() / '.kimi-code/bin/kimi',),
            'dsh': (*DSH_SYSTEM_PATHS, Path.home() / '.npm-global/bin/dsh')}[agent]


def cli_installations(agent) -> list[str]:
    """Every distinct executable installation, PATH first; nothing is executed."""
    on_path = shutil.which(agent)
    found, seen = [], set()
    for path in ((Path(on_path),) if on_path else ()) + _install_locations(agent):
        try:
            if path.is_file() and os.access(path, os.X_OK) and os.path.realpath(path) not in seen:
                seen.add(os.path.realpath(path))
                found.append(str(path.absolute()))
        except OSError:
            # An inaccessible stale installation must not hide a later usable one.
            continue
    return found


# One verified installation per tool is shared by checks, telemetry and
# inference, so a quota read never describes a different CLI from the one
# that runs. A stale copy earlier on PATH (an old nvm global, a second
# Homebrew install) is skipped and reported instead of silently used.
_SELECTIONS = {agent: {'path': None, 'skipped': [], 'checked': False} for agent in ('codex', 'claude', 'kimi', 'dsh')}
_SELECTION_LOCKS = weakref.WeakKeyDictionary()


def _selected_cli(agent, missing):
    installed = cli_installations(agent)
    if _SELECTIONS[agent]['path'] in installed:
        return _SELECTIONS[agent]['path']
    if installed:
        return installed[0]
    raise RuntimeError(missing)


def find_codex() -> str:
    return _selected_cli('codex', 'Codex CLI is unavailable')


def find_claude() -> str:
    return _selected_cli('claude', 'Claude Code CLI is unavailable')


def find_kimi() -> str:
    return _selected_cli('kimi', 'Kimi Code CLI is unavailable')


def find_dsh() -> str:
    return _selected_cli('dsh', 'DeepSeek Harness is unavailable')


def cli_selection(agent) -> dict:
    """The last verified choice and why other installations were skipped."""
    selection = _SELECTIONS[agent]
    return {'path': selection['path'], 'checked': selection['checked'],
            'skipped': [dict(item) for item in selection['skipped']]}


async def _probe_installation(agent, path, cwd, timeout):
    """Return (authentication or None, skip reason or None) for one installation."""
    command = {'codex': [path, 'login', 'status'],
               'claude': [path, '--safe-mode', '--restricted', 'auth', 'status', '--json'],
               'kimi': [path, '--version'], 'dsh': [path, '--version']}[agent]
    lines, code = await _run(command, '', cwd, timeout, include_stderr=True)
    text = b'\n'.join(lines)[:65536].decode('utf-8', errors='replace')
    if agent in ('kimi', 'dsh'):
        # Kimi login is one shared managed configuration and dsh has no login
        # (each partner brings its own key); a copy only has to run.
        usable = code == 0 and re.search(r'[0-9]+\.[0-9]+', text) is not None
        return ({'available': True} if usable else None), (None if usable else 'cli_unusable')
    authentication = _authentication(agent, code, text)
    if authentication['subscription_confirmed']:
        return authentication, None
    if 'Error loading configuration' in text:
        return None, 'config_unreadable'
    if re.search(r"unknown option|unexpected argument|unrecognized (?:option|argument)", text, re.IGNORECASE):
        return None, 'cli_outdated'
    try:
        json.loads(text) if agent == 'claude' else None
    except ValueError:
        return None, 'cli_unusable'
    return None, 'login_unconfirmed'


def _selection_applies(agent):
    """Arbitrate only between discovered copies; a substituted executable stays authoritative."""
    if _ACCOUNT_HOME.get() is not None:
        # Selection confirms the local login. A profile home that is not yet
        # signed in must not deselect the binary every other profile uses.
        return False
    installed = cli_installations(agent)
    try:
        current = globals()['find_' + agent]()
    except RuntimeError:
        return False
    return len(installed) > 1 and current in installed


async def select_cli(agent, cwd, *, timeout=10) -> dict:
    """Use the first installation that runs and confirms the existing login."""
    if not _selection_applies(agent):
        return {'confirmed': None, 'authentication': None, **cli_selection(agent)}
    lock = _SELECTION_LOCKS.setdefault(asyncio.get_running_loop(), {}).setdefault(agent, asyncio.Lock())
    async with lock:
        installed = cli_installations(agent)
        ordered = sorted(installed, key=lambda path: path != _SELECTIONS[agent]['path'])
        skipped, chosen, authentication = [], None, None
        for path in ordered:
            try:
                authentication, reason = await _probe_installation(agent, path, cwd, timeout)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                authentication, reason = None, 'login_check_failed'
            if reason is None:
                chosen = path
                break
            skipped.append({'path': path, 'reason': reason, 'version': None})
        for item in skipped:
            try:
                lines, code = await _run([item['path'], '--version'], '', cwd, timeout, include_stderr=True)
                match = re.search(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?', b'\n'.join(lines)[:4096].decode('utf-8', 'replace'))
                item['version'] = match.group(0) if code == 0 and match else None
            except (OSError, RuntimeError, asyncio.TimeoutError):
                pass
        _SELECTIONS[agent].update(path=chosen, checked=True, skipped=skipped)
        return {'confirmed': chosen is not None, 'authentication': authentication if chosen else None, **cli_selection(agent)}


async def _ensure_cli(agent, cwd, timeout=10):
    if not _SELECTIONS[agent]['checked'] and _selection_applies(agent):
        await select_cli(agent, cwd, timeout=timeout)


# The user's own network path to the provider: an outbound proxy or a
# corporate CA. These neither select an account nor reroute the provider API.
_NETWORK_ENVIRONMENT = frozenset({
    'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
})


# A partner profile may own a Beanest-created CLI home instead of the shared
# local login. Only that explicit binding selects a home: the user's own shell
# CLAUDE_CONFIG_DIR/CODEX_HOME/KIMI_CODE_HOME are still stripped below.
ACCOUNT_HOME_VARIABLES = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME', 'kimi': 'KIMI_CODE_HOME',
                          'dsh': 'DSH_HOME'}
_DEFAULT_HOME_NAMES = {'claude': '.claude', 'codex': '.codex', 'kimi': '.kimi-code', 'dsh': '.dsh'}
_ACCOUNT_HOME = contextvars.ContextVar('beanest_account_home', default=None)
_API_ENDPOINT = contextvars.ContextVar('beanest_api_endpoint', default=None)


@dataclass(frozen=True)
class ApiEndpoint:
    """Where an API model partner's key lives and which models it prices.

    Holds a reference to the key, never the key: the value is read only when
    a child is about to start, and only that child's environment receives it.
    """
    host: str
    key: tuple  # sorted (name, value) pairs of the key reference
    default_model: str
    models: tuple = ()

    def key_reference(self) -> dict:
        return dict(self.key)


class ApiKeyUnavailable(RuntimeError):
    """The referenced key could not be read; carries no path and no value."""


@contextmanager
def account_home(agent, home, endpoint: ApiEndpoint | None = None):
    """Bind one profile's CLI home for every child started in this context.

    ``None`` keeps the local login. Tasks created inside inherit the binding,
    so a whole invocation, check or cleanup sees one consistent home. An API
    model partner always brings its own home together with its key reference.
    """
    if home is None:
        value = None
    else:
        if agent not in ACCOUNT_HOME_VARIABLES or not Path(home).is_absolute():
            raise ValueError('account home must be an absolute path for a supported CLI')
        value = (agent, str(Path(home)))
    if endpoint is not None and (not isinstance(endpoint, ApiEndpoint) or endpoint.host != agent or value is None):
        raise ValueError('an API endpoint needs its own home on its host tool')
    token = _ACCOUNT_HOME.set(value)
    endpoint_token = _API_ENDPOINT.set(endpoint)
    try:
        yield
    finally:
        _API_ENDPOINT.reset(endpoint_token)
        _ACCOUNT_HOME.reset(token)


def bound_api_endpoint(agent) -> ApiEndpoint | None:
    endpoint = _API_ENDPOINT.get()
    return endpoint if endpoint is not None and endpoint.host == agent else None


def api_key_environment(agent) -> dict[str, str]:
    """The one variable that carries the bound partner's key; {} when none is bound.

    Reads the key now. Raises ApiKeyUnavailable without saying why in detail.
    """
    endpoint = bound_api_endpoint(agent)
    if endpoint is None:
        return {}
    from .api_models import ApiModelError, HOSTS, read_key
    try:
        key = read_key(endpoint.key_reference())
    except ApiModelError:
        raise ApiKeyUnavailable('api_key_unavailable') from None
    return {HOSTS[endpoint.host]['key_variable']: key}


REDACTED = '[REDACTED]'


def scrub_secret(value, secret):
    """Drop a key from anything a tool returned before it is parsed, stored or shown.

    A model with shell tools can print its own environment; whatever it
    prints must not carry the key into receipts, events or the room. Works on
    text, bytes and nested JSON values.
    """
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, REDACTED)
    if isinstance(value, bytes):
        return value.replace(secret.encode('utf-8'), REDACTED.encode())
    if isinstance(value, list):
        return [scrub_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {scrub_secret(key, secret): scrub_secret(item, secret) for key, item in value.items()}
    return value


def bound_account_home(agent) -> Path | None:
    value = _ACCOUNT_HOME.get()
    return Path(value[1]) if value is not None and value[0] == agent else None


def native_home(agent, home=None) -> Path:
    """Where this CLI keeps login, sessions and config for the bound profile."""
    if home is not None:
        return Path(home)
    return bound_account_home(agent) or Path.home() / _DEFAULT_HOME_NAMES[agent]


def _environment() -> dict[str, str]:
    # Keep OS essentials, not arbitrary agent/provider overrides. In particular,
    # ANTHROPIC_API_KEY, OPENAI_API_KEY, OAuth-token overrides, provider routing
    # such as *_BASE_URL and cloud credentials never reach the child.
    allowed = {'HOME', 'PATH', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL',
               'USER', 'LOGNAME', 'SHELL', 'SYSTEMROOT'} | _NETWORK_ENVIRONMENT
    environment = {k: v for k, v in os.environ.items() if k in allowed}
    bound = _ACCOUNT_HOME.get()
    if bound is not None:
        environment[ACCOUNT_HOME_VARIABLES[bound[0]]] = bound[1]
    return environment


def _status(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, env=_environment(), capture_output=True,
                                timeout=15, check=False)
        output = result.stdout + result.stderr
        if len(output) > 64 * 1024:
            return -1, ''
        return result.returncode, output.decode('utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return -1, ''


def _authentication(agent: str, code: int, text: str) -> dict:
    if agent == 'codex':
        subscribed = code == 0 and 'Logged in using ChatGPT' in text
        return {'available': True, 'authMethod': 'chatgpt' if subscribed else 'unconfirmed',
                'subscription_confirmed': subscribed}
    try:
        raw = json.loads(text) if code == 0 else {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    safe = {k: raw[k] for k in ('loggedIn', 'authMethod', 'apiProvider', 'subscriptionType')
            if k in raw and isinstance(raw[k], (str, bool))}
    safe['available'] = True
    safe['subscription_confirmed'] = (raw.get('loggedIn') is True
        and raw.get('authMethod') == 'claude.ai'
        and raw.get('apiProvider') == 'firstParty'
        and raw.get('subscriptionType') in CLAUDE_SUBSCRIPTIONS)
    return safe


def doctor() -> dict:
    """No inference. Return selected nonidentifying authentication fields only."""
    result = {'codex': {'available': False, 'subscription_confirmed': False},
              'claude': {'available': False, 'subscription_confirmed': False}}
    try:
        codex = find_codex()
    except RuntimeError:
        codex = None
    if codex:
        code, text = _status([codex, 'login', 'status'])
        result['codex'] = _authentication('codex', code, text)
    try:
        claude = find_claude()
    except RuntimeError:
        claude = None
    if claude:
        code, text = _status([claude, '--safe-mode', '--restricted', 'auth', 'status'])
        result['claude'] = _authentication('claude', code, text)
    result['ready'] = all(result[agent]['subscription_confirmed'] for agent in ('codex', 'claude'))
    return result


def _command(agent: str, session_id: str | None, *, schema_json: str | None = None,
             schema_path: str | None = None, runtime_settings: dict | None = None) -> list[str]:
    choice = runtime_settings or {}
    if (set(choice) - {'model', 'effort'} or any(value is not None and not isinstance(value, str)
                                               for value in choice.values())):
        raise ValueError('invalid runtime settings')
    if session_id is not None:
        # Never allow implicit last-session selection or names that resolve to
        # somebody else's existing conversation. Coordinator checks ownership.
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError('session_id must be an explicit UUID') from exc
    if agent == 'codex':
        command = [find_codex(), 'exec', '--json', '--ignore-user-config',
                   '--ignore-rules', '--skip-git-repo-check', '-s', 'read-only',
                   '-c', 'forced_login_method="chatgpt"',
                   '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                   '-c', 'mcp_servers={}', '-c', 'model_provider="openai"',
                   '-c', 'tools.update_plan.enabled=false',
                   '-c', 'tools.experimental_request_user_input.enabled=false']
        for feature in _CODEX_DISABLED_FEATURES:
            command += ['--disable', feature]
        if schema_json is not None:
            if schema_path is None:
                raise ValueError('Codex response schema requires a private file')
            command += ['--output-schema', schema_path]
        if session_id:
            command += ['resume', session_id]
        if choice.get('model') is not None:
            command += ['--model', choice['model']]
        if choice.get('effort') is not None:
            command += ['-c', 'model_reasoning_effort=' + json.dumps(choice['effort'])]
        return command + ['-']
    if agent == 'claude':
        command = [find_claude(), '--safe-mode', '--restricted', '--tools', '',
                   '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                   '--disable-slash-commands', '--no-chrome',
                   '--settings', '{"apiKeyHelper":""}',
                   '-p', '--output-format', 'stream-json', '--verbose',
                   '--permission-prompts', 'none']
        if schema_json is not None:
            command += ['--json-schema', schema_json]
        if session_id:
            command += ['--resume', session_id]
        if choice.get('model') is not None:
            command += ['--model', choice['model']]
        if choice.get('effort') is not None:
            command += ['--effort', choice['effort']]
        return command
    raise ValueError('agent must be codex or claude')


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    keys = {'input_tokens', 'output_tokens', 'cached_input_tokens',
            'cache_read_input_tokens', 'cache_creation_input_tokens'}
    return {k: v for k, v in value.items()
            if k in keys and isinstance(v, int) and not isinstance(v, bool) and v >= 0}


# Codex diagnostics that accompany a turn which still completed.
CODEX_NOTICES = frozenset({'code_mode_disabled_notice', 'ignored_legacy_config_notice', 'model_resume_notice'})


def _provider_error_code(message: object) -> str:
    """Classify in memory, never expose provider messages or embedded URLs."""
    if not isinstance(message, str):
        return 'unknown_provider_error'
    if message == CODE_MODE_DISABLED_NOTICE:
        return 'code_mode_disabled_notice'
    # Exact installed-CLI diagnostic observed while inheriting a user's config.
    # Only this deprecated key is classified; unknown settings/errors stay fatal.
    if re.fullmatch(
            r'Codex is ignoring 1 unrecognized configuration setting\. '
            r'Check for typos or deprecated settings\.\n'
            r'  user \(/[a-zA-Z0-9_ ./()@+-]{1,4096}/\.codex/config\.toml\): '
            r'`features\.rmcp_client` is ignored\.', message):
        return 'ignored_legacy_config_notice'
    # Observed on resume after Codex's default model changed between calls (a
    # partner left on its tool default). The turn itself completes normally.
    if re.fullmatch(
            r'This session was recorded with model `[A-Za-z0-9._:/-]{1,128}` but is resuming with '
            r'`[A-Za-z0-9._:/-]{1,128}`\. Consider switching back to `[A-Za-z0-9._:/-]{1,128}` '
            r'as it may affect Codex performance\.', message):
        return 'model_resume_notice'
    # Add recovery rules only after observing and verifying an exact CLI
    # diagnostic. A completed turn alone does not make an unknown error safe.
    return 'unknown_provider_error'


def _claude_native_error_code(event: dict, session_id: str | None = None) -> str:
    """Classify the observed native quota notice without retaining its text.

    This shape was observed in an owned native transcript. Live stream-json
    delivery of these fields is a separate integration boundary, not inferred.
    """
    message = event.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if (event.get('parent_tool_use_id') is not None or event.get('parentToolUseID') is not None
            or event.get('isSidechain') not in (None, False)
            or isinstance(message, dict) and message.get('role', 'assistant') != 'assistant'):
        return 'unknown_provider_error'
    for key in ('session_id', 'sessionId'):
        if key in event and (session_id is None or event[key] != session_id):
            return 'unknown_provider_error'
    # The CLI also emits a terminal-only refusal: success names the native
    # transport envelope, while is_error marks the provider refusal. Restrict
    # this fallback to the entire official notice, never arbitrary result text.
    if (event.get('type') == 'result' and event.get('subtype') == 'success'
            and event.get('is_error') is True and isinstance(event.get('result'), str)
            and len(event['result']) <= 512 and _CLAUDE_SUBSCRIPTION_LIMIT.fullmatch(event['result'])):
        return 'subscription_limit_reached'
    # Native provider metadata, unlike model prose or tool output, identifies
    # the subscription window without depending on translated reset wording.
    explicit_limit = (event.get('apiError') == 'usage_limit_reached'
                      and event.get('error') == 'rate_limit')
    assistant_error = (event.get('type') == 'assistant' and event.get('isApiErrorMessage') is True
                       and isinstance(content, list) and len(content) == 1
                       and isinstance(content[0], dict) and content[0].get('type') == 'text'
                       and isinstance(content[0].get('text'), str))
    if explicit_limit and (assistant_error
                          or event.get('type') == 'result' and event.get('is_error') is True):
        return 'subscription_limit_reached'
    if (event.get('isApiErrorMessage') is True and event.get('error') == 'rate_limit'
            and isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and content[0].get('type') == 'text'):
        text = content[0].get('text')
        if isinstance(text, str) and len(text) <= 512 and _CLAUDE_SUBSCRIPTION_LIMIT.fullmatch(text):
            return 'subscription_limit_reached'
    return 'unknown_provider_error'


def _strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value
    def invalid_constant(_value):
        raise ValueError('nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def parse_response_object(text):
    """Read one response object, allowing unambiguous presentation around it.

    Transport records still use _strict_json. Response prose may precede an
    object only on separate lines. Balanced inline non-JSON bracket labels are
    prose; JSON-looking brackets, extra values and trailing prose stay ambiguous.
    """
    if not isinstance(text, str):
        raise ValueError("response must be text")
    candidate = text.strip()
    lines = candidate.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines)
                  if line.lstrip().startswith("{") or re.fullmatch(r"```(?:json)?[ \t]*", line.strip(), re.IGNORECASE)), None)
    if start is not None and start > 0:
        prefix = "".join(lines[:start])
        if (re.search(r'[{}]|```|(?m:^\s*\[)', prefix)
                or re.search(r'\[\s*(?:["\d\-\]]|true\b|false\b|null\b|NaN\b|Infinity\b)', prefix)):
            raise ValueError("ambiguous response prefix")
        depth = 0
        for character in prefix:
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth < 0:
                    raise ValueError("ambiguous response prefix")
        if depth:
            raise ValueError("ambiguous response prefix")
        for line in prefix.splitlines():
            if not line.strip():
                continue
            try:
                _strict_json(line.strip())
            except ValueError:
                continue
            raise ValueError("JSON value before response object")
        candidate = "".join(lines[start:])
    wrapped = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*)\r?\n```", candidate, re.IGNORECASE)
    if wrapped is not None:
        candidate = wrapped[1]
    value = _strict_json(candidate)
    if not isinstance(value, dict):
        raise ValueError("response must be one JSON object")
    return value


def _object_text(value):
    if not isinstance(value, dict):
        raise ValueError('structured response must be an object')
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        size = len(text.encode('utf-8'))
    except UnicodeError:
        raise ValueError('structured response must be valid UTF-8') from None
    if size > MAX_TEXT:
        raise ValueError('structured response exceeds size limit')
    return text


def parse_events(agent: str, lines: list[bytes], *, structured: bool = False,
                 allow_codex_nonterminal_diagnostics: bool = False) -> dict:
    """Whitelist protocol fields; never retain thinking, tool args or raw stderr."""
    result = {'session_id': None, 'text': '', 'usage': None, 'events': [], 'status': 'failed'}
    texts = []
    turn_failed = False
    structured_invalid = False
    claude_results = 0
    structured_value = None
    codex_diagnostics = []
    codex_terminals = 0
    codex_completed = 0
    codex_diagnostics_invalid = False
    codex_compatibility = agent == 'codex' and structured and allow_codex_nonterminal_diagnostics
    for line in lines:
        if not line.strip():
            continue
        try:
            event = _strict_json(line) if structured else json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            if structured:
                structured_invalid = True
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if agent == 'codex':
            if kind == 'thread.started':
                result['session_id'] = event.get('thread_id')
                result['events'].append({'type': kind})
            elif kind in ('item.started', 'item.completed') or codex_compatibility and kind == 'item.updated':
                item = event.get('item', {})
                if kind == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                    if not structured:
                        texts.append(item['text'])
                    elif item.get('phase') != 'commentary':
                        # Keep the final whole message, never concatenate a
                        # valid-looking fragment with earlier commentary.
                        texts = [item['text']]
                    result['events'].append({'type': 'assistant_text'})
                elif isinstance(item, dict) and item.get('type') == 'error':
                    # ErrorItem is distinct from a tool invocation. Preserve a
                    # classified signal without leaking provider diagnostics.
                    code = _provider_error_code(item.get('message'))
                    result['events'].append({'type': 'warning' if code in CODEX_NOTICES else 'provider_error',
                        'code': code})
                    if codex_compatibility:
                        codex_diagnostics.append(len(result['events']) - 1)
                        if (codex_terminals or not isinstance(item.get('message'), str)
                                or not item['message'].strip()):
                            codex_diagnostics_invalid = True
                elif isinstance(item, dict) and item.get('type') not in ('agent_message', 'reasoning'):
                    # Unknown/new item types fail the coordinator's no-tools
                    # check without retaining tool arguments or responses.
                    item_type = item.get('type')
                    # Protocol labels only: never copy arbitrary provider text.
                    known_types = {'command_execution', 'file_change', 'mcp_tool_call',
                                   'web_search', 'todo_list', 'error', 'collab_tool_call',
                                   'plan', 'context_compaction', 'image_generation',
                                   'tool_call', 'function_call'}
                    result['events'].append({'type': 'tool_observed',
                        'item_type': item_type if item_type in known_types else 'unknown'})
            elif kind == 'turn.completed':
                codex_terminals += 1
                codex_completed += 1
                result['usage'] = _usage(event.get('usage'))
                result['status'] = 'completed'
                result['events'].append({'type': kind})
            elif kind in ('turn.failed', 'error'):
                if kind == 'turn.failed':
                    codex_terminals += 1
                if kind != 'error' or not codex_compatibility:
                    turn_failed = True
                result['status'] = 'failed'
                result['events'].append({'type': kind})
                if kind == 'error' and codex_compatibility:
                    codex_diagnostics.append(len(result['events']) - 1)
                    if (codex_terminals or not isinstance(event.get('message'), str)
                            or not event['message'].strip()):
                        codex_diagnostics_invalid = True
        elif agent == 'claude':
            if kind == 'system' and event.get('subtype') == 'init':
                result['session_id'] = event.get('session_id')
                result['events'].append({'type': 'session.started'})
            elif kind == 'assistant':
                native_error = event.get('isApiErrorMessage') is True
                sidechain = (event.get('parent_tool_use_id') is not None or event.get('parentToolUseID') is not None
                             or event.get('isSidechain') not in (None, False))
                if native_error and sidechain:
                    # A subagent's API refusal is reported to its parent as a
                    # tool result; the main turn's own terminal decides.
                    result['events'].append({'type': 'warning', 'code': 'sidechain_provider_error'})
                elif native_error:
                    result['events'].append({'type': 'provider_error',
                        'code': _claude_native_error_code(event, result['session_id'])})
                message = event.get('message', {})
                if isinstance(message, dict):
                    if not native_error and not sidechain and _usage(message.get('usage')):
                        result['last_iteration_usage'] = _usage(message.get('usage'))
                    content = message.get('content')
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                            if not structured and not native_error:
                                texts.append(block['text'])
                            if not native_error:
                                result['events'].append({'type': 'assistant_text'})
                        elif (structured and isinstance(block, dict) and block.get('type') == 'tool_use'
                              and block.get('name') == 'StructuredOutput' and isinstance(block.get('input'), dict)):
                            # This exact native format tool was observed with
                            # --json-schema. Its input is not a response source.
                            result['events'].append({'type': 'structured_output_format'})
                        elif isinstance(block, dict) and block.get('type') not in ('thinking', 'redacted_thinking'):
                            result['events'].append({'type': 'tool_observed'})
            elif kind == 'result':
                claude_results += 1
                if event.get('is_error') is True:
                    native_code = _claude_native_error_code(event, result['session_id'])
                    if native_code != 'unknown_provider_error' or 'apiError' in event or 'error' in event:
                        # A label-free generic terminal must not overwrite an
                        # earlier typed API refusal or invent a provider code.
                        result['events'].append({'type': 'provider_error', 'code': native_code})
                result['session_id'] = event.get('session_id', result['session_id'])
                result['usage'] = _usage(event.get('usage'))
                result['status'] = 'completed' if event.get('subtype') == 'success' and not event.get('is_error') else 'failed'
                if structured:
                    structured_value = event.get('structured_output')
                elif not texts and isinstance(event.get('result'), str):
                    texts.append(event['result'])
                result['events'].append({'type': 'turn.' + result['status']})
    if structured:
        try:
            if (structured_invalid or result['status'] != 'completed' or turn_failed
                    or codex_compatibility and (codex_terminals != 1 or codex_completed != 1
                                               or codex_diagnostics_invalid)):
                raise ValueError('incomplete structured result')
            if agent == 'claude':
                if claude_results != 1:
                    raise ValueError('ambiguous structured result')
                value = structured_value
            else:
                if not texts:
                    raise ValueError('missing structured result')
                value = _strict_json(texts[-1])
                if codex_compatibility and codex_diagnostics and not value:
                    raise ValueError('empty structured result cannot resolve diagnostics')
            result['text'] = _object_text(value)
            if codex_compatibility:
                # The native caller enables this only after successful exit,
                # paired tools, identity and cleanup. Earlier diagnostics do
                # not override a unique valid success terminal; late errors do.
                for index in codex_diagnostics:
                    if result['events'][index].get('type') != 'warning':
                        result['events'][index] = {'type': 'warning', 'code': 'codex_nonterminal_diagnostic'}
        except (ValueError, TypeError, RecursionError):
            result.update(status='failed', text='', failure_code='protocol_turn_incomplete')
    else:
        result['text'] = '\n'.join(texts)[:MAX_TEXT]
    if turn_failed:
        result['status'] = 'failed'
    if any(event.get('code') == 'unknown_provider_error' for event in result['events']):
        result['status'] = 'failed'
        result['failure_code'] = 'unknown_provider_error'
    elif any(event.get('code') == 'subscription_limit_reached' for event in result['events']):
        result.update(status='failed', failure_code='subscription_limit_reached')
    if agent == 'claude' and any(event['type'] == 'provider_error' for event in result['events']):
        # Native API errors and result.result diagnostics are not chat content.
        result['text'] = ''
    if result['session_id'] is not None:
        try:
            uuid.UUID(result['session_id'])
        except (ValueError, TypeError, AttributeError):
            result['session_id'] = None
    return result


async def _terminate(process: asyncio.subprocess.Process) -> None:
    # Kill the process group even when the launcher already exited: descendants
    # may still own its stdout pipe. A fresh session prevents killing our host.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _run(command: list[str], prompt: str, cwd: Path, timeout: float, *,
               include_stderr: bool = False, max_output: int = MAX_OUTPUT,
               cleanup_metadata: dict | None = None) -> tuple[list[bytes], int]:
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=_environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    async def collect():
        size = 0
        async def read(stream):
            nonlocal size
            chunks = []
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > max_output:
                    raise RuntimeError('Agent output exceeded limit')
                chunks.append(chunk)
            return b''.join(chunks)
        async def write():
            try:
                process.stdin.write(prompt.encode('utf-8'))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
        stdout, stderr, _ = await asyncio.gather(read(process.stdout), read(process.stderr), write())
        if include_stderr:
            stdout += b'\n' + stderr
        return stdout.splitlines(), await process.wait()
    def stopped():
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return process.returncode is not None
        except PermissionError:
            pass
        return False
    try:
        result = await asyncio.wait_for(collect(), timeout=timeout)
        if cleanup_metadata is not None:
            cleanup_metadata['cleanup_confirmed'] = stopped()
        return result
    except BaseException as error:
        cleanup = asyncio.create_task(_terminate(process))
        # Repeated stop requests must not turn a detached cleanup task into a
        # false "cancelled" result while the owned process is still alive.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        error.cleanup_confirmed = stopped()
        raise


async def _subscription_status(agent: str, cwd: Path) -> dict:
    # Authentication checks need the same cancellable process ownership as inference.
    # A cancelled to_thread(doctor) left its synchronous status child running.
    cleanup, attempted = {}, False
    try:
        if _selection_applies(agent):
            attempted = True
            selection = await select_cli(agent, cwd, timeout=15)
            return {**(selection['authentication'] or {'available': True, 'authMethod': 'unconfirmed',
                                                       'subscription_confirmed': False}), 'cleanup_confirmed': True}
        command = ([find_codex(), 'login', 'status'] if agent == 'codex' else
                   [find_claude(), '--safe-mode', '--restricted', 'auth', 'status'])
        attempted = True
        lines, code = await _run(command, '', cwd, 15, include_stderr=True, cleanup_metadata=cleanup)
        raw = b'\n'.join(lines)
        if len(raw) > 64 * 1024:
            return {'subscription_confirmed': False, **cleanup}
        return {**_authentication(agent, code, raw.decode('utf-8', errors='replace')), **cleanup}
    except (OSError, RuntimeError, asyncio.TimeoutError) as error:
        return {'subscription_confirmed': False,
                'cleanup_confirmed': not attempted or getattr(error, 'cleanup_confirmed', False) is True}


class AccountTelemetryError(RuntimeError):
    """Stable failure code, never a raw provider payload or credential."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _account_email(value):
    if (isinstance(value, str) and len(value) <= 254
            and re.fullmatch(r"[^\s@<>()\[\]{}\"',:;\\]+@[^\s@<>()\[\]{}\"',:;\\]+\.[^\s@<>()\[\]{}\"',:;\\]+", value)
            and all(character.isprintable() for character in value)):
        return value
    return None


def _account_plan(value):
    plans = {'free', 'go', 'plus', 'pro', 'team', 'business', 'enterprise', 'edu', 'max', 'ultra'}
    return value.lower() if isinstance(value, str) and value.lower() in plans else None


def _quota_window(name, value):
    if not isinstance(value, dict):
        return None
    used = value.get('usedPercent')
    if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
        used = None
    minutes = value.get('windowDurationMins')
    if type(minutes) is not int or not 1 <= minutes <= 525600:
        minutes = None
    resets = value.get('resetsAt')
    if type(resets) not in (int, float) or not math.isfinite(resets) or not 0 <= resets <= 32503680000:
        resets = None
    return {'name': name, 'used_percent': used,
            'remaining_percent': 100 - used if used is not None else None,
            'window_minutes': minutes, 'resets_at': resets}


def _account_quota(raw):
    """Retain quota windows only; credits, opaque IDs and other fields stay out."""
    windows = []
    buckets = raw.get('rateLimitsByLimitId') if isinstance(raw, dict) else None
    if isinstance(buckets, dict) and buckets:
        values = sorted((key, value) for key, value in buckets.items()
                        if isinstance(key, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key))[:8]
    else:
        bucket = raw.get('rateLimits') if isinstance(raw, dict) else None
        values = [('codex', bucket)] if isinstance(bucket, dict) else []
    for name, bucket in values:
        if not isinstance(bucket, dict):
            continue
        for period in ('primary', 'secondary'):
            window = _quota_window(name + ':' + period, bucket.get(period))
            if window is not None:
                windows.append(window)
    available = any(window['used_percent'] is not None for window in windows)
    return {'state': 'available' if available else 'unavailable', 'windows': windows,
            'source': 'codex_app_server_account_rate_limits', 'scope': 'provider_account',
            'reason': None if available else 'provider_usage_unavailable'}


def _codex_identity(raw):
    account = raw.get('account') if isinstance(raw, dict) else None
    if not isinstance(account, dict) or account.get('type') != 'chatgpt':
        return {'email': None, 'plan': None, 'auth_method': None}
    return {'email': _account_email(account.get('email')), 'plan': _account_plan(account.get('planType')),
            'auth_method': 'chatgpt'}


async def _codex_account_rpc(command, cwd, *, timeout=10, models=False):
    """One private connection, limited to account reads or model metadata."""
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=_environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    size = 0

    def count(raw):
        nonlocal size
        size += len(raw)
        if size > 64 * 1024:
            raise AccountTelemetryError('account_output_limit')

    async def send(value):
        process.stdin.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
        await process.stdin.drain()

    async def rpc(identifier, method, params=None):
        request = {'id': identifier, 'method': method}
        if params is not None:
            request['params'] = params
        await send(request)
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise AccountTelemetryError('account_check_failed')
            count(raw)
            try:
                value = _strict_json(raw.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError):
                raise AccountTelemetryError('account_protocol_invalid') from None
            if not isinstance(value, dict):
                raise AccountTelemetryError('account_protocol_invalid')
            # Never answer a server request (including auth refresh or tools).
            if 'method' in value:
                if 'id' in value:
                    raise AccountTelemetryError('account_request_rejected')
                continue
            if value.get('id') != identifier:
                raise AccountTelemetryError('account_protocol_invalid')
            if 'error' in value or not isinstance(value.get('result'), dict):
                raise AccountTelemetryError('account_read_unavailable')
            return value['result']

    async def exchange():
        await rpc(0, 'initialize', {'clientInfo': {'name': 'beanest_account_status',
            'title': 'Beanest account status', 'version': '0.3.0'},
            'capabilities': {'experimentalApi': False}})
        await send({'method': 'initialized', 'params': {}})
        if models:
            result, cursor = [], None
            for identifier in range(1, 9):
                page = await rpc(identifier, 'model/list', {'limit': 100, 'cursor': cursor})
                if not isinstance(page.get('data'), list):
                    raise AccountTelemetryError('account_protocol_invalid')
                for item in page['data']:
                    if not isinstance(item, dict) or item.get('hidden') is True:
                        continue
                    model = item.get('model')
                    efforts = item.get('supportedReasoningEfforts')
                    if (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}', model)
                            or not isinstance(efforts, list)):
                        continue
                    values = [entry['reasoningEffort'] for entry in efforts if isinstance(entry, dict)
                              and isinstance(entry.get('reasoningEffort'), str)
                              and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', entry['reasoningEffort'])]
                    name = item.get('displayName')
                    result.append({'id': model, 'name': name if isinstance(name, str) and len(name) <= 160 else model,
                                   'efforts': list(dict.fromkeys(values)),
                                   'default_effort': item.get('defaultReasoningEffort') if item.get('defaultReasoningEffort') in values else None,
                                   'is_default': item.get('isDefault') is True})
                cursor = page.get('nextCursor')
                if cursor is None:
                    return result
                if not isinstance(cursor, str) or len(cursor) > 2048 or len(result) > 100:
                    break
            raise AccountTelemetryError('account_output_limit')
        first = _codex_identity(await rpc(1, 'account/read', {'refreshToken': False}))
        quota = _account_quota({})
        if first['auth_method'] == 'chatgpt':
            try:
                quota = _account_quota(await rpc(2, 'account/rateLimits/read'))
            except AccountTelemetryError as error:
                if error.code != 'account_read_unavailable':
                    raise
                quota.update(state='error', reason='provider_usage_unavailable')
            second = _codex_identity(await rpc(3, 'account/read', {'refreshToken': False}))
            if first != second:
                quota = {**_account_quota({}), 'reason': 'account_changed'}
                first = second
            if first['email'] is None:
                quota = {**_account_quota({}), 'reason': 'account_identity_unavailable'}
        return {**first, 'source': 'codex_app_server', 'scope': 'default_cli_login',
                'state': ('ready' if quota['state'] == 'available' else 'partial')
                         if first['auth_method'] else 'unavailable',
                'reason': None if first['auth_method'] else 'auth_unconfirmed', 'quota': quota}

    async def stderr():
        while raw := await process.stderr.read(4096):
            count(raw)

    tasks = [asyncio.create_task(exchange()), asyncio.create_task(stderr())]
    try:
        async def collect():
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[1] in done:
                tasks[1].result()
            return await tasks[0]
        return await asyncio.wait_for(collect(), timeout=timeout)
    finally:
        for task in tasks:
            task.cancel()
        process.stdin.close()
        cleanup = asyncio.create_task(_terminate(process))
        cancelled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        cleanup.result()
        settled = asyncio.gather(*tasks, return_exceptions=True)
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        settled.result()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError


def _claude_quota(raw):
    """Keep advertised meters separate; utilization is already a percentage."""
    windows = []
    rate = raw.get('rate_limits') if isinstance(raw, dict) else None
    if isinstance(rate, dict) and raw.get('rate_limits_available') is not False:
        rows = rate.get('limits')
        if not isinstance(rows, list):
            rows = []
            for key, kind in (('five_hour', 'session'), ('seven_day', 'weekly_all')):
                row = rate.get(key)
                if isinstance(row, dict):
                    rows.append({'kind': kind, 'percent': row.get('utilization'), 'resets_at': row.get('resets_at')})
            for row in rate.get('model_scoped', []) if isinstance(rate.get('model_scoped'), list) else []:
                if isinstance(row, dict):
                    rows.append({'kind': 'weekly_scoped', 'percent': row.get('utilization'),
                                 'resets_at': row.get('resets_at'), 'scope': {'model': {'display_name': row.get('display_name')}}})
        for row in rows[:16]:
            if (not isinstance(row, dict) or not isinstance(row.get('kind'), str)
                    or row['kind'] not in {'session', 'weekly_all', 'weekly_scoped'}):
                continue
            used = row.get('percent')
            if type(used) not in (int, float) or not 0 <= used <= 100 or not math.isfinite(used):
                continue
            resets = row.get('resets_at')
            try:
                parsed = datetime.fromisoformat(resets.replace('Z', '+00:00')) if isinstance(resets, str) and len(resets) <= 64 else None
                resets = parsed.timestamp() if parsed is not None and parsed.tzinfo is not None else None
            except (ValueError, OverflowError, OSError):
                resets = None
            if resets is not None and not 0 <= resets <= 32503680000:
                resets = None
            kind, model = row['kind'], None
            scope = row.get('scope')
            if kind != 'weekly_scoped' and scope is not None:
                # A familiar kind with an explicit model/surface restriction
                # does not become an account-wide meter by its name alone.
                if not isinstance(scope, dict) or any(value is not None for value in scope.values()):
                    continue
            if kind == 'weekly_scoped':
                candidate = scope.get('model') if isinstance(scope, dict) else None
                model = candidate.get('display_name') if isinstance(candidate, dict) else None
                if (not isinstance(model, str) or not 1 <= len(model) <= 100
                        or not all(character.isprintable() for character in model)):
                    continue
            label, name, minutes = ('Claude · 5小时窗口', 'claude:five_hour', 300) if kind == 'session' else (
                ('Claude · 7天窗口', 'claude:seven_day', 10080) if kind == 'weekly_all' else
                (model + ' · 模型周额度', 'claude:model_scoped:' + str(len(windows)), 10080))
            windows.append({'name': name, 'label': label, 'kind': kind, 'model': model,
                'scope': 'model' if model is not None else 'provider_account',
                'active': row['is_active'] if type(row.get('is_active')) is bool else None,
                'used_percent': used, 'remaining_percent': 100 - used, 'window_minutes': minutes, 'resets_at': resets})
    return {'state': 'available' if windows else 'unavailable', 'windows': windows, 'source': 'claude_sdk_get_usage',
            'scope': 'provider_account_and_model' if any(row['scope'] == 'model' for row in windows) else 'provider_account',
            'reason': None if windows else 'provider_usage_unavailable'}


def _claude_control_command():
    return [find_claude(), '--safe-mode', '--restricted', '--tools', '', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands', '--no-chrome',
            '--settings', '{"apiKeyHelper":""}', '--no-session-persistence', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--permission-prompts', 'none', '-p']


def _claude_models(rows):
    if not isinstance(rows, list):
        raise AccountTelemetryError('account_protocol_invalid')
    if len(rows) > 100:
        raise AccountTelemetryError('account_output_limit')
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value, resolved = row.get('value'), row.get('resolvedModel')
        if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}', value)
                or value in seen):
            continue
        if not isinstance(resolved, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}', resolved):
            resolved = None
        efforts = row.get('supportedEffortLevels')
        efforts = ([effort for effort in efforts if isinstance(effort, str)
                    and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', effort)]
                   if row.get('supportsEffort') is True and isinstance(efforts, list) and len(efforts) <= 16 else [])
        name = row.get('displayName')
        models.append({'id': value, 'name': name if isinstance(name, str) and 0 < len(name) <= 160 and name.isprintable() else value,
                       'resolved_model': resolved, 'efforts': list(dict.fromkeys(efforts)),
                       'default_effort': None, 'is_default': value == 'default'})
        seen.add(value)
    return models


async def _claude_control_rpc(command, cwd, *, query, timeout=10, expected_email=None):
    """A fixed initialize-only catalog or initialize/get_usage exchange."""
    if query not in {'models', 'usage'}:
        raise AccountTelemetryError('account_request_rejected')
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=_environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    size = 0

    def count(raw):
        nonlocal size
        size += len(raw)
        if size > 128 * 1024:
            raise AccountTelemetryError('account_output_limit')

    async def request(identifier, value):
        process.stdin.write(json.dumps({'type': 'control_request', 'request_id': identifier,
                                       'request': value}, separators=(',', ':')).encode() + b'\n')
        await process.stdin.drain()
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError:
                raise AccountTelemetryError('account_output_limit') from None
            count(line)
            if not line:
                raise AccountTelemetryError('account_check_failed')
            try:
                event = _strict_json(line.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError):
                raise AccountTelemetryError('account_protocol_invalid') from None
            if not isinstance(event, dict):
                raise AccountTelemetryError('account_protocol_invalid')
            if event.get('type') != 'control_response':
                # Even a plausible assistant result is forbidden on this
                # no-inference channel. Never answer tools or hook callbacks.
                raise AccountTelemetryError('account_request_rejected')
            response = event.get('response')
            if not isinstance(response, dict) or response.get('request_id') != identifier:
                raise AccountTelemetryError('account_protocol_invalid')
            if response.get('subtype') != 'success' or not isinstance(response.get('response'), dict):
                raise AccountTelemetryError('account_read_unavailable')
            return response['response']

    async def exchange():
        initialized = await request('beanest_init', {'subtype': 'initialize', 'hooks': {}})
        if query == 'models':
            if 'models' not in initialized:
                raise AccountTelemetryError('runtime_catalog_not_exposed')
            return _claude_models(initialized['models'])
        account = initialized.get('account')
        email = _account_email(account.get('email')) if isinstance(account, dict) else None
        if expected_email is not None:
            if email is None or account.get('apiProvider') != 'firstParty':
                raise AccountTelemetryError('account_identity_unknown')
            if email.casefold() != expected_email.casefold():
                raise AccountTelemetryError('account_changed')
        return await request('beanest_usage', {'subtype': 'get_usage', 'skip_behaviors': True})

    async def stderr():
        while raw := await process.stderr.read(4096):
            count(raw)

    tasks = [asyncio.create_task(exchange()), asyncio.create_task(stderr())]
    try:
        async def collect():
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[1] in done:
                tasks[1].result()
            return await tasks[0]
        return await asyncio.wait_for(collect(), timeout=timeout)
    finally:
        for task in tasks:
            task.cancel()
        process.stdin.close()
        cleanup = asyncio.create_task(_terminate(process))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        settled = asyncio.gather(*tasks, return_exceptions=True)
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                cancelled = True
        settled.result()
        if cancelled:
            raise asyncio.CancelledError


async def _claude_usage_rpc(command, cwd, *, timeout=10, expected_email=None):
    return await _claude_control_rpc(command, cwd, query='usage', timeout=timeout, expected_email=expected_email)


async def _claude_auth_status(cwd, timeout):
    lines, code = await _run([find_claude(), '--safe-mode', '--restricted', 'auth', 'status', '--json'],
                             '', cwd, timeout, max_output=64 * 1024)
    raw = b'\n'.join(lines)
    if code or len(raw) > 64 * 1024:
        raise AccountTelemetryError('account_check_failed')
    try:
        value = _strict_json(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise AccountTelemetryError('account_protocol_invalid') from None
    confirmed = (isinstance(value, dict) and value.get('loggedIn') is True
                 and value.get('authMethod') == 'claude.ai' and value.get('apiProvider') == 'firstParty')
    return {'email': _account_email(value.get('email')) if confirmed else None,
            'plan': _account_plan(value.get('subscriptionType')) if confirmed else None,
            'auth_method': 'claude.ai' if confirmed else None}


async def _claude_account_status(cwd, timeout):
    first = await _claude_auth_status(cwd, timeout)
    quota = _claude_quota({})
    if first['auth_method'] == 'claude.ai' and first['email'] is not None:
        try:
            quota = _claude_quota(await _claude_usage_rpc(_claude_control_command(), cwd, timeout=timeout, expected_email=first['email']))
        except (AccountTelemetryError, OSError, RuntimeError):
            quota.update(state='error', reason='provider_usage_unavailable')
        second = await _claude_auth_status(cwd, timeout)
        if first != second:
            quota = {**_claude_quota({}), 'reason': 'account_changed'}
        first = second
    return {**first, 'source': 'claude_auth_status', 'scope': 'default_cli_login',
            'state': ('ready' if quota['state'] == 'available' else 'partial') if first['auth_method'] else 'unavailable',
            'reason': None if first['auth_method'] else 'auth_unconfirmed', 'quota': quota}


async def kimi_status(cwd, *, timeout=10):
    from .kimi_acp import status
    await _ensure_cli('kimi', cwd, timeout=timeout)
    return await status(cwd, timeout=timeout)


async def login_confirmed(agent, cwd, *, timeout=10):
    """Whether this tool is signed in to a subscription; reads no credential."""
    path = {'codex': find_codex, 'claude': find_claude}[agent]()
    command = {'codex': [path, 'login', 'status'],
               'claude': [path, '--safe-mode', '--restricted', 'auth', 'status', '--json']}[agent]
    lines, code = await _run(command, '', cwd, timeout, include_stderr=True)
    return _authentication(agent, code, b'\n'.join(lines)[:65536].decode('utf-8', errors='replace'))['subscription_confirmed']


async def account_status(agent, cwd, *, timeout=10):
    """Explicit no-inference identity/limit observation, never credential reads."""
    from .kimi_usage import DEFAULT_TIMEOUT as kimi_timeout
    maximum = kimi_timeout if agent == 'kimi' else 12
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= maximum:
        raise AccountTelemetryError('account_check_failed')
    if agent == 'kimi':
        from .kimi_usage import account_status as kimi_account_status
        await _ensure_cli('kimi', cwd, timeout=timeout)
        return await kimi_account_status(cwd, timeout=timeout)
    if agent == 'codex':
        await _ensure_cli('codex', cwd, timeout=timeout)
        command = [find_codex(), 'app-server', '--listen', 'stdio://',
                   '-c', 'mcp_servers={}', '-c', 'model_provider="openai"',
                   '-c', 'forced_login_method="chatgpt"', '-c', 'analytics.enabled=false']
        for feature in _CODEX_DISABLED_FEATURES:
            command += ['--disable', feature]
        return await _codex_account_rpc(command, cwd, timeout=timeout)
    if agent != 'claude':
        raise AccountTelemetryError('account_adapter_unsupported')
    await _ensure_cli('claude', cwd, timeout=timeout)
    return await asyncio.wait_for(_claude_account_status(cwd, timeout), timeout=timeout)


async def runtime_options(agent, cwd, *, timeout=10):
    """Explicit CLI capability reads; never starts a model turn or refreshes auth."""
    if agent == 'kimi':
        from .kimi_acp import managed_configuration
        configuration = managed_configuration()
        if not configuration['models'] or not (await kimi_status(cwd, timeout=timeout))['ready']:
            raise AccountTelemetryError('runtime_options_unavailable')
        return {'models': configuration['models'], 'catalog_source': 'kimi_managed_config',
                'effort_scope': 'per_model', 'reason': 'provider_model_entitlement_not_exposed'}
    if agent == 'codex':
        await _ensure_cli('codex', cwd, timeout=timeout)
        command = [find_codex(), 'app-server', '--listen', 'stdio://',
                   '-c', 'mcp_servers={}', '-c', 'model_provider="openai"',
                   '-c', 'forced_login_method="chatgpt"', '-c', 'analytics.enabled=false']
        for feature in _CODEX_DISABLED_FEATURES:
            command += ['--disable', feature]
        models = await _codex_account_rpc(command, cwd, timeout=timeout, models=True)
        if not models:
            raise AccountTelemetryError('runtime_options_unavailable')
        return {'models': models, 'catalog_source': 'codex_app_server_model_list',
                'effort_scope': 'per_model', 'reason': None}
    if agent != 'claude':
        raise AccountTelemetryError('runtime_settings_unsupported')
    await _ensure_cli('claude', cwd, timeout=timeout)
    try:
        models = await _claude_control_rpc(_claude_control_command(), cwd, query='models', timeout=timeout)
    except AccountTelemetryError as error:
        if error.code != 'runtime_catalog_not_exposed':
            raise
    else:
        if not models:
            raise AccountTelemetryError('runtime_options_unavailable')
        return {'models': models, 'catalog_source': 'claude_sdk_initialize',
                'effort_scope': 'per_model', 'reason': 'provider_model_entitlement_not_exposed'}
    # A successful old initialize response without model metadata can expose
    # only help aliases. Failures and malformed catalogs never become guesses.
    lines, code = await _run([find_claude(), '--safe-mode', '--restricted', '--help'], '', cwd,
                             timeout, max_output=64 * 1024)
    text = b'\n'.join(lines).decode('utf-8', errors='replace')
    model = re.search(r'--model\s+<[^>]+>.*?(?=\n\s*--|\Z)', text, re.DOTALL)
    if code or model is None:
        raise AccountTelemetryError('runtime_options_unavailable')
    aliases = list(dict.fromkeys(re.findall(r"'([a-z][a-z0-9_-]{0,31})'", model[0])))
    if not aliases:
        raise AccountTelemetryError('runtime_options_unavailable')
    return {'models': [{'id': alias, 'name': alias, 'efforts': [], 'default_effort': None,
                        'is_default': False} for alias in aliases],
            'catalog_source': 'claude_cli_help_aliases', 'effort_scope': 'unknown',
            'reason': 'provider_model_entitlement_not_exposed'}


async def invoke(agent: str, prompt: str, cwd: Path, session_id: str | None = None,
                 timeout: float = 120, *, response_schema: dict | None = None,
                 runtime_settings: dict | None = None) -> dict:
    if agent == 'kimi':
        from .kimi_acp import invoke_native as invoke_kimi
        result = await invoke_kimi(prompt, cwd, session_id, timeout, permission='plan',
                                   response_schema=response_schema, runtime_settings=runtime_settings)
        # Legacy text rooms retain their existing tools-disabled contract.
        # Native project rooms invoke the native transport directly instead.
        if result['native_execution']['tool_calls']:
            result['events'] = [*result.get('events', []), {'type': 'tool_observed'}]
        return result
    if agent == 'dsh':
        from .dsh_acp import invoke_native as invoke_dsh
        # A reply-only call runs in dsh's own read-only mode.
        result = await invoke_dsh(prompt, cwd, session_id, timeout, permission='plan',
                                  response_schema=response_schema, runtime_settings=runtime_settings)
        if result.get('native_execution', {}).get('tool_calls'):
            result['events'] = [*result.get('events', []), {'type': 'tool_observed'}]
        return result
    if agent not in ('codex', 'claude'):
        raise ValueError('agent must be codex or claude')
    if not 0 < timeout <= 600:
        raise ValueError('timeout must be between 0 and 600 seconds')
    if not isinstance(prompt, str) or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('prompt exceeds size limit')
    schema_json = None
    if response_schema is not None:
        try:
            schema_json = _object_text(response_schema)
            if response_schema.get('type') != 'object' or len(schema_json.encode('utf-8')) > 64 * 1024:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ValueError('response schema must be a bounded object schema') from None
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('cwd must be a directory')
    command = _command(agent, session_id, runtime_settings=runtime_settings) if schema_json is None else None
    status = await _subscription_status(agent, cwd)
    if not status['subscription_confirmed']:
        error = RuntimeError(f'{agent} subscription authentication is unconfirmed; inference refused')
        # Inference was never launched; the auth check owns its own cleanup proof.
        error.native_execution = {'agent': agent, 'never_started': True, 'tool_calls': 0, 'tool_results': 0,
                                  'side_effects_possible': False,
                                  'cleanup_confirmed': status.get('cleanup_confirmed') is True,
                                  'exit_code': None, 'receipt_path': None}
        raise error
    schema_directory = None
    try:
        if schema_json is not None:
            schema_path = None
            if agent == 'codex':
                schema_directory = tempfile.TemporaryDirectory(prefix='.response-schema-', dir=cwd)
                path = Path(schema_directory.name) / 'schema.json'
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(schema_json)
                schema_path = str(path)
            command = _command(agent, session_id, schema_json=schema_json, schema_path=schema_path,
                               runtime_settings=runtime_settings)
        try:
            lines, exit_code = await _run(command, prompt, cwd, timeout)
        except asyncio.TimeoutError:
            return {'session_id': session_id, 'text': '', 'usage': None, 'events': [],
                    'status': 'timeout', 'failure_code': 'deadline_exceeded'}
        except (OSError, RuntimeError):
            return {'session_id': session_id, 'text': '', 'usage': None, 'events': [],
                    'status': 'failed', 'failure_code': 'process_or_output_failure'}
        result = (parse_events(agent, lines, structured=True) if response_schema is not None
                  else parse_events(agent, lines))
    finally:
        if schema_directory is not None:
            schema_directory.cleanup()
    if exit_code:
        result['status'] = 'failed'
        # A verified native quota signal is more specific than the CLI's exit
        # status. Never preserve it over evidence of an actual tool invocation.
        quota = (result.get('failure_code') == 'subscription_limit_reached'
                 and not any(event['type'] == 'tool_observed' for event in result['events']))
        if not quota:
            result['failure_code'] = 'cli_nonzero_exit'
    elif any(event['type'] == 'tool_observed' for event in result['events']):
        result['status'] = 'failed'
        result['failure_code'] = 'unexpected_tool_event'
    elif result['status'] != 'completed' and 'failure_code' not in result:
        result['failure_code'] = 'protocol_turn_incomplete'
    return result


JSON_CLI_PROTOCOL = 'beanest-json-v1'
_JSON_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_JSON_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?\Z')
_SHELL_LAUNCHERS = frozenset({'sh', 'bash', 'zsh', 'dash', 'fish', 'csh', 'tcsh',
                            'env', 'osascript', 'cmd', 'cmd.exe', 'powershell', 'pwsh'})


def json_cli_connection(value: object) -> dict:
    """Validate a user-selected connector, without executing or expanding it.

    This is an executable protocol, not a shell command or a promise that an
    arbitrary vendor CLI understands Beanest. Arguments remain literal argv.
    """
    if (not isinstance(value, dict)
            or set(value) != {'protocol', 'executable', 'args', 'billing'}
            or value['protocol'] != JSON_CLI_PROTOCOL
            or not isinstance(value['billing'], str)
            or value['billing'] not in {'existing_subscription', 'local'}
            or not isinstance(value['executable'], str)
            or not 1 <= len(value['executable']) <= 4096
            or not Path(value['executable']).is_absolute()
            or any(ord(char) < 32 for char in value['executable'])
            or not isinstance(value['args'], list) or len(value['args']) > 16
            or any(not isinstance(arg, str) or len(arg) > 512
                   or any(ord(char) < 32 for char in arg) for arg in value['args'])):
        raise ValueError('invalid_partner_connection')
    try:
        json.dumps(value, ensure_ascii=False).encode('utf-8')
    except UnicodeError:
        raise ValueError('invalid_partner_connection') from None
    if Path(value['executable']).name in _SHELL_LAUNCHERS:
        raise ValueError('invalid_partner_connection')
    # No shell, interpreter, env assignment, or executable lookup is inferred.
    # A connector must itself be an executable implementing the protocol.
    return {**value, 'args': list(value['args'])}


def find_json_cli(connection: dict) -> str:
    connection = json_cli_connection(connection)
    path = Path(connection['executable'])
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError('configured CLI is unavailable')
    if path.resolve().name in _SHELL_LAUNCHERS:
        raise RuntimeError('connector_protocol_invalid')
    return str(path)


async def check_json_cli(connection: dict, cwd: Path, timeout: float = 10) -> dict:
    """Run only the connector's no-inference handshake and retain safe fields."""
    connection = json_cli_connection(connection)
    command = [find_json_cli(connection), *connection['args']]
    request = {'protocol': JSON_CLI_PROTOCOL, 'kind': 'check'}
    lines, code = await _run(command, json.dumps(request), cwd, min(timeout, 10))
    raw = b'\n'.join(lines)
    if code or len(raw) > 65536:
        raise RuntimeError('connector_check_failed')
    try:
        value = _strict_json(raw.decode('utf-8'))
        if (not isinstance(value, dict)
                or set(value) != {'protocol', 'kind', 'ready', 'version', 'billing', 'capabilities'}
                or value['protocol'] != JSON_CLI_PROTOCOL or value['kind'] != 'check'
                or type(value['ready']) is not bool
                or not isinstance(value['version'], str) or not _JSON_VERSION.fullmatch(value['version'])
                or value['billing'] != connection['billing']
                or value['capabilities'] != {'text_only': True, 'tools': False}
                or type(value['capabilities'].get('text_only')) is not bool
                or type(value['capabilities'].get('tools')) is not bool):
            raise ValueError
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise RuntimeError('connector_protocol_invalid') from None
    return {'ready': value['ready'], 'version': value['version'], 'billing': value['billing']}


async def invoke_json_cli(connection: dict | None, prompt: str, cwd: Path,
                          session_id: str | None = None, timeout: float = 120, *,
                          response_schema: dict | None = None) -> dict:
    """Invoke one explicitly configured text connector with bounded ownership.

    The user trusts the local connector executable; this protocol is not an OS
    sandbox. It receives no API environment overrides and must expose no tools.
    """
    if connection is None:
        raise RuntimeError('connector_not_configured')
    connection = json_cli_connection(connection)
    if (type(timeout) not in (int, float) or not 0 < timeout <= 600
            or not isinstance(prompt, str) or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES
            or (session_id is not None and (not isinstance(session_id, str)
                                           or not _JSON_SESSION.fullmatch(session_id)))):
        raise ValueError('invalid_connector_request')
    if response_schema is not None:
        if (not isinstance(response_schema, dict) or response_schema.get('type') != 'object'
                or len(_object_text(response_schema).encode('utf-8')) > 65536):
            raise ValueError('invalid_connector_schema')
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('cwd must be a directory')
    request = {'protocol': JSON_CLI_PROTOCOL, 'kind': 'invoke', 'prompt': prompt,
               'session_id': session_id, 'response_schema': response_schema,
               'capabilities': {'text_only': True, 'tools': False}}
    request_text = json.dumps(request, ensure_ascii=False, allow_nan=False)
    if len(request_text.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('connector request exceeds size limit')
    started = time.monotonic()
    failed = {'session_id': session_id, 'text': '', 'usage': None, 'events': [], 'status': 'failed'}
    try:
        health = await check_json_cli(connection, cwd, timeout)
        if not health['ready']:
            return {**failed, 'failure_code': 'connector_not_ready'}
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise asyncio.TimeoutError
        lines, code = await _run([find_json_cli(connection), *connection['args']],
                                request_text, cwd, remaining)
        raw = b'\n'.join(lines)
        value = _strict_json(raw.decode('utf-8'))
        if (code or not isinstance(value, dict)
                or set(value) != {'protocol', 'kind', 'status', 'session_id', 'text', 'usage', 'tools_used'}
                or value['protocol'] != JSON_CLI_PROTOCOL or value['kind'] != 'result'
                or value['status'] != 'completed' or value['tools_used'] != []
                or not isinstance(value['session_id'], str) or not _JSON_SESSION.fullmatch(value['session_id'])
                or (session_id is not None and value['session_id'] != session_id)
                or not isinstance(value['text'], str) or len(value['text'].encode('utf-8')) > MAX_TEXT
                or value['usage'] is not None and not isinstance(value['usage'], dict)):
            raise ValueError
        return {'session_id': value['session_id'], 'text': value['text'],
                'usage': _usage(value['usage']), 'events': [], 'status': 'completed'}
    except asyncio.TimeoutError:
        return {**failed, 'status': 'timeout', 'failure_code': 'deadline_exceeded'}
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, RecursionError):
        return {**failed, 'failure_code': 'connector_protocol_failure'}
