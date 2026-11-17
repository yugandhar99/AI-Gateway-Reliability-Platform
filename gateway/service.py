"""Input validation and provider dispatch, independent of HTTP."""

from time import perf_counter

from gateway.providers import PROVIDERS, resolve_provider, simulate_response

MAX_PROMPT_LENGTH = 10_000


class ValidationError(ValueError):
    """A client supplied an invalid chat request."""


def chat(payload):
    started = perf_counter()
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("'prompt' must be a non-empty string.")
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValidationError(f"'prompt' must be at most {MAX_PROMPT_LENGTH} characters.")
    name = payload.get("provider")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("'provider' must be a non-empty string.")
    provider = resolve_provider(name.strip())
    if provider is None:
        raise ValidationError(f"Unknown provider. Choose one of: {', '.join(PROVIDERS)}.")
    response = simulate_response(prompt.strip(), provider)
    return {
        "response": response,
        "provider": provider,
        "status": "success",
        "response_time_ms": round((perf_counter() - started) * 1000, 3),
    }
