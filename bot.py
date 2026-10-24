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
    method TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount NUMERIC(78,18) NOT NULL,
    asset TEXT,
    units NUMERIC(78,0),
    address TEXT,
    txid TEXT,
    charge TEXT UNIQUE,
    checkout_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires TIMESTAMPTZ NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    proof BYTEA,
    proof_name TEXT,
    UNIQUE(asset, units)
);

CREATE INDEX IF NOT EXISTS orders_customer
    ON orders(user_id,id DESC);

CREATE TABLE IF NOT EXISTS payments (
    asset TEXT NOT NULL,
    txid TEXT NOT NULL,
    order_id BIGINT NOT NULL UNIQUE REFERENCES orders(id),
    PRIMARY KEY(asset,txid)
);

CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    dedup TEXT UNIQUE NOT NULL,
    target TEXT NOT NULL,
    chat BIGINT NOT NULL,
    text TEXT NOT NULL,
    marketing BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'queued',
    due TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires TIMESTAMPTZ NOT NULL
        DEFAULT now()+interval '1 day'
);

CREATE INDEX IF NOT EXISTS job_queue
    ON jobs(status,due);
"""


class TGError(Exception):
    def __init__(self, code, retry=0, description=""):
        self.code = code
        self.retry = retry
        safe = re.sub(r"https?://\S+|[0-9]{5,}:[A-Za-z0-9_-]{20,}", "[redacted]", description)
        self.description = safe[:250]
        super().__init__(f"Telegram error {code}: {self.description}")


class Telegram:
    def __init__(self, token, session):
        self.token = token
        self.http = session

    async def call(self, method, **data):
        form = aiohttp.FormData()

        for key, value in data.items():
            if value is None:
                continue

            if isinstance(value, tuple):
                filename, content = value

                form.add_field(
                    key,
                    content,
                    filename=filename,
                    content_type="application/octet-stream",
                )
            else:
                encoded = (
                    json.dumps(value)
                    if isinstance(value, (dict, list, bool))
                    else str(value)
                )

                form.add_field(key, encoded)

        url = (
            "https://api.telegram.org/"
            f"bot{self.token}/{method}"
        )

        async with self.http.post(
            url,
            data=form,
            timeout=65,
        ) as response:
            result = await response.json()

        if not result.get("ok"):
            raise TGError(
                result.get("error_code", 500),
                result.get(
                    "parameters", {}
                ).get("retry_after", 0),
                result.get("description", ""),
            )

        return result["result"]

    async def send(self, chat, text, buttons=None):
        return await self.call(
            "sendMessage",
            chat_id=chat,
            text=text[:4000],
            reply_markup=buttons,
        )

    async def download(self, file_id, maximum):
        data = await self.call(
            "getFile",
            file_id=file_id,
        )

        if data.get("file_size", maximum + 1) > maximum:
            raise ValueError("File is too large.")

        url = (
            "https://api.telegram.org/file/"
            f"bot{self.token}/{data['file_path']}"
        )

        async with self.http.get(
            url,
            timeout=30,
        ) as response:
            response.raise_for_status()
            content = bytearray()

            async for chunk in response.content.iter_chunked(
                65536
            ):
                content.extend(chunk)

                if len(content) > maximum:
                    raise ValueError("File is too large.")

        return bytes(content)


def keyboard(items):
    return {
        "inline_keyboard": [
            [
                {
                    "text": label,
                    "callback_data": data,
                }
            ]
            for label, data in items
        ]
    }


def positive(value):
    try:
        number = Decimal(value)
    except Exception:
        raise ValueError("Enter a valid price/rate.")

    if (
        not number.is_finite()
        or not Decimal("0.000001")
        <= number
        <= Decimal("1000000000000")
        or number != number.quantize(
            Decimal("0.000001")
        )
    ):
        raise ValueError(
            "Use 0.000001–1000000000000, "
            "with at most six decimal places."
        )

    return number


def now():
    return datetime.now(timezone.utc)


CURRENCY_LABELS = {
    "NGN": "Nigeria · NGN", "GHS": "Ghana · GHS", "KES": "Kenya · KES",
    "ZAR": "South Africa · ZAR", "UGX": "Uganda · UGX", "TZS": "Tanzania · TZS",
    "XOF": "West African CFA · XOF", "XAF": "Central African CFA · XAF",
    "EGP": "Egypt · EGP", "MAD": "Morocco · MAD", "DZD": "Algeria · DZD",
    "AOA": "Angola · AOA", "BWP": "Botswana · BWP", "BIF": "Burundi · BIF",
    "CVE": "Cabo Verde · CVE", "KMF": "Comoros · KMF", "CDF": "DR Congo · CDF",
    "DJF": "Djibouti · DJF", "ERN": "Eritrea · ERN", "SZL": "Eswatini · SZL",
    "ETB": "Ethiopia · ETB", "GMD": "Gambia · GMD", "GNF": "Guinea · GNF",
    "LSL": "Lesotho · LSL", "LRD": "Liberia · LRD", "LYD": "Libya · LYD",
    "MGA": "Madagascar · MGA", "MWK": "Malawi · MWK", "MRU": "Mauritania · MRU",
    "MUR": "Mauritius · MUR", "MZN": "Mozambique · MZN", "NAD": "Namibia · NAD",
    "RWF": "Rwanda · RWF", "STN": "São Tomé · STN", "SCR": "Seychelles · SCR",
    "SLE": "Sierra Leone · SLE", "SOS": "Somalia · SOS", "SSP": "South Sudan · SSP",
    "SDG": "Sudan · SDG", "TND": "Tunisia · TND", "ZMW": "Zambia · ZMW",
    "ZWG": "Zimbabwe · ZWG", "SHP": "Saint Helena · SHP", "USD": "US dollar · USD",
    "EUR": "Euro · EUR", "GBP": "British pound · GBP",
}


def currency_quantum(code):
    if code in ("XAF", "XOF", "RWF", "BIF", "DJF", "GNF", "KMF", "UGX"):
        return Decimal("1")
    return Decimal("0.001") if code in ("TND", "LYD") else Decimal("0.01")


def format_amount(amount, currency):
    amount = Decimal(amount)
    if currency == "XTR":
        return f"{int(amount):,} Stars"
    if currency in ASSETS:
        value = format(amount, "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        return f"{value} {currency}"
    places = -currency_quantum(currency).as_tuple().exponent
    return f"{amount:,.{places}f} {currency}"


def home_buttons():
    return keyboard([
        ("🛍 Browse products", "catalog:0"),
        ("💳 Payment options", "payments"),
        ("📦 My orders", "myorders"),
        ("🔔 Enable reminders & alerts", "alerts:on"),
        ("🔕 Disable reminders & alerts", "alerts:off"),
        ("💬 Support", "support"),
    ])


def enabled_crypto():
    return [asset for asset in ASSETS
            if os.getenv(f"{asset}_ADDRESS", "").strip()
            and (asset not in ("ETH", "SOL") or os.getenv(f"{asset}_RPC_URL", "").strip())]


async def local_methods():
    rows = await db.fetch("SELECT key,value FROM settings WHERE key LIKE 'method:%' OR key LIKE 'rate:%'")
    config = {row["key"]: row["value"] for row in rows}
    result = {}
    for code in CURRENCIES:
        instructions = config.get("method:" + code, "").strip()
        raw_rate = config.get("rate:" + code, "1" if code == "USD" else "")
        if not instructions or not raw_rate:
            continue
        try:
            rate = positive(raw_rate)
        except (ValueError, ArithmeticError):
            continue
        result[code] = (instructions, rate)
    return result


async def admin_panel(chat):
    await admin_bot.send(chat, "☃️ SnowmanBot Admin\nChoose an option below.", keyboard([
        ("📦 Products", "admin:products"),
        ("➕ Add product", "admin:add"),
        ("🧾 Orders & payments", "admin:orders"),
        ("📊 Statistics", "admin:stats"),
        ("⚙️ Payments & reminders", "admin:settings"),
        ("📖 All commands", "admin:help"),
        ("🆔 My Telegram ID", "admin:myid"),
    ]))


async def local_currency_menu(chat, product_id=0, page=0):
    methods = await local_methods()
    codes = list(methods)
    back = f"buy:{product_id}" if product_id else "payments"
    if not codes:
        return await main_bot.send(chat, "Local bank transfer is not available yet. Choose crypto or contact support.", keyboard([("← Back", back)]))
    page = max(0, min(page, (len(codes)-1)//6))
    selected = codes[page*6:page*6+6]
    buttons = []
    product = None
    if product_id:
        product = await db.fetchrow("SELECT * FROM products WHERE id=$1 AND published", product_id)
        if not product or product["kind"] != "physical":
            raise ValueError("This product does not support local bank transfer.")
    for code in selected:
        label = CURRENCY_LABELS.get(code, code)
        if product:
            total = (product["price"]*methods[code][1]).quantize(currency_quantum(code), rounding=ROUND_UP)
            label += " — " + format_amount(total, code)
        buttons.append((label, f"bank:{product_id}:{code}" if product_id else f"currency:{code}"))
    if page:
        buttons.append(("← Previous", f"local:{product_id}:{page-1}"))
    if (page+1)*6 < len(codes):
        buttons.append(("Next →", f"local:{product_id}:{page+1}"))
    buttons.append(("← Payment methods", back))
    await main_bot.send(chat, "🏦 Local bank transfer\nChoose your country/currency:", keyboard(buttons))


async def crypto_menu(chat, product_id):
    assets = enabled_crypto()
    back = f"buy:{product_id}" if product_id else "payments"
    if not assets:
        return await main_bot.send(chat, "Crypto payments are not available yet. Choose bank transfer or contact support.", keyboard([("← Back", back)]))
    if not product_id:
        return await main_bot.send(chat, "🪙 Crypto payments\nAvailable: " + ", ".join(assets) + ".\nSelect a product to get its payment amount and network.", keyboard([("Browse products", "catalog:0"), ("← Back", back)]))
    product = await db.fetchrow("SELECT * FROM products WHERE id=$1 AND published", product_id)
    if not product or product["kind"] != "physical":
        raise ValueError("This product does not support crypto checkout.")
    await main_bot.send(chat, "🪙 Choose cryptocurrency", keyboard(
        [(f"{asset} · {ASSETS[asset][0]}", f"pay:{product_id}:{asset}") for asset in assets]
        + [("← Payment methods", back)]))


async def begin_checkout(chat, product_id, method, request_key, currency=None):
    product = await db.fetchrow("SELECT * FROM products WHERE id=$1 AND published", product_id)
    if not product:
        raise ValueError("Product unavailable.")
    if product["kind"] == "physical":
        if method == "local" and currency not in await local_methods():
            raise ValueError("This bank-transfer option is unavailable. Choose another payment method.")
        if method != "local" and method not in enabled_crypto():
            raise ValueError("This payment method is unavailable.")
        shipping = await db.fetchval("SELECT shipping FROM users WHERE id=$1", chat)
        if not shipping:
            await state(chat, {"shipping": product_id, "checkout": {"method": method, "currency": currency}})
            return await main_bot.send(chat, "Send your name, phone number, country and full delivery address in one message. /cancel to cancel.")
    elif method != "stars":
        raise ValueError("This digital product uses Telegram Stars.")
    order = await create_order(chat, product_id, method, request_key, local_currency=currency)
    if method == "stars" and order["status"] == "pending":
        await main_bot.call("sendInvoice", chat_id=chat, title=order["name"][:32],
            description=f"SnowmanBot order #{order['id']}", payload=f"snow:{order['id']}",
            currency="XTR", provider_token="", prices=[{"label": order["name"][:32], "amount": int(order["amount"])}],
            start_parameter=f"order_{order['id']}")
    else:
        await show_order(chat, order)


async def setting(key, default=""):
    value = await db.fetchval(
        "SELECT value FROM settings WHERE key=$1",
        key,
    )

    return value or default


async def set_setting(key, value):
    await db.execute(
        """
        INSERT INTO settings VALUES($1,$2)
        ON CONFLICT(key) DO UPDATE SET value=$2
        """,
        key,
        value,
    )


async def queue(
    conn,
    dedup,
    target,
    chat,
    text,
    marketing=False,
):
    await conn.execute(
        """
        INSERT INTO jobs(
            dedup,target,chat,text,marketing
        )
        VALUES($1,$2,$3,$4,$5)
        ON CONFLICT(dedup) DO NOTHING
        """,
        dedup,
        target,
        chat,
        text[:4000],
        marketing,
    )


async def alert_admins(conn, key, text):
    for admin_id in ADMINS:
        await queue(
            conn,
            f"{key}:{admin_id}",
            "admin",
            admin_id,
            text,
        )


async def state(user, value):
    await db.execute(
        """
        UPDATE users
        SET state=$2::jsonb
        WHERE id=$1
        """,
        user,
        json.dumps(value),
    )


async def product_card(chat, product, admin=False):
    bot = admin_bot if admin else main_bot

    currency = (
        "XTR"
        if product["kind"] == "digital"
        else "USD"
    )

    text = (
        f"#{product['id']} — {product['name']}\n"
        f"{format_amount(product['price'], currency)}\n\n"
        f"{product['description']}"
    )

    if product["image"]:
        await bot.call(
            "sendPhoto",
            chat_id=chat,
            photo=("product.jpg", product["image"]),
        )

    buttons = (
        None
        if admin
        else keyboard(
            [("Buy", f"buy:{product['id']}")]
        )
    )

    await bot.send(chat, text, buttons)


async def catalog(chat, offset=0, admin=False):
    rows = await db.fetch(
        """
        SELECT *
        FROM products
        WHERE ($1 OR published)
        ORDER BY id DESC
        LIMIT 5 OFFSET $2
        """,
        admin,
        offset,
    )

    bot = admin_bot if admin else main_bot

    if not rows:
        return await bot.send(
            chat,
            "No products here yet.",
        )

    for product in rows:
        await product_card(chat, product, admin)

    if len(rows) == 5:
        await bot.send(
            chat,
            "More products",
            keyboard(
                [("Next", f"catalog:{offset + 5}")]
            ),
        )


async def payment_menu(chat, product_id=0):
    product = None
    if product_id:
        product = await db.fetchrow("SELECT * FROM products WHERE id=$1 AND published", product_id)
        if not product:
            raise ValueError("This product is unavailable.")
        if product["kind"] == "digital":
            return await main_bot.send(chat, f"{product['name']}\n{format_amount(product['price'], 'XTR')}", keyboard([
                ("⭐ Pay with Stars", f"pay:{product_id}:stars"), ("← Products", "catalog:0")]))
    title = f"{product['name']}\n{format_amount(product['price'], 'USD')}\n\n" if product else ""
    await main_bot.send(chat, title + "How would you like to pay?", keyboard([
        ("🪙 Cryptocurrency", f"crypto:{product_id}"),
        ("🏦 Local bank transfer", f"local:{product_id}:0"),
        ("← Products", "catalog:0"),
    ]))


async def create_order(
    chat,
    product_id,
    method,
    request_key,
    local_currency=None,
):
    existing = await db.fetchrow(
        """
        SELECT * FROM orders
        WHERE request_key=$1
        """,
        request_key,
    )

    if existing:
        return existing

    product = await db.fetchrow(
        """
        SELECT * FROM products
        WHERE id=$1 AND published
        """,
        product_id,
    )

    user = await db.fetchrow(
        "SELECT * FROM users WHERE id=$1",
        chat,
    )

    if not product:
        raise ValueError("Product unavailable.")

    asset = None
    units = None
    address = None
    note = ""

    currency = "XTR"
    amount = product["price"]

    if product["kind"] == "digital":
        if (
            method != "stars"
            or (
                not product["delivery"]
                and not product["file"]
            )
        ):
            raise ValueError(
                "Digital delivery is not configured, "
                "or payment method is invalid."
            )

    else:
        if not user["shipping"]:
            raise ValueError(
                "Use Buy and enter your delivery "
                "address first."
            )

        if method == "local":
            currency = local_currency or user["currency"]

            rate = await setting(
                "rate:" + currency,
                "1" if currency == "USD" else "",
            )

            note = await setting(
                "method:" + currency
            )

            if not rate or not note:
                raise ValueError(
                    "Local payment is unavailable "
                    "for this currency."
                )

            quantum = (
                Decimal("1")
                if currency in (
                    "XAF", "XOF", "RWF", "BIF",
                    "DJF", "GNF", "KMF", "UGX",
                )
                else Decimal("0.001")
                if currency in ("TND", "LYD")
                else Decimal("0.01")
            )

            amount = (
                product["price"] * positive(rate)
            ).quantize(
                quantum,
                rounding=ROUND_UP,
            )

        elif method in ASSETS:
            asset = method
            currency = method

            address = os.getenv(
                f"{asset}_ADDRESS", ""
            ).strip()

            if (
                not address
                or (
                    asset in ("ETH", "SOL")
                    and not os.getenv(
                        f"{asset}_RPC_URL"
                    )
                )
            ):
                raise ValueError(
                    "This payment method is unavailable."
                )

            if asset == "TON":
                ton_address(address)

            try:
                price = await crypto.price(asset)
            except Exception:
                raise ValueError(
                    "Crypto pricing is temporarily "
                    "unavailable. Please try again later."
                )

            scale = Decimal(10) ** ASSETS[asset][1]

            base_units = int(
                (
                    product["price"] / price * scale
                ).to_integral_value(
                    rounding=ROUND_UP
                )
            )

        else:
            raise ValueError(
                "Invalid payment method."
            )

    async with db.acquire() as conn:
        async with conn.transaction():
            # Serializes amount allocation and
            # duplicate callback processing.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(9173002)"
            )

            existing = await conn.fetchrow(
                """
                SELECT * FROM orders
                WHERE request_key=$1
                """,
                request_key,
            )

            if existing:
                return existing

            open_orders = await conn.fetchval(
                """
                SELECT count(*) FROM orders
                WHERE user_id=$1
                  AND status IN (
                    'pending','review','checkout'
                  )
                  AND created > now()-interval '1 day'
                """,
                chat,
            )

            if open_orders >= 10:
                raise ValueError(
                    "You have 10 open orders. Complete "
                    "an order or contact support."
                )

            if asset:
                # Never reuse an asset/amount pair.
                # Small extra units identify this invoice.
                for _ in range(100):
                    units = (
                        base_units
                        + secrets.randbelow(999)
                        + 1
                    )

                    used = await conn.fetchval(
                        """
                        SELECT 1 FROM orders
                        WHERE asset=$1 AND units=$2
                        """,
                        asset,
                        Decimal(units),
                    )

                    if not used:
                        break
                else:
                    raise ValueError(
                        "Please try again in a minute."
                    )

                amount = Decimal(units) / scale

            order = await conn.fetchrow(
                """
                INSERT INTO orders(
                    request_key,user_id,product_id,
                    name,kind,delivery,file,filename,
                    shipping,method,currency,amount,
                    asset,units,address,note,expires
                )
                VALUES(
                    $1,$2,$3,$4,$5,$6,$7,$8,
                    $9,$10,$11,$12,$13,$14,$15,$16,
                    now()+CASE
                        WHEN $13::text IS NULL
                        THEN interval '24 hours'
                        ELSE interval '30 minutes'
                    END
                )
                RETURNING *
                """,
                request_key,
                chat,
                product_id,
                product["name"],
                product["kind"],
                product["delivery"],
                product["file"],
                product["filename"],
                user["shipping"],
                method,
                currency,
                amount,
                asset,
                Decimal(units) if units else None,
                address,
                note,
            )

    return order


async def show_order(chat, order):
    text = (
        f"Order #{order['id']} • {order['name']}\n"
        f"Status: {order['status']}\n"
        f"Amount: {format_amount(order['amount'], order['currency'])}"
    )

    if (
        order["asset"]
        and order["status"] == "pending"
    ):
        amount = (
            Decimal(order["units"])
            / Decimal(10) ** ASSETS[order["asset"]][1]
        )

        text = (
            f"Order #{order['id']} • {order['name']}\n"
            f"Status: {order['status']}\n"
            f"Send EXACTLY {format_amount(amount, order['asset'])}\n"
            f"Network: {ASSETS[order['asset']][0]}\n"
            f"Address: {order['address']}\n"
            f"Send by: "
            f"{order['expires']:%Y-%m-%d %H:%M UTC}\n\n"
            "Fees must be paid separately. "
            "Do not round the amount.\n"
            f"After sending: /tx {order['id']} "
            "TRANSACTION_HASH\n"
            "TON: use the receiving wallet's "
            "transaction hash."
        )

    elif (
        order["method"] == "local"
        and order["status"] in ("pending", "review")
    ):
        text += (
            f"\n\n{order['note']}\n"
            f"Reference: SNOW-{order['id']}\n"
            "Send proof as a photo/document "
            "with caption:\n"
            f"/proof {order['id']}"
        )

    if (
        order["asset"]
        and order["status"] == "pending"
        and order["expires"] <= now()
    ):
        text = (
            f"Order #{order['id']}: quote expired. "
            "Do not send a new payment. "
            "If you already paid on time, submit "
            f"/tx {order['id']} TRANSACTION_HASH "
            "or use /support."
        )

    if order["status"] == "fulfilled":
        text += "\nDelivery: " + order["note"]

    await main_bot.send(chat, text)

    if (
        order["status"] in ("paid", "fulfilled")
        and order["kind"] == "digital"
    ):
        if order["delivery"]:
            await main_bot.send(
                chat,
                order["delivery"],
            )

        if order["file"]:
            await main_bot.call(
                "sendDocument",
                chat_id=chat,
                document=(
                    order["filename"] or "product.bin",
                    order["file"],
                ),
            )


async def mark_paid(conn, order):
    await conn.execute(
        """
        UPDATE orders SET status='paid'
        WHERE id=$1
        """,
        order["id"],
    )

    await queue(
        conn,
        f"paid:{order['id']}",
        "main",
        order["user_id"],
        (
            f"Payment confirmed for order "
            f"#{order['id']}: {order['name']}.\n"
            f"Open /order {order['id']} to view "
            "your purchase/digital delivery."
        ),
    )

    if order["kind"] == "digital":
        await queue(
            conn,
            f"delivery:{order['id']}",
            "delivery",
            order["user_id"],
            str(order["id"]),
        )

    await alert_admins(
        conn,
        f"sale:{order['id']}",
        (
            f"PAID #{order['id']} • {order['name']}\n"
            f"Customer: {order['user_id']}\n"
            f"Method: {order['method']}\n"
            f"Delivery: {order['shipping']}\n"
            f"Inspect: /order {order['id']}"
        ),
    )


ADMIN_HELP = """SnowmanBot admin

/admin — open admin menu
/myid — show your personal Telegram user ID

/add physical | Product name | USD price | Description
/add digital | Product name | Integer Stars price | Description

/edit ID name | New name
/edit ID price | New price
/edit ID description | New description
/delivery ID Delivery text or download link

Send photo with caption /photo ID (max 5 MB)
Send digital file with caption /file ID (max 10 MB)

/publish ID — publish + one new-product alert
/hide ID — hide from store
/products — list products

/method NGN | Bank/mobile-money instructions
/method NGN | off — disable method
/rate NGN 1600 — example only: local units per USD

/reminder 09:00 | Your daily message (UTC)
/reminderoff — disable daily reminders

/orders — recent orders
/order ID — order details
/approve ID — confirm LOCAL payment after checking your bank
/reject ID — reject LOCAL payment
/fulfill ID | Shipping/tracking information
/refund ID — refund a Stars payment

/stats — users, orders, queue counts
/retryjob ID — resend an uncertain notification
/rescan ID — recheck an unresolved crypto order

Run /start in BOTH bots.
Configure payment details before publishing.
"""


async def admin_message(message, request_key):
    chat = message["chat"]["id"]

    text = (
        message.get("text")
        or message.get("caption")
        or ""
    )

    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = rest.strip()

    if cmd in ("/start", "/admin"):
        return await admin_panel(chat)
    if cmd == "/help":
        return await admin_bot.send(chat, ADMIN_HELP, keyboard([("← Admin menu", "admin:menu")]))

    if cmd == "/add":
        pieces = [
            value.strip()
            for value in rest.split("|", 3)
        ]

        if (
            len(pieces) != 4
            or pieces[0] not in ("physical", "digital")
        ):
            raise ValueError(
                "Use /add physical | Name | "
                "USD price | Description"
            )

        kind, name, raw_price, description = pieces
        price = positive(raw_price)

        if (
            not name
            or len(name) > 80
            or not description
            or len(description) > 2500
        ):
            raise ValueError(
                "Name: 1–80 characters. "
                "Description: 1–2500 characters."
            )

        if (
            kind == "digital"
            and (
                price != int(price)
                or price > 1000000
            )
        ):
            raise ValueError(
                "Stars price must be a whole number "
                "from 1 to 1,000,000."
            )

        pid = await db.fetchval(
            """
            INSERT INTO products(
                request_key,kind,name,price,description
            )
            VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(request_key)
            DO UPDATE SET
                request_key=EXCLUDED.request_key
            RETURNING id
            """,
            request_key,
            kind,
            name,
            price,
            description,
        )

        return await admin_bot.send(
            chat,
            f"Created draft product #{pid}. "
            "Add its image/delivery, "
            f"then /publish {pid}.",
        )

    if cmd in (
        "/edit", "/photo", "/file",
        "/delivery", "/publish", "/hide",
    ):
        raw_id, _, value = rest.partition(" ")
        pid = int(raw_id)

        product = await db.fetchrow(
            "SELECT * FROM products WHERE id=$1",
            pid,
        )

        if not product:
            raise ValueError("Product not found.")

        if cmd == "/edit":
            field, sep, new = value.partition("|")
            field = field.strip()
            new = new.strip()

            if (
                field not in (
                    "name", "price", "description"
                )
                or not sep
                or not new
            ):
                raise ValueError(
                    "Use /edit ID name|price|description "
                    "| New value"
                )

            if field == "price":
                new = positive(new)

                if (
                    product["kind"] == "digital"
                    and (
                        new != int(new)
                        or new > 1000000
                    )
                ):
                    raise ValueError(
                        "Digital price must be whole "
                        "Stars, maximum 1,000,000."
                    )

            elif len(new) > (
                80 if field == "name" else 2500
            ):
                raise ValueError("Text is too long.")

            # field is restricted to the allowlist above.
            await db.execute(
                f"UPDATE products SET {field}=$2 "
                "WHERE id=$1",
                pid,
                new,
            )

        elif cmd == "/delivery":
            if not value or len(value) > 3500:
                raise ValueError(
                    "Delivery text must be "
                    "1–3500 characters."
                )

            await db.execute(
                """
                UPDATE products SET delivery=$2
                WHERE id=$1
                """,
                pid,
                value,
            )

        elif cmd in ("/photo", "/file"):
            if cmd == "/photo" and message.get("photo"):
                media = message["photo"][-1]
            elif cmd == "/file":
                media = message.get("document")
            else:
                media = None

            if not media:
                raise ValueError(
                    "Attach the photo/file and use "
                    "the command as its caption."
                )

            maximum = (
                5 if cmd == "/photo" else 10
            ) * 1024 * 1024

            content = await admin_bot.download(
                media["file_id"],
                maximum,
            )

            if cmd == "/photo":
                await db.execute(
                    """
                    UPDATE products SET image=$2
                    WHERE id=$1
                    """,
                    pid,
                    content,
                )
            else:
                await db.execute(
                    """
                    UPDATE products
                    SET file=$2,filename=$3
                    WHERE id=$1
                    """,
                    pid,
                    content,
                    media.get(
                        "file_name", "product.bin"
                    ),
                )

        elif cmd == "/hide":
            await db.execute(
                """
                UPDATE products SET published=false
                WHERE id=$1
                """,
                pid,
            )

        else:
            if (
                product["kind"] == "digital"
                and not product["delivery"]
                and not product["file"]
            ):
                raise ValueError(
                    "Set /delivery or /file before "
                    "publishing a digital product."
                )

            async with db.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE products
                        SET published=true
                        WHERE id=$1
                        """,
                        pid,
                    )

                    await conn.execute(
                        """
                        INSERT INTO jobs(
                            dedup,target,chat,text,marketing
                        )
                        SELECT
                            'product:'||$1::text||':'||id::text,
                            'main',id,$2,true
                        FROM users
                        WHERE alerts AND NOT blocked
                        ON CONFLICT(dedup) DO NOTHING
                        """,
                        str(pid),
                        (
                            f"New product: "
                            f"{product['name']}\n"
                            f"Open /product {pid} "
                            "to see it.\n"
                            "/stop to unsubscribe."
                        ),
                    )

        return await admin_bot.send(
            chat,
            f"Product #{pid} updated.",
        )

    if cmd == "/method":
        code, sep, instructions = rest.partition("|")
        code = code.strip().upper()
        instructions = instructions.strip()

        if (
            not sep
            or code not in CURRENCIES
            or not instructions
            or len(instructions) > 1800
        ):
            raise ValueError(
                "Use /method NGN | Payment instructions "
                "(max 1800 characters)"
            )

        await set_setting(
            "method:" + code,
            (
                ""
                if instructions.lower() == "off"
                else instructions
            ),
        )

    elif cmd == "/rate":
        code, raw_rate = rest.upper().split()

        if code not in CURRENCIES:
            raise ValueError(
                "Unsupported currency code."
            )

        await set_setting(
            "rate:" + code,
            str(positive(raw_rate)),
        )

    elif cmd == "/reminder":
        hour, sep, body = rest.partition("|")
        hour = hour.strip()
        body = body.strip()

        datetime.strptime(hour, "%H:%M")

        if (
            len(hour) != 5
            or not sep
            or not body
            or len(body) > 3000
        ):
            raise ValueError(
                "Use /reminder 09:00 | Message (UTC)"
            )

        await set_setting(
            "reminder",
            json.dumps(
                {
                    "time": hour,
                    "text": body,
                }
            ),
        )

    elif cmd == "/reminderoff":
        await set_setting("reminder", "{}")

    elif cmd == "/orders":
        rows = await db.fetch(
            """
            SELECT id,name,status FROM orders
            ORDER BY id DESC LIMIT 20
            """
        )

        text = "\n".join(
            f"#{row['id']} {row['name']} — "
            f"{row['status']}"
            for row in rows
        )

        return await admin_bot.send(
            chat,
            text or "No orders yet.",
        )

    elif cmd in (
        "/order", "/approve", "/reject",
        "/fulfill", "/refund",
    ):
        raw_id, _, note = rest.partition("|")
        oid = int(raw_id.strip())

        order = await db.fetchrow(
            "SELECT * FROM orders WHERE id=$1",
            oid,
        )

        if not order:
            raise ValueError("Order not found.")

        if cmd == "/order":
            if order["proof"]:
                await admin_bot.call(
                    "sendDocument",
                    chat_id=chat,
                    document=(
                        order["proof_name"]
                        or "proof.jpg",
                        order["proof"],
                    ),
                )

            return await admin_bot.send(
                chat,
                (
                    f"#{oid} {order['name']}\n"
                    f"User: {order['user_id']}\n"
                    f"{order['status']} • "
                    f"{order['method']}\n"
                    f"{format_amount(order['amount'], order['currency'])}\n"
                    f"Crypto units: {order['units']}\n"
                    f"TX: {order['txid']}\n"
                    f"Address: {order['address']}\n"
                    f"Shipping: {order['shipping']}\n"
                    f"Created: {order['created']}\n"
                    f"Expires: {order['expires']}"
                ),
            )

        if cmd == "/refund":
            if (
                order["method"] != "stars"
                or order["status"] not in (
                    "paid", "fulfilled", "refunding"
                )
            ):
                raise ValueError(
                    "Only paid Stars orders can "
                    "be refunded here."
                )

            await db.execute(
                """
                UPDATE orders SET status='refunding'
                WHERE id=$1
                """,
                oid,
            )

            await main_bot.call(
                "refundStarPayment",
                user_id=order["user_id"],
                telegram_payment_charge_id=order["charge"],
            )

            await db.execute(
                """
                UPDATE orders SET status='refunded'
                WHERE id=$1
                """,
                oid,
            )

            await queue(
                db,
                f"refund:{oid}",
                "main",
                order["user_id"],
                f"Order #{oid} refunded in Stars.",
            )

        else:
            async with db.acquire() as conn:
                async with conn.transaction():
                    order = await conn.fetchrow(
                        """
                        SELECT * FROM orders
                        WHERE id=$1 FOR UPDATE
                        """,
                        oid,
                    )

                    if cmd == "/fulfill":
                        if (
                            order["status"] != "paid"
                            or not note.strip()
                            or len(note) > 2000
                        ):
                            raise ValueError(
                                "Use /fulfill ID | "
                                "Delivery information "
                                "for a paid order."
                            )

                        await conn.execute(
                            """
                            UPDATE orders
                            SET status='fulfilled',note=$2
                            WHERE id=$1
                            """,
                            oid,
                            note.strip(),
                        )

                        await queue(
                            conn,
                            f"fulfilled:{oid}",
                            "main",
                            order["user_id"],
                            f"Order #{oid}: {note.strip()}",
                        )

                    else:
                        if (
                            order["method"] != "local"
                            or order["status"] not in (
                                "review", "pending"
                            )
                        ):
                            raise ValueError(
                                "Only pending LOCAL "
                                "payments can be approved/"
                                "rejected manually."
                            )

                        if cmd == "/approve":
                            await mark_paid(conn, order)
                        else:
                            await conn.execute(
                                """
                                UPDATE orders
                                SET status='rejected'
                                WHERE id=$1
                                """,
                                oid,
                            )

                            await queue(
                                conn,
                                f"rejected:{oid}",
                                "main",
                                order["user_id"],
                                (
                                    f"Payment for order "
                                    f"#{oid} was rejected. "
                                    "/support"
                                ),
                            )

    elif cmd == "/stats":
        users = await db.fetchval(
            "SELECT count(*) FROM users"
        )

        rows = await db.fetch(
            """
            SELECT status,count(*) AS n
            FROM orders GROUP BY status
            """
        )

        jobs = await db.fetch(
            """
            SELECT id,status FROM jobs
            WHERE status IN ('uncertain','sending')
            ORDER BY id DESC LIMIT 10
            """
        )

        summary = ", ".join(
            f"{row['status']}={row['n']}"
            for row in rows
        )

        uncertain = ", ".join(
            str(row["id"])
            for row in jobs
        )

        return await admin_bot.send(
            chat,
            f"Users: {users}\n"
            f"Orders: {summary}\n"
            f"Uncertain notifications: {uncertain}",
        )

    elif cmd == "/rescan":
        await db.execute(
            """
            UPDATE orders SET status='pending'
            WHERE id=$1 AND status='needs_review'
            """,
            int(rest),
        )

    elif cmd == "/retryjob":
        await db.execute(
            """
            UPDATE jobs SET status='queued',due=now()
            WHERE id=$1
              AND status IN ('uncertain','failed')
            """,
            int(rest),
        )

    else:
        raise ValueError(
            "Unknown command. Use /help."
        )

    await admin_bot.send(chat, "Saved.")


async def customer_message(message):
    chat = message["chat"]["id"]
    payment = message.get("successful_payment")

    if payment:
        oid = int(
            payment["invoice_payload"].removeprefix(
                "snow:"
            )
        )

        async with db.acquire() as conn:
            async with conn.transaction():
                order = await conn.fetchrow(
                    """
                    SELECT * FROM orders
                    WHERE id=$1 FOR UPDATE
                    """,
                    oid,
                )

                if (
                    not order
                    or order["user_id"] != chat
                    or order["method"] != "stars"
                    or payment["currency"] != "XTR"
                    or Decimal(payment["total_amount"])
                    != order["amount"]
                ):
                    await alert_admins(
                        conn,
                        (
                            "payment-error:"
                            + payment[
                                "telegram_payment_charge_id"
                            ]
                        ),
                        "Unmatched Stars payment. "
                        "Inspect Telegram transactions "
                        "immediately.",
                    )
                    return

                charge = payment[
                    "telegram_payment_charge_id"
                ]

                if order["charge"] == charge:
                    return

                if order["charge"]:
                    await alert_admins(
                        conn,
                        f"duplicate:{charge}",
                        (
                            f"Additional Stars payment "
                            f"for #{oid}. Investigate/"
                            f"refund charge: {charge}"
                        ),
                    )
                    return

                await conn.execute(
                    """
                    UPDATE orders SET charge=$2
                    WHERE id=$1
                    """,
                    oid,
                    charge,
                )

                await mark_paid(conn, order)

        return

    if message.get("refunded_payment"):
        await db.execute(
            """
            UPDATE orders SET status='refunded'
            WHERE charge=$1
            """,
            message["refunded_payment"][
                "telegram_payment_charge_id"
            ],
        )
        return

    text = (
        message.get("text")
        or message.get("caption")
        or ""
    )

    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = rest.strip()

    user = await db.fetchrow(
        "SELECT * FROM users WHERE id=$1",
        chat,
    )

    pending = json.loads(user["state"])

    if cmd in ("/cancel", "/start"):
        await state(chat, {})
        return await main_bot.send(chat,
            "☃️ Welcome to SnowmanBot!\nBrowse our products and choose crypto or local bank transfer at checkout.\nDigital products use Stars.", home_buttons())

    if cmd in ("/support", "/paysupport"):
        return await main_bot.send(chat, SUPPORT)

    if cmd in ("/stop", "/subscribe"):
        enabled = cmd == "/subscribe"

        await db.execute(
            """
            UPDATE users SET alerts=$2
            WHERE id=$1
            """,
            chat,
            enabled,
        )

        return await main_bot.send(
            chat,
            (
                "Notifications enabled."
                if enabled
                else "Marketing notifications stopped. "
                "Order updates stay enabled."
            ),
        )

    if cmd == "/currency":
        return await local_currency_menu(chat)
    if cmd == "/payments":
        return await payment_menu(chat)

    if cmd == "/products":
        return await catalog(chat)

    if cmd == "/product":
        product = await db.fetchrow(
            """
            SELECT * FROM products
            WHERE id=$1 AND published
            """,
            int(rest),
        )

        if not product:
            raise ValueError("Product unavailable.")

        return await product_card(chat, product)

    if cmd == "/orders":
        rows = await db.fetch(
            """
            SELECT id,name,status FROM orders
            WHERE user_id=$1
            ORDER BY id DESC LIMIT 20
            """,
            chat,
        )

        text = "\n".join(
            f"/order {row['id']} — {row['name']} "
            f"({row['status']})"
            for row in rows
        )

        return await main_bot.send(
            chat,
            text or "No orders yet.",
        )

    if cmd == "/address":
        if not rest:
            return await main_bot.send(
                chat,
                "Set delivery details with /address "
                "Name, phone, country, full address. "
                "These apply to future orders.",
            )

        if len(rest) > 1500:
            raise ValueError("Address too long.")

        await db.execute(
            """
            UPDATE users SET shipping=$2
            WHERE id=$1
            """,
            chat,
            rest,
        )

        return await main_bot.send(
            chat,
            "Delivery address saved.",
        )

    if cmd in ("/order", "/tx", "/proof"):
        raw_id, _, value = rest.partition(" ")

        order = await db.fetchrow(
            """
            SELECT * FROM orders
            WHERE id=$1 AND user_id=$2
            """,
            int(raw_id),
            chat,
        )

        if not order:
            raise ValueError("Order not found.")

        if cmd == "/order":
            return await show_order(chat, order)

        if cmd == "/tx":
            if (
                not order["asset"]
                or order["status"] != "pending"
            ):
                raise ValueError(
                    "This order does not accept "
                    "a transaction hash."
                )

            value = tx_hash(
                order["asset"],
                value,
            )

            await db.execute(
                """
                UPDATE orders SET txid=$2
                WHERE id=$1 AND status='pending'
                """,
                order["id"],
                value,
            )

            return await main_bot.send(
                chat,
                "Transaction saved. I will verify "
                "the exact amount, destination, "
                "network, payment time and confirmations. "
                f"Use /order {order['id']} "
                "to check status.",
            )

        if (
            order["method"] != "local"
            or order["status"] not in (
                "pending", "review"
            )
        ):
            raise ValueError(
                "This order does not accept "
                "local-payment proof."
            )

        if (
            not message.get("photo")
            and not message.get("document")
        ):
            raise ValueError(
                "Send a photo/file with caption "
                "/proof ORDER_ID"
            )

        media = (
            message["photo"][-1]
            if message.get("photo")
            else message["document"]
        )

        content = await main_bot.download(
            media["file_id"],
            10 * 1024 * 1024,
        )

        async with db.acquire() as conn:
            async with conn.transaction():
                changed = await conn.fetchval(
                    """
                    UPDATE orders
                    SET status='review',
                        proof=$2,
                        proof_name=$3
                    WHERE id=$1
                      AND status IN ('pending','review')
                    RETURNING id
                    """,
                    order["id"],
                    content,
                    media.get("file_name", "proof.jpg"),
                )

                if not changed:
                    raise ValueError(
                        "Order status changed. "
                        "Use /order to check it."
                    )

                await alert_admins(
                    conn,
                    (
                        f"proof:{order['id']}:"
                        f"{message['message_id']}"
                    ),
                    (
                        "New local-payment proof: "
                        f"/order {order['id']}\n"
                        "Check your bank, then "
                        f"/approve {order['id']} or "
                        f"/reject {order['id']}."
                    ),
                )

        return await main_bot.send(
            chat,
            "Proof submitted. We will notify you "
            "after payment review.",
        )

    if (
        "shipping" in pending
        and text
        and not text.startswith("/")
    ):
        if len(text) > 1500:
            raise ValueError("Address too long.")

        await db.execute(
            """
            UPDATE users SET shipping=$2
            WHERE id=$1
            """,
            chat,
            text,
        )

        await state(chat, {})

        checkout = pending.get("checkout")
        if checkout:
            return await begin_checkout(chat, int(pending["shipping"]), checkout["method"],
                f"shipping:{chat}:{message['message_id']}", checkout.get("currency"))
        return await payment_menu(chat, int(pending["shipping"]))

    await main_bot.send(
        chat,
        "Use /start to open the store.",
    )


async def callback(query, admin, request_key):
    bot = admin_bot if admin else main_bot
    chat, data = query["from"]["id"], query["data"]
    try:
        await bot.call("answerCallbackQuery", callback_query_id=query["id"])
    except TGError:
        pass
    if admin:
        if data == "admin:menu":
            return await admin_panel(chat)
        if data == "admin:myid":
            return await bot.send(chat, f"Your Telegram user ID: {chat}")
        if data == "admin:add":
            return await bot.send(chat, "Add a product using one of these formats:\n\n/add physical | Name | USD price | Description\n/add digital | Name | Stars price | Description\n\nThen send an image with caption /photo ID and publish using /publish ID.", keyboard([("← Admin menu", "admin:menu")]))
        if data == "admin:settings":
            return await bot.send(chat, "Bank details: /method NGN | Your account details\nExchange rate: /rate NGN YOUR_RATE\nDaily reminder (UTC): /reminder 08:00 | Your message\nDisable reminder: /reminderoff\n\nOnly configured bank currencies appear to customers. Crypto wallet addresses remain in hosting variables.", keyboard([("← Admin menu", "admin:menu")]))
        if data in ("admin:products", "admin:orders", "admin:stats", "admin:help"):
            return await admin_message({"chat": {"id": chat}, "text": "/" + data.split(":")[1]}, request_key)
        if data.startswith("catalog:"):
            return await catalog(chat, int(data.split(":")[1]), True)
        return await admin_panel(chat)
    if data.startswith("catalog:"):
        return await catalog(chat, int(data.split(":")[1]))
    if data in ("payments", "currencies"):
        return await payment_menu(chat)
    if data in ("myorders", "support"):
        return await customer_message({"chat": {"id": chat}, "text": "/orders" if data == "myorders" else "/support"})
    if data.startswith("local:"):
        _, pid, page = data.split(":")
        return await local_currency_menu(chat, int(pid), int(page))
    if data.startswith("crypto:"):
        return await crypto_menu(chat, int(data.split(":")[1]))
    if data.startswith("bank:"):
        _, pid, code = data.split(":")
        if code not in await local_methods():
            raise ValueError("That bank-transfer option is no longer available.")
        await db.execute("UPDATE users SET currency=$2 WHERE id=$1", chat, code)
        return await begin_checkout(chat, int(pid), "local", request_key, code)
    if data.startswith("currency:"):
        code = data.split(":")[1]
        if code not in await local_methods():
            return await local_currency_menu(chat)
        await db.execute("UPDATE users SET currency=$2 WHERE id=$1", chat, code)
        return await bot.send(chat, f"Local payment currency saved: {code}.", keyboard([("Browse products", "catalog:0")]))
    if data.startswith("alerts:"):
        enabled = data == "alerts:on"
        await db.execute("UPDATE users SET alerts=$2 WHERE id=$1", chat, enabled)
        return await bot.send(chat, "Reminders and product alerts enabled." if enabled else "Marketing notifications disabled.")
    if data.startswith("buy:"):
        return await payment_menu(chat, int(data.split(":")[1]))
    if data.startswith("pay:"):
        _, pid, method = data.split(":")
        if method == "local":
            return await local_currency_menu(chat, int(pid))
        return await begin_checkout(chat, int(pid), method, request_key)


async def handle(update, admin):
    bot = admin_bot if admin else main_bot

    request_key = (
        f"{'admin' if admin else 'main'}:"
        f"{update['update_id']}"
    )

    query = update.get("pre_checkout_query")

    if query:
        if admin:
            return

        ok = False

        if query.get(
            "invoice_payload", ""
        ).startswith("snow:"):
            async with db.acquire() as conn:
                async with conn.transaction():
                    order = await conn.fetchrow(
                        """
                        SELECT * FROM orders
                        WHERE id=$1 FOR UPDATE
                        """,
                        int(query["invoice_payload"][5:]),
                    )

                    ok = bool(
                        order
                        and order["user_id"]
                        == query["from"]["id"]
                        and order["method"] == "stars"
                        and query["currency"] == "XTR"
                        and Decimal(query["total_amount"])
                        == order["amount"]
                        and order["expires"] > now()
                        and (
                            order["status"] == "pending"
                            or (
                                order["status"] == "checkout"
                                and order["checkout_id"]
                                == query["id"]
                            )
                        )
                    )

                    if ok:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET status='checkout',
                                checkout_id=$2
                            WHERE id=$1
                            """,
                            order["id"],
                            query["id"],
                        )

        await bot.call(
            "answerPreCheckoutQuery",
            pre_checkout_query_id=query["id"],
            ok=ok,
            error_message=(
                None
                if ok
                else "Order expired or already processing. "
                "Create a new order."
            ),
        )
        return

    query = update.get("callback_query")
    message = update.get("message")
    item = query or message

    if not item or not item.get("from"):
        return

    user = item["from"]["id"]

    chat = (
        query.get("message", {})
        if query
        else message
    ).get("chat", {})

    if (
        chat.get("type") != "private"
        or chat.get("id") != user
    ):
        return

    command = (message.get("text", "").split(maxsplit=1) or [""])[0].split("@")[0].lower() if message else ""
    if command == "/myid":
        await bot.send(user, f"Your Telegram user ID: {user}")
        return
    if admin and user not in ADMINS:
        if query:
            try:
                await bot.call("answerCallbackQuery", callback_query_id=query["id"], text="Admin access required", show_alert=True)
            except TGError:
                pass
        await bot.send(user, f"🔒 Admin access is not enabled for this account.\nYour Telegram user ID: {user}\n\nStore owner: add this numeric USER ID to ADMIN_IDS in your hosting environment, then deploy the updated variables. Use your personal user ID, not a bot ID or @username.")
        LOG.warning("Admin request denied: user is not in ADMIN_IDS")
        return

    if not admin:
        await db.execute(
            """
            INSERT INTO users(id) VALUES($1)
            ON CONFLICT(id)
            DO UPDATE SET blocked=false
            """,
            user,
        )

    try:
        if query:
            await callback(
                query,
                admin,
                request_key,
            )
        elif admin:
            await admin_message(
                message,
                request_key,
            )
        else:
            await customer_message(message)

    except TGError as error:
        if error.code >= 500 or error.code == 429:
            raise

        if error.code != 403:
            await bot.send(
                user,
                "Telegram rejected that action. "
                "Check the command and bot permissions; "
                "use /support for payment problems.",
            )

    except (ValueError, ArithmeticError) as error:
        await bot.send(
            user,
            (
                str(error)[:500]
                if isinstance(error, ValueError)
                else "Invalid number."
            ),
        )


async def poll(admin):
    bot, key = (
        (admin_bot, f"offset:admin:{BOT_IDS['admin']}")
        if admin
        else (main_bot, f"offset:main:{BOT_IDS['main']}")
    )

    role = "admin" if admin else "main"
    offset = int(await setting(key, await setting("offset:" + role, "0")))
    # The migration below removes inherited legacy offsets for a new bot identity.
    identity_key = "identity:" + role
    previous_id = await setting(identity_key)
    if previous_id != str(BOT_IDS[role]):
        offset = 0
        await set_setting(identity_key, str(BOT_IDS[role]))
        await set_setting(key, "0")
    LOG.info("Polling %s bot started", role)

    await bot.call(
        "deleteWebhook",
        drop_pending_updates=False,
    )

    while True:
        try:
            updates = await bot.call(
                "getUpdates",
                offset=offset,
                timeout=40,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "pre_checkout_query",
                ],
            )

            for update in updates:
                try:
                    await handle(update, admin)
                except TGError as error:
                    if error.code not in (400, 403):
                        raise
                    # Expired callbacks/pre-checkouts must not block later updates.
                    LOG.warning("Skipped expired/rejected %s update: %s", role, error)

                offset = update["update_id"] + 1

                await set_setting(
                    key,
                    str(offset),
                )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            # Never log URLs, tokens or uploaded proof.
            LOG.warning(
                "Polling %s: %s",
                "admin" if admin else "main",
                str(error) if isinstance(error, TGError) else type(error).__name__,
            )
            if isinstance(error, TGError) and error.code == 409:
                raise RuntimeError("Telegram polling conflict: stop another service using this bot token") from None
            await asyncio.sleep(max(error.retry, 3) if isinstance(error, TGError) else 3)


async def worker():
    # After a crash, a message may have reached Telegram
    # before its DB status was saved. Do not blindly resend.
    await db.execute(
        """
        UPDATE jobs SET status='uncertain'
        WHERE status='sending'
        """
    )

    last_scheduled_minute = None
    while True:
        try:
            reminder = json.loads(
                await setting("reminder", "{}")
            )

            moment = now()

            if (
                reminder.get("time")
                and last_scheduled_minute != moment.strftime("%Y-%m-%d %H:%M")
                and moment.strftime("%H:%M")
                >= reminder["time"]
            ):
                await db.execute(
                    """
                    INSERT INTO jobs(
                        dedup,target,chat,text,
                        marketing,expires
                    )
                    SELECT
                        $1||':'||id::text,
                        'main',
                        id,
                        $2,
                        true,
                        (
                            (now() AT TIME ZONE 'UTC')::date
                            + 1
                        )::timestamp AT TIME ZONE 'UTC'
                    FROM users
                    WHERE alerts AND NOT blocked
                    ON CONFLICT(dedup) DO NOTHING
                    """,
                    "daily:" + moment.strftime("%Y-%m-%d"),
                    (
                        reminder["text"]
                        + "\n\n/products • "
                        "/stop to unsubscribe."
                    ),
                )

                last_scheduled_minute = moment.strftime("%Y-%m-%d %H:%M")

            job = await db.fetchrow(
                """
                UPDATE jobs SET status='sending'
                WHERE id=(
                    SELECT id FROM jobs
                    WHERE status='queued' AND due<=now()
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """
            )

            if not job:
                await asyncio.sleep(2)
                continue

            if job["marketing"]:
                subscribed = await db.fetchval(
                    """
                    SELECT alerts AND NOT blocked
                    FROM users WHERE id=$1
                    """,
                    job["chat"],
                )

                if job["expires"] <= now() or not subscribed:
                    await db.execute(
                        """
                        UPDATE jobs SET status='skipped'
                        WHERE id=$1
                        """,
                        job["id"],
                    )
                    continue

            try:
                if job["target"] == "delivery":
                    order = await db.fetchrow(
                        "SELECT * FROM orders WHERE id=$1",
                        int(job["text"]),
                    )

                    if order and order["status"] in (
                        "paid", "fulfilled"
                    ):
                        await show_order(
                            job["chat"],
                            order,
                        )
                else:
                    bot = (
                        admin_bot
                        if job["target"] == "admin"
                        else main_bot
                    )

                    await bot.send(
                        job["chat"],
                        job["text"],
                    )

                await db.execute(
                    """
                    UPDATE jobs SET status='sent'
                    WHERE id=$1
                    """,
                    job["id"],
                )

            except TGError as error:
                if error.code == 429:
                    await db.execute(
                        """
                        UPDATE jobs
                        SET status='queued',
                            due=now()+$2*interval '1 second'
                        WHERE id=$1
                        """,
                        job["id"],
                        max(error.retry, 5),
                    )

                else:
                    await db.execute(
                        """
                        UPDATE jobs SET status=$2
                        WHERE id=$1
                        """,
                        job["id"],
                        (
                            "uncertain"
                            if error.code >= 500
                            else "failed"
                        ),
                    )

                    if (
                        error.code == 403
                        and job["target"] == "main"
                    ):
                        await db.execute(
                            """
                            UPDATE users SET blocked=true
                            WHERE id=$1
                            """,
                            job["chat"],
                        )

            except Exception:
                await db.execute(
                    """
                    UPDATE jobs SET status='uncertain'
                    WHERE id=$1
                    """,
                    job["id"],
                )

            await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            LOG.warning(
                "Notification worker: %s",
                type(error).__name__,
            )

            await asyncio.sleep(5)


async def payment_worker():
    while True:
        try:
            rows = await db.fetch(
                """
                SELECT * FROM orders
                WHERE asset IS NOT NULL
                  AND txid IS NOT NULL
                  AND status='pending'
                ORDER BY id
                """
            )

            for order in rows:
                try:
                    valid = await crypto.verify(order)

                    if valid:
                        async with db.acquire() as conn:
                            async with conn.transaction():
                                fresh = await conn.fetchrow(
                                    """
                                    SELECT * FROM orders
                                    WHERE id=$1 FOR UPDATE
                                    """,
                                    order["id"],
                                )

                                if (
                                    fresh["status"] == "pending"
                                    and fresh["txid"]
                                    == order["txid"]
                                ):
                                    claimed = await conn.fetchval(
                                        """
                                        INSERT INTO payments(
                                            asset,txid,order_id
                                        )
                                        VALUES($1,$2,$3)
                                        ON CONFLICT DO NOTHING
                                        RETURNING order_id
                                        """,
                                        order["asset"],
                                        order["txid"],
                                        order["id"],
                                    )

                                    if claimed:
                                        await mark_paid(
                                            conn,
                                            fresh,
                                        )

                    elif (
                        now() - order["expires"]
                    ).total_seconds() > 86400:
                        async with db.acquire() as conn:
                            async with conn.transaction():
                                result = await conn.fetchval(
                                    """
                                    UPDATE orders
                                    SET status='needs_review'
                                    WHERE id=$1
                                      AND status='pending'
                                    RETURNING id
                                    """,
                                    order["id"],
                                )

                                if result:
                                    await alert_admins(
                                        conn,
                                        (
                                            "crypto-review:"
                                            f"{order['id']}"
                                        ),
                                        (
                                            f"Crypto order "
                                            f"#{order['id']} was "
                                            "not verified within "
                                            "24h after expiry. "
                                            "Investigate the "
                                            "transaction; do not "
                                            "request another "
                                            "payment before "
                                            "checking."
                                        ),
                                    )

                                    await queue(
                                        conn,
                                        (
                                            "crypto-review-user:"
                                            f"{order['id']}"
                                        ),
                                        "main",
                                        order["user_id"],
                                        (
                                            f"Order #{order['id']} "
                                            "needs payment review. "
                                            "/support"
                                        ),
                                    )

                except Exception as error:
                    LOG.warning(
                        "Crypto order %s: %s",
                        order["id"],
                        type(error).__name__,
                    )

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            LOG.warning(
                "Payment worker: %s",
                type(error).__name__,
            )

        await asyncio.sleep(60)


async def keep_lock(connection):
    while True:
        await connection.fetchval("SELECT 1")
        await asyncio.sleep(10)


async def start_http(runtime, port=None):
    async def health(request):
        if not runtime["ready"]:
            return web.json_response({"status": runtime["role"]}, status=503)
        try:
            await asyncio.wait_for(db.fetchval("SELECT 1"), timeout=2)
        except Exception:
            return web.json_response({"status": "database_unavailable"}, status=503)
        return web.json_response({"status": runtime["role"], "version": VERSION})
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    selected_port = int(os.getenv("PORT", "10000")) if port is None else port
    await web.TCPSite(runner, "0.0.0.0", selected_port).start()
    LOG.info("HTTP health endpoint bound to 0.0.0.0:%s", selected_port)
    return runner


async def main():
    global db, main_bot, admin_bot, crypto
    runtime = {"ready": False, "role": "starting"}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    runner = await start_http(runtime)
    db = None
    tasks = []
    try:
        main_token = os.environ["MAIN_BOT_TOKEN"].strip()
        admin_token = os.environ["ADMIN_BOT_TOKEN"].strip()
        if main_token == admin_token:
            raise RuntimeError("MAIN_BOT_TOKEN and ADMIN_BOT_TOKEN must be for two different bots")
        dsn = os.getenv("DATABASE_URL", "").strip()
        parsed = urlparse(dsn)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            raise RuntimeError("Set DATABASE_URL to your hosted PostgreSQL connection URL, not localhost or an empty value")
        if not ADMINS:
            LOG.warning("No ADMIN_IDS configured. The admin bot will show your user ID but keep management locked.")
        db = await asyncpg.create_pool(dsn, min_size=2, max_size=8, command_timeout=30)
        async with db.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(9173003)")
                await connection.execute(SCHEMA)
        for env_name, prefix in [("LOCAL_PAYMENTS_JSON", "method:"), ("FX_RATES_JSON", "rate:")]:
            for code, value in json.loads(os.getenv(env_name, "{}")).items():
                if code not in CURRENCIES:
                    raise ValueError("Unsupported currency in " + env_name)
                value = str(positive(str(value))) if prefix == "rate:" else str(value)
                if len(value)>1800:
                    raise ValueError("Payment instructions too long")
                await db.execute("INSERT INTO settings VALUES($1,$2) ON CONFLICT DO NOTHING", prefix+code, value)
        async with aiohttp.ClientSession() as session:
            main_bot = Telegram(main_token, session)
            admin_bot = Telegram(admin_token, session)
            crypto = Crypto(session)
            customer_info, admin_info = await asyncio.gather(main_bot.call("getMe"), admin_bot.call("getMe"))
            if customer_info["id"] == admin_info["id"]:
                raise RuntimeError("Both tokens identify the same Telegram bot; use a separate admin bot")
            BOT_IDS.update(main=customer_info["id"], admin=admin_info["id"])
            LOG.info("Customer bot: @%s | Admin bot: @%s | Configured admin users: %s", customer_info.get("username"), admin_info.get("username"), len(ADMINS))
            # A verified standby is HTTP-ready before taking the polling lock.
            # This lets Render retire the previous instance during a rolling deploy.
            runtime.update(ready=True, role="standby")
            async with db.acquire() as leader:
                while not await leader.fetchval("SELECT pg_try_advisory_lock(9173001)"):
                    if stop.is_set():
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
                if stop.is_set():
                    return
                runtime["role"] = "running"
                LOG.info("SnowmanBot started | %s", VERSION)
                tasks = [asyncio.create_task(coro) for coro in (poll(False), poll(True), worker(), payment_worker(), keep_lock(leader))]
                shutdown = asyncio.create_task(stop.wait())
                tasks.append(shutdown)
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if shutdown not in done:
                        for task in done:
                            task.result()
                        raise RuntimeError("A SnowmanBot background task stopped unexpectedly")
                finally:
                    runtime.update(ready=False, role="stopping")
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        runtime.update(ready=False, role="stopping")
        if db is not None:
            try:
                await asyncio.wait_for(db.close(), timeout=5)
            except Exception:
                db.terminate()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

