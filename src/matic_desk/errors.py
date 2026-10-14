"""The one error type every front-end surfaces (see docs/protocol.md)."""

from __future__ import annotations

from typing import Literal, get_args

ErrorType = Literal["not_paired", "offline", "room_not_found", "invalid_request", "robot_error"]
ERROR_TYPES: frozenset[str] = frozenset(get_args(ErrorType))


class MaticDeskError(Exception):
    """A failure with a stable `type` for programs and a `message` for people."""

    def __init__(self, type: ErrorType, message: str) -> None:
        if type not in ERROR_TYPES:
            raise ValueError(f"unknown error type {type!r}")
        super().__init__(message)
        self.type: ErrorType = type
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "message": self.message}

    def __str__(self) -> str:
        return f"{self.type}: {self.message}"


def invalid(message: str) -> MaticDeskError:
    return MaticDeskError("invalid_request", message)
