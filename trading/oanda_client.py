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
    def _request(self, method: str, path: str,
                 params: Optional[Dict[str, Any]] = None,
                 json: Optional[Dict[str, Any]] = None,
                 max_retries: int = 4) -> Dict[str, Any]:
        if not self.settings.oanda_api_token:
            raise OandaError("OANDA_API_TOKEN が未設定です。")
        url = f"{self.settings.oanda_host}{path}"
        delay = 2.0
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = self._session.request(
                    method, url, params=params, json=json, timeout=30
                )
                if resp.status_code == 429:  # レート制限。バックオフ
                    raise OandaError("rate limited (429)")
                if resp.status_code >= 400:
                    raise OandaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return resp.json() if resp.text else {}
            except Exception as exc:  # ネットワーク/レート制限はリトライ
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                time.sleep(delay)
                delay *= 2
        raise OandaError(f"{method} {path} に失敗しました: {last_exc}")

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    @property
    def _account_path(self) -> str:
        return f"/v3/accounts/{self.settings.oanda_account_id}"

    # -- 公開 API（読み取り） -------------------------------------------------
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
        return self._get(f"{self._account_path}/summary").get("account", {})

    def get_pricing(self, instruments: List[str]) -> Dict[str, Any]:
        """指定インストルメントの現在価格（bid/ask）を取得する。"""
        params = {"instruments": ",".join(instruments)}
        data = self._get(f"{self._account_path}/pricing", params=params)
        return {p["instrument"]: p for p in data.get("prices", [])}

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """オープン中のトレード一覧。"""
        return self._get(f"{self._account_path}/openTrades").get("trades", [])

    def get_trade(self, trade_id: str) -> Dict[str, Any]:
        """単一トレードの詳細（決済済みの realizedPL 参照に使う）。"""
        return self._get(f"{self._account_path}/trades/{trade_id}").get("trade", {})

    # -- 公開 API（発注・決済 / Phase 2） ------------------------------------
    def create_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: Optional[float] = None,
        client_id: Optional[str] = None,
        price_precision: int = 5,
    ) -> Dict[str, Any]:
        """成行注文を出す。units は買い正・売り負。SL を同時に設定可能。

        client_id を渡すと clientExtensions.id として重複発注防止に使える。
        """
        order: Dict[str, Any] = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
        if stop_loss_price is not None:
            order["stopLossOnFill"] = {"price": f"{stop_loss_price:.{price_precision}f}"}
        if client_id is not None:
            order["clientExtensions"] = {"id": client_id}
        return self._request("POST", f"{self._account_path}/orders", json={"order": order})

    def set_trade_stop_loss(self, trade_id: str, stop_loss_price: float,
                            price_precision: int = 5) -> Dict[str, Any]:
        """既存トレードの損切り価格を更新（トレーリング）。"""
        body = {"stopLoss": {"price": f"{stop_loss_price:.{price_precision}f}"}}
        return self._request("PUT", f"{self._account_path}/trades/{trade_id}/orders",
                             json=body)

    def close_trade(self, trade_id: str, units: str = "ALL") -> Dict[str, Any]:
        """トレードを（部分）決済する。"""
        return self._request("PUT", f"{self._account_path}/trades/{trade_id}/close",
                             json={"units": units})
