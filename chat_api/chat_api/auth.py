"""Хэширование и проверка паролей.

Пароли пользователей в анонимных чатах хранятся только в виде хэша
PBKDF2-HMAC-SHA256 со случайной солью на каждого пользователя.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ALGORITHM = "pbkdf2-sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Возвращает строку вида: pbkdf2-sha256$200000$<salt_hex>$<digest_hex>."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _ITERATIONS
    )
    return "{0}${1}${2}${3}".format(
        _ALGORITHM,
        _ITERATIONS,
        salt.hex(),
        digest.hex(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Сравнивает переданный пароль с сохранённым хэшем."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        iterations = int(iterations)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def generate_secret() -> str:
    """Случайный секрет (например, для тестовых токенов и сброса данных)."""
    return os.urandom(32).hex()