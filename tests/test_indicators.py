import unittest

from okx_bot.indicators import ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_keeps_input_length(self) -> None:
        values = [1, 2, 3, 4, 5]
        result = ema(values, 3)
        self.assertEqual(len(result), len(values))
        self.assertGreater(result[-1], result[0])

    def test_rsi_keeps_input_length(self) -> None:
        values = [1, 2, 3, 4, 5, 6]
        result = rsi(values, 3)
        self.assertEqual(len(result), len(values))
        self.assertIsNone(result[0])
        self.assertEqual(result[-1], 100.0)


if __name__ == "__main__":
    unittest.main()

