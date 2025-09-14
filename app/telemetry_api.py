from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
import json, asyncio
from . import telemetry

app = FastAPI()

@app.get("/api/telemetry")
def api_telemetry(component: str = "", q: str = "", limit: int = 200):
    rows = telemetry.recent_filtered(limit=limit, component=component, q=q)
    return JSONResponse({"rows": rows, "count": len(rows)})

# Optional: Server-Sent Events stream (push-style)
@app.get("/api/telemetry/stream")
async def telemetry_stream(component: str = "", q: str = "", limit: int = 200):
    async def gen():
        last = []
        while True:
            rows = telemetry.recent_filtered(limit=limit, component=component, q=q)
            if rows != last:
                data = json.dumps(rows)
                yield f"data: {data}\n\n"
                last = rows
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")