"""CLOB V2 Order Execution Layer.

Handles EIP-712 order signing and submission to Polymarket CLOB V2.
Uses py-clob-client-v2 for order construction and eth-account for signing.

Important: pUSD (not USDC) is the collateral in CLOB V2.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("moneymaker.executor")

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class CLOBClient:
    """Thin client for Polymarket CLOB V2 order submission.

    Requires: private key (from CryptoVault) + API credentials
    generated via EIP-712 signing.
    """

    def __init__(
        self,
        private_key_hex: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ) -> None:
        self._private_key = private_key_hex
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._address: str | None = None

    def _derive_address(self) -> str:
        """Derive Ethereum address from private key."""
        if self._address:
            return self._address
        try:
            from eth_account import Account
            account = Account.from_key(self._private_key)
            self._address = account.address
            return self._address
        except ImportError:
            logger.error("eth-account not installed")
            raise

    def _sign_order(self, order_data: dict) -> str:
        """Sign EIP-712 order hash with private key."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_structured_data

            domain = {
                "name": "Polymarket CTF Exchange",
                "version": "1",
                "chainId": CHAIN_ID,
                "verifyingContract": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
            }

            types = {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            }

            signable = encode_structured_data({
                "primaryType": "Order",
                "domain": domain,
                "types": {k: v for k, v in types.items() if k != "EIP712Domain"},
                "message": order_data,
            })

            account = Account.from_key(self._private_key)
            signed = account.sign_message(signable)
            return signed.signature.hex()
        except ImportError:
            logger.error("eth-account not installed")
            raise

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate L1 authentication headers for CLOB V2 API."""
        import hashlib
        import hmac
        import base64

        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            base64.b64decode(self._api_secret),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        return {
            "POLY_ADDRESS": self._derive_address(),
            "POLY_SIGNATURE": base64.b64encode(signature).decode(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self._api_key,
            "POLY_PASSPHRASE": self._api_passphrase,
        }

    async def get_balance(self, token_id: str | None = None) -> dict[str, Any]:
        """Get pUSD balance for the wallet."""
        address = self._derive_address()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{CLOB_BASE}/balance-allowance",
                params={"asset_type": "COLLATERAL", "address": address},
            )
            if resp.status_code == 200:
                return resp.json()
            return {"balance": "0", "allowance": "0"}

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee_rate_bps: int = 0,
    ) -> dict[str, Any]:
        """Place a limit order on CLOB V2.

        Args:
            token_id: The CLOB token ID for the outcome
            side: "BUY" or "SELL"
            price: Price per token (0.01–0.99)
            size: Number of tokens
            fee_rate_bps: Fee rate in basis points
        """
        address = self._derive_address()
        salt = str(int(time.time() * 1000))

        # Calculate amounts: makerAmount = size * price (in pUSD), takerAmount = size (in tokens)
        maker_amount = int(size * price * 1e6)  # pUSD has 6 decimals
        taker_amount = int(size * 1e6)

        order_data = {
            "salt": salt,
            "maker": address,
            "signer": address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": "0",
            "nonce": "0",
            "feeRateBps": str(fee_rate_bps),
            "side": 0 if side.upper() == "BUY" else 1,
            "signatureType": 0,  # EOA
        }

        signature = self._sign_order(order_data)
        order_data["signature"] = signature

        body = json.dumps(order_data)
        headers = self._auth_headers("POST", "/order", body)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{CLOB_BASE}/order",
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
            result = resp.json()
            logger.info("CLOB order response: %s", result)
            return result

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an existing order."""
        body = json.dumps({"orderID": order_id})
        headers = self._auth_headers("DELETE", "/order", body)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(
                "DELETE",
                f"{CLOB_BASE}/order",
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
            return resp.json()
