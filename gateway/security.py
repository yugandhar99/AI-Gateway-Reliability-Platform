"""Local API-key identities. Credentials stay in memory, never in SQLite."""

from dataclasses import dataclass
import hashlib
import hmac
import os


@dataclass(frozen=True)
class Tier:
    requests_per_minute: int
    monthly_tokens: int


TIERS = {
    "Starter": Tier(5, 10_000),
    "Business": Tier(30, 100_000),
    "Enterprise": Tier(100, 1_000_000),
}


@dataclass(frozen=True)
class Customer:
    customer_id: str
    tier: str


class AuthenticationError(Exception):
    pass


def environment_key(name):
    """Read mounted credentials without placing values in container environment."""
    path = os.environ.get(name + '_FILE')
    if path and os.environ.get(name):
        raise ValueError('Configure a key or its file, not both')
    if path:
        with open(path, encoding='ascii') as source:
            return source.read().strip()
    return os.environ.get(name)


class KeyRegistry:
    def __init__(self, customers=None, admin_key=None):
        self.customers = []
        seen = set()
        for key, customer in (customers or {}).items():
            digest = self._digest(key)
            if digest in seen or customer.tier not in TIERS:
                raise ValueError("Invalid key registry configuration")
            seen.add(digest)
            self.customers.append((digest, customer))
        self.admin = self._digest(admin_key) if admin_key else None
        if self.admin in seen:
            raise ValueError("Administrator key must differ from customer keys")

    @staticmethod
    def _digest(key):
        if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("API keys must be nonempty ASCII without whitespace")
        return hashlib.sha256(key.encode('ascii')).digest()

    @classmethod
    def from_environment(cls):
        customers = {}
        for tier in TIERS:
            key = environment_key(f"GATEWAY_{tier.upper()}_API_KEY")
            if key:
                if key in customers:
                    raise ValueError("Customer keys must be distinct")
                customers[key] = Customer(tier.lower() + '-demo', tier)
        if not customers:
            raise ValueError("Configure at least one GATEWAY_<TIER>_API_KEY")
        return cls(customers, environment_key('GATEWAY_ADMIN_API_KEY'))

    def authenticate(self, headers, admin=False):
        values = headers.get_all('Authorization', [])
        try:
            if len(values) != 1:
                raise ValueError()
            parts = values[0].split()
            if len(parts) != 2 or parts[0].lower() != 'bearer':
                raise ValueError()
            digest = self._digest(parts[1])
        except ValueError:
            raise AuthenticationError() from None
        if admin:
            if self.admin is not None and hmac.compare_digest(digest, self.admin):
                return None
        else:
            for expected, customer in self.customers:
                if hmac.compare_digest(digest, expected):
                    return customer
        raise AuthenticationError()
