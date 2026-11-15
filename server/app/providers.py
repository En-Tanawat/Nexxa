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


PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    Provider("anthropic", "anthropic", "ANTHROPIC_API_KEY"),
    Provider("openai", "openai_compat", "OPENAI_API_KEY"),
    Provider("gemini", "openai_compat", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    Provider("qwen", "openai_compat", "DASHSCOPE_API_KEY", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    Provider("deepseek", "openai_compat", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
)}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise UnknownProviderError(f"unknown vision provider: {name!r}") from None
