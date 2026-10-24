import base64
import binascii
import json
import os
import re
import time
from decimal import Decimal


ASSETS = {
    "USDT": ("TRC-20 / TRON", 6, "tether"),
    "BTC": ("Bitcoin", 8, "bitcoin"),
    "ETH": ("Ethereum mainnet", 18, "ethereum"),
    "SOL": ("Solana", 9, "solana"),
    "TON": ("TON", 9, "the-open-network"),
}

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def ton_address(value):
    if re.fullmatch(r"-?\d+:[0-9a-fA-F]{64}", value):
        wc, body = value.split(":")
        return f"{int(wc)}:{body.lower()}"

    raw = base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )

    if (
        len(raw) != 36
        or binascii.crc_hqx(raw[:34], 0).to_bytes(2, "big")
        != raw[34:]
    ):
        raise ValueError("Invalid TON address")

    if raw[0] not in (0x11, 0x51):
        raise ValueError("Use a mainnet TON address")

    workchain = int.from_bytes(
        raw[1:2], "big", signed=True
    )

    return f"{workchain}:{raw[2:34].hex()}"


def tx_hash(asset, value):
    value = value.strip()

    if asset == "SOL":
        if not re.fullmatch(
            r"[1-9A-HJ-NP-Za-km-z]{80,90}", value
        ):
            raise ValueError(
                "Send the Solana transaction signature, not a link."
            )
        return value

    if (
        asset == "TON"
        and not re.fullmatch(r"[a-fA-F0-9]{64}", value)
    ):
        try:
            raw = base64.urlsafe_b64decode(
                value + "=" * (-len(value) % 4)
            )
            if len(raw) != 32:
                raise ValueError()
            return raw.hex()
        except Exception:
            raise ValueError(
                "Send the recipient TON transaction hash, not a link."
            )

    value = value.removeprefix("0x")

    if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError(
            "Send a 64-character transaction hash, not a link."
        )

    prefix = "0x" if asset == "ETH" else ""
    return prefix + value.lower()


class Crypto:
    def __init__(self, session):
        self.http = session
        self.cache = {}

    async def request(
        self,
        url,
        *,
        params=None,
        body=None,
        headers=None,
    ):
        async with self.http.request(
            "POST" if body is not None else "GET",
            url,
            params=params,
            json=body,
            headers=headers,
            timeout=25,
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Payment API HTTP {response.status}"
                )

            return json.loads(
                await response.text(),
                parse_float=str,
            )

    async def rpc(self, url, method, params):
        data = await self.request(
            url,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
        )

        if data.get("error"):
            raise RuntimeError("Payment RPC unavailable")

        return data.get("result")

    async def price(self, asset):
        cached = self.cache.get(asset)

        if cached and time.time() - cached[0] < 60:
            return cached[1]

        key = os.getenv("COINGECKO_API_KEY", "")

        data = await self.request(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ASSETS[asset][2],
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            headers=(
                {"x-cg-demo-api-key": key}
                if key
                else {}
            ),
        )

        item = data[ASSETS[asset][2]]
        price = Decimal(str(item["usd"]))
        age = time.time() - int(item["last_updated_at"])

        if (
            not price.is_finite()
            or price <= 0
            or not -60 <= age <= 600
        ):
            raise RuntimeError(
                "Crypto price is unavailable or stale"
            )

        self.cache[asset] = (time.time(), price)
        return price

    async def verify(self, order):
        asset = order["asset"]
        tx = order["txid"]
        address = order["address"]
        expected = int(order["units"])

        amount = None
        timestamp = None

        if asset == "BTC":
            base = "https://blockstream.info/api"
            data = await self.request(f"{base}/tx/{tx}")
            status = data.get("status", {})

            if (
                not status.get("confirmed")
                or data.get("txid") != tx
            ):
                return False

            tip = await self.request(
                f"{base}/blocks/tip/height"
            )

            confirmations = (
                int(tip) - int(status["block_height"]) + 1
            )

            if confirmations < 3:
                return False

            amount = sum(
                int(output["value"])
                for output in data["vout"]
                if output.get("scriptpubkey_address")
                == address
            )

            timestamp = status["block_time"]

        elif asset == "ETH":
            url = os.environ["ETH_RPC_URL"]

            chain_id = await self.rpc(
                url, "eth_chainId", []
            )

            if int(chain_id, 16) != 1:
                raise RuntimeError(
                    "ETH RPC must use Ethereum mainnet"
                )

            data = await self.rpc(
                url,
                "eth_getTransactionByHash",
                [tx],
            )

            receipt = await self.rpc(
                url,
                "eth_getTransactionReceipt",
                [tx],
            )

            if (
                not data
                or not receipt
                or receipt.get("status") != "0x1"
            ):
                return False

            if (
                (data.get("to") or "").lower()
                != address.lower()
            ):
                return False

            tip = int(
                await self.rpc(
                    url, "eth_blockNumber", []
                ),
                16,
            )

            confirmations = (
                tip
                - int(receipt["blockNumber"], 16)
                + 1
            )

            if confirmations < 12:
                return False

            block = await self.rpc(
                url,
                "eth_getBlockByHash",
                [receipt["blockHash"], False],
            )

            if (
                not block
                or data.get("blockHash")
                != receipt["blockHash"]
            ):
                return False

            amount = int(data["value"], 16)
            timestamp = int(block["timestamp"], 16)

        elif asset == "SOL":
            url = os.environ["SOL_RPC_URL"]

            data = await self.rpc(
                url,
                "getTransaction",
                [
                    tx,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )

            if (
                not data
                or not data.get("meta")
                or data["meta"].get("err") is not None
            ):
                return False

            if tx not in data["transaction"]["signatures"]:
                return False

            instructions = data[
                "transaction"
            ]["message"]["instructions"]

            amount = 0

            for instruction in instructions:
                parsed = instruction.get("parsed", {})
                info = parsed.get("info", {})

                if (
                    instruction.get("programId")
                    == "11111111111111111111111111111111"
                    and parsed.get("type") == "transfer"
                    and info.get("destination") == address
                ):
                    amount += int(info["lamports"])

            timestamp = data.get("blockTime")

        elif asset == "TON":
            key = os.getenv("TONCENTER_API_KEY", "")

            data = await self.request(
                "https://toncenter.com/api/v3/transactions",
                params={
                    "hash": tx,
                    "limit": 10,
                },
                headers=(
                    {"X-API-Key": key}
                    if key
                    else {}
                ),
            )

            for item in data.get("transactions", []):
                message = item.get("in_msg") or {}

                if (
                    ton_address(item["account"])
                    == ton_address(address)
                    and tx_hash("TON", item["hash"]) == tx
                    and item.get("mc_block_seqno") is not None
                    and not item.get("emulated", False)
                    and item.get(
                        "description", {}
                    ).get("aborted") is False
                    and message.get("bounced") is False
                    and message.get("source")
                    and ton_address(message["destination"])
                    == ton_address(address)
                ):
                    amount = int(message["value"])
                    timestamp = int(item["now"])
                    break

        elif asset == "USDT":
            key = os.getenv("TRONGRID_API_KEY", "")

            params = {
                "only_confirmed": "true",
                "only_to": "true",
                "limit": 200,
                "contract_address": USDT_CONTRACT,
                "min_timestamp": (
                    int(order["created"].timestamp()) * 1000
                ),
                "max_timestamp": (
                    int(order["expires"].timestamp()) * 1000
                ),
            }

            # Bounded pagination. Leave unresolved payments
            # pending rather than guessing.
            for _ in range(10):
                data = await self.request(
                    (
                        "https://api.trongrid.io/v1/accounts/"
                        f"{address}/transactions/trc20"
                    ),
                    params=params,
                    headers=(
                        {"TRON-PRO-API-KEY": key}
                        if key
                        else {}
                    ),
                )

                if not data.get("success"):
                    raise RuntimeError(
                        "TRON index unavailable"
                    )

                matches = [
                    item
                    for item in data.get("data", [])
                    if (
                        item.get(
                            "transaction_id", ""
                        ).lower() == tx
                        and item.get("to") == address
                        and item.get("type") == "Transfer"
                        and item.get(
                            "token_info", {}
                        ).get("address") == USDT_CONTRACT
                        and int(
                            item.get(
                                "token_info", {}
                            ).get("decimals", -1)
                        ) == 6
                    )
                ]

                if matches:
                    amount = sum(
                        int(item["value"])
                        for item in matches
                    )

                    timestamp = (
                        int(matches[0]["block_timestamp"])
                        // 1000
                    )
                    break

                fingerprint = data.get(
                    "meta", {}
                ).get("fingerprint")

                if not fingerprint:
                    break

                params["fingerprint"] = fingerprint

        if timestamp is None or amount != expected:
            return False

        return (
            int(order["created"].timestamp())
            <= int(timestamp)
            <= int(order["expires"].timestamp())
        )
