import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import tracker as t

A = "0x" + "1" * 40
B = "0x" + "2" * 40
C = "0x" + "3" * 40


def addr(n):
    return "0x" + f"{n:040x}"


def log(wallet=A, standard="ERC721", quantity=1):
    topics = [t.TRANSFER, t.ZERO_TOPIC, "0x" + wallet[2:].rjust(64, "0")]
    data = "0x" + f"{quantity:064x}"
    if standard == "ERC721":
        topics.append("0x"+f"{7:064x}")
        data = "0x"
    if standard == "ERC1155":
        topics = [t.SINGLE, "0x"+A[2:].rjust(64, "0"), t.ZERO_TOPIC, topics[2]]
        data = "0x"+f"{7:064x}{quantity:064x}"
    return {"topics": topics, "data": data, "address": C, "transactionHash": "0x"+"a"*64,
            "blockNumber": "0x1", "logIndex": "0x0", "removed": False}


def observations(n=20, paid=False, duplicate=False):
    events, txs, receipts = [], {}, {}
    for i in range(n):
        wallet = addr(i+1)
        h = "0x"+f"{i+100:064x}"
        events.append(dict(t.decode_mint(log(wallet)), tx=h))
        txs[h] = {"from": wallet, "to": C, "value": "0x1" if paid else "0x0",
                  "input": t.topic("mint(uint256)")[:10]+f"{1:064x}"}
        receipts[h] = {"status": "0x1", "logs": []}
    if duplicate:
        events += copy.deepcopy(events)
    return events, txs, receipts, {1: 10000}


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.c = t.config()
        self.temp_runtime = tempfile.TemporaryDirectory(dir=t.ROOT)
        assert Path(self.temp_runtime.name).resolve().parent == t.ROOT.resolve()
        self.addCleanup(self.temp_runtime.cleanup)
        runtime_patch = patch.object(t, "RUNTIME", Path(self.temp_runtime.name))
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    def test_clean_setup_is_watch_only_and_preserves_existing_config(self):
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "ROOT", Path(folder)):
            root = Path(folder)
            (root / "config.example.json").write_bytes((Path(__file__).resolve().parents[1] / "config.example.json").read_bytes())
            (root / ".env.example").write_text("BSC_RPC_URL=\n")
            result = t.initialize()
            c = json.loads((root / "config.json").read_text())
            self.assertEqual(result["mode"], "watch-only")
            self.assertFalse(c["auto_free_mints_authorized"])
            self.assertFalse(c["auto_paid_mints_authorized"])
            self.assertEqual(c["wallet_address"], "")
            with self.assertRaises(RuntimeError):
                t.initialize(A, True)

    def test_auto_free_setup_requires_a_real_wallet(self):
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "ROOT", Path(folder)):
            root = Path(folder)
            (root / "config.example.json").write_bytes((Path(__file__).resolve().parents[1] / "config.example.json").read_bytes())
            (root / ".env.example").write_text("BSC_RPC_URL=\n")
            with self.assertRaises(RuntimeError):
                t.initialize(auto_free=True)
            with self.assertRaises(RuntimeError):
                t.initialize(t.ZERO, True)
            t.initialize(A, True)
            self.assertTrue(json.loads((root / "config.json").read_text())["auto_free_mints_authorized"])

    def test_ethereum_hash_vectors(self):
        self.assertEqual(t.keccak(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(t.topic("Transfer(address,address,uint256)"), t.TRANSFER)
        self.assertEqual(t.topic("mint(uint256)")[:10], "0xa0712d68")

    def test_imported_watchlist_count_and_integrity(self):
        self.assertEqual(len(t.wallets(self.c)), 4942)

    def test_standard_mints_and_removed_logs(self):
        for standard in ["ERC721", "ERC20", "ERC1155"]:
            self.assertEqual(t.decode_mint(log(standard=standard))["standard"], standard)
        self.assertIsNone(t.decode_mint(dict(log(), removed=True)))
        transfer = log()
        transfer["topics"][1] = "0x"+A[2:].rjust(64, "0")
        self.assertIsNone(t.decode_mint(transfer))

    def test_twenty_unique_wallets_not_twenty_logs(self):
        es, txs, rs, stamps = observations(n=19, duplicate=True)
        self.assertEqual(t.group_signals(es, txs, rs, stamps, 10001, self.c), [])
        es, txs, rs, stamps = observations(duplicate=True)
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        self.assertEqual(g["wallet_count"], 20)
        self.assertTrue(g["free_trigger"])

    def test_unsolicited_airdrops_do_not_count(self):
        es, txs, rs, stamps = observations()
        for tx in txs.values():
            tx["from"] = B
        self.assertEqual(t.group_signals(es, txs, rs, stamps, 10001, self.c), [])

    def test_mint_threshold_is_rechecked_when_old_evidence_ages_out(self):
        es, txs, rs, stamps = observations()
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        g["events"][0]["timestamp"] = 5799
        current = t.current_signal(g, self.c, now=10000)
        self.assertEqual(current["free_wallet_count"], 19)
        self.assertFalse(current["free_trigger"])
        self.assertTrue(g["free_trigger"])

    def test_failed_mints_do_not_count(self):
        es, txs, rs, stamps = observations()
        for r in rs.values():
            r["status"] = "0x0"
        self.assertEqual(t.group_signals(es, txs, rs, stamps, 10001, self.c), [])

    def test_paid_classification_with_zero_native_value(self):
        outgoing = log(A, "ERC20", 1000000)
        outgoing["topics"][1] = "0x"+A[2:].rjust(64, "0")
        outgoing["topics"][2] = "0x"+B[2:].rjust(64, "0")
        p = t.payment({"value": "0x0"}, {"logs": [outgoing]}, A)
        self.assertEqual(p["classification"], "paid")
        self.assertEqual(len(p["token_outflows"]), 1)

    def test_paid_threshold_and_hot_count(self):
        es, txs, rs, stamps = observations(40, paid=True)
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        self.assertTrue(g["paid_trigger"])
        self.assertTrue(g["hot"])
        self.assertFalse(g["free_trigger"])
        self.assertEqual(g["paid_wallet_count"], 40)

    def test_verified_one_unit_call_is_rebuilt(self):
        es, txs, rs, stamps = observations()
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        g.update(execution_status="never_attempted", verification={"status": "verified", "abi": [
            {"type": "function", "name": "mint", "stateMutability": "payable",
             "inputs": [{"type": "uint256", "name": "quantity"}]}]})
        call = t.make_call(g, B)
        self.assertEqual(call["from"], B)
        self.assertEqual(call["value"], "0")
        self.assertEqual(call["inputData"], t.topic("mint(uint256)")[:10]+f"{1:064x}")
        g["verification"]["abi"][0]["inputs"][0]["name"] = "tokenId"
        with self.assertRaises(RuntimeError):
            t.make_call(g, B)

    def test_erc1155_id_and_quantity_have_separate_meanings(self):
        es, txs, rs, stamps = observations()
        sig = "mint(uint256,uint256)"
        for e in es:
            e.update(standard="ERC1155", token_id="7", units=1)
            txs[e["tx"]]["input"] = t.topic(sig)[:10]+f"{7:064x}{1:064x}"
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        g.update(execution_status="never_attempted", verification={"status": "verified", "abi": [
            {"type": "function", "name": "mint", "stateMutability": "payable", "inputs": [
                {"type": "uint256", "name": "id"}, {"type": "uint256", "name": "amount"}]}]})
        call = t.make_call(g, B)
        self.assertEqual(call["arguments"], ["7", 1])
        self.assertEqual(call["expected_token_id"], "7")
        for e in g["events"]:
            e["token_id"] = "8"
        with self.assertRaises(RuntimeError):
            t.make_call(g, B)

    def test_unsupported_or_unverified_mint_is_not_prepared(self):
        es, txs, rs, stamps = observations()
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        g.update(execution_status="never_attempted", verification={"status": "unknown", "abi": []})
        with self.assertRaises(RuntimeError):
            t.make_call(g, B)
        g["verification"]["status"] = "verified"
        with self.assertRaises(RuntimeError):
            t.make_call(g, B)

    def test_recipient_is_replaced_only_in_recognized_argument(self):
        es, txs, rs, stamps = observations()
        sig = "mint(address,uint256)"
        for e in es:
            txs[e["tx"]]["input"] = t.topic(sig)[:10]+e["recipient"][2:].rjust(64,"0")+f"{1:064x}"
        g = t.group_signals(es, txs, rs, stamps, 10001, self.c)[0]
        g.update(execution_status="never_attempted", verification={"status": "verified", "abi": [
            {"type": "function", "name": "mint", "stateMutability": "payable", "inputs": [
                {"type": "address", "name": "to"}, {"type": "uint256", "name": "quantity"}]}]})
        call = t.make_call(g, B)
        self.assertEqual(call["arguments"], [B, 1])
        self.assertIn(B[2:], call["inputData"])

    def test_reservations_survive_restart_and_prevent_double_mint(self):
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "RUNTIME", Path(folder)):
            db = t.database()
            t.reserve(db, self.c, C, "0.0002")
            db.close()
            db = t.database()
            with self.assertRaises(RuntimeError):
                t.reserve(db, self.c, C, "0.0002")
            self.assertEqual(db.execute("SELECT status FROM executions").fetchone()[0], "reserved")
            db.close()

    def test_gas_and_daily_limits(self):
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "RUNTIME", Path(folder)):
            db = t.database()
            with self.assertRaises(RuntimeError):
                t.reserve(db, self.c, C, "0.000200000000000001")
            for i in range(5):
                t.reserve(db, self.c, addr(i+1), "0.0002")
            with self.assertRaises(RuntimeError):
                t.reserve(db, self.c, C, "0.00001")
            db.close()

    def test_first_block_uses_timestamps_not_assumed_block_time(self):
        class FakeRPC:
            def block(self, n):
                return {"timestamp": hex(n*n)}
        self.assertEqual(t.first_block(FakeRPC(), 100, 2000), 45)

    def test_provider_access_error_is_not_retried_as_smaller_ranges(self):
        rpc = t.RPC("https://example.invalid", interval=0)
        with patch.object(t, "get_json", return_value={"error": {"code": -32701, "message": "address required"}}) as request:
            with self.assertRaises(RuntimeError):
                rpc.logs(1, 1000, [t.TRANSFER])
            self.assertEqual(request.call_count, 1)

    def test_truncated_read_is_retried_without_using_partial_json(self):
        partial = t.http.client.IncompleteRead(b'{"result":', 3)
        complete = io.BytesIO(b'{"result":56}')
        with patch.object(t.urllib.request, "urlopen", side_effect=[partial, complete]) as request, patch.object(t.time, "sleep"):
            self.assertEqual(t.get_json("https://example.invalid"), {"result": 56})
            self.assertEqual(request.call_count, 2)

    def test_local_api_budget_stops_before_allowance(self):
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "RUNTIME", Path(folder)):
            meter = t.APIBudget(120)
            meter.reserve()
            meter.reserve()
            with self.assertRaises(RuntimeError):
                meter.reserve()

    def test_transfer_pagination_and_raw_log_cross_check(self):
        first = {"from": t.ZERO, "to": A, "blockNum": "0x1", "hash": "0x"+"a"*64,
                 "rawContract": {"address": C}}
        second = first | {"to": B, "hash": "0x"+"b"*64}
        class FakeAPI:
            calls = 0
            def call(self, method, params):
                self.calls += 1
                self.last_payload = params[0].copy()
                return {"transfers": [first], "pageKey": "next"} if self.calls == 1 else {"transfers": [second]}
            def logs(self, start, end, topics):
                return [log(A)] if topics[0] == t.TRANSFER else []
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "RUNTIME", Path(folder)):
            api = FakeAPI()
            events = []
            t.collect_transfers(api, 1, 1, {A, B}, events, set())
            self.assertEqual(len(events), 2)
            self.assertEqual(api.last_payload["pageKey"], "next")
            # A raw mint absent from the index makes the entire scan incomplete.
            with patch.object(api, "logs", return_value=[log("0x"+"4"*40)]):
                with self.assertRaises(RuntimeError):
                    t.collect_transfers(api, 1, 1, {A, B}, [], set())

    def test_complete_scan_and_report_with_reorg_checked_receipts(self):
        es, txs, receipts, _ = observations()
        now = 100000
        head = 1000
        event_block = head-30
        block_hash = lambda n: "0x"+f"{n:064x}"
        logs = []
        for i, e in enumerate(es):
            item = log(e["recipient"])
            item.update(transactionHash=e["tx"], blockNumber=hex(event_block), logIndex=hex(i))
            logs.append(item)
            receipts[e["tx"]].update(blockHash=block_hash(event_block), logs=[item])
        class FakeRPC:
            def __init__(self, *args):
                self.calls = 0
                self.head_reads = 0
            def block(self, n):
                self.calls += 1
                return {"timestamp": hex(now-(head-n)*10), "hash": block_hash(n)}
            def logs(self, lo, hi, topics):
                self.calls += 1
                return logs if lo <= event_block <= hi and topics[0] == t.TRANSFER else []
            def call(self, method, params):
                self.calls += 1
                if method == "eth_chainId":
                    return "0x38"
                if method == "eth_blockNumber":
                    self.head_reads += 1
                    return hex(head+20+(20 if self.head_reads > 1 else 0))
                if method == "eth_getTransactionByHash":
                    return txs[params[0]]
                if method == "eth_getTransactionReceipt":
                    return receipts[params[0]]
                if method == "alchemy_getAssetTransfers":
                    lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
                    rows = [{"from": t.ZERO, "to": e["recipient"], "blockNum": hex(event_block),
                             "hash": e["tx"], "rawContract": {"address": C}} for e in es]
                    return {"transfers": rows if lo <= event_block <= hi else []}
                raise AssertionError(method)
        with tempfile.TemporaryDirectory(dir=t.RUNTIME) as folder, patch.object(t, "RUNTIME", Path(folder)), patch.object(t, "RPC", FakeRPC), patch.object(t, "wallets", return_value={e["recipient"] for e in es}), patch.object(t.time, "time", return_value=now+200), patch.object(t, "verified_contract", return_value={"status": "unknown", "abi": []}):
            c = self.c | {"rpc_provider": "test", "discovery_mode": "alchemy_transfers"}
            db = t.database()
            try:
                result = t.scan(c, db)
                self.assertTrue(result["ok"])
                self.assertEqual(result["signals"][0]["wallet_count"], 20)
                self.assertEqual(result["new_or_changed_signals"], [C])
                self.assertEqual(result["to_block"], head+20)
                self.assertTrue((Path(folder) / "latest-scan.json").exists())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
