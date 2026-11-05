"""Explicit local agents and their text-only adapter contracts.

Registration is an extension point, not discovery or permission to run a tool.
Only the two subscription paths already verified on this machine are defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
import re
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping
from uuid import UUID

from . import adapters


_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESERVED = frozenset({"user", "service", "system", "mcp", "all", "both"})
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")
LEGACY_NAMES = {"claude": "Claude Code", "codex": "Codex"}
BEAN_STYLES = (
    ("sprout", "芽芽豆", "小叶子"),
    ("peach", "桃桃豆", "小花"),
    ("sky", "云朵豆", "云朵发型"),
    ("lavender", "芋泥豆", "圆眼镜"),
    ("sunshine", "奶黄豆", "小星星"),
    ("cocoa", "可可豆", "小围巾"),
    ("mint", "薄荷豆", "音乐耳机"),
    ("coral", "珊瑚豆", "蝴蝶结"),
    ("ocean", "海盐豆", "水手帽"),
    ("rose", "玫瑰豆", "小贝雷帽"),
)
BEAN_STYLE_IDS = frozenset(item[0] for item in BEAN_STYLES)


def bean_styles() -> list[dict]:
    return [{"id": identifier, "name": name, "description": description}
            for identifier, name, description in BEAN_STYLES]


# Each built-in tool starts with its own look, so partners stay apart without setup.
_DEFAULT_BEAN_STYLES = {"claude": "peach", "codex": "sprout", "kimi": "sky"}


def default_bean_style(adapter: str) -> str:
    return _DEFAULT_BEAN_STYLES.get(adapter, "sprout")


def safe_agent_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None and value not in _RESERVED


def bounded_session_id(value: object) -> str | None:
    """Keep opaque adapter identities safe for persistence and explicit continuation."""
    return value if isinstance(value, str) and _SESSION.fullmatch(value) else None


def uuid_session_id(value: object) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    invoke: Callable[..., Awaitable[dict]]
    usage_source: str = ""
    usage_fields: tuple[str, ...] = ()
    capabilities: Mapping[str, bool] = field(default_factory=lambda: {"text_only": True, "tools": False})
    normalize_session_id: Callable[[object], str | None] = bounded_session_id
    avatar: str = ""
    adapter: str = ""
    provider: str = ""
    bean_style: str = ""
    # Optional transport hook. Custom adapters keep the original text contract
    # unless they explicitly provide native structured-output support.
    invoke_structured: Callable[..., Awaitable[dict]] | None = None
    supports_runtime_settings: bool = False
    native_account_status: bool = False

    def __post_init__(self):
        if (not safe_agent_id(self.id) or not isinstance(self.name, str)
                or not self.name.strip() or len(self.name) > 80
                or any(ord(character) < 32 for character in self.name)
                or not callable(self.invoke) or not callable(self.normalize_session_id)
                or (self.invoke_structured is not None and not callable(self.invoke_structured))
                or type(self.supports_runtime_settings) is not bool
                or type(self.native_account_status) is not bool
                or not isinstance(self.adapter, str)
                or (self.adapter and not _ID.fullmatch(self.adapter))
                or not isinstance(self.provider, str)
                or (self.provider and not _ID.fullmatch(self.provider))
                or not isinstance(self.bean_style, str)
                or (self.bean_style and self.bean_style not in BEAN_STYLE_IDS)
                or not isinstance(self.avatar, str) or len(self.avatar) > 2
                or any(not character.isprintable() or character.isspace() for character in self.avatar)
                or not isinstance(self.usage_source, str) or len(self.usage_source) > 80
                or (self.usage_source and not _SESSION.fullmatch(self.usage_source))
                or not isinstance(self.usage_fields, tuple)
                or len(self.usage_fields) > 32
                or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in self.usage_fields)
                or len(set(self.usage_fields)) != len(self.usage_fields)
                or (self.usage_fields and not self.usage_source)
                or not isinstance(self.capabilities, Mapping)
                or dict(self.capabilities) != {"text_only": True, "tools": False}
                or type(self.capabilities.get("text_only")) is not bool
                or type(self.capabilities.get("tools")) is not bool):
            raise ValueError("invalid_agent_spec")
        try:
            self.name.encode("utf-8")
            self.avatar.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid_agent_spec") from None
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        if not self.bean_style:
            object.__setattr__(self, "bean_style", default_bean_style(self.adapter or self.id))

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "capabilities": dict(self.capabilities),
                "usage_source": self.usage_source, "usage_fields": list(self.usage_fields),
                "avatar": self.avatar or self.name.strip()[0].upper(),
                "adapter": self.adapter, "provider": self.provider, "bean_style": self.bean_style}


class AgentRegistry:
    def __init__(self, specs, *, allow_empty=False):
        if type(allow_empty) is not bool:
            raise ValueError("invalid_agent_registry")
        self._specs = {}
        for spec in specs:
            if not isinstance(spec, AgentSpec) or spec.id in self._specs:
                raise ValueError("invalid_agent_registry")
            self._specs[spec.id] = spec
        if not self._specs and not allow_empty:
            raise ValueError("invalid_agent_registry")

    def ids(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, agent_id: str) -> AgentSpec | None:
        return self._specs.get(agent_id)

    def describe(self) -> list[dict]:
        return [spec.describe() for spec in self._specs.values()]


def default_registry(invoke=None) -> AgentRegistry:
    implementation = invoke if invoke is not None else adapters.invoke
    return AgentRegistry(AgentSpec(
        agent_id, name, partial(implementation, agent_id),
        usage_source=agent_id + "_cli", usage_fields=TOKEN_FIELDS,
        normalize_session_id=uuid_session_id, avatar="C" if agent_id == "claude" else "X",
        adapter=agent_id, provider="anthropic" if agent_id == "claude" else "openai",
        invoke_structured=partial(adapters.invoke, agent_id) if invoke is None else None,
        supports_runtime_settings=True,
        native_account_status=invoke is None,
    ) for agent_id, name in LEGACY_NAMES.items())


def native_spec(adapter: str) -> AgentSpec | None:
    """A supported native template; discovery does not add a room participant."""
    if adapter != "kimi":
        return None
    return AgentSpec("kimi", "Kimi Code", partial(adapters.invoke, "kimi"),
                     usage_source="kimi_cli", usage_fields=TOKEN_FIELDS,
                     normalize_session_id=bounded_session_id, avatar="K",
                     adapter="kimi", provider="moonshot",
                     invoke_structured=partial(adapters.invoke, "kimi"),
                     supports_runtime_settings=True, native_account_status=True)
