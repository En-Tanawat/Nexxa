import fnmatch
from dataclasses import dataclass
from typing import Literal


class UnknownProviderError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    kind: Literal["anthropic", "openai_compat"]
    key_env: str
    base_url: str | None = None
    api_hosts: tuple[str, ...] = ()  # official API hosts a web-set base URL may use; ".x.com" = any subdomain


def host_allowed(provider: Provider, host: str) -> bool:
    for h in provider.api_hosts:
        if h.startswith("."):
            if host.endswith(h):
                return True
        elif "*" in h:
            if fnmatch.fnmatch(host, h):
                return True
        elif host == h:
            return True
    return False


PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    Provider("anthropic", "anthropic", "ANTHROPIC_API_KEY"),  # the anthropic path never uses a base URL
    Provider("openai", "openai_compat", "OPENAI_API_KEY", None, ("api.openai.com",)),
    Provider("gemini", "openai_compat", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/",
             ("generativelanguage.googleapis.com",)),
    # DashScope has regional and per-workspace hosts, e.g. <id>.ap-southeast-1.maas.aliyuncs.com
    Provider("qwen", "openai_compat", "DASHSCOPE_API_KEY", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
             ("dashscope*.aliyuncs.com", "*.maas.aliyuncs.com")),
    Provider("deepseek", "openai_compat", "DEEPSEEK_API_KEY", "https://api.deepseek.com", ("api.deepseek.com",)),
)}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name.strip().lower()]
    except KeyError:
        raise UnknownProviderError(f"unknown vision provider: {name!r}") from None
