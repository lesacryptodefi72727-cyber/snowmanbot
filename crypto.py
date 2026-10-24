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
            await self.sol_mainnet(url)

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

            amount = self.sol_units(data, address)

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


    async def sol_mainnet(self, url):
        cache_key = ('sol-mainnet', url)
        cached = self.cache.get(cache_key)
        if not cached or time.time()-cached[0] > 3600:
            genesis = await self.rpc(url, 'getGenesisHash', [])
            if not isinstance(genesis, str) or not genesis.startswith('5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'):
                raise RuntimeError('SOL RPC must use Solana mainnet')
            self.cache[cache_key] = (time.time(), True)

    @staticmethod
    def sol_units(data, address):
        if not data or not data.get('meta') or data['meta'].get('err') is not None:
            return 0
        instructions = list(data['transaction']['message']['instructions'])
        for group in data['meta'].get('innerInstructions') or []:
            instructions.extend(group.get('instructions') or [])
        amount = 0
        for instruction in instructions:
            parsed = instruction.get('parsed') or {}
            info = parsed.get('info') or {}
            if (instruction.get('programId') == '11111111111111111111111111111111'
                    and parsed.get('type') in ('transfer', 'transferWithSeed')
                    and info.get('destination') == address):
                amount += int(info['lamports'])
        return amount

    async def incoming(self, asset, address, since):
        """Find candidate transfers. verify() still checks the authoritative chain.

        Public indexes can be delayed/rate limited. Bounded history pagination is
        supplemented by the customer's transaction-hash and receipt buttons.
        """
        from datetime import datetime
        key = ('incoming', asset, address, since)
        cached = self.cache.get(key)
        if cached and time.time()-cached[0] < 40:
            return cached[1]
        found = []
        if asset == 'BTC':
            base = f'https://blockstream.info/api/address/{address}/txs'
            data = await self.request(base)
            for page in range(10):
                for item in data:
                    units = sum(int(v['value']) for v in item.get('vout', []) if v.get('scriptpubkey_address') == address)
                    timestamp = item.get('status', {}).get('block_time')
                    if units and (timestamp is None or timestamp >= since):
                        found.append(dict(txid=item['txid'], units=units, timestamp=timestamp))
                confirmed = [i for i in data if i.get('status', {}).get('confirmed')]
                if not confirmed or len(confirmed)<25 or any(i['status']['block_time'] < since for i in confirmed): break
                data = await self.request(f"{base}/chain/{confirmed[-1]['txid']}")
        elif asset == 'ETH':
            params = {'filter': 'to'}
            for page in range(10):
                data = await self.request(f'https://eth.blockscout.com/api/v2/addresses/{address}/transactions', params=params)
                items = data.get('items', [])
                stop = False
                for item in items:
                    raw_time = item.get('timestamp')
                    timestamp = int(datetime.fromisoformat(raw_time.replace('Z', '+00:00')).timestamp()) if raw_time else None
                    if timestamp is not None and timestamp < since:
                        stop = True; continue
                    if ((item.get('to') or {}).get('hash', '').lower() == address.lower() and int(item.get('value') or 0)>0):
                        found.append(dict(txid=item['hash'], units=int(item['value']), timestamp=timestamp))
                cursor = data.get('next_page_params')
                if stop or not cursor: break
                params = dict(cursor, filter='to')
        elif asset == 'SOL':
            url = os.environ['SOL_RPC_URL']
            await self.sol_mainnet(url)
            params = {'limit': 100, 'commitment': 'confirmed'}
            for page in range(3):
                data = await self.rpc(url, 'getSignaturesForAddress', [address, params]) or []
                stop = False
                for item in data:
                    timestamp = item.get('blockTime')
                    if timestamp is not None and timestamp < since:
                        stop = True; continue
                    if item.get('err') is not None: continue
                    signature = item['signature']
                    txkey = ('sol-tx', signature)
                    previous = self.cache.get(txkey)
                    if previous:
                        tx = previous[1]
                    else:
                        tx = await self.rpc(url, 'getTransaction', [signature, {'encoding':'jsonParsed', 'commitment':'confirmed', 'maxSupportedTransactionVersion':0}])
                        if tx: self.cache[txkey] = (time.time(), tx)
                    units = self.sol_units(tx, address)
                    if units: found.append(dict(txid=signature, units=units, timestamp=timestamp))
                if stop or len(data)<100: break
                params['before'] = data[-1]['signature']
        elif asset == 'TON':
            api_key = os.getenv('TONCENTER_API_KEY', '')
            for page in range(10):
                data = await self.request('https://toncenter.com/api/v3/transactions', params={'account':address, 'start_utime':since, 'limit':100, 'offset':page*100, 'sort':'desc'}, headers={'X-API-Key':api_key} if api_key else {})
                items = data.get('transactions', [])
                for item in items:
                    message = item.get('in_msg') or {}
                    if (message.get('source') and message.get('bounced') is False
                            and message.get('destination') and ton_address(message['destination']) == ton_address(address)
                            and ton_address(item['account']) == ton_address(address)
                            and item.get('description', {}).get('aborted') is False):
                        found.append(dict(txid=tx_hash('TON', item['hash']), units=int(message.get('value') or 0), timestamp=int(item['now'])))
                if len(items)<100: break
        elif asset == 'USDT':
            api_key = os.getenv('TRONGRID_API_KEY', '')
            params = {'only_confirmed':'true', 'only_to':'true', 'limit':200, 'contract_address':USDT_CONTRACT, 'min_timestamp':since*1000, 'order_by':'block_timestamp,desc'}
            aggregated = {}
            for page in range(10):
                data = await self.request(f'https://api.trongrid.io/v1/accounts/{address}/transactions/trc20', params=params, headers={'TRON-PRO-API-KEY':api_key} if api_key else {})
                if not data.get('success'): raise RuntimeError('TRON index unavailable')
                for item in data.get('data', []):
                    token = item.get('token_info') or {}
                    if item.get('to') == address and item.get('type') == 'Transfer' and token.get('address') == USDT_CONTRACT and int(token.get('decimals', -1)) == 6:
                        txid = tx_hash('USDT', item['transaction_id'])
                        aggregate = aggregated.setdefault(txid, dict(txid=txid, units=0, timestamp=int(item['block_timestamp'])//1000))
                        aggregate['units'] += int(item['value'])
                fingerprint = data.get('meta', {}).get('fingerprint')
                if not fingerprint: break
                params['fingerprint'] = fingerprint
            found = list(aggregated.values())
        else:
            raise ValueError('Unsupported crypto asset')
        self.cache[key] = (time.time(), found)
        # Bound the cache, including parsed Solana transactions.
        if len(self.cache)>1500:
            self.cache = {k:v for k,v in self.cache.items() if time.time()-v[0]<300}
        return found
