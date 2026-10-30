"""BNB mint signals and a durable execution ledger. Python standard library only.

The scanner never signs. A scheduled AI agent reviews Binance's actual preview
and uses the official wallet CLI, under POLICY.md. Unrecognized calls are skipped.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import contextlib
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
ZERO = "0x" + "0" * 40
ZERO_TOPIC = "0x" + "0" * 64
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SHEET = "https://docs.google.com/spreadsheets/d/1jcKyWK1JJ8uBchTS3p1ok18DeB7yrDb2ZDxOaNYF73Q/export?format=csv"


def keccak(data):
    """Ethereum Keccak-256 (not NIST SHA3). Used only for ABI selectors/topics."""
    rotations = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39,
                 41, 45, 15, 21, 8, 18, 2, 61, 56, 14]
    rounds = [0x1, 0x8082, 0x800000000000808a, 0x8000000080008000,
              0x808b, 0x80000001, 0x8000000080008081, 0x8000000000008009,
              0x8a, 0x88, 0x80008009, 0x8000000a, 0x8000808b,
              0x800000000000008b, 0x8000000000008089, 0x8000000000008003,
              0x8000000000008002, 0x8000000000000080, 0x800a,
              0x800000008000000a, 0x8000000080008081, 0x8000000000008080,
              0x80000001, 0x8000000080008008]
    mask = (1 << 64) - 1
    def rol(v, n):
        return ((v << n) | (v >> ((64 - n) % 64))) & mask
    pad = bytearray(data)
    pad.append(1)
    pad.extend(b"\0" * ((136 - len(pad) % 136) % 136))
    pad[-1] |= 128
    a = [0] * 25
    for pos in range(0, len(pad), 136):
        block = pad[pos:pos + 136]
        for i in range(17):
            a[i] ^= int.from_bytes(block[i*8:i*8+8], "little")
        for rc in rounds:
            c = [a[x] ^ a[x+5] ^ a[x+10] ^ a[x+15] ^ a[x+20] for x in range(5)]
            d = [c[(x-1) % 5] ^ rol(c[(x+1) % 5], 1) for x in range(5)]
            for i in range(25):
                a[i] ^= d[i % 5]
            b = [0] * 25
            for x in range(5):
                for y in range(5):
                    b[y + 5*((2*x+3*y) % 5)] = rol(a[x+5*y], rotations[x+5*y])
            for x in range(5):
                for y in range(5):
                    a[x+5*y] = b[x+5*y] ^ ((~b[(x+1) % 5+5*y]) & b[(x+2) % 5+5*y])
            a[0] ^= rc
    return b"".join(v.to_bytes(8, "little") for v in a)[:32]


def topic(signature):
    return "0x" + keccak(signature.encode()).hex()


SINGLE = topic("TransferSingle(address,address,address,uint256,uint256)")
BATCH = topic("TransferBatch(address,address,address,uint256[],uint256[])")
CONSECUTIVE = topic("ConsecutiveTransfer(uint256,uint256,address,address)")


def integer(v):
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)


def address(v):
    if not isinstance(v, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
        raise ValueError("Invalid EVM address")
    return v.lower()


def topic_address(v):
    if not re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", v):
        raise ValueError("Invalid address topic")
    return address("0x" + v[-40:])


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def wei(bnb):
    v = Decimal(str(bnb)) * 10**18
    if v != int(v) or v < 0:
        raise ValueError("Invalid BNB amount")
    return int(v)


def get_json(url, payload=None, retries=2):
    for attempt in range(retries + 1):
        req = urllib.request.Request(url,
            None if payload is None else json.dumps(payload).encode(),
            {"Content-Type": "application/json", "User-Agent": "binancewatcher/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                try:
                    delay = min(30, max(5, int(exc.headers.get("Retry-After", "5"))))
                except ValueError:
                    delay = 5
                time.sleep(delay)
                continue
            raise RuntimeError("Network HTTP " + str(exc.code) + "; provider unavailable or access restricted") from None
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead,
                http.client.RemoteDisconnected, ConnectionError, json.JSONDecodeError):
            if attempt == retries:
                # URLs can contain RPC API keys: don't echo underlying exception.
                raise RuntimeError("Network request failed; check connectivity/provider") from None
            time.sleep(attempt + 1)


class RPCRangeError(RuntimeError):
    pass


class APIBudget:
    """Conservative local allowance, separate from the provider's account meter."""
    def __init__(self, limit):
        RUNTIME.mkdir(exist_ok=True)
        self.limit = limit

    def reserve(self, cost=60):
        month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        with contextlib.closing(sqlite3.connect(RUNTIME / "api-usage.sqlite", timeout=30)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS usage (month TEXT PRIMARY KEY, cu INTEGER)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cu FROM usage WHERE month=?", (month,)).fetchone()
            used = row[0] if row else 0
            if used+cost > self.limit:
                raise RuntimeError("Local Alchemy monthly CU budget reached; monitoring needs a provider/budget review. No paid upgrade authorized.")
            db.execute("INSERT OR REPLACE INTO usage VALUES (?,?)", (month, used+cost))


class RPC:
    def __init__(self, url, interval=0.3, meter=None):
        self.url = url
        self.calls = 0
        self.interval = interval
        self.last_call = 0
        self.lock = threading.Lock()
        self.meter = meter

    def call(self, method, params):
        with self.lock:
            delay = self.interval - (time.monotonic() - self.last_call)
            if delay > 0:
                time.sleep(delay)
            self.last_call = time.monotonic()
            self.calls += 1
            request_id = self.calls
            cost = 120 if method == "alchemy_getAssetTransfers" else 60
            if self.meter:
                self.meter.reserve(cost)
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        data = get_json(self.url, request)
        # Shared public backends can temporarily return an internal-error/null
        # block. Retry reads only; the RPC class never sends transactions.
        for attempt in range(2):
            transient = data.get("error", {}).get("code") == -32603
            missing_block = method == "eth_getBlockByNumber" and data.get("result", False) is None
            if not (transient or missing_block):
                break
            time.sleep(attempt + 1)
            if self.meter:
                self.meter.reserve(cost)
            data = get_json(self.url, request)
        if "error" in data:
            code = data["error"].get("code")
            if code == -32701:
                raise RuntimeError("RPC disallows chain-wide mint queries; configure a log-capable BSC_RPC_URL")
            message = str(data["error"].get("message", "")).lower()
            if code in (-32602, -32005, -32000) and any(s in message for s in ("block range", "blocks range", "too many results", "response size")):
                raise RPCRangeError("RPC log range/result limit")
            raise RuntimeError("RPC " + method + " failed (code " + str(code) + ")")
        if "result" not in data:
            raise RuntimeError("RPC missing result")
        return data["result"]

    def block(self, n):
        block = self.call("eth_getBlockByNumber", [hex(n), False])
        if not block:
            raise RuntimeError("Block not available")
        return block

    def logs(self, start, end, topics):
        try:
            return self.call("eth_getLogs", [{"fromBlock": hex(start),
                "toBlock": hex(end), "topics": topics}])
        except RPCRangeError:
            if start == end:
                raise
            middle = (start + end) // 2
            return self.logs(start, middle, topics) + self.logs(middle+1, end, topics)


def config():
    path = ROOT / "config.json"
    c = json.loads((path if path.exists() else ROOT / "config.example.json").read_text(encoding="utf-8"))
    # Optional local .env, never echoed. Environment takes precedence.
    dotenv = {}
    if (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("BSC_RPC_URL="):
                dotenv["BSC_RPC_URL"] = line.split("=", 1)[1].strip().strip("\"'")
    override = os.environ.get("BSC_RPC_URL") or dotenv.get("BSC_RPC_URL")
    c["rpc_url"] = override or c["rpc_url"]
    if "bnb-mainnet.g.alchemy.com/" in c["rpc_url"]:
        c["log_chunk_blocks"] = 10
        c["rpc_min_interval_seconds"] = 0.5
        c["combined_topic_queries"] = True
        c["rpc_provider"] = "Alchemy"
        c["discovery_mode"] = "alchemy_transfers"
    else:
        c["rpc_provider"] = "public or custom"
    if c["chain_id"] != 56 or c["auto_paid_mints_authorized"] is not False:
        raise RuntimeError("Policy requires BNB chain 56 and paid auto-mint disabled")
    return c


def initialize(wallet=None, auto_free=False):
    """Create local settings without inheriting another deployment's authority."""
    if (ROOT / "config.json").exists():
        raise RuntimeError("config.json already exists; edit it locally instead of overwriting an active setup")
    if auto_free and not wallet:
        raise RuntimeError("--auto-free requires --wallet and explicit authorization from this deployment's user")
    c = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    c["wallet_address"] = address(wallet) if wallet else ""
    if c["wallet_address"] == ZERO:
        raise RuntimeError("The zero address cannot be a tracker wallet")
    c["auto_free_mints_authorized"] = bool(auto_free)
    write_json(ROOT / "config.json", c)
    if not (ROOT / ".env").exists():
        (ROOT / ".env").write_bytes((ROOT / ".env.example").read_bytes())
    return {"initialized": True, "mode": "auto-free" if auto_free else "watch-only",
            "paid_auto_mint": False, "next_step": "Set BSC_RPC_URL in .env locally; then run doctor"}


def doctor(c, check_rpc=False):
    import shutil
    result = {"ok": True, "python": sys.version.split()[0],
              "private_config_exists": (ROOT / "config.json").exists(),
              "watchlist_count": len(wallets(c)), "rpc_provider": c["rpc_provider"],
              "wallet_configured": bool(c["wallet_address"]),
              "auto_free_enabled": c["auto_free_mints_authorized"],
              "paid_auto_enabled": c["auto_paid_mints_authorized"],
              "node_available": bool(shutil.which("node")),
              "baw_available": bool(shutil.which("baw"))}
    if c["wallet_address"]:
        address(c["wallet_address"])
    if check_rpc:
        rpc = RPC(c["rpc_url"], c["rpc_min_interval_seconds"])
        if integer(rpc.call("eth_chainId", [])) != 56:
            raise RuntimeError("RPC is not BNB Smart Chain mainnet")
        result["rpc_chain_id"] = 56
        result["rpc_head"] = integer(rpc.call("eth_blockNumber", []))
    return result


def database():
    RUNTIME.mkdir(exist_ok=True)
    db = sqlite3.connect(RUNTIME / "state.sqlite", timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, payload TEXT, at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS executions (contract TEXT PRIMARY KEY, status TEXT, "
               "reserved_wei TEXT, created TEXT, tx_hash TEXT, actual_wei TEXT)")
    db.commit()
    return db


def cached(db, key, fetch, seconds=7*86400):
    row = db.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
    if row and row[1] > time.time():
        return json.loads(row[0])
    result = fetch()
    if result is None:
        return None
    db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
               (key, json.dumps(result), time.time() + seconds))
    db.commit()
    return result


def parallel_map(function, items, workers=4):
    """Yield read results with only a bounded number of outstanding requests."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(items)
        pending = {pool.submit(function, item) for _, item in zip(range(workers), it)}
        try:
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    yield future.result()
                    item = next(it, None)
                    if item is not None:
                        pending.add(pool.submit(function, item))
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def read_many(db, rpc, prefix, method, keys):
    result, missing = {}, []
    for key in set(keys):
        row = db.execute("SELECT value,expires FROM cache WHERE key=?", (prefix+key,)).fetchone()
        if row and row[1] > time.time():
            result[key] = json.loads(row[0])
        else:
            missing.append(key)
    def fetch(key):
        data = rpc.call(method, [key])
        if data is None:
            raise RuntimeError("Missing transaction/receipt; scan incomplete")
        return key, data
    completed = 0
    write_json(RUNTIME / "scan-progress.json", {"at": utc(), "phase": method,
        "remaining_reads": len(missing), "cached_reads": len(result)})
    try:
        for key, data in parallel_map(fetch, missing, workers=24):
            result[key] = data
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                       (prefix+key, json.dumps(data), time.time()+7*86400))
            completed += 1
            if completed % 100 == 0:
                db.commit()
                write_json(RUNTIME / "scan-progress.json", {"at": utc(), "phase": method,
                    "remaining_reads": len(missing)-completed, "cached_reads": len(result)})
    finally:
        db.commit()
    return result


def wallets(c):
    raw = (ROOT / "data/wallets.txt").read_bytes()
    source = json.loads((ROOT / "data/watchlist-source.json").read_text())
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise RuntimeError("Watchlist hash changed; review before updating pinned source")
    result = {address(a.strip()) for a in raw.decode().splitlines() if a.strip()}
    if len(result) != c["expected_wallet_count"]:
        raise RuntimeError("Watchlist count mismatch")
    return result


def decode_mint(log):
    if log.get("removed"):
        return None
    t = log.get("topics", [])
    if not t:
        return None
    kind = t[0].lower()
    units = None
    token_id = None
    if kind == TRANSFER and len(t) in (3, 4) and t[1] == ZERO_TOPIC:
        recipient = topic_address(t[2])
        standard = "ERC721" if len(t) == 4 else "ERC20"
        units = 1 if len(t) == 4 else integer(log["data"])
        if len(t) == 4:
            token_id = str(integer(t[3]))
    elif kind in (SINGLE, BATCH) and len(t) == 4 and t[2] == ZERO_TOPIC:
        recipient = topic_address(t[3])
        standard = "ERC1155"
        words = [int(log["data"][i:i+64], 16) for i in range(2, len(log["data"]), 64)]
        if kind == SINGLE:
            if len(words) != 2:
                raise ValueError("Malformed ERC1155 mint")
            units = words[1]
            token_id = str(words[0])
        else:
            # Signal only. Batch calls are never copied automatically.
            units = None
    elif kind == CONSECUTIVE and len(t) == 4 and t[2] == ZERO_TOPIC:
        recipient = topic_address(t[3])
        standard = "ERC721-consecutive"
        units = integer(log["data"]) - integer(t[1]) + 1
    else:
        return None
    if units is not None and units <= 0:
        return None
    return {"contract": address(log["address"]), "recipient": recipient,
            "standard": standard, "units": units, "token_id": token_id, "tx": log["transactionHash"].lower(),
            "block": integer(log["blockNumber"]), "log_index": integer(log["logIndex"])}


def payment(tx, receipt, minter):
    """Gross native value and on-chain outgoing token events, excluding gas.

    Zero gross value can still be ambiguous. A free *signal* is provisional until
    the user's fresh wallet preview proves no payment or authority changes.
    """
    native = integer(tx["value"])
    outgoing = []
    for log in receipt.get("logs", []):
        t = log.get("topics", [])
        sender_index = 1 if t and t[0].lower() == TRANSFER else 2
        if t and t[0].lower() in (TRANSFER, SINGLE, BATCH, CONSECUTIVE) and len(t) > sender_index:
            if topic_address(t[sender_index]) == minter:
                outgoing.append({"token": address(log["address"]),
                                 "event": t[0], "raw_amount": log.get("data", "0x")})
    return {"classification": "paid" if native or outgoing else "free_candidate",
            "native_wei": str(native), "token_outflows": outgoing}


def first_block(rpc, head, timestamp):
    # Search near the head; public non-archive endpoints may omit ancient blocks.
    hi = head
    step = 1
    lo = max(0, head-step)
    while lo > 0 and integer(rpc.block(lo)["timestamp"]) >= timestamp:
        hi = lo
        step *= 2
        lo = max(0, head-step)
    while lo < hi:
        mid = (lo + hi) // 2
        if integer(rpc.block(mid)["timestamp"]) < timestamp:
            lo = mid + 1
        else:
            hi = mid
    return lo


def verified_contract(contract):
    # v1 was retired in July 2026. v2 is read-only and needs no API key.
    try:
        d = get_json("https://sourcify.dev/server/v2/contract/56/" + contract + "?fields=all", retries=0)
        match = d.get("match")
        verified = match in ("exact_match", "match", "perfect")
        return {"status": "verified" if verified else "unknown", "provider": "Sourcify v2",
                "match": match, "abi": d.get("abi", []),
                "runtime": (d.get("runtimeBytecode") or {}).get("onchainBytecode"),
                "source": "https://repo.sourcify.dev/56/" + contract}
    except (RuntimeError, ValueError, TypeError, AttributeError):
        return {"status": "unknown", "provider": "Sourcify v2", "abi": []}


def group_signals(events, txs, receipts, timestamps, now, c):
    groups = {}
    for e in events:
        tx = txs[e["tx"]]
        receipt = receipts[e["tx"]]
        # Recipients alone would count unsolicited airdrops as purchases.
        if address(tx["from"]) != e["recipient"] or integer(receipt["status"]) != 1:
            continue
        p = payment(tx, receipt, e["recipient"])
        e = dict(e, timestamp=timestamps[e["block"]], payment=p,
                 call_to=tx.get("to"), input=tx.get("input", "0x"))
        g = groups.setdefault(e["contract"], {"contract": e["contract"], "events": []})
        g["events"].append(e)
    signals = []
    for g in groups.values():
        es = g["events"]
        free = {e["recipient"] for e in es if e["payment"]["classification"] == "free_candidate"}
        paid = {e["recipient"] for e in es if e["payment"]["classification"] == "paid"}
        all_wallets = free | paid
        recent = {e["recipient"] for e in es if e["timestamp"] > now-900}
        preceding = {e["recipient"] for e in es if now-1800 < e["timestamp"] <= now-900}
        surge = len(recent) >= c["surge_min_wallets"] and len(recent) >= c["surge_ratio"]*max(1, len(preceding))
        if len(free) < c["free_threshold"] and len(paid) < c["paid_threshold"]:
            continue
        # A wallet is counted once per contract/classification, not per log or mint.
        native_prices = []
        for e in es:
            if e["standard"] in ("ERC721", "ERC1155") and e["units"]:
                native_prices.append(Decimal(e["payment"]["native_wei"])/e["units"]/10**18)
        g.update(wallet_count=len(all_wallets), free_wallet_count=len(free), paid_wallet_count=len(paid),
                 free_trigger=len(free) >= c["free_threshold"],
                 paid_trigger=len(paid) >= c["paid_threshold"],
                 hot=len(all_wallets) >= c["hot_threshold"] or surge,
                 recent_15m_wallets=len(recent), previous_15m_wallets=len(preceding), surge=surge,
                 native_price_range_bnb=[str(min(native_prices)), str(max(native_prices))] if native_prices else None,
                 market_floor={"status": "unavailable", "reason": "No authenticated NFT market source configured"},
                 explorer="https://bscscan.com/address/" + g["contract"])
        signals.append(g)
    return sorted(signals, key=lambda g: (-g["hot"], -g["wallet_count"]))


def write_json(path, data):
    path.parent.mkdir(exist_ok=True, parents=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            # Antivirus/OneDrive can briefly lock the destination on Windows.
            time.sleep(0.05 * 2**attempt)


def collect_events(log_rpc, start, head, c, tracked, events, seen, base_calls):
    filters = [[TRANSFER, ZERO_TOPIC], [SINGLE, None, ZERO_TOPIC],
               [BATCH, None, ZERO_TOPIC], [CONSECUTIVE, None, ZERO_TOPIC]]
    if c["combined_topic_queries"]:
        filters = [[TRANSFER, ZERO_TOPIC], [[SINGLE, BATCH, CONSECUTIVE], None, ZERO_TOPIC]]
    jobs = [(lo, min(head, lo+c["log_chunk_blocks"]-1), topics)
            for lo in range(start, head+1, c["log_chunk_blocks"]) for topics in filters]
    def fetch(job):
        lo, hi, topics = job
        return job, log_rpc.logs(lo, hi, topics)
    done_count = 0
    # Bounded outstanding work: a provider failure cannot enqueue a whole scan.
    with ThreadPoolExecutor(max_workers=c["rpc_workers"]) as pool:
        it = iter(jobs)
        pending = {pool.submit(fetch, job) for _, job in zip(range(c["rpc_workers"]), it)}
        try:
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    (lo, hi, topics), logs = future.result()
                    for log in logs:
                        if not lo <= integer(log["blockNumber"]) <= hi:
                            raise RuntimeError("Provider returned logs outside requested block range")
                        for i, value in enumerate(topics):
                            if value is None:
                                continue
                            allowed = value if isinstance(value, list) else [value]
                            if len(log.get("topics", [])) <= i or log["topics"][i].lower() not in [v.lower() for v in allowed]:
                                raise RuntimeError("Provider returned logs outside requested topic filter")
                        e = decode_mint(log)
                        if not e or e["recipient"] not in tracked:
                            continue
                        key = (e["tx"], e["log_index"])
                        if key not in seen:
                            events.append(e)
                            seen.add(key)
                    done_count += 1
                    write_json(RUNTIME / "scan-progress.json", {"at": utc(), "scanned_to_block": hi,
                        "target_block": head, "percent": round(100*done_count/len(jobs), 1),
                        "tracked_mint_events": len(events), "rpc_calls": base_calls+log_rpc.calls})
                    next_job = next(it, None)
                    if next_job is not None:
                        pending.add(pool.submit(fetch, next_job))
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def collect_transfers(rpc, start, head, tracked, events, seen, progress=True):
    """Indexed discovery only; actual mint events are rebuilt from chain receipts."""
    payload = {"fromBlock": hex(start), "toBlock": hex(head), "fromAddress": ZERO,
               "category": ["erc20", "erc721", "erc1155"], "excludeZeroValue": False,
               "maxCount": "0x3e8", "order": "asc", "withMetadata": False}
    page_keys, recent_keys = set(), set()
    page = 0
    while True:
        data = rpc.call("alchemy_getAssetTransfers", [payload])
        if not isinstance(data, dict) or not isinstance(data.get("transfers"), list):
            raise RuntimeError("Invalid Alchemy Transfers response")
        for row in data["transfers"]:
            if address(row["from"]) != ZERO:
                raise RuntimeError("Transfers API returned non-mint data")
            block = integer(row["blockNum"])
            if not start <= block <= head:
                raise RuntimeError("Transfers API returned data outside requested window")
            recipient = address(row["to"])
            contract = address(row["rawContract"]["address"])
            tx_hash = row["hash"].lower()
            if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
                raise RuntimeError("Invalid indexed transaction hash")
            key = (tx_hash, contract, recipient)
            if block >= head-9:
                recent_keys.add(key)
            if recipient in tracked and key not in seen:
                seen.add(key)
                events.append({"tx": tx_hash, "contract": contract,
                               "recipient": recipient, "block": block})
        page += 1
        if progress:
            write_json(RUNTIME / "scan-progress.json", {"at": utc(), "phase": "indexed_discovery",
                "pages": page, "tracked_mint_candidates": len(events), "rpc_calls": rpc.calls})
        next_key = data.get("pageKey")
        if not next_key:
            break
        if not isinstance(next_key, str) or next_key in page_keys or page >= 2000:
            raise RuntimeError("Invalid/excessive transfer pagination; scan incomplete")
        page_keys.add(next_key)
        payload["pageKey"] = next_key
    # Check the stable recent end against actual logs, so indexing lag never
    # silently masquerades as a complete scan. No payment decisions use API value.
    for topics in [[TRANSFER, ZERO_TOPIC], [[SINGLE, BATCH, CONSECUTIVE], None, ZERO_TOPIC]]:
        for log in rpc.logs(max(start, head-9), head, topics):
            event = decode_mint(log)
            if event and (event["tx"], event["contract"], event["recipient"]) not in recent_keys:
                raise RuntimeError("Transfers index is behind recent mint logs; incomplete scan, retry later")


def collect_transfer_windows(rpc, start, head, c, tracked, events, seen):
    # Each cursor is sequential within its own bounded block window. Independent
    # windows can run concurrently, under the shared account-wide rate limiter.
    ranges = [(lo, min(head, lo+1499)) for lo in range(start, head+1, 1500)]
    def fetch(bounds):
        window_events = []
        collect_transfers(rpc, *bounds, tracked, window_events, set(), progress=False)
        return window_events
    count = 0
    for window_events in parallel_map(fetch, ranges, workers=c["rpc_workers"]):
        for event in window_events:
            key = (event["tx"], event["contract"], event["recipient"])
            if key not in seen:
                seen.add(key)
                events.append(event)
        count += 1
        write_json(RUNTIME / "scan-progress.json", {"at": utc(), "phase": "indexed_discovery",
            "windows_completed": count, "total_windows": len(ranges),
            "tracked_mint_candidates": len(events), "rpc_calls": rpc.calls})


def scan(c, db):
    tracked = wallets(c)
    meter = APIBudget(c["alchemy_monthly_cu_budget"]) if c["rpc_provider"] == "Alchemy" else None
    log_rpc = RPC(c["rpc_url"], c["rpc_min_interval_seconds"], meter)
    rpc = RPC(c["block_rpc_url"], c["block_rpc_min_interval_seconds"])
    if integer(rpc.call("eth_chainId", [])) != 56:
        raise RuntimeError("RPC is not BNB Smart Chain mainnet")
    if integer(log_rpc.call("eth_chainId", [])) != 56:
        raise RuntimeError("Log RPC is not BNB Smart Chain mainnet")
    latest = integer(rpc.call("eth_blockNumber", []))
    head = latest - c["confirmations"]
    end_block = rpc.block(head)
    now = integer(end_block["timestamp"])
    if abs(time.time()-now) > 300:
        raise RuntimeError("RPC head is stale or machine clock is incorrect")
    start = first_block(rpc, head, now-c["lookback_minutes"]*60)
    events, seen = [], set()
    indexed = c.get("discovery_mode") == "alchemy_transfers"
    def collect(lo, hi):
        if indexed:
            collect_transfer_windows(log_rpc, lo, hi, c, tracked, events, seen)
        else:
            collect_events(log_rpc, lo, hi, c, tracked, events, seen, rpc.calls)
    collect(start, head)
    # A free-plan historical sweep takes several minutes. Catch up the tail so
    # decisions use a current 70-minute window rather than a stale initial head.
    for _ in range(2):
        if time.time()-now <= 90:
            break
        new_head = integer(rpc.call("eth_blockNumber", []))-c["confirmations"]
        if new_head <= head:
            break
        new_end_block = rpc.block(new_head)
        collect(head+1, new_head)
        head, end_block = new_head, new_end_block
        now = integer(end_block["timestamp"])
    start = first_block(rpc, head, now-c["lookback_minutes"]*60)
    events = [e for e in events if e["block"] >= start]
    txs = read_many(db, rpc, "tx:", "eth_getTransactionByHash", {e["tx"] for e in events})
    # Drop unsolicited mints BEFORE fetching receipts and block metadata.
    events = [e for e in events if address(txs[e["tx"]]["from"]) == e["recipient"]]
    receipts = read_many(db, rpc, "receipt:", "eth_getTransactionReceipt", {e["tx"] for e in events})
    if indexed:
        actual_events, actual_seen = [], set()
        for tx_hash, receipt in receipts.items():
            for log in receipt.get("logs", []):
                event = decode_mint(log)
                if not event or not start <= event["block"] <= head or event["recipient"] not in tracked or address(txs[tx_hash]["from"]) != event["recipient"]:
                    continue
                key = (event["tx"], event["log_index"])
                if key not in actual_seen:
                    actual_events.append(event)
                    actual_seen.add(key)
        events = actual_events
    blocks = dict(parallel_map(lambda b: (b, rpc.block(b)), {e["block"] for e in events}, workers=24))
    stamps = {b: (integer(block["timestamp"]), block["hash"]) for b, block in blocks.items()}
    for e in events:
        h, b = e["tx"], e["block"]
        if receipts[h].get("blockHash") != stamps[b][1]:
            db.execute("DELETE FROM cache WHERE key IN (?,?)", ("tx:"+h, "receipt:"+h))
            db.commit()
            raise RuntimeError("Chain reorganization detected; retry scan")
    # Refuse results if the scan's anchor moved to another fork.
    if rpc.block(head)["hash"] != end_block["hash"]:
        raise RuntimeError("Chain reorganization during scan")
    signals = group_signals(events, txs, receipts, {b: v[0] for b, v in stamps.items()}, now, c)
    notifications = []
    for g in signals:
        g["verification"] = verified_contract(g["contract"])
        row = db.execute("SELECT status FROM executions WHERE contract=?", (g["contract"],)).fetchone()
        g["execution_status"] = row[0] if row else "never_attempted"
        # Alert first signal, first hot signal, or a material rise (10 unique wallets).
        key = g["contract"]
        old = db.execute("SELECT payload FROM alerts WHERE key=?", (key,)).fetchone()
        prev = json.loads(old[0]) if old else None
        if not prev or (g["hot"] and not prev["hot"]) or g["wallet_count"] >= prev["wallet_count"]+10 or (g["paid_trigger"] and not prev.get("paid_trigger")) or (g["free_trigger"] and not prev.get("free_trigger")):
            notifications.append(key)
            # Saved only on explicit ack, so interrupted agent runs do not lose alerts.
    report = {"ok": True, "at": utc(), "watchlist_count": len(tracked), "chain_id": 56,
              "lookback_minutes": c["lookback_minutes"], "from_block": start, "to_block": head,
              "anchor_hash": end_block["hash"], "window_end_timestamp": now,
              "tracked_mint_events": len(events), "signals": signals,
              "new_or_changed_signals": notifications, "rpc_calls": rpc.calls+log_rpc.calls,
              "discovery_mode": "Alchemy Transfers + chain receipt verification" if indexed else "raw chain logs",
              "coverage": "Standard indexed ERC20/721/1155 mints, decoded from actual receipts; sender=recipient; custom/non-indexed events and smart-account routers not covered" if indexed else "ERC20/721 Transfer, ERC1155 Single/Batch, ERC2309 ConsecutiveTransfer; sender=recipient; custom events/smart-account routers not covered"}
    write_json(RUNTIME / "latest-scan.json", report)
    write_json(RUNTIME / "health.json", {"last_success": utc(), "failure": None})
    return report


def current_signal(signal, c, now=None):
    """Recheck the moving window before preparing a financially meaningful call."""
    now = time.time() if now is None else now
    events = [e for e in signal["events"] if now-c["lookback_minutes"]*60 <= e["timestamp"] <= now]
    free = {e["recipient"] for e in events if e["payment"]["classification"] == "free_candidate"}
    paid = {e["recipient"] for e in events if e["payment"]["classification"] == "paid"}
    return signal | {"events": events, "wallet_count": len(free | paid),
        "free_wallet_count": len(free), "paid_wallet_count": len(paid),
        "free_trigger": len(free) >= c["free_threshold"],
        "paid_trigger": len(paid) >= c["paid_threshold"]}


def make_call(signal, own):
    """Infer only simple, direct, verified NFT mint functions, never copy calldata."""
    own = address(own)
    if not signal["free_trigger"] or signal["execution_status"] != "never_attempted":
        raise RuntimeError("No eligible free trigger or contract already attempted")
    verified = signal["verification"]
    if verified["status"] != "verified":
        raise RuntimeError("Contract source verification unavailable")
    # Parameter names matter: an arbitrary uint256 may be a token ID, not quantity.
    qty_names = {"quantity", "_quantity", "amount", "_amount", "count", "_count", "numberoftokens", "mintamount", "_mintamount", "qty", "_qty"}
    recipient_names = {"to", "_to", "recipient", "_recipient", "receiver", "_receiver"}
    id_names = {"id", "_id", "tokenid", "_tokenid", "token_id", "_token_id"}
    allowed_names = {"mint", "publicMint", "freeMint", "mintPublic"}
    for e in signal["events"]:
        if e["payment"]["classification"] != "free_candidate" or e["standard"] not in ("ERC721", "ERC1155") or e["units"] != 1 or (e["call_to"] or "").lower() != signal["contract"]:
            continue
        for f in verified.get("abi", []):
            if f.get("type") != "function" or f.get("name") not in allowed_names or f.get("stateMutability") not in ("payable", "nonpayable"):
                continue
            inputs = f.get("inputs", [])
            types = [i["type"] for i in inputs]
            if len(inputs) > 3:
                continue
            signature = f["name"] + "(" + ",".join(types) + ")"
            selector = topic(signature)[:10]
            if not e["input"].lower().startswith(selector) or len(e["input"]) != 10+64*len(inputs):
                continue
            encoded, arguments, good = "", [], True
            for i, p in enumerate(inputs):
                word = e["input"][10+64*i:74+64*i]
                if p["type"] == "uint256" and p["name"].lower() in qty_names:
                    if int(word, 16) != 1:
                        good = False
                    encoded += f"{1:064x}"
                    arguments.append(1)
                elif p["type"] == "address" and p["name"].lower() in recipient_names:
                    if topic_address("0x"+word) != e["recipient"]:
                        good = False
                    encoded += own[2:].rjust(64, "0")
                    arguments.append(own)
                elif p["type"] == "uint256" and p["name"].lower() in id_names and e["standard"] == "ERC1155" and e.get("token_id") is not None:
                    token_id = int(e["token_id"])
                    if int(word, 16) != token_id:
                        good = False
                    encoded += f"{token_id:064x}"
                    arguments.append(str(token_id))
                else:
                    good = False
            if good and e["units"] == 1:
                return {"chain_id": 56, "from": own, "to": signal["contract"], "value": "0",
                        "inputData": selector+encoded, "function": signature, "arguments": arguments,
                        "evidence_transaction": e["tx"], "quantity": 1, "standard": e["standard"],
                        "expected_token_id": e.get("token_id") if e["standard"] == "ERC1155" else None,
                        "required_next_step": "Read POLICY.md, inspect verified source, then Binance preview. No execute without full clean preview."}
    raise RuntimeError("No supported direct one-unit public NFT mint; custom/allowlist/signature/batch/ERC20 calls are alert-only")


def reserve(db, c, contract, max_gas):
    contract = address(contract)
    amount = wei(max_gas)
    if not 0 < amount <= wei(c["max_gas_per_mint_bnb"]):
        raise RuntimeError("Gas reservation exceeds per-mint budget or is zero")
    # Serializes reservations across scheduler runs and persists BEFORE broadcasting.
    db.execute("BEGIN IMMEDIATE")
    try:
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        rows = db.execute("SELECT reserved_wei,actual_wei FROM executions WHERE substr(created,1,10)=?", (day,)).fetchall()
        spent = sum(int(actual if actual is not None else reserved) for reserved, actual in rows)
        if len(rows) >= c["max_mints_per_day"] or spent+amount > wei(c["daily_gas_budget_bnb"]):
            raise RuntimeError("Daily mint or gas budget exhausted")
        if db.execute("SELECT 1 FROM executions WHERE contract=?", (contract,)).fetchone():
            raise RuntimeError("Contract already attempted; no duplicate broadcast")
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)", (contract, "reserved", str(amount), utc(), None, None))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"contract": contract, "status": "reserved", "max_gas_wei": str(amount)}


@contextlib.contextmanager
def run_lock():
    RUNTIME.mkdir(exist_ok=True)
    f = (RUNTIME / "run.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        f.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="Create private local configuration; watch-only by default")
    p.add_argument("--wallet")
    p.add_argument("--auto-free", action="store_true", help="Only use after the operator explicitly authorizes automatic free mints")
    p = sub.add_parser("doctor", help="Check local setup without exposing credentials")
    p.add_argument("--rpc", action="store_true", help="Also perform read-only RPC connection checks")
    sub.add_parser("scan")
    sub.add_parser("status")
    p = sub.add_parser("prepare")
    p.add_argument("--contract", required=True)
    p.add_argument("--wallet", required=True)
    p = sub.add_parser("reserve")
    p.add_argument("--contract", required=True)
    p.add_argument("--max-gas-bnb", required=True)
    p = sub.add_parser("record")
    p.add_argument("--contract", required=True)
    p.add_argument("--status", required=True, choices=["broadcast", "confirmed", "reverted", "uncertain", "pending_app", "not_broadcast"])
    p.add_argument("--tx-hash")
    p.add_argument("--actual-gas-bnb")
    p = sub.add_parser("ack")
    p.add_argument("--contract", required=True)
    args = parser.parse_args()
    if args.command == "init":
        print(json.dumps(initialize(args.wallet, args.auto_free), indent=2))
        return
    c = config()
    if args.command == "doctor":
        print(json.dumps(doctor(c, args.rpc), indent=2))
        return
    db = database()
    if args.command == "scan":
        with run_lock():
            result = scan(c, db)
        print(json.dumps({k: v for k, v in result.items() if k != "signals"}, indent=2))
    elif args.command == "status":
        print(json.dumps({"wallet_count": len(wallets(c)), "policy": c | {"rpc_url": "[configured]"},
            "executions": db.execute("SELECT contract,status,created,tx_hash FROM executions").fetchall()}, indent=2))
    elif args.command in ("prepare", "ack"):
        report = json.loads((RUNTIME / "latest-scan.json").read_text())
        contract = address(args.contract)
        signal = next((g for g in report["signals"] if g["contract"] == contract), None)
        if signal is None:
            raise RuntimeError("Contract not in latest complete scan")
        if args.command == "prepare":
            if c["auto_free_mints_authorized"] is not True:
                raise RuntimeError("Automatic free minting is disabled")
            if address(args.wallet) != address(c["wallet_address"]):
                raise RuntimeError("Connected wallet differs from authorized tracker wallet")
            if time.time()-report["window_end_timestamp"] > 600:
                raise RuntimeError("Scan is too old; rescan before preparing")
            if db.execute("SELECT 1 FROM executions WHERE contract=?", (contract,)).fetchone():
                raise RuntimeError("Contract already attempted")
            result = make_call(current_signal(signal, c), args.wallet)
            write_json(RUNTIME / ("call-"+contract+".json"), result)
            print(json.dumps(result, indent=2))
        else:
            db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?)",
                (contract, json.dumps({k: signal[k] for k in ["wallet_count", "hot", "paid_trigger", "free_trigger"]}), time.time()))
            db.commit()
            print(json.dumps({"acknowledged": contract}))
    elif args.command == "reserve":
        print(json.dumps(reserve(db, c, args.contract, args.max_gas_bnb)))
    elif args.command == "record":
        if args.tx_hash and not re.fullmatch(r"0x[0-9a-fA-F]{64}", args.tx_hash):
            raise RuntimeError("Invalid transaction hash")
        actual = str(wei(args.actual_gas_bnb)) if args.actual_gas_bnb is not None else None
        row = db.execute("UPDATE executions SET status=?,tx_hash=coalesce(?,tx_hash),actual_wei=coalesce(?,actual_wei) WHERE contract=?",
            (args.status, args.tx_hash, actual, address(args.contract)))
        if row.rowcount != 1:
            raise RuntimeError("No reservation exists; refuse untracked execution")
        db.commit()
        print(json.dumps({"recorded": args.status}))
    db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        RUNTIME.mkdir(exist_ok=True)
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        write_json(RUNTIME / "last-error.json", {"ok": False, "at": utc(), "error": message})
        print(json.dumps({"ok": False, "error": message}))
        sys.exit(1)
