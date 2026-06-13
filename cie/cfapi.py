"""Cloudflare REST 用戶端 — Workers AI(嵌入)+ Vectorize(向量庫)。

§13.5 慣例:外部呼叫獨立成模組,typed error、防禦式解析、上游錯誤帶 body。
所有回應走 Cloudflare API v4 標準信封:
    {"result": <...|null>, "success": bool, "errors": [{code,message}], "messages": []}
本模組只在有金鑰時被實例化;離線預設不觸及。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ._http import HttpError, get_json, post_json

CF_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    """Cloudflare 應用層錯誤(success=false 或信封異常)。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class CloudflareClient:
    """持有 account_id + token;封裝 Workers AI run 與 Vectorize v2 端點。"""

    def __init__(self, account_id: str, api_token: str,
                 timeout_s: float = 30.0, max_retries: int = 2):
        if not account_id or not api_token:
            raise CloudflareError(
                "缺少 Cloudflare 金鑰(CIE_CF_ACCOUNT_ID / CIE_CF_API_TOKEN)。"
            )
        self.account_id = account_id
        self.api_token = api_token
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    # ── 共用 ──
    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _post(self, url: str, *, payload: Optional[Any] = None,
              raw_body: Optional[str] = None,
              content_type: str = "application/json") -> Dict[str, Any]:
        try:
            return post_json(
                url, payload=payload, raw_body=raw_body, content_type=content_type,
                headers=self._headers, timeout_s=self.timeout_s, max_retries=self.max_retries,
            )
        except HttpError as e:
            raise CloudflareError(str(e), getattr(e, "status", None),
                                  getattr(e, "body", "")) from e

    @staticmethod
    def _unwrap(resp: Dict[str, Any], what: str) -> Any:
        """檢查 v4 信封 success,回傳 result;失敗拋 CloudflareError。"""
        if not isinstance(resp, dict) or not resp.get("success", False):
            errors = resp.get("errors") if isinstance(resp, dict) else None
            raise CloudflareError(f"{what} 失敗: {errors}", body=str(resp)[:2000])
        return resp.get("result")

    # ── Workers AI ──
    def workers_ai_run(self, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /ai/run/{model};回傳 result(模型輸出)。"""
        url = f"{CF_API_BASE}/accounts/{self.account_id}/ai/run/{model}"
        return self._unwrap(self._post(url, payload=payload), f"Workers AI {model}")

    # ── Vectorize v2 ──
    def _vectorize_url(self, index: str, suffix: str) -> str:
        return f"{CF_API_BASE}/accounts/{self.account_id}/vectorize/v2/indexes/{index}/{suffix}"

    def vectorize_query(self, index: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = self._vectorize_url(index, "query")
        return self._unwrap(self._post(url, payload=body), "Vectorize query")

    def vectorize_upsert(self, index: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """NDJSON upsert(覆寫既有 id)。"""
        url = self._vectorize_url(index, "upsert")
        ndjson = "\n".join(json.dumps(l, ensure_ascii=False) for l in lines)
        return self._unwrap(
            self._post(url, raw_body=ndjson, content_type="application/x-ndjson"),
            "Vectorize upsert",
        )

    def vectorize_get_by_ids(self, index: str, ids: List[str]) -> List[Dict[str, Any]]:
        url = self._vectorize_url(index, "get_by_ids")
        return self._unwrap(self._post(url, payload={"ids": ids}), "Vectorize get_by_ids") or []

    def vectorize_delete_by_ids(self, index: str, ids: List[str]) -> Dict[str, Any]:
        url = self._vectorize_url(index, "delete_by_ids")
        return self._unwrap(self._post(url, payload={"ids": ids}), "Vectorize delete_by_ids")

    def vectorize_create_metadata_index(self, index: str, property_name: str,
                                        index_type: str = "string") -> Dict[str, Any]:
        url = self._vectorize_url(index, "metadata_index/create")
        return self._unwrap(
            self._post(url, payload={"propertyName": property_name, "indexType": index_type}),
            "Vectorize metadata_index/create",
        )

    def vectorize_info(self, index: str) -> Dict[str, Any]:
        """GET /info → result(含 vectorCount,最終一致)。"""
        url = self._vectorize_url(index, "info")
        try:
            resp = get_json(url, headers=self._headers,
                            timeout_s=self.timeout_s, max_retries=self.max_retries)
        except HttpError as e:
            raise CloudflareError(str(e), getattr(e, "status", None),
                                  getattr(e, "body", "")) from e
        return self._unwrap(resp, "Vectorize info") or {}
