import asyncio
import json
import logging
import os
import re
import signal
from urllib.parse import urlparse
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, getcontext

import aiohttp
from aiohttp import web
import asyncpg

from crypto import ASSETS, Crypto, ton_address, tx_hash


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

LOG = logging.getLogger("snowman")
getcontext().prec = 78

def parse_admin_ids(raw):
    values = [x for x in re.split(r"[,;\s]+", raw.strip()) if x]
    if any(not x.isdecimal() or int(x) <= 0 for x in values):
        raise ValueError("ADMIN_IDS must contain numeric Telegram USER IDs, separated by commas. Do not use usernames or bot IDs.")
    return {int(x) for x in values}


ADMINS = parse_admin_ids(os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", ""))
BOT_IDS = {}
VERSION = "snowman-2026-10-24-checkout-fix"


CURRENCIES = sorted(
    set(
        (
            "USD EUR GBP DZD AOA BWP BIF CVE XAF KMF "
            "CDF DJF EGP ERN SZL ETB GMD GHS GNF XOF "
            "KES LSL LRD LYD MGA MWK MRU MUR MAD MZN "
            "NAD NGN RWF STN SCR SLE SOS ZAR SSP SDG "
            "TZS TND UGX ZMW ZWG SHP"
        ).split()
    )
)

SUPPORT = os.getenv(
    "SUPPORT_CONTACT",
    "Contact the store administrator",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    currency TEXT NOT NULL DEFAULT 'USD',
    alerts BOOLEAN NOT NULL DEFAULT FALSE,
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    shipping TEXT NOT NULL DEFAULT '',
    state JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    request_key TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL
        CHECK(kind IN ('physical','digital')),
    name TEXT NOT NULL,
    price NUMERIC(24,6) NOT NULL CHECK(price>0),
    description TEXT NOT NULL,
    image BYTEA,
    file BYTEA,
    filename TEXT,
    delivery TEXT NOT NULL DEFAULT '',
    published BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    request_key TEXT UNIQUE NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id),
    product_id BIGINT NOT NULL REFERENCES products(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT '',
    file BYTEA,
    filename TEXT,
    shipping TEXT NOT NULL DEFAULT '',
