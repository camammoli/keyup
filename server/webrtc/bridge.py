"""
WebRTC ↔ DMR audio bridge.

The server is the WebRTC offerer.  It creates a bidirectional DataChannel
('audio') before calling createOffer() so SCTP is included in the SDP.
The browser receives the channel via ondatachannel and uses it for:
  TX: browser → server: raw PCM (Int16, 8 kHz, 160-sample chunks)
  RX: server → browser: raw PCM (Int16, 8 kHz, decoded from AMBE+2)

mbelib must be available for real audio; otherwise silence is sent.
"""

import asyncio
import logging
import os
import struct
import time
import uuid
from typing import Optional

from server.dmr.homebrew import DMRFrame, FrameType, Slot

log = logging.getLogger('keyup.bridge')

try:
    import mbelib
    _HAVE_MBELIB = True
    log.info('mbelib loaded — AMBE codec available')
except ImportError:
    _HAVE_MBELIB = False
    log.warning('mbelib not found — audio will be silent')

SAMPLE_RATE   = 8000
FRAME_SAMPLES = 160
AMBE_FRAME_SZ = 9


class AudioBridge:

    def __init__(self, cfg, homebrew):
        self.cfg       = cfg
        self.homebrew  = homebrew
        self._pc       = None
        self._channel  = None          # RTCDataChannel (bidirectional)
        self._rx_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._stream_id: bytes        = os.urandom(4)
        self._seq: int                = 0
        self._ptt_active: bool        = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_offer(self) -> dict:
        from aiortc import RTCPeerConnection

        self._pc = RTCPeerConnection()

        # Create data channel BEFORE createOffer so SCTP appears in SDP.
        # Browser receives this channel via ondatachannel event.
        self._channel = self._pc.createDataChannel(
            'audio', ordered=False, maxRetransmits=0
        )

        @self._channel.on('open')
        def on_open():
            log.info('DataChannel open — starting RX loop')
            asyncio.ensure_future(self._rx_loop())

        @self._channel.on('message')
        def on_message(msg):
            if isinstance(msg, bytes):
                asyncio.ensure_future(self._handle_pcm_from_browser(msg))

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        return {
            'type': self._pc.localDescription.type,
            'sdp':  self._pc.localDescription.sdp,
        }

    async def set_answer(self, sdp: str, sdp_type: str):
        from aiortc import RTCSessionDescription
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type=sdp_type)
        )

    def push_dmr_frame(self, frame: DMRFrame):
        try:
            self._rx_queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    def set_ptt(self, active: bool):
        self._ptt_active = active
        if active:
            self._stream_id = os.urandom(4)
            self._seq = 0
            log.debug('PTT ON  stream=%s', self._stream_id.hex())
        else:
            log.debug('PTT OFF stream=%s', self._stream_id.hex())

    async def close(self):
        if self._pc:
            await self._pc.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _handle_pcm_from_browser(self, pcm: bytes):
        """PCM from browser → AMBE+2 encode → transmit as DMRD."""
        if not self._ptt_active:
            return
        ambe = _pcm_to_ambe(pcm)
        if ambe is None:
            return
        frame = DMRFrame(
            sequence   = self._seq & 0xFF,
            src_id     = self.cfg.repeater_id,
            dst_id     = self.cfg.talkgroup,
            rpt_id     = self.cfg.repeater_id,
            slot       = Slot.TS2,
            call_type  = 0,
            frame_type = FrameType.VOICE,
            stream_id  = self._stream_id,
            data       = ambe,
        )
        self._seq += 1
        self.homebrew.transmit(frame)

    async def _rx_loop(self):
        """Incoming DMRD frames → PCM → browser DataChannel."""
        while True:
            frame: DMRFrame = await self._rx_queue.get()
            pcm = _ambe_to_pcm(frame.data)
            if pcm and self._channel and self._channel.readyState == 'open':
                self._channel.send(pcm)


# ── Codec helpers ─────────────────────────────────────────────────────────────

def _ambe_to_pcm(dmr_data: bytes) -> Optional[bytes]:
    if not _HAVE_MBELIB:
        return bytes(FRAME_SAMPLES * 2)
    try:
        samples = mbelib.decode_dmr(dmr_data)
        return struct.pack(f'<{len(samples)}h', *samples)
    except Exception as e:
        log.debug('AMBE decode error: %s', e)
        return None


def _pcm_to_ambe(pcm: bytes) -> Optional[bytes]:
    if not _HAVE_MBELIB:
        return bytes(33)
    try:
        samples = list(struct.unpack(f'<{len(pcm)//2}h', pcm))
        return mbelib.encode_dmr(samples)
    except Exception as e:
        log.debug('AMBE encode error: %s', e)
        return None
