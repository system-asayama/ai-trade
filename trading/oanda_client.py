"""OANDA v20 REST クライアント（薄いラッパ）。

Phase 1 では価格(ローソク足)と口座情報の取得のみを扱う。発注系は Phase 2。
`requests` のみに依存。token 未設定でもインスタンス化は可能（呼び出し時に検証）。
practice / live は Settings.oanda_env で切り替わる（既定 practice）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:  # requests は実行時のみ必要（テストはモック可能）
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .config import Settings


class OandaError(RuntimeError):
    """OANDA API 呼び出し失敗。"""


class OandaClient:
    """OANDA v20 REST API クライアント。"""

    def __init__(self, settings: Settings, session: Optional[Any] = None) -> None:
        self.settings = settings
        if session is not None:
            self._session = session
        else:
            if requests is None:  # pragma: no cover
                raise OandaError("requests がインストールされていません。")
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {settings.oanda_api_token}",
                    "Content-Type": "application/json",
                }
            )

    # -- 内部 ----------------------------------------------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             max_retries: int = 4) -> Dict[str, Any]:
        if not self.settings.oanda_api_token:
            raise OandaError("OANDA_API_TOKEN が未設定です。")
        url = f"{self.settings.oanda_host}{path}"
        delay = 2.0
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = self._session.get(url, params=params, timeout=30)
                if resp.status_code == 429:  # レート制限。バックオフ
                    raise OandaError("rate limited (429)")
                if resp.status_code >= 400:
                    raise OandaError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except Exception as exc:  # ネットワーク/レート制限はリトライ
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                time.sleep(delay)
                delay *= 2
        raise OandaError(f"GET {path} に失敗しました: {last_exc}")

    # -- 公開 API ------------------------------------------------------------
    def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int = 500,
        price: str = "M",  # Midpoint
    ) -> List[Dict[str, Any]]:
        """ローソク足を取得し、生の candle リストを返す。

        OANDA は count 上限 5000。期間指定が必要ならここを拡張する。
        """
        path = f"/v3/instruments/{instrument}/candles"
        params = {"granularity": granularity, "count": count, "price": price}
        data = self._get(path, params=params)
        return data.get("candles", [])

    def get_account_summary(self) -> Dict[str, Any]:
        """口座サマリ（残高・建玉数など）。"""
        path = f"/v3/accounts/{self.settings.oanda_account_id}/summary"
        return self._get(path).get("account", {})
