import tempfile
import os
import sqlite3
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import flightwatch as fw


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fw.load_config(fw.ROOT / "config.json")
        self.cfg["exclude_russian_connections"] = False
        self.rows = fw.demo_offers(self.cfg)

    def test_filters_dates_stops_return_and_invalid_prices(self):
        bad = []
        for override in ({"departure_at": "2026-11-09T12:00:00Z"}, {"transfers": 3},
                         {"price": -1}, {"price": float("nan")}, {"return_at": "2026-12-01"},
                         {"link": "//evil.example/search/test"}, {"destination": "BKK", "destination_airport": "BKK"}):
            bad.append(dict(self.rows[0], **override))
        result = fw.select_offers(self.rows + bad, self.cfg, date(2026, 10, 24))
        self.assertEqual(len(result), len(self.cfg["origins"]))
        self.assertEqual(result[0]["price"], 25000)

    def test_deduplication_keeps_cheapest_and_currency_history_is_separate(self):
        rows = self.rows + [dict(self.rows[0], price=20000)]
        offers = fw.select_offers(rows, self.cfg, date(2026, 10, 24))
        self.assertEqual(len(offers), len(self.cfg["origins"]))
        self.assertEqual(offers[0]["price"], 20000)
        with tempfile.TemporaryDirectory() as temp:
            with fw.open_database(Path(temp) / "history.db") as db:
                first = fw.record_offers(db, offers, "rub")
                self.assertIsNone(first[offers[0]["key"]])
                offers[0]["price"] = 19000
                second = fw.record_offers(db, offers, "rub")
                self.assertEqual(second[offers[0]["key"]], 20000)
                self.assertIsNone(fw.record_offers(db, offers, "usd")[offers[0]["key"]])

    def test_moscow_airport_searches_merge_same_flight_at_lowest_price(self):
        base = dict(self.rows[0], origin="MOW", origin_airport="SVO", destination="NHA", destination_airport="CXR", flight_number="0294", price=40000)
        duplicate = dict(base, origin="SVO", destination="CXR", flight_number="294", price=39000)
        another = dict(base, departure_at="2026-11-20T18:00:00+03:00")
        selected = fw.select_offers([base, duplicate, another], self.cfg, date(2026, 10, 24))
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["price"], 39000)
        self.assertEqual({row["origin"] for row in selected}, {"MOW"})
        self.assertEqual(len(fw.digest_offers(selected, self.cfg)), 2)
        self.assertTrue({"MOW", "SVO", "DME", "VKO"}.issubset(fw.search_origins(self.cfg)))

    def test_month_boundaries(self):
        self.assertEqual(list(fw.months("2026-12-11", "2027-02-01")), ["2026-12", "2027-01", "2027-02"])

    def test_api_city_code_differs_from_requested_airport(self):
        row = dict(self.rows[0], destination="NHA", destination_airport="CXR")
        self.assertEqual(len(fw.select_offers([row], self.cfg, date(2026, 10, 24))), 1)
        row["destination_airport"] = "HAN"
        self.assertEqual(fw.select_offers([row], self.cfg, date(2026, 10, 24)), [])

    def test_partial_fetch_failure_preserves_other_routes(self):
        response = {"success": True, "data": [self.rows[0]]}
        with patch.object(fw, "request_json", side_effect=[RuntimeError("HTTP 503")] + [response] * (2 * len(fw.search_origins(self.cfg)) - 1)):
            rows, errors = fw.fetch_offers(self.cfg, "test")
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(rows), 2 * len(fw.search_origins(self.cfg)) - 1)

    def test_fetch_requests_direct_separately(self):
        cfg = dict(self.cfg, origins=["SVX"])
        with patch.object(fw, "request_json", return_value={"success": True, "data": []}) as request:
            fw.fetch_offers(cfg, "test")
        self.assertEqual(request.call_count, 2)
        self.assertIn("direct=true", request.call_args_list[0].args[0])
        self.assertIn("direct=false", request.call_args_list[1].args[0])

    def test_quiet_hours_samara_and_strict_threshold(self):
        self.cfg["quiet_hours_enabled"] = True
        offers = [{"price": 36999}, {"price": 37000}, {"price": 40000}]
        for timestamp in ("2026-11-10T20:00:00+00:00", "2026-11-11T04:59:59+00:00"):
            selected, quiet = fw.notification_offers(offers, self.cfg, datetime.fromisoformat(timestamp))
            self.assertTrue(quiet)
            self.assertEqual(selected, [{"price": 36999}])
        for timestamp in ("2026-11-10T19:59:59+00:00", "2026-11-11T05:00:00+00:00"):
            selected, quiet = fw.notification_offers(offers, self.cfg, datetime.fromisoformat(timestamp))
            self.assertFalse(quiet)
            self.assertEqual(selected, offers)
        self.assertEqual(fw.notification_offers([], self.cfg, datetime.fromisoformat("2026-11-11T00:00:00+04:00")), ([], True))

    def test_night_updates_have_no_special_price_limit_when_disabled(self):
        offers = [{"price": 36999}, {"price": 55000}]
        selected, quiet = fw.notification_offers(offers, dict(self.cfg, quiet_hours_enabled=False),
                                                 datetime.fromisoformat("2026-11-11T02:00:00+04:00"))
        self.assertEqual(selected, offers)
        self.assertFalse(quiet)

    def test_messages_fit_telegram_and_warn_on_partial_results(self):
        offers = fw.select_offers(self.rows, self.cfg, date(2026, 10, 24))
        messages = fw.render_messages(offers, self.cfg, {}, ["error"], True)
        self.assertEqual(len(messages), 1)
        self.assertTrue(all(fw.visible_length(message) <= 4096 for message in messages))
        self.assertIn("ВЫМЫШЛЕННЫЕ", messages[0])
        self.assertIn("неполная", messages[0])

    def test_samara_time_hyperlink_and_day_rollover(self):
        row = dict(self.rows[0], departure_at="2026-11-26T22:00:00+03:00", airline="A&B")
        offers = fw.select_offers([row], self.cfg, date(2026, 10, 24))
        message = "\n".join(fw.render_messages(offers, self.cfg, {}))
        self.assertIn("26.11 23:00", message)
        self.assertIn("IJK → CXR", message)
        self.assertNotIn("Авиакомпания", message)
        self.assertNotIn("Источник:", message)
        self.assertNotIn("без изменений", message)
        self.assertNotIn("новое в истории", message)
        self.assertIn("Обновлено:", message)
        self.assertIn("(Самара)", message)
        self.assertIn("<b><u>25 000 ₽</u></b></a>", message)
        self.assertIn("🔁", message)
        self.assertNotIn("1 пересадка", message)
        self.assertNotIn("с багажом", message)
        offers[0]["departure_at"] = "2026-11-26T23:30:00+03:00"
        self.assertIn("27.11 00:30", "\n".join(fw.render_messages(offers, self.cfg, {})))
        offers[0].update(origin="SVX", origin_airport="SVX", departure_at="2026-11-26T22:00:00+05:00")
        self.assertIn("26.11 22:00", fw.render_messages(offers, self.cfg, {})[0])

    def test_two_transfers_are_excluded(self):
        row = dict(self.rows[0], transfers=2)
        self.assertEqual(fw.select_offers([row], self.cfg, date(2026, 10, 24)), [])

    def test_direct_flights_first_and_price_ascending_within_groups(self):
        rows = [dict(self.rows[0], flight_number=str(i), transfers=stops, price=price)
                for i, (stops, price) in enumerate([(1, 30000), (0, 49000), (1, 25000), (0, 45000)])]
        offers = fw.select_offers(rows, self.cfg, date(2026, 10, 24))
        self.assertEqual([(row["transfers"], row["price"]) for row in offers],
                         [(0, 45000), (0, 49000), (1, 25000), (1, 30000)])

    def test_long_hidden_urls_do_not_split_message(self):
        offers = fw.select_offers(self.rows, self.cfg, date(2026, 10, 24))
        for row in offers:
            row["url"] = "https://www.aviasales.ru/search/?q=" + "x" * 1800
        messages = fw.render_messages(offers, self.cfg, {})
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].count('<a href='), len(offers))
        self.assertGreater(len(messages[0]), 4096)
        self.assertLess(fw.visible_length(messages[0]), 4096)

    def test_large_selection_stays_one_message(self):
        cfg = dict(self.cfg, top_n=50)
        row = fw.select_offers(self.rows, cfg, date(2026, 10, 24))[0]
        messages = fw.render_messages([dict(row, key=str(i), price=row["price"] + i) for i in range(150)], cfg, {})
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(fw.visible_length(messages[0]), 4096)
        self.assertGreater(messages[0].count('<a href='), 1)
        self.assertIn("Не поместилось", messages[0])

    def test_digest_reserves_place_for_every_available_origin(self):
        rows = [dict(self.rows[0], origin="MOW", origin_airport="SVO", key=str(i), price=30000 + i, transfers=0)
                for i in range(15)]
        rows += [dict(self.rows[0], origin=origin, origin_airport=origin, key=origin, price=60000 + i, transfers=1)
                 for i, origin in enumerate(["SVX", "KZN", "PEE", "IJK", "UFA"])]
        selected = fw.digest_offers(rows, self.cfg)
        self.assertEqual(len(selected), len(rows))
        self.assertEqual({row["origin"] for row in selected}, set(self.cfg["origins"]))
        self.assertIn(30000, [row["price"] for row in selected])
        self.assertEqual(fw.digest_offers([], self.cfg), [])

    def test_digest_keeps_cheapest_connection_beside_direct(self):
        row = dict(self.rows[0], origin="MOW", origin_airport="SVO")
        offers = [dict(row, transfers=0, price=40000 + i) for i in range(20)]
        offers += [dict(row, transfers=1, price=30000), dict(row, transfers=1, price=31000)]
        selected = fw.digest_offers(offers, self.cfg)
        self.assertEqual(len(selected), 22)
        self.assertEqual(selected[0]["price"], 40000)
        self.assertEqual(selected[-2]["price"], 30000)

    def test_rolling_minimum_ignores_old_other_currency_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = fw.open_database(Path(directory) / "history.db")
            try:
                db.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
                    ("test", "rub", 10000, "2026-10-16T12:00:00+00:00"),
                    ("test", "usd", 100, "2026-10-24T12:00:00+00:00"),
                    ("test", "rub", 30000, "2026-10-24T12:00:00+00:00"),
                    ("test", "rub", 32000, "2026-10-25T11:00:00+00:00")])
                row = {"key": "test", "price": 32000}
                fw.annotate_minimums(db, [row], "rub", datetime.fromisoformat("2026-10-25T12:00:00+00:00"))
                self.assertEqual(row["minimum_7d"], 30000)
                row["unavailable"] = True
                fw.annotate_minimums(db, [row], "rub")
                self.assertNotIn("minimum_7d", row)
            finally:
                db.close()

    def test_cheap_alert_first_detection_and_only_new_low(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "resources").symlink_to(fw.ROOT / "resources", target_is_directory=True)
            row = dict(self.rows[0], transfers=0, key="test", url="https://www.aviasales.ru/search/test")
            with patch.object(fw, "ROOT", root), patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "-123"}), patch.object(fw, "send_telegram") as send:
                for price, expected in [(36000, 0), (35000, 1), (35000, 0), (35500, 0), (35200, 0), (34900, 1)]:
                    self.assertEqual(fw.send_cheap_alerts([dict(row, price=price)], self.cfg), expected)
                self.assertEqual(send.call_count, 2)
                self.assertIn("Дешевые авиабилеты", send.call_args.args[0][0])
                self.assertIn("🟢 (35 000 ₽)", send.call_args.args[0][0])

    def test_connection_alert_threshold_and_failed_delivery_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            row = dict(self.rows[0], price=24999, connection_airport="CAN")
            with patch.object(fw, "ROOT", root), patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "-123"}), patch.object(fw, "render_messages", return_value=['<a href="https://example.com">test</a>']):
                with patch.object(fw, "send_telegram", side_effect=RuntimeError("send failed")):
                    with self.assertRaises(RuntimeError):
                        fw.send_cheap_alerts([row], self.cfg)
                with patch.object(fw, "send_telegram") as send:
                    self.assertEqual(fw.send_cheap_alerts([dict(row, price=25000)], self.cfg), 0)
                    self.assertEqual(fw.send_cheap_alerts([dict(row, unavailable=True)], self.cfg), 0)
                    self.assertEqual(fw.send_cheap_alerts([row], self.cfg), 1)
                    self.assertEqual(fw.send_cheap_alerts([row], self.cfg), 0)
                    self.assertEqual(send.call_count, 1)

    def test_price_cap_includes_exactly_55000(self):
        rows = [dict(self.rows[0], flight_number=str(i), price=price)
                for i, price in enumerate([54999, 55000, 55001])]
        selected = fw.select_offers(rows, self.cfg, date(2026, 10, 24))
        self.assertEqual([r["price"] for r in selected], [54999, 55000])

    def test_refresh_updates_price_and_departure_but_marks_missing(self):
        old = dict(self.rows[0], key="old")
        changed = dict(old, key="new", price=31000, departure_at=old["departure_at"].replace("12:00", "13:00"))
        refreshed = fw.refresh_offers([old], [changed])
        self.assertEqual(refreshed, [dict(changed, price_changed=True, previous_price=old["price"])])
        self.assertFalse(fw.refresh_offers(refreshed, [changed])[0]["price_changed"])
        self.assertTrue(fw.refresh_offers([old], [])[0]["unavailable"])
        self.assertTrue(fw.refresh_offers([old], [changed, dict(changed, price=32000)])[0]["unavailable"])

    def test_updated_prices_have_marker_only_when_changed(self):
        row = fw.select_offers(self.rows, self.cfg, date(2026, 10, 24))[0]
        changed = dict(row, price=row["price"] + 100)
        refreshed = fw.refresh_offers([row], [changed])
        message = fw.render_messages(refreshed, self.cfg, {})[0]
        self.assertIn("CXR 🆕 🔴 (<s>25 000 ₽</s>) · цена изменилась\n", message)
        self.assertNotIn("🔁 🆕", message)
        same = fw.refresh_offers(refreshed, [changed])
        self.assertNotIn("🆕", fw.render_messages(same, self.cfg, {})[0])
        lower = fw.refresh_offers([changed], [dict(changed, price=24000)])
        self.assertIn("CXR 🆕 🟢 (25 100 ₽)\n", fw.render_messages(lower, self.cfg, {})[0])

    def test_minimum_is_displayed_and_current_minimum_marked(self):
        row = fw.select_offers(self.rows, self.cfg, date(2026, 10, 24))[0]
        row["minimum_7d"] = row["price"]
        self.assertIn("мин. 7д: 25 000 ₽ ✓", fw.render_messages([row], self.cfg, {})[0])
        row["minimum_7d"] = 24000
        message = fw.render_messages([row], self.cfg, {})[0]
        self.assertIn("мин. 7д: 24 000 ₽", message)
        self.assertNotIn("✓", message)

    def test_automatic_update_keeps_new_offers_and_marks_changed_prices(self):
        old = dict(self.rows[0], key="old")
        current = dict(old, price=30000)
        new = dict(self.rows[1], key="new")
        result = fw.mark_current_prices([old], [current, new])
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0]["price_changed"])
        self.assertEqual(result[0]["previous_price"], old["price"])
        self.assertEqual(result[1], new)

    def test_refresh_edits_same_message_and_preserves_button(self):
        snapshot = {"config": self.cfg, "offers": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            with patch.object(fw, "ROOT", root), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "-123"}):
                with patch.object(fw, "request_json", return_value={"ok": True, "result": {"message_id": 42}}) as request:
                    fw.send_telegram(["test"], snapshot=snapshot)
                    self.assertTrue(request.call_args.args[0].endswith("/sendMessage"))
                with patch.object(fw, "request_json", return_value={"ok": True, "unchanged": True}) as request:
                    fw.send_telegram(["updated"], message_id=42, snapshot=snapshot)
                    self.assertTrue(request.call_args.args[0].endswith("/editMessageText"))
                    payload = request.call_args.kwargs["payload"]
                    self.assertEqual(payload["message_id"], 42)
                    self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "refresh_prices")
                self.assertEqual(fw.latest_message("-123"), (42, snapshot))
                self.assertIsNone(fw.latest_message("other"))

    def test_connection_country_filter_rejects_unknown_and_russian_routes(self):
        cfg = dict(self.cfg, exclude_russian_connections=True)
        row = dict(self.rows[0], origin_airport="IJK", destination_airport="CXR")
        for route in ("IJKSVOCXR", "IJKZZZCXR", "IJKCANCXRJFK"):
            row["link"] = "/search/test?t=CZ17955621001795686000001825" + route + "_signature"
            self.assertEqual(fw.select_offers([row], cfg, date(2026, 10, 24)), [])
        row["link"] = "/search/test?t=CZ17955621001795686000001825IJKCANCXR_signature"
        selected = fw.select_offers([row], cfg, date(2026, 10, 24))
        self.assertEqual(selected[0]["connection_airport"], "CAN")
        message = fw.render_messages(selected, cfg, {})[0]
        self.assertIn("→ Гуанчжоу (CAN, время уточняется) → CXR", message)
        self.assertIn("Обновлено:", message)
        row["origin_airport"] = "KZN"
        self.assertEqual(fw.select_offers([row], cfg, date(2026, 10, 24)), [])
        row["transfers"] = 0
        self.assertEqual(len(fw.select_offers([row], cfg, date(2026, 10, 24))), 1)

    def test_connection_duration_verified_against_aviasales(self):
        self.assertEqual(fw.connection_duration({"duration": 1370, "duration_to": 705}), "11 ч 5 мин")
        self.assertEqual(fw.connection_duration({"duration": 890, "duration_to": 710}), "3 ч")
        self.assertEqual(fw.connection_duration({"duration": 100, "duration_to": 55}), "45 мин")
        for row in ({}, {"duration": 50, "duration_to": 80}, {"duration": float('nan'), "duration_to": 80}):
            self.assertEqual(fw.connection_duration(row), "время уточняется")


if __name__ == "__main__":
    unittest.main()
