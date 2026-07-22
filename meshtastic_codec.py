from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


DEFAULT_CHANNEL_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")
VALID_AES_KEY_LENGTHS = {16, 24, 32}


def normalize_channel_key(value: str) -> bytes:
    """Decode a Meshtastic PSK and expand its one-byte simple-key form."""
    encoded = value.strip().replace("-", "+").replace("_", "/")
    encoded += "=" * (-len(encoded) % 4)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("必須是有效的 Base64") from exc

    if len(key) == 1:
        key = DEFAULT_CHANNEL_KEY[:-1] + key
    if len(key) not in VALID_AES_KEY_LENGTHS:
        raise ValueError("解碼後的金鑰長度必須是 16、24 或 32 bytes")
    return key


def channel_hash(name: str, key: bytes) -> int:
    result = 0
    for byte in name.encode("utf-8") + key:
        result ^= byte
    return result


def crypt_payload(payload: bytes, key: bytes, packet_id: int, sender_id: int) -> bytes:
    nonce = packet_id.to_bytes(8, "little") + sender_id.to_bytes(8, "little")
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return encryptor.update(payload) + encryptor.finalize()
