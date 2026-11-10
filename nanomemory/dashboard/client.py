"""HTTP client used by the Streamlit dashboard.

The dashboard never opens storage files: every read and write goes through the FastAPI
surface so validation, locking and version checks are always applied (spec §22.18).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

SESSION_HEADER = "X-NanoMemory-Session-ID"
DEFAULT_TIMEOUT = 30.0


@dataclass(slots=True)
class ApiError(Exception):
    """Normalised API failure for UI display (never contains secrets)."""

    code: str
    message: str
    status: int
    details: dict[str, Any] | None = None
    request_id: str | None = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ApiClient:
    """Small typed wrapper around the NanoMemory REST API."""

    def __init__(
        self,
        base_url: str,
        *,
        session_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout

    # ------------------------------------------------------------------ plumbing
    def _headers(self, *, admin_token: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if admin_token:
            headers["Authorization"] = f"Bearer {admin_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        admin_token: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(admin_token=admin_token),
                )
        except httpx.HTTPError as exc:
            raise ApiError(
                code="connection_error",
                message=f"Cannot reach the NanoMemory API at {self.base_url}.",
                status=0,
                details={"reason": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": "http_error", "message": response.text[:200]}
            raise ApiError(
                code=str(payload.get("code", "http_error")),
                message=str(payload.get("message", "Request failed")),
                status=response.status_code,
                details=payload.get("details"),
                request_id=payload.get("request_id"),
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ endpoints
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_session(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/sessions")

    def get_working(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/working")

    def put_working(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/api/v1/memory/working", json=payload)

    def compress(self, *, mode: str, apply: bool) -> dict[str, Any]:
        return self._request("POST", "/api/v1/memory/compress", json={"mode": mode, "apply": apply})

    def list_long_term(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/long-term", params=params)

    def create_long_term(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/memory/long-term", json=payload)

    def delete_long_term(
        self,
        memory_id: str,
        *,
        permanent: bool = False,
        if_version: int | None = None,
        admin_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"permanent": str(permanent).lower()}
        if if_version is not None:
            params["if_version"] = if_version
        return self._request(
            "DELETE",
            f"/api/v1/memory/long-term/{memory_id}",
            params=params,
            admin_token=admin_token,
        )

    def list_episodes(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/episodes", params=params)

    def append_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/memory/episodes", json=payload)

    def quarantine(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/episodes/quarantine")

    def acknowledge_quarantine(
        self, ids: list[str], reason: str, *, admin_token: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/episodes/quarantine/acknowledge",
            json={"quarantine_ids": ids, "reason": reason, "confirm": True},
            admin_token=admin_token,
        )

    def run_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/agent/run", json=payload)

    def usage(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/api/v1/metrics/usage", params=params)

    def summary(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/metrics/summary")

    def statistics(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/statistics")

    def capacity(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/capacity")

    def create_backup(self, label: str, *, admin_token: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/memory/backup", json={"label": label}, admin_token=admin_token
        )

    def list_backups(self, *, admin_token: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/memory/backups", admin_token=admin_token)

    def verify_backup(self, backup_id: str, *, admin_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/memory/backups/{backup_id}/verify",
            admin_token=admin_token,
        )

    def restore(
        self, backup_id: str, *, confirmation: str, force: bool, admin_token: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/restore",
            json={"backup_id": backup_id, "confirmation": confirmation, "force": force},
            admin_token=admin_token,
        )


__all__ = ["ApiClient", "ApiError", "SESSION_HEADER"]
