from typing import Any


class MemoryError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


class LeaseLost(MemoryError):
    def __init__(self):
        super().__init__("LEASE_LOST", "This worker no longer owns the processing task.")
