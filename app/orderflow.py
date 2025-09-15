import asyncio

import aiohttp

from . import telemetry

_CVD = 0.0


def get_cvd():
    return _CVD


async def start_ws(pair_ccxt: str):
    # Minimal Delta trades WS -> accumulates CVD.
    sym = pair_ccxt.replace("/", "")
    url = "wss://socket.delta.exchange"
    global _CVD
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    await ws.send_json(
                        {
                            "type": "subscribe",
                            "payload": {"channels": [{"name": "trades", "symbols": [sym]}]},
                        }
                    )
                    telemetry.log("orderflow", "WS", "connected", {"pair": pair_ccxt})
                    async for msg in ws:
                        try:
                            data = msg.json()
                        except Exception:
                            continue
                        if data.get("type") == "trade":
                            for t in data.get("trades", []):
                                side = t.get("side")
                                size = float(t.get("size", 0))
                                if side == "buy":
                                    _CVD += size
                                elif side == "sell":
                                    _CVD -= size
        except Exception as e:
            telemetry.log("orderflow", "WS_ERR", str(e), {})
            await asyncio.sleep(3)
