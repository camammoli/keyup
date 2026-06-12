"""
ctypes wrapper for the system libmbe.so.1 (mbelib).

Provides decode_dmr / encode_dmr so that bridge.py can do:
    import mbelib
    samples = mbelib.decode_dmr(dmr_payload_33_bytes)

Each 33-byte DMRD voice payload contains two AMBE+2 codewords at
bit offsets 0-71 and 168-239 (ETSI TS 102 361-1 Voice+Sync burst).
Each codeword decodes to 160 PCM samples @ 8 kHz (20 ms of audio).
"""

import ctypes
import ctypes.util

_lib = ctypes.CDLL('/usr/local/lib/libmbe.so.1')


class _MbeParms(ctypes.Structure):
    _fields_ = [
        ('w0',     ctypes.c_float),
        ('L',      ctypes.c_int),
        ('K',      ctypes.c_int),
        ('Vl',     ctypes.c_int   * 57),
        ('Ml',     ctypes.c_float * 57),
        ('log2Ml', ctypes.c_float * 57),
        ('PHIl',   ctypes.c_float * 57),
        ('PSIl',   ctypes.c_float * 57),
        ('gamma',  ctypes.c_float),
        ('un',     ctypes.c_int),
        ('repeat', ctypes.c_int),
    ]


_P  = ctypes.POINTER
_PP = ctypes.POINTER(_MbeParms)

_lib.mbe_initMbeParms.argtypes  = [_PP, _PP, _PP]
_lib.mbe_initMbeParms.restype   = None
_lib.mbe_moveMbeParms.argtypes  = [_PP, _PP]
_lib.mbe_moveMbeParms.restype   = None

# void mbe_processAmbe3600x2450Frame(short *aout_buf, int *errs, int *errs2,
#     char *err_str, char ambe_fr[4][24], char ambe_d[49],
#     mbe_parms *cur, mbe_parms *prev, mbe_parms *prev_enh, int uvquality)
_lib.mbe_processAmbe3600x2450Frame.argtypes = [
    _P(ctypes.c_short),   # aout_buf[160]
    _P(ctypes.c_int),     # errs
    _P(ctypes.c_int),     # errs2
    ctypes.c_char_p,      # err_str[64]
    ctypes.c_char_p,      # ambe_fr flat (4*24 = 96 bytes, each byte = 1 bit)
    ctypes.c_char_p,      # ambe_d flat  (49 bytes, each byte = 1 bit)
    _PP, _PP, _PP,        # cur_mp, prev_mp, prev_mp_enhanced
    ctypes.c_int,         # uvquality (3 = good)
]
_lib.mbe_processAmbe3600x2450Frame.restype = None

# Module-level state — safe under asyncio (single-threaded)
_cur_mp   = _MbeParms()
_prev_mp  = _MbeParms()
_prev_enh = _MbeParms()
_lib.mbe_initMbeParms(
    ctypes.byref(_cur_mp),
    ctypes.byref(_prev_mp),
    ctypes.byref(_prev_enh),
)

_aout    = (ctypes.c_short * 160)()
_errs    = ctypes.c_int(0)
_errs2   = ctypes.c_int(0)
_err_str = (ctypes.c_char * 64)()
_ambe_d  = (ctypes.c_char * 49)()


def _payload_to_bits(data: bytes) -> list:
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def _decode_word(bits_72: list) -> list:
    """72 AMBE+2 channel bits → 160 PCM samples (int16, 8 kHz)."""
    # ambe_fr[4][24] flat: rows 0-2 from channel bits, row 3 = zeros
    ambe_fr = bytearray(96)
    for i in range(72):
        ambe_fr[i] = bits_72[i]
    buf = (ctypes.c_char * 96).from_buffer_copy(bytes(ambe_fr))

    _lib.mbe_processAmbe3600x2450Frame(
        _aout,
        ctypes.byref(_errs), ctypes.byref(_errs2),
        _err_str, buf, _ambe_d,
        ctypes.byref(_cur_mp), ctypes.byref(_prev_mp), ctypes.byref(_prev_enh),
        3,
    )
    _lib.mbe_moveMbeParms(ctypes.byref(_cur_mp), ctypes.byref(_prev_mp))
    return list(_aout)


def decode_dmr(data: bytes) -> list:
    """
    Decode a 33-byte DMRD voice payload to PCM samples (int16, 8 kHz).
    Returns 320 samples (2 × 160) = 40 ms of audio.
    """
    if len(data) < 33:
        return [0] * 320
    bits = _payload_to_bits(data[:33])
    s1 = _decode_word(bits[0:72])    # first AMBE+2 codeword
    s2 = _decode_word(bits[168:240]) # second AMBE+2 codeword
    return s1 + s2


def encode_dmr(samples: list) -> bytes:
    """AMBE+2 software encoding is not available — returns silence."""
    return bytes(33)
