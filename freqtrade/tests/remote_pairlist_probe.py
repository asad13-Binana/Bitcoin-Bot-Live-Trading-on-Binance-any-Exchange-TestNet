"""Run the real pinned Freqtrade pair-list plugin; never start a trading bot.

Invoked by verify.sh with --network none and read-only source mounts. Only
exchange metadata is synthetic; URL parsing, JSON reads, cache and failure
handling are the actual installed Freqtrade implementation, not a copied parser.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from freqtrade import __version__
from freqtrade.exceptions import OperationalException
from freqtrade.plugins.pairlist.RemotePairList import RemotePairList


CONFIG = Path("/freqtrade/user_data/config.json")
PAIR_FILE = Path("/freqtrade/shared/pair/current_pairlist.json")


class OfflineExchange:
    """In-memory public-market metadata; deliberately has no network client."""

    name = "binance"
    markets = {
        "BTC/USDT": {"base": "BTC", "quote": "USDT", "active": True, "spot": True},
    }

    def get_markets(self):
        return self.markets

    def market_is_tradable(self, market):
        return market["spot"] is True

    def get_pair_quote_currency(self, pair):
        return self.markets[pair]["quote"]


class RemotePairListContract(unittest.TestCase):
    def setUp(self):
        self.assertEqual(__version__, "2026.6", "review this contract before upgrading")
        self.assertEqual(Path.cwd(), Path("/freqtrade"))
        self.config_bytes = CONFIG.read_bytes()
        self.pair_bytes = PAIR_FILE.read_bytes()
        self.config = json.loads(self.config_bytes)
        self.assertIs(self.config["dry_run"], True)
        self.assertIs(self.config["force_entry_enable"], False)
        self.assertEqual(self.config["trading_mode"], "spot")
        self.assertFalse(self.config["exchange"]["key"])
        self.assertFalse(self.config["exchange"]["secret"])
        self.assertEqual(json.loads(self.pair_bytes)["pairs"], ["BTC/USDT"])
        self.assertIs(self.config["pairlists"][0]["keep_pairlist_on_failure"], False)
        guard = patch("requests.get", side_effect=AssertionError("network is forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def tearDown(self):
        self.assertEqual(CONFIG.read_bytes(), self.config_bytes)
        self.assertEqual(PAIR_FILE.read_bytes(), self.pair_bytes)

    def plugin(self, url=None):
        entry = deepcopy(self.config["pairlists"][0])
        if url is not None:
            entry["pairlist_url"] = url
        return RemotePairList(OfflineExchange(), None, self.config, entry, 0)

    def test_shipped_url_real_initialisation_and_three_uncached_refreshes(self):
        # Use the SHIPPED URL, never substitute the candidate inside this test.
        plugin = self.plugin()
        for _ in range(3):
            plugin._pair_cache.clear()  # Force periodic I/O without wall-clock sleeps.
            self.assertEqual(plugin.gen_pairlist({}), ["BTC/USDT"])
            self.assertTrue(plugin._init_done)
            self.assertEqual(plugin._last_pairlist, ["BTC/USDT"])
        print("REAL_REMOTEPAIRLIST_INITIALISATION_AND_REFRESH=PASS", flush=True)

    def test_old_three_slash_url_reproduces_real_plugin_failure(self):
        bad_url = "file:///" + PAIR_FILE.as_posix().lstrip("/")
        self.assertFalse(Path(bad_url.split("file:///", 1)[1]).exists())
        plugin = self.plugin(bad_url)
        with self.assertRaisesRegex(OperationalException, "does not exist"):
            plugin.gen_pairlist({})
        self.assertFalse(plugin._init_done)
        print("THREE_SLASH_NEGATIVE_CONTROL=REPRODUCED", flush=True)

    def test_initial_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.plugin("file:///" + str(Path(temp) / "missing.json"))
            with self.assertRaisesRegex(OperationalException, "does not exist"):
                plugin.gen_pairlist({})

    def test_initial_malformed_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "pairlist.json"
            fixture.write_text("{invalid", encoding="utf-8")
            plugin = self.plugin("file:///" + str(fixture))
            with self.assertRaisesRegex(OperationalException, "processing JSON"):
                plugin.gen_pairlist({})

    def test_refresh_failure_does_not_keep_a_stale_pair(self):
        for failure in ("missing", "malformed"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                fixture = Path(temp) / "pairlist.json"
                fixture.write_bytes(self.pair_bytes)
                plugin = self.plugin("file:///" + str(fixture))
                self.assertEqual(plugin.gen_pairlist({}), ["BTC/USDT"])
                if failure == "missing":
                    fixture.unlink()
                else:
                    fixture.write_text("{invalid", encoding="utf-8")
                plugin._pair_cache.clear()
                self.assertEqual(plugin.gen_pairlist({}), [])

    def test_empty_pairlist_refreshes_to_the_fixture_without_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "pairlist.json"
            fixture.write_text('{"pairs": []}', encoding="utf-8")
            plugin = self.plugin("file:///" + str(fixture))
            self.assertEqual(plugin.gen_pairlist({}), [])
            fixture.write_bytes(self.pair_bytes)
            plugin._pair_cache.clear()
            self.assertEqual(plugin.gen_pairlist({}), ["BTC/USDT"])


if __name__ == "__main__":
    print("PAIR_FIXTURE_SHA256=" + hashlib.sha256(PAIR_FILE.read_bytes()).hexdigest())
    unittest.main(verbosity=2)
