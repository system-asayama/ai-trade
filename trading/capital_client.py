"""Capital.com REST クライアント（OANDA クライアントと同一インターフェース）。

エンジン/エグゼキュータ/データフィードが使うメソッド名・戻り値の形を
OandaClient に合わせることで、ブローカーを差し替え可能にする。

Capital.com の認証はセッション方式:
  POST /session (X-CAP-API-KEY + identifier/password) →
  レスポンスヘッダ CST と X-SECURITY-TOKEN を以後のリクエストに付与。

requests を注入可能にし、ネットワーク非依存でテストできる。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .config import Settings

CAPITAL_HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}
_BASE = "/api/v1"

# OANDA granularity -> Capital resolution
_RESOLUTION = {
    "M1": "MINUTE", "M5": "MINUTE_5", "M15": "MINUTE_15", "M30": "MINUTE_30",
    "H1": "HOUR", "H4": "HOUR_4", "D": "DAY",
}


class CapitalError(RuntimeError):
    pass


def to_epic(instrument: str) -> str:
    """'USD_JPY' -> 'USDJPY'（Capital の epic 表記）。"""
    return instrument.replace("_", "")


class CapitalClient:
    def __init__(self, settings: Settings, session: Optional[Any] = None) -> None:
        self.settings = settings
        env = getattr(settings, "capital_env", "demo")
        self.host = CAPITAL_HOSTS.get(env, CAPITAL_HOSTS["demo"])
        self.api_key = getattr(settings, "capital_api_key", "")
        self.identifier = getattr(settings, "capital_identifier", "")
        self.password = getattr(settings, "capital_password", "")
        # instrument <-> epic の逆引き（設定の通貨ペアから構築）
        self._epic_to_inst = {to_epic(i): i for i in settings.instruments}
        if session is not None:
            self._session = session
        elif requests is not None:
            self._session = requests.Session()
        else:  # pragma: no cover
            self._session = None
        self._cst: Optional[str] = None
        self._xst: Optional[str] = None

    # -- 認証 ----------------------------------------------------------------
    def _ensure_session(self) -> None:
        if self._cst and self._xst:
            return
        if not (self.api_key and self.identifier and self.password):
            raise CapitalError("Capital.com の APIキー/ID/パスワードが未設定です。")
        url = f"{self.host}{_BASE}/session"
        resp = self._session.request(
            "POST", url,
            headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"identifier": self.identifier, "password": self.password},
            timeout=30,
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise CapitalError(f"セッション作成失敗: HTTP {resp.status_code} {resp.text[:200]}")
        headers = resp.headers
        self._cst = headers.get("CST")
        self._xst = headers.get("X-SECURITY-TOKEN")
        if not (self._cst and self._xst):
            raise CapitalError("セッショントークンを取得できませんでした。")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "X-CAP-API-KEY": self.api_key,
            "CST": self._cst or "",
            "X-SECURITY-TOKEN": self._xst or "",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 json: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Dict[str, Any]:
        self._ensure_session()
        url = f"{self.host}{_BASE}{path}"
        delay = 2.0
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = self._session.request(method, url, params=params, json=json,
                                             headers=self._auth_headers(), timeout=30)
                code = getattr(resp, "status_code", 200)
                if code == 401:  # トークン切れ → 再認証
                    self._cst = self._xst = None
                    self._ensure_session()
                    raise CapitalError("re-auth")
                if code == 429:
                    raise CapitalError("rate limited")
                if code >= 400:
                    raise CapitalError(f"HTTP {code}: {resp.text[:200]}")
                return resp.json() if resp.text else {}
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                time.sleep(delay)
                delay *= 2
        raise CapitalError(f"{method} {path} 失敗: {last_exc}")

    # -- 読み取り（OANDA 互換の形で返す） ------------------------------------
    def get_candles(self, instrument: str, granularity: str, count: int = 500,
                    price: str = "M") -> List[Dict[str, Any]]:
        resolution = _RESOLUTION.get(granularity, "MINUTE_15")
        data = self._request("GET", f"/prices/{to_epic(instrument)}",
                             params={"resolution": resolution, "max": count})
        out = []
        for p in data.get("prices", []):
            o, h, l, c = (p.get("openPrice"), p.get("highPrice"),
                          p.get("lowPrice"), p.get("closePrice"))
            if None in (o, h, l, c):
                continue
            out.append({
                "time": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                "complete": True,
                "mid": {
                    "o": _mid(o), "h": _mid(h), "l": _mid(l), "c": _mid(c),
                },
                "volume": p.get("lastTradedVolume", 0) or 0,
            })
        return out

    def get_pricing(self, instruments: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for inst in instruments:
            try:
                data = self._request("GET", f"/markets/{to_epic(inst)}")
                snap = data.get("snapshot", {})
                bid = snap.get("bid")
                offer = snap.get("offer")
                out[inst] = {"bids": [{"price": bid}], "asks": [{"price": offer}]}
            except Exception:  # noqa: BLE001
                out[inst] = {"bids": [{}], "asks": [{}]}
        return out

    def get_account_summary(self) -> Dict[str, Any]:
        data = self._request("GET", "/accounts")
        accounts = data.get("accounts", [])
        acc = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        bal = acc.get("balance", {}) or {}
        return {"balance": bal.get("balance", 0.0), "currency": acc.get("currency", "")}

    def get_open_trades(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/positions")
        trades = []
        for item in data.get("positions", []):
            pos = item.get("position", {})
            market = item.get("market", {})
            epic = market.get("epic", "")
            size = float(pos.get("size", 0))
            units = size if pos.get("direction") == "BUY" else -size
            stop = pos.get("stopLevel")
            trades.append({
                "id": str(pos.get("dealId", "")),
                "instrument": self._epic_to_inst.get(epic, epic),
                "currentUnits": units,
                "initialUnits": units,
                "price": pos.get("level", 0.0),
                "stopLossOrder": {"price": stop} if stop is not None else None,
            })
        return trades

    def get_trade(self, trade_id: str) -> Dict[str, Any]:
        # Capital.com は決済後の実現損益を簡単に取得できないため空を返す
        return {}

    # -- 発注・決済 ----------------------------------------------------------
    def create_market_order(self, instrument: str, units: int,
                            stop_loss_price: Optional[float] = None,
                            client_id: Optional[str] = None,
                            price_precision: int = 5) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "epic": to_epic(instrument),
            "direction": "BUY" if units > 0 else "SELL",
            "size": abs(int(units)),
        }
        if stop_loss_price is not None:
            body["stopLevel"] = round(float(stop_loss_price), price_precision)
        resp = self._request("POST", "/positions", json=body)
        deal_ref = resp.get("dealReference")
        deal_id = self._resolve_deal_id(deal_ref)
        return {"orderFillTransaction": {"tradeOpened": {"tradeID": deal_id or deal_ref}}}

    def _resolve_deal_id(self, deal_ref: Optional[str]) -> Optional[str]:
        if not deal_ref:
            return None
        try:
            conf = self._request("GET", f"/confirms/{deal_ref}")
            return str(conf.get("dealId")) if conf.get("dealId") else None
        except Exception:  # noqa: BLE001
            return None

    def set_trade_stop_loss(self, trade_id: str, stop_loss_price: float,
                            price_precision: int = 5) -> Dict[str, Any]:
        return self._request("PUT", f"/positions/{trade_id}",
                             json={"stopLevel": round(float(stop_loss_price), price_precision)})

    def close_trade(self, trade_id: str, units: str = "ALL") -> Dict[str, Any]:
        return self._request("DELETE", f"/positions/{trade_id}")


def _mid(price: Dict[str, Any]) -> float:
    """Capital の {bid, ask} から仲値を計算する。"""
    bid = price.get("bid")
    ask = price.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    return float(bid if bid is not None else ask or 0.0)
