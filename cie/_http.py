"""極簡 HTTP 用戶端(標準函式庫 urllib)。

刻意零新增依賴:雲端後端有金鑰時才會被呼叫,離線預設路徑完全不觸及網路。
提供 typed error、逾時、對暫時性錯誤(429 / 5xx / 連線)指數退避重試。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class HttpError(RuntimeError):
    """傳輸層錯誤,帶上游狀態碼與(截斷的)回應 body 以利除錯。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def post_json(
    url: str,
    *,
    payload: Optional[Any] = None,
    raw_body: Optional[str] = None,
    content_type: str = "application/json",
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 30.0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """POST 並回傳解析後的 JSON dict。

    payload(dict→JSON)與 raw_body(已序列化字串,如 NDJSON)二擇一。
    非 2xx:4xx(429 除外)立即拋出不重試;429 / 5xx / 連線錯誤退避重試。
    """
    if raw_body is not None:
        data = raw_body.encode("utf-8")
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        data = b""

    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)
    return _request("POST", url, data=data, headers=hdrs,
                    timeout_s=timeout_s, max_retries=max_retries)


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 30.0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """GET 並回傳解析後的 JSON dict。"""
    text = request_text("GET", url, headers=dict(headers or {}),
                         timeout_s=timeout_s, max_retries=max_retries)
    return json.loads(text) if text else {}


def request_text(
    method: str,
    url: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 30.0,
    max_retries: int = 2,
) -> str:
    """送出請求並回傳**原始文字** body(不做 JSON 解析)。

    供 R2 物件存取等回傳非 JSON 信封的端點(canonical JSONL 即原始文字)。
    重試政策同 JSON 路徑:4xx(429 除外)立即拋,429/5xx/連線錯誤退避重試。
    """
    last_err: Optional[HttpError] = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as e:  # pragma: no cover - 需非 UTF-8 回應
                # 真相層寧可拋 typed error 也不靜默用 replace 損壞資料;
                # HttpError(RuntimeError)非以下 except 所捕,直接上拋由呼叫端轉 CloudflareError。
                raise HttpError(f"{method} {url} → 回應非 UTF-8(無法解碼): {e}") from e
        except urllib.error.HTTPError as e:  # pragma: no cover - 需網路
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:2000]
            except Exception:
                pass
            transient = e.code == 429 or 500 <= e.code < 600
            last_err = HttpError(f"{method} {url} → HTTP {e.code}", e.code, body)
            if not transient:
                raise last_err from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # pragma: no cover - 需網路
            last_err = HttpError(f"{method} {url} → 連線/逾時錯誤: {e}")
        if attempt < max_retries:  # pragma: no cover - 需網路
            time.sleep(0.5 * (2 ** attempt))
    raise last_err or HttpError(f"{method} {url} 失敗")


def _request(method: str, url: str, *, data: Optional[bytes],
             headers: Dict[str, str], timeout_s: float, max_retries: int) -> Dict[str, Any]:
    text = request_text(method, url, data=data, headers=headers,
                        timeout_s=timeout_s, max_retries=max_retries)
    return json.loads(text) if text else {}
