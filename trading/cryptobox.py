"""APIキーの保存時暗号化（依存追加なし・標準ライブラリのみ）。

各ユーザー（法人）が持ち込む OANDA / Anthropic のキーを DB に平文で
置かないための、保存時暗号化ユーティリティ。

方式: HMAC-SHA256 を PRF とした keystream(CTR) で XOR 暗号化し、
encrypt-then-MAC（HMAC-SHA256）で改ざん検知する。マスター鍵は
APP_ENCRYPTION_KEY（無ければ SECRET_KEY）から SHA-256 で導出する。

注: OS ネイティブ依存（cryptography/AES）を避け、どのデプロイ環境でも
動くことを優先。より強固にするなら APP_ENCRYPTION_KEY に十分な
ランダム値を設定すること。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Optional

_NONCE_LEN = 16
_TAG_LEN = 32


def _master_key() -> bytes:
    raw = os.environ.get("APP_ENCRYPTION_KEY")
    if not raw:
        raw = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _subkey(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """平文文字列を暗号化トークン（urlsafe base64）にする。None は None。"""
    if plaintext is None:
        return None
    master = _master_key()
    ek = _subkey(master, b"enc")
    mk = _subkey(master, b"mac")
    data = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ct = _xor(data, _keystream(ek, nonce, len(data)))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt(token: Optional[str]) -> str:
    """暗号化トークンを復号する。空/None は空文字。改ざん時は ValueError。"""
    if not token:
        return ""
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise ValueError("暗号文が短すぎます。")
    nonce = raw[:_NONCE_LEN]
    tag = raw[-_TAG_LEN:]
    ct = raw[_NONCE_LEN:-_TAG_LEN]
    master = _master_key()
    mk = _subkey(master, b"mac")
    expected = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("復号に失敗しました（改ざん、または鍵の不一致）。")
    ek = _subkey(master, b"enc")
    return _xor(ct, _keystream(ek, nonce, len(ct))).decode("utf-8")
