"""Input validation and provider dispatch, independent of HTTP."""

from time import perf_counter

from gateway.providers import PROVIDERS, resolve_provider, simulate_response
from gateway.reliability import Router
from gateway.usage import estimate_tokens

DEFAULT_ROUTER = Router()

MAX_PROMPT_LENGTH = 10_000


class ValidationError(ValueError):
    """A client supplied an invalid chat request."""


def validate_chat(payload):
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
    return prompt.strip(), provider


def chat(payload, router=None):
    started = perf_counter()
    prompt, provider = validate_chat(payload)
    response, provider = (router or DEFAULT_ROUTER).route(prompt, provider)
    return {
        "response": response,
        "provider": provider,
        "status": "success",
        "response_time_ms": round((perf_counter() - started) * 1000, 3),
    }


def customer_chat(payload, router, store, customer):
    """Reserve quota around the unchanged router and settle exactly once."""
    prompt, _ = validate_chat(payload)
    input_tokens = estimate_tokens(prompt)
    reservation = input_tokens + max(
        estimate_tokens(simulate_response(prompt, provider)) for provider in PROVIDERS)
    ticket = store.admit(customer, reservation)
    try:
        result = chat(payload, router)
    except Exception:
        store.finish(ticket)
        raise
    tokens = input_tokens + estimate_tokens(result["response"])
    store.finish(ticket, tokens)
    router.observability.add_tokens(tokens)
    return result
