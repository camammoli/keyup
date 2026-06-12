"""
RadioID.net API lookup — resolves DMR IDs to callsigns/names.
Results are cached in memory for the lifetime of the process.
"""

import logging
import aiohttp
from typing import Optional

log = logging.getLogger('keyup.contact')

RADIOID_URL = 'https://radioid.net/api/dmr/user/?id={}'
_cache: dict[int, dict] = {}


async def lookup(dmr_id: int) -> Optional[dict]:
    """Return {'callsign': ..., 'name': ..., 'city': ..., 'country': ...} or None."""
    if dmr_id in _cache:
        return _cache[dmr_id]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RADIOID_URL.format(dmr_id), timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                results = data.get('results', [])
                if not results:
                    return None
                entry = results[0]
                info = {
                    'callsign': entry.get('callsign', str(dmr_id)),
                    'name':     entry.get('fname', '') + ' ' + entry.get('surname', ''),
                    'city':     entry.get('city', ''),
                    'country':  entry.get('country', ''),
                }
                _cache[dmr_id] = info
                return info
    except Exception as e:
        log.warning('RadioID lookup failed for %d: %s', dmr_id, e)
        return None
