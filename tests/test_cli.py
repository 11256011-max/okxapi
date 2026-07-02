import unittest

from ccxt.base.errors import PermissionDenied

from okx_bot.cli import concise_okx_error_message


class CliTests(unittest.TestCase):
    def test_concise_okx_error_message_extracts_code_and_message(self) -> None:
        error = PermissionDenied('okx {"msg":"Your IP is not included in your API key IP whitelist.","code":"50110"}')

        message = concise_okx_error_message(error)

        self.assertEqual(message, "OKX code 50110: Your IP is not included in your API key IP whitelist.")


if __name__ == "__main__":
    unittest.main()
