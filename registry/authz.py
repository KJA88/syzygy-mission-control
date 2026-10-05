"""Server-side roles. A tool argument cannot grant operate."""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import os


@dataclass(frozen=True)
class Grants:
    discover_token: str = ""
    operate_token: str = ""
    discover_identity: str = "discover"
    operate_identity: str = "operate"
    discover_actor: str = "discover"
    operate_actor: str = "operate"

    def __post_init__(self) -> None:
        if self.operate_token and self.operate_token == self.discover_token:
            raise ValueError("Discover and operate credentials must differ")

    @classmethod
    def from_env(cls) -> Grants:
        return cls(
            discover_token=os.environ.get("SYZYGY_DISCOVER_TOKEN", ""),
            operate_token=os.environ.get("SYZYGY_OPERATE_TOKEN", ""),
            discover_identity=os.environ.get("SYZYGY_DISCOVER_IDENTITY", "discover"),
            operate_identity=os.environ.get("SYZYGY_OPERATE_IDENTITY", "operate"),
            discover_actor=os.environ.get("SYZYGY_DISCOVER_ACTOR", "discover"),
            operate_actor=os.environ.get("SYZYGY_OPERATE_ACTOR", "operate"),
        )


@dataclass(frozen=True)
class Authorization:
    profile: str
    actor: str
    identity: str

    @staticmethod
    def resolved(profile: str) -> Authorization:
        """A role already decided by the server. Not for client input."""
        if profile not in {"discover", "operate"}:
            raise ValueError(profile)
        return Authorization(profile, profile, profile)


def authorize(headers, grants: Grants | None) -> Authorization:
    """Map a presented credential to a role. Unknown credentials stay discover."""
    supplied = _bearer(headers)
    if grants is None or not supplied:
        return Authorization("discover", "anonymous", "anonymous")
    if _matches(supplied, grants.operate_token):
        return Authorization("operate", grants.operate_actor, grants.operate_identity)
    if _matches(supplied, grants.discover_token):
        return Authorization("discover", grants.discover_actor, grants.discover_identity)
    return Authorization("discover", "anonymous", "anonymous")


def _bearer(headers) -> str:
    if headers is None:
        return ""
    raw = headers.get("Authorization") if hasattr(headers, "get") else ""
    if not isinstance(raw, str):
        return ""
    prefix = "Bearer "
    if raw.startswith(prefix):
        return raw[len(prefix):].strip()
    return ""


def _matches(supplied: str, expected: str) -> bool:
    if not supplied or not expected:
        return False
    left = supplied.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
