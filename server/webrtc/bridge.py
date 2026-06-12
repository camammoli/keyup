"""
WebRTC ↔ DMR audio bridge.

Browser sends Opus audio via WebRTC DataChannel (PCM after decode).
We encode to AMBE+2 using mbelib and inject as DMRD frames.
Incoming DMRD frames are decoded from AMBE+2 to PCM and forwarded
to the browser via the same DataChannel.

mbelib must be compiled and installed:
  https://github.com/szechyjs/mbelib
  pip install mbelib   (Python bindings)

If mbelib is not available, a stub is used and audio is silent.
"""

import asyncio
import logging
import os
import struct
import time
import uuid
from typing import Optional, Callable

from server.dmr.homebrew import DMRFrame, FrameType, Slot

log = logging.getLogger('keyup.bridge')

try:
    import mbelib
    _HAVE_MBELIB = True
    log.info('mbelib loaded — AMBE codec available')
except ImportError:
    _HAVE_MBELIB = False
    log.warning('mbelib not found — audio will be silent (install mbelib for real audio)')

SAMPLE_RATE   = 8000    # AMBE operates at 8 kHz
FRAME_SAMPLES = 160     # 20 ms @ 8 kHz
AMBE_FRAME_SZ = 9       # bytes per AMBE+2 frame inside a DMR superframe
DMR_FRAME_MS  = 20      # one DMR voice frame = 20 ms


class AudioBridge:
    """
    One instance per active WebRTC session.
    Wires together the aiortc peer connection and the HomeBrew protocol.
    """

    def __init__(self, cfg, homebrew):
        self.cfg       = cfg
        self.homebrew  = homebrew
        self._pc       = None          # aiortc RTCPeerConnection
        self._tx_task: Optional[asyncio.Task] = None
        self._rx_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._stream_id: bytes        = os.urandom(4)
        self._seq: int                = 0
        self._ptt_active: bool        = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_offer(self) -> dict:
        """Create a WebRTC offer and return SDP dict {type, sdp}."""
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.contrib.media import MediaBlackhole, MediaRecorder

        self._pc = RTCPeerConnection()

        @self._pc.on('datachannel')
        def on_datachannel(channel):
            log.info('DataChannel opened: %s', channel.label)

            @channel.on('message')
            def on_message(msg):
                if isinstance(msg, bytes):
                    asyncio.ensure_future(self._handle_pcm_from_browser(msg, channel))

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        return {'type': self._pc.localDescription.type,
                'sdp':  self._pc.localDescription.sdp}

    async def set_answer(self, sdp: str, sdp_type: str):
        from aiortc import RTCSessionDescription
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))

    def push_dmr_frame(self, frame: DMRFrame):
        """Called by HomebrewProtocol.on_frame — queues incoming audio."""
        try:
            self._rx_queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # drop if browser is too slow

    def set_ptt(self, active: bool):
        self._ptt_active = active
        if active:
            self._stream_id = os.urandom(4)
            self._seq = 0
            log.debug('PTT ON  stream=%s', self._stream_id.hex())
        else:
            log.debug('PTT OFF stream=%s', self._stream_id.hex())

    async def close(self):
        if self._tx_task:
            self._tx_task.cancel()
        if self._pc:
            await self._pc.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _handle_pcm_from_browser(self, pcm: bytes, channel):
        """Encode PCM → AMBE+2 → DMRD and transmit."""
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

    async def _rx_loop(self, channel):
        """Decode incoming DMRD frames and push PCM to browser DataChannel."""
        while True:
            frame: DMRFrame = await self._rx_queue.get()
            pcm = _ambe_to_pcm(frame.data)
            if pcm and channel.readyState == 'open':
                channel.send(pcm)


# ── Codec helpers ─────────────────────────────────────────────────────────────

def _ambe_to_pcm(dmr_data: bytes) -> Optional[bytes]:
    """Decode one 33-byte DMR payload to 160-sample PCM (16-bit LE)."""
    if not _HAVE_MBELIB:
        return bytes(FRAME_SAMPLES * 2)  # silence
    try:
        # DMR payload contains 3 AMBE+2 frames interleaved with sync bits.
        # mbelib.decode_dmr() handles the de-interleaving internally.
        samples = mbelib.decode_dmr(dmr_data)
        return struct.pack(f'<{len(samples)}h', *samples)
    except Exception as e:
        log.debug('AMBE decode error: %s', e)
        return None


def _pcm_to_ambe(pcm: bytes) -> Optional[bytes]:
    """Encode 160-sample PCM (16-bit LE) to 33-byte DMR payload."""
    if not _HAVE_MBELIB:
        return bytes(33)
    try:
        samples = list(struct.unpack(f'<{len(pcm)//2}h', pcm))
        return mbelib.encode_dmr(samples)
    except Exception as e:
        log.debug('AMBE encode error: %s', e)
        return None
