"""
FastAPI routes:
  GET  /             → serve web/index.html
  GET  /static/...   → serve web/ assets
  GET  /api/status   → connection state + active calls
  GET  /api/contacts → last heard list
  POST /api/offer    → WebRTC SDP exchange
  POST /api/answer   → WebRTC SDP answer
  WS   /ws           → real-time events to browser
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.dmr import contact as contact_db
from server.dmr.homebrew import HomebrewState

log = logging.getLogger('keyup.api')

WEB_DIR = Path(__file__).parent.parent.parent / 'web'
router  = APIRouter()

# Shared state — injected by main.py
_homebrew     = None
_bridge       = None
_ws_clients: list[WebSocket] = []
_last_heard:  list[dict]     = []   # [{ts, src_id, dst_id, callsign, name, duration}]
MAX_LAST_HEARD = 20


def init(homebrew, bridge):
    global _homebrew, _bridge
    _homebrew = homebrew
    _bridge   = bridge

    homebrew.on_frame = _on_dmr_frame
    homebrew.on_state = _on_state_change


# ── REST ──────────────────────────────────────────────────────────────────────

@router.get('/')
async def index():
    return FileResponse(WEB_DIR / 'index.html')


@router.get('/api/status')
async def status():
    return {
        'state':       _homebrew.state if _homebrew else HomebrewState.DISCONNECTED,
        'talkgroup':   _homebrew.cfg.talkgroup if _homebrew else 0,
        'last_heard':  _last_heard[:10],
        'active_calls': [
            {'stream_id': sid.hex(), **info}
            for sid, info in (_homebrew._active_streams.items() if _homebrew else {})
        ],
    }


@router.get('/api/contacts')
async def contacts():
    return {'contacts': _last_heard}


class OfferBody(BaseModel):
    sdp: str
    type: str


class AnswerBody(BaseModel):
    sdp: str
    type: str


@router.post('/api/offer')
async def webrtc_offer(body: OfferBody):
    if not _bridge:
        raise HTTPException(503, 'Bridge not ready')
    offer = await _bridge.create_offer()
    return offer


@router.post('/api/answer')
async def webrtc_answer(body: AnswerBody):
    if not _bridge:
        raise HTTPException(503, 'Bridge not ready')
    await _bridge.set_answer(body.sdp, body.type)
    return {'ok': True}


class TalkgroupBody(BaseModel):
    talkgroup: int


@router.post('/api/talkgroup')
async def set_talkgroup(body: TalkgroupBody):
    if not _homebrew:
        raise HTTPException(503, 'Not connected')
    _homebrew.cfg.talkgroup = body.talkgroup
    await _broadcast({'type': 'talkgroup', 'talkgroup': body.talkgroup})
    return {'ok': True, 'talkgroup': body.talkgroup}


class PttBody(BaseModel):
    active: bool


@router.post('/api/ptt')
async def ptt(body: PttBody):
    if not _bridge:
        raise HTTPException(503, 'Bridge not ready')
    _bridge.set_ptt(body.active)
    return {'ok': True, 'ptt': body.active}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@router.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        # Send current state immediately
        await ws.send_json({'type': 'state', 'state': _homebrew.state if _homebrew else 'disconnected'})
        while True:
            await ws.receive_text()   # keep-alive / ping from browser
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.remove(ws)


async def _broadcast(msg: dict):
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# ── Internal callbacks ────────────────────────────────────────────────────────

def _on_state_change(state: str):
    asyncio.ensure_future(_broadcast({'type': 'state', 'state': state}))


_stream_start: dict[bytes, float] = {}


def _on_dmr_frame(frame):
    from server.dmr.homebrew import FrameType

    # Track call start/end for last-heard list
    if frame.frame_type == FrameType.DATA_SYNC:
        _stream_start[frame.stream_id] = time.time()
        asyncio.ensure_future(_handle_call_start(frame))
    elif frame.frame_type == FrameType.VOICE_SYNC:
        # End of voice burst — calculate duration
        start = _stream_start.pop(frame.stream_id, None)
        if start:
            asyncio.ensure_future(_handle_call_end(frame, time.time() - start))

    # Forward audio to WebRTC bridge
    if _bridge:
        _bridge.push_dmr_frame(frame)

    asyncio.ensure_future(_broadcast({
        'type':      'frame',
        'src_id':    frame.src_id,
        'dst_id':    frame.dst_id,
        'stream_id': frame.stream_id.hex(),
        'frame_type': frame.frame_type.name,
    }))


async def _handle_call_start(frame):
    info = await contact_db.lookup(frame.src_id)
    entry = {
        'ts':       int(time.time()),
        'src_id':   frame.src_id,
        'dst_id':   frame.dst_id,
        'callsign': info['callsign'] if info else str(frame.src_id),
        'name':     info['name'].strip() if info else '',
        'duration': None,
    }
    await _broadcast({'type': 'call_start', **entry})


async def _handle_call_end(frame, duration: float):
    info = await contact_db.lookup(frame.src_id)
    entry = {
        'ts':       int(time.time()),
        'src_id':   frame.src_id,
        'dst_id':   frame.dst_id,
        'callsign': info['callsign'] if info else str(frame.src_id),
        'name':     info['name'].strip() if info else '',
        'duration': round(duration, 1),
    }
    # Prepend to last-heard list (most recent first)
    _last_heard.insert(0, entry)
    del _last_heard[MAX_LAST_HEARD:]
    await _broadcast({'type': 'call_end', **entry})
