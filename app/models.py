from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceAuth:
    device_id: str
    status: str
    expires_at: int
    request_count: int
    total_tokens: int

    @property
    def is_active(self) -> bool:
        import time

        return self.status == "active" and self.expires_at > int(time.time())

