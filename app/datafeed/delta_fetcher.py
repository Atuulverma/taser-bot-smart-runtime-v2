from __future__ import annotations

import time
from typing import List, Optional

try:
    # Community client: https://pypi.org/project/delta-rest-client/
    # Typical usage: from delta_rest_client import DeltaRestClient
    from delta_rest_client import DeltaRestClient
except Exception:
    DeltaRestClient = None


class DeltaFetcher:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        endpoint: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.endpoint = endpoint or "https://api.delta.exchange"
        self.client = None
        if DeltaRestClient is not None:
            try:
                # Some versions expect base_url, others endpoint; try both safely.
                try:
                    self.client = DeltaRestClient(
                        api_key=api_key,
                        api_secret=api_secret,
                        base_url=self.endpoint,
                    )
                except TypeError:
                    self.client = DeltaRestClient(
                        api_key=api_key,
                        api_secret=api_secret,
                        endpoint=self.endpoint,
                    )
            except Exception:
                self.client = None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> List[List[float]]:
        if self.client is not None:
            # TODO: map client response to [ts, o,h,l,c,v]
            pass
        now = int(time.time() * 1000)
        step = 60_000 if timeframe.endswith("m") else 60 * 60 * 1000
        return [[now - i * step, 0, 0, 0, 0, 0] for i in range(limit)]
