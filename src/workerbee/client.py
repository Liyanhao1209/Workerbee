"""内核 API 的 Python 客户端。

终端入口与脚本共用它。这样「CLI 看到的状态」与「网页看到的状态」必然一致——
它们本来就是同一份事实的两个视图（UI-03：不另造一套任务事实）。
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["ApiClient", "ApiError"]


class ApiError(RuntimeError):
    def __init__(self, status_code: int, payload: Any, url: str) -> None:
        super().__init__(f"{url} 返回 {status_code}: {payload}")
        self.status_code = status_code
        self.payload = payload
        self.url = url


class ApiClient:
    def __init__(
        self, base_url: str, *, token: str | None = None, timeout: float = 30.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"X-Workerbee-Token": self.token} if self.token else {}

    def _handle(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = resp.text
            raise ApiError(resp.status_code, payload, str(resp.request.url))
        if not resp.content:
            return None
        return resp.json()

    def get(self, path: str, *, params: dict | None = None) -> Any:
        return self._handle(
            self._client.get(path, params=params, headers=self._headers())
        )

    def post(self, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        return self._handle(
            self._client.post(path, json=json, params=params, headers=self._headers())
        )

    def patch(self, path: str, *, json: Any = None) -> Any:
        return self._handle(self._client.patch(path, json=json, headers=self._headers()))

    def delete(self, path: str, *, json: Any = None) -> Any:
        return self._handle(self._client.delete(path, json=json, headers=self._headers()))
