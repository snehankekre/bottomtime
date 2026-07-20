"""Pure-Python decoder for Shearwater's Petrel Native Format (PNF).

Shearwater Cloud stores each dive as a `sw-pnf` blob: a 4-byte little-endian
uncompressed-length prefix followed by a gzip stream. The decompressed payload
is a sequence of 32-byte records; byte 0 of each record is its type:

  0x01       dive sample            0x10-0x19  opening (config) records
  0x02       freedive sample (4x8)  0x20-0x29  closing records
  0x03       Avelo sample           0x30       info event
  0xE1       extended dive sample   0xFF       final record

Byte offsets follow libdivecomputer's shearwater_predator_parser.c: sample
fields sit at legacy-offset + 1 (the PNF type byte), opening/closing record
fields at the absolute offsets used for their legacy 128-byte block.

Record types this decoder does not understand are returned raw in
`PnfDive.unknown_records`; nothing is dropped.

DECODER_VERSION history:
  1  initial port of the libdivecomputer field set
  2  adds ceiling_m (byte 24), gf99 (byte 25), battery_v (byte 18 / 100) and
     @+5 TTS (bytes 26-27), identified empirically: byte 25 converges on
     dive_details.EndGF99 and decays during surface offgassing; byte 24 sits
     just below the 3 m-quantized stop depth and zeroes when deco clears;
     byte 18 equals the XML export's batteryVoltage x 100
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field

DECODER_VERSION = 2

RECORD_SIZE = 32

DIVE_SAMPLE = 0x01
FREEDIVE_SAMPLE = 0x02
AVELO_SAMPLE = 0x03
OPENING_0, OPENING_9 = 0x10, 0x19
CLOSING_0, CLOSING_9 = 0x20, 0x29
INFO_EVENT = 0x30
DIVE_SAMPLE_EXT = 0xE1
FINAL = 0xFF

# status flag bits (sample byte 12)
FLAG_GASSWITCH = 0x01
FLAG_PPO2_EXTERNAL = 0x02
FLAG_SETPOINT_HIGH = 0x04
FLAG_SC = 0x08
FLAG_OC = 0x10

DIVE_MODES = {
    0: "cc",
    1: "oc_tec",
    2: "gauge",
    3: "ppo2",
    4: "sc",
    5: "cc2",
    6: "oc_rec",
    7: "freedive",
    12: "avelo",
}

DECO_MODELS = {0: "gf", 1: "vpmb", 2: "vpmb_gfs", 3: "dciem"}

# Final-record byte 13 model ids, names per libdivecomputer's descriptors.
# The PNF layout is shared across the family; bottomtime's field mappings are
# validated so far on Petrel 3 (10) and Perdix 2 (11) logs.
COMPUTER_MODELS = {
    2: "Predator",
    3: "Petrel",
    4: "Nerd",
    5: "Perdix",
    6: "Perdix AI",
    7: "Nerd 2",
    8: "Teric",
    9: "Peregrine",
    10: "Petrel 3",
    11: "Perdix 2",
    12: "Tern",
}

# Sample bytes not (yet) assigned a meaning; kept per sample as b<offset>.
# 18 (battery), 24 (ceiling), 25 (gf99), 26-27 (@+5) were identified
# empirically and promoted in v2. 30-31 read 0xFFFF (sentinel) so far.
UNMAPPED_SAMPLE_BYTES = (11, 17, 30, 31)


def _u16be(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def _u24be(data: bytes, off: int) -> int:
    return (data[off] << 16) | (data[off + 1] << 8) | data[off + 2]


def _u32be(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def _bcd2dec(value: int) -> int:
    return ((value >> 4) & 0x0F) * 10 + (value & 0x0F)


def _bcd_bytes(data: bytes, off: int, n: int) -> int:
    result = 0
    for i in range(n):
        result = result * 100 + _bcd2dec(data[off + i])
    return result


@dataclass
class PnfSample:
    t_s: float
    depth_m: float | None = None
    temp_c: float | None = None
    stop_depth_m: float | None = None
    stop_or_ndl_min: float | None = None
    tts_min: float | None = None
    in_deco: int | None = None
    avg_ppo2: float | None = None
    o2_pct: float | None = None
    he_pct: float | None = None
    setpoint: float | None = None
    cns_pct: float | None = None
    gf99: float | None = None
    ceiling_m: float | None = None
    sensor1_raw: int | None = None
    sensor2_raw: int | None = None
    sensor3_raw: int | None = None
    sensor1_ppo2: float | None = None
    sensor2_ppo2: float | None = None
    sensor3_ppo2: float | None = None
    battery_v: float | None = None
    status_flags: int | None = None
    tank0_psi: float | None = None
    tank1_psi: float | None = None
    gas_time_min: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class PnfGas:
    index: int
    o2_pct: int
    he_pct: int
    diluent: bool
    enabled: bool


@dataclass
class PnfEvent:
    t_s: float
    event: int
    timestamp: int
    w1: int
    w2: int
    raw_hex: str


@dataclass
class PnfDive:
    header: dict
    samples: list[PnfSample]
    gases: list[PnfGas]
    events: list[PnfEvent]
    unknown_records: list[tuple[str, int, bytes]]  # (type_key, seq, raw)
    sample_interval_ms: int


def decompress(blob: bytes) -> bytes:
    """Strip the 4-byte LE length prefix and gunzip; verify the length."""
    expected = struct.unpack_from("<I", blob, 0)[0]
    data = gzip.decompress(blob[4:])
    if len(data) != expected:
        raise ValueError(f"PNF length mismatch: prefix {expected}, got {len(data)}")
    if len(data) % RECORD_SIZE:
        raise ValueError(f"PNF payload not a multiple of {RECORD_SIZE} bytes")
    return data


def decode(blob: bytes) -> PnfDive:
    """Decode a raw sw-pnf blob (as stored in Shearwater Cloud's log_data)."""
    data = decompress(blob)

    opening: dict[int, int] = {}
    closing: dict[int, int] = {}
    final_off: int | None = None
    raw_records: list[tuple[int, int]] = []  # (type, offset), stream order

    for off in range(0, len(data), RECORD_SIZE):
        rec = data[off : off + RECORD_SIZE]
        if rec == b"\x00" * RECORD_SIZE:
            continue
        rtype = rec[0]
        raw_records.append((rtype, off))
        if OPENING_0 <= rtype <= OPENING_9:
            opening[rtype - OPENING_0] = off
        elif CLOSING_0 <= rtype <= CLOSING_9:
            closing[rtype - CLOSING_0] = off
        elif rtype == FINAL:
            final_off = off

    for i in range(5):
        if i not in opening or i not in closing:
            raise ValueError(f"missing opening/closing record {i}")

    header = _parse_header(data, opening, closing, final_off)
    interval_ms = header["sample_interval_ms"]
    calibrated = header["sensor_calibrated_mask"]
    calibration = header["sensor_calibration"]
    imperial = header["imperial"]

    gases = [
        PnfGas(
            index=i,
            o2_pct=header["gas_o2"][i],
            he_pct=header["gas_he"][i],
            diluent=i >= 5,
            enabled=bool(header["gas_enabled_mask"] & (1 << i)),
        )
        for i in range(10)
    ]

    samples: list[PnfSample] = []
    events: list[PnfEvent] = []
    unknown: list[tuple[str, int, bytes]] = []
    seq = 0
    t = 0.0

    for rtype, off in raw_records:
        rec = data[off : off + RECORD_SIZE]
        if rtype in (DIVE_SAMPLE, AVELO_SAMPLE):
            samples.append(
                _parse_sample(rec, t, imperial, calibrated, calibration, rtype)
            )
            t += interval_ms / 1000.0
        elif rtype == FREEDIVE_SAMPLE:
            for i in range(4):
                sub = rec[i * 8 : (i + 1) * 8]
                if sub == b"\x00" * 8:
                    break
                pressure_mbar = _u16be(sub, 1)
                depth = (
                    (pressure_mbar - header["atmospheric_mbar"])
                    * 100.0
                    / (header["water_density"] * 9.80665)
                )
                temp = struct.unpack_from(">h", sub, 3)[0] / 10.0
                samples.append(PnfSample(t_s=t, depth_m=round(depth, 2), temp_c=temp))
                t += interval_ms / 1000.0
        elif rtype == DIVE_SAMPLE_EXT:
            s = samples[-1] if samples else None
            for i, key in enumerate(("tank2_psi", "tank3_psi")):
                v = _u16be(rec, 1 + i * 2)
                if v < 0xFFF0 and (v & 0x0FFF) and s is not None:
                    s.extra[key] = (v & 0x0FFF) * 2
            for i, key in enumerate(("tank_dil_psi", "tank_o2_psi")):
                v = _u16be(rec, 1 + 4 + i * 2)
                if v and s is not None:
                    s.extra[key] = v * 2
        elif rtype == INFO_EVENT:
            events.append(
                PnfEvent(
                    t_s=t,
                    event=rec[1],
                    timestamp=_u32be(rec, 4),
                    w1=_u32be(rec, 8),
                    w2=_u32be(rec, 12),
                    raw_hex=rec.hex(),
                )
            )
        elif OPENING_0 <= rtype <= OPENING_9 or CLOSING_0 <= rtype <= CLOSING_9 or rtype == FINAL:
            pass  # consumed by _parse_header; raw copies kept in header dict
        else:
            unknown.append((f"0x{rtype:02x}", seq, rec))
        seq += 1

    return PnfDive(
        header=header,
        samples=samples,
        gases=gases,
        events=events,
        unknown_records=unknown,
        sample_interval_ms=interval_ms,
    )


def _parse_sample(
    rec: bytes,
    t: float,
    imperial: bool,
    calibrated: int,
    calibration: list[float],
    rtype: int,
) -> PnfSample:
    ft = 0.3048
    depth = _u16be(rec, 1) / 10.0
    stop_depth = float(_u16be(rec, 3))
    if imperial:
        depth *= ft
        stop_depth *= ft

    temp = struct.unpack_from("b", rec, 14)[0]
    if temp < 0:
        temp = min(temp + 102, 0)
    if imperial:
        temp = (temp - 32.0) * 5.0 / 9.0

    status = rec[12] if rtype != AVELO_SAMPLE else 0
    ccr = (status & FLAG_OC) == 0 if rtype != AVELO_SAMPLE else False

    sample = PnfSample(
        t_s=t,
        depth_m=round(depth, 2),
        temp_c=float(temp),
        stop_depth_m=stop_depth,
        tts_min=float(_u16be(rec, 5)),
        stop_or_ndl_min=float(rec[10]),
        in_deco=1 if _u16be(rec, 3) else 0,
        avg_ppo2=rec[7] / 100.0,
        o2_pct=float(rec[8]),
        he_pct=float(rec[9]),
        status_flags=status,
        sensor1_raw=rec[13],
        sensor2_raw=rec[15],
        sensor3_raw=rec[16],
        cns_pct=float(rec[23]),
        ceiling_m=float(rec[24]) if rec[24] != 0xFF else None,
        gf99=float(rec[25]) if rec[25] != 0xFF else None,  # 0xFF = not available
        battery_v=rec[18] / 100.0,
    )

    if ccr:
        sample.setpoint = rec[19] / 100.0
        if not (status & FLAG_PPO2_EXTERNAL):
            if calibrated & 0x01:
                sample.sensor1_ppo2 = round(rec[13] * calibration[0], 3)
            if calibrated & 0x02:
                sample.sensor2_ppo2 = round(rec[15] * calibration[1], 3)
            if calibrated & 0x04:
                sample.sensor3_ppo2 = round(rec[16] * calibration[2], 3)

    for idx, key in ((20, "tank1_psi"), (28, "tank0_psi")):
        v = _u16be(rec, idx)
        if v < 0xFFF0:
            pressure = v & 0x0FFF
            battery = (v >> 12) & 0x0F
            if key == "tank1_psi":
                sample.tank1_psi = pressure * 2.0
            else:
                sample.tank0_psi = pressure * 2.0
            if battery:
                sample.extra[key + "_battery"] = battery

    if rec[22] < 0xF0:
        sample.gas_time_min = float(rec[22])

    # @+5: TTS if the diver stayed 5 more minutes (minutes, u16be)
    at_plus_five = _u16be(rec, 26)
    if at_plus_five != 0xFFFF:
        sample.extra["at_plus_five_min"] = at_plus_five

    for b in UNMAPPED_SAMPLE_BYTES:
        if rec[b]:
            sample.extra[f"b{b}"] = rec[b]

    return sample


def _parse_header(
    data: bytes,
    opening: dict[int, int],
    closing: dict[int, int],
    final_off: int | None,
) -> dict:
    o = opening
    c = closing

    logversion = data[o[4] + 16]
    imperial = data[o[0] + 8] == 1

    gas_o2 = [data[o[0] + 20 + i] for i in range(10)]
    gas_he = [data[o[0] + 30 + i] for i in range(2)] + [
        data[o[1] + 1 + i] for i in range(8)
    ]

    calib_base = o[3] + 6
    calibrated_mask = data[calib_base]
    calibration = [
        _u16be(data, calib_base + 1 + i * 2) / 100000.0 for i in range(3)
    ]

    interval_ms = 10000
    if logversion >= 9 and 5 in o:
        interval_ms = _u16be(data, o[5] + 23) or 10000

    max_depth = _u16be(data, c[0] + 4) / 10.0
    if imperial:
        max_depth *= 0.3048

    header = {
        "logversion": logversion,
        "imperial": imperial,
        "divemode": DIVE_MODES.get(data[o[4] + 1], str(data[o[4] + 1])),
        "gf_low": data[o[0] + 4],
        "gf_high": data[o[0] + 5],
        "deco_model": DECO_MODELS.get(data[o[2] + 18], str(data[o[2] + 18])),
        "vpmb_conservatism": data[o[2] + 19],
        "atmospheric_mbar": _u16be(data, o[1] + 16),
        "water_density": _u16be(data, o[3] + 3),
        "sensor_calibrated_mask": calibrated_mask,
        "sensor_calibration": calibration,
        "gas_o2": gas_o2,
        "gas_he": gas_he,
        "gas_enabled_mask": _u16be(data, o[4] + 17),
        "ai_mode": data[o[4] + 28],
        "sample_interval_ms": interval_ms,
        "max_depth_m": round(max_depth, 2),
        "divetime_s": _u24be(data, c[0] + 6),
    }

    if final_off is not None:
        header["computer_serial"] = _u32be(data, final_off + 2)
        header["computer_firmware"] = _bcd2dec(data[final_off + 10])
        header["computer_model"] = data[final_off + 13]

    if logversion >= 17:
        for name, recs in (("gnss_start", o), ("gnss_end", c)):
            if 9 in recs:
                off = recs[9]
                fix = data[off + 16]
                if fix in (2, 3):  # 2D / 3D fix
                    lat = struct.unpack_from(">i", data, off + 21)[0] / 100000.0
                    lon = struct.unpack_from(">i", data, off + 25)[0] / 100000.0
                    header[name] = {"fix": fix, "lat": lat, "lon": lon}

    # Verbatim copies of every config record, keyed by record type.
    header["raw_opening"] = {
        f"0x{OPENING_0 + i:02x}": data[off : off + RECORD_SIZE].hex()
        for i, off in sorted(o.items())
    }
    header["raw_closing"] = {
        f"0x{CLOSING_0 + i:02x}": data[off : off + RECORD_SIZE].hex()
        for i, off in sorted(c.items())
    }
    if final_off is not None:
        header["raw_final"] = data[final_off : final_off + RECORD_SIZE].hex()

    return header
