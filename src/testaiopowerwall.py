# Example: aiopowerwall Usage Demo
# --------------------------------
# cSpell:ignore aiopowerwall dotenv usefullpassword testaiopowerwall
# This script demonstrates how to connect to a Tesla Powerwall using the aiopowerwall library.
# It is a proof of concept for an early PW2 with an early GW1 (Gateway 1) without a reset button
# This gateway requires the Legacy password to log in.
# The legacy password is set in the Tesla One app, and must be passed - in full - to the v1r login method.
# It supports one connection mode (v1r).
#
# Usage:
#   - Set your connection mode and credentials below, or use a .env file with the following variables:
#       POWERWALL_HOST, POWERWALL_PASSWORD, POWERWALL_RSA_KEY_PATH
#   - Run: python testaiopowerwall.py
#
# For more info, see: https://github.com/Teslemetry/aiopowerwall
#
import asyncio
import os
from pathlib import Path

import dotenv
from aiopowerwall import PowerwallClient

# Load environment variables from .env file if present
dotenv.load_dotenv()
ip: str = ""
password: str = ""
rsa_key_path: str = ""
pem: bytes

# Override with .env variables
ip = os.getenv("POWERWALL_HOST", ip)
password = os.getenv("POWERWALL_PASSWORD", password)
rsa_key_path = os.getenv("POWERWALL_RSA_KEY_PATH", rsa_key_path)

pem = Path(rsa_key_path).read_bytes()


async def main() -> None:
    async with PowerwallClient(host=ip, gateway_password=password, rsa_private_key_pem=pem, usefullpassword=True) as pw:
        await pw.connect()
        status = await pw.get_status()
        config = await pw.get_config()
        print("Status:", status)
        print("Config", config)


asyncio.run(main())
