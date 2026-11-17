"""Simulated providers. This module never makes network requests."""

PROVIDERS = ("OpenAI", "Anthropic", "Gemini")


def resolve_provider(name):
    """Return the canonical provider name, or None for an unknown name."""
    return next((p for p in PROVIDERS if p.casefold() == name.casefold()), None)


def simulate_response(prompt, provider):
    return f"[Simulated {provider} response] You said: {prompt}"
