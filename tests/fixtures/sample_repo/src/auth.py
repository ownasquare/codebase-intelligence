"""Authentication boundary for the fixture application."""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    identifier: str
    active: bool


class AuthenticationError(Exception):
    pass


def authenticate_bearer_token(token: str) -> User:
    """Validate a bearer token and return the active user it represents."""

    if not token.startswith("valid-"):
        raise AuthenticationError("invalid bearer token")
    user = User(identifier=token.removeprefix("valid-"), active=True)
    if not user.active:
        raise AuthenticationError("inactive user")
    return user
