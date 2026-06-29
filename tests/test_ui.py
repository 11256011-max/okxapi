from decimal import Decimal
import unittest

from okx_bot.models import Signal
from okx_bot.ui import action_label, decimal_string, first_query_value, percent_string, signal_snapshot


class UiTests(unittest.TestCase):
    def test_percent_and_decimal_formatting(self) -> None:
        self.assertEqual(decimal_string(Decimal("200.00")), "200")
        self.assertEqual(decimal_string(Decimal("0.0200")), "0.02")
        self.assertEqual(percent_string(Decimal("0.02")), "2.00%")

    def test_action_label_uses_trading_terms(self) -> None:
        self.assertEqual(action_label("buy"), "做多")
        self.assertEqual(action_label("sell"), "做空")
        self.assertEqual(action_label("hold"), "觀望")

    def test_signal_snapshot_includes_confidence_gap(self) -> None:
        signal = Signal("buy", "ready", Decimal("100"), {}, Decimal("0.72"))

        snapshot = signal_snapshot(signal, Decimal("0.68"))

        self.assertEqual(snapshot["action_label"], "做多")
        self.assertEqual(snapshot["confidence_pct"], "72.00%")
        self.assertEqual(snapshot["confidence_gap_pct"], "4.00%")

    def test_first_query_value_uses_default_for_missing_values(self) -> None:
        self.assertEqual(first_query_value({}, "days", "30"), "30")
        self.assertEqual(first_query_value({"days": ["90"]}, "days", "30"), "90")


if __name__ == "__main__":
    unittest.main()
