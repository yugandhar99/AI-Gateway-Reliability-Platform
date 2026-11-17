"""Thread-safe routing and circuit breakers for local simulated providers."""

from dataclasses import dataclass, field
import json
import logging
import math
from threading import Lock
from time import monotonic, sleep

from gateway.providers import PROVIDERS, simulate_response

logger = logging.getLogger("gateway.routing")


class ProviderFailure(Exception):
    """A safe, fixed-category provider failure."""


class AllProvidersFailed(Exception):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("All providers are unavailable. Retry later or inspect /metrics.")


@dataclass(frozen=True)
class Policy:
    timeout_seconds: float = 0.2
    max_retries: int = 1
    failure_threshold: int = 3
    recovery_seconds: float = 5.0

    def __post_init__(self):
        for name in ("timeout_seconds", "recovery_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        for name, minimum, maximum in (("max_retries", 0, 10), ("failure_threshold", 1, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


@dataclass
class Health:
    state: str = "CLOSED"
    consecutive_failures: int = 0
    opened_at: float = 0
    probe_active: bool = False
    generation: int = 0
    requests: int = 0
    successes: int = 0
    failures: int = 0
    skipped: int = 0
    last_error: str | None = None
    lock: Lock = field(default_factory=Lock)


class Simulator:
    """Read optional live scenario JSON; sequences repeat their final outcome."""

    def __init__(self, path=None, sleeper=sleep):
        self.path = path
        self.sleeper = sleeper
        self.lock = Lock()
        self.previous = None
        self.positions = {}

    def __call__(self, provider, prompt, timeout):
        with self.lock:
            try:
                if self.path:
                    with open(self.path, encoding="utf-8") as source:
                        config = json.load(source)
                else:
                    config = {}
                if not isinstance(config, dict) or any(p not in PROVIDERS for p in config):
                    raise ValueError()
                if config != self.previous:
                    self.positions = {}
                    self.previous = config
                outcomes = config.get(provider, ["success"])
                if not isinstance(outcomes, list) or not outcomes:
                    raise ValueError()
                index = self.positions.get(provider, 0)
                outcome = outcomes[min(index, len(outcomes) - 1)]
                self.positions[provider] = index + 1
                if outcome not in ("success", "503", "timeout"):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise ProviderFailure("simulation_config_error") from None
        if outcome == "timeout":
            self.sleeper(timeout)
            raise ProviderFailure("timeout")
        if outcome == "503":
            raise ProviderFailure("upstream_503")
        return simulate_response(prompt, provider)


class Router:
    def __init__(self, policies=None, simulator=None, clock=monotonic):
        self.policies = {p: Policy() for p in PROVIDERS}
        if policies:
            if any(p not in PROVIDERS for p in policies):
                raise ValueError("Unknown provider in policies")
            self.policies.update(policies)
        self.simulator = simulator or Simulator()
        self.clock = clock
        self.health = {p: Health() for p in PROVIDERS}

    def _log(self, event, provider, **fields):
        logger.info(json.dumps({"event": event, "provider": provider, **fields}, sort_keys=True))

    def route(self, prompt, preferred):
        failures = {}
        for provider in (preferred, *(p for p in PROVIDERS if p != preferred)):
            policy, health = self.policies[provider], self.health[provider]
            for attempt in range(policy.max_retries + 1):
                with health.lock:
                    if health.state == "OPEN" and self.clock() - health.opened_at >= policy.recovery_seconds:
                        health.state = "HALF_OPEN"
                        self._log("circuit_transition", provider, state=health.state)
                    if health.state == "OPEN" or health.probe_active:
                        health.skipped += 1
                        failures[provider] = "circuit_open" if health.state == "OPEN" else "probe_in_progress"
                        self._log("provider_skipped", provider, reason=failures[provider])
                        break
                    generation = health.generation
                    probe = health.state == "HALF_OPEN"
                    health.probe_active = probe
                    health.requests += 1
                self._log("provider_attempt", provider, attempt=attempt + 1)
                try:
                    result = self.simulator(provider, prompt, policy.timeout_seconds)
                except ProviderFailure as exc:
                    reason = str(exc)
                    with health.lock:
                        health.failures += 1
                        if generation == health.generation:
                            health.consecutive_failures += 1
                            health.last_error = reason
                            health.probe_active = False
                            if probe or health.consecutive_failures >= policy.failure_threshold:
                                health.state = "OPEN"
                                health.generation += 1
                                health.opened_at = self.clock()
                                self._log("circuit_transition", provider, state="OPEN")
                    failures[provider] = reason
                    self._log("provider_failure", provider, attempt=attempt + 1, reason=reason)
                    if probe:
                        break
                else:
                    with health.lock:
                        health.successes += 1
                        # Ignore stale outcomes for circuit state, but count every attempt.
                        if generation == health.generation:
                            health.consecutive_failures = 0
                            health.last_error = None
                            if probe:
                                health.state = "CLOSED"
                                health.generation += 1
                                self._log("circuit_transition", provider, state="CLOSED")
                            health.probe_active = False
                    self._log("provider_success", provider, attempt=attempt + 1)
                    return result, provider
            self._log("failover", provider)
        raise AllProvidersFailed(failures)

    def metrics(self):
        result = {}
        for provider, health in self.health.items():
            with health.lock:
                result[provider] = {key: getattr(health, key) for key in (
                    "state", "consecutive_failures", "requests", "successes", "failures", "skipped", "last_error")}
                result[provider]["healthy"] = health.state == "CLOSED" and health.consecutive_failures == 0
        return {"providers": result}
