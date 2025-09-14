import json
from typing import Dict
from openai import OpenAI
from . import config as C
from .memory import recent_lessons
client = OpenAI(api_key=C.OPENAI_API_KEY)
def decide(features: Dict) -> Dict:
    lessons = recent_lessons(features["pair"], limit=15)
    prompt = {
        "policy": "Capital saved is capital earned. Avoid hard targets; use structure, flow and risk.",
        "features": features, "recent_lessons": lessons,
        "allowed_actions": ["HOLD","EXIT","TRIM","MOVE_SL_BE","TRAIL_TIGHT","EXTEND"],
        "principles": [
            "Exit if structure breaks (1m/5m swing fail against position).",
            "Tighten risk when momentum stalls after MFE expansion.",
            "Move SL to breakeven once trade covers typical adverse excursion.",
            "Extend when orderflow remains strong and liquidity above/below is likely to be swept.",
            "Prefer small loss or flat over hope. No emotion."
        ]
    }
    resp = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0, response_format={"type":"json_object"},
        messages=[{"role":"system","content":"You are an unemotional intraday risk manager. Decide action. Be concise."},
                  {"role":"user","content": json.dumps(prompt)}]
    )
    try: data = json.loads(resp.choices[0].message.content)
    except Exception: data = {"action":"HOLD","why":"Model output malformed; default HOLD","confidence":0.3}
    return data
