import aiohttp
from . import config as C

async def tg_send(text: str):
    if not C.TG_TOKEN or not C.TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{C.TG_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        try:
            r = await s.post(url, json={"chat_id": C.TG_CHAT_ID, "text": text}, timeout=10)
            if r.status != 200:
                # Try to extract Telegram error description for clarity
                body_text = await r.text()
                desc = body_text
                try:
                    data = await r.json()
                    desc = data.get("description", body_text)
                except Exception:
                    # response was not JSON
                    pass
                print("[TG] HTTP", r.status, desc)
        except Exception as e:
            # Many network timeouts raise exceptions with empty str(e); include the class name
            print("[TG] Exception:", type(e).__name__, repr(e))


