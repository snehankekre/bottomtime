"""Parse Shearwater Cloud 'Shearwater XML' exports.

Used as an independent cross-check of the PNF decoder (verify), not as a
third sample series: the XML is a lossy rendering of the same native log.
Files declare encoding="utf-16" but are written as ASCII; parse accordingly.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# AI columns carry text sentinels when no transmitter is paired
_TEXT_SENTINELS = {"AI is off", "N/A", "Not paired", "No comms", ""}

_NUMERIC_SAMPLE_FIELDS = (
    "currentTime",
    "currentDepth",
    "firstStopDepth",
    "firstStopTime",
    "ttsMins",
    "averagePPO2",
    "fractionO2",
    "fractionHe",
    "currentNdl",
    "waterTemp",
    "batteryVoltage",
    "sensor1Millivolts",
    "sensor2Millivolts",
    "sensor3Millivolts",
    "sac",
    "sad",
)


def _num(text: str | None) -> float | None:
    if text is None or text.strip() in _TEXT_SENTINELS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_xml(path: Path) -> dict:
    text = path.read_text(encoding="ascii", errors="replace")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text, count=1)
    root = ET.fromstring(text)
    log = root.find("diveLog")

    header = {
        el.tag: (el.text or "").strip()
        for el in log
        if el.tag != "diveLogRecords" and len(el) == 0
    }

    samples = []
    for rec in log.find("diveLogRecords") or []:
        sample = {}
        for el in rec:
            if el.tag in _NUMERIC_SAMPLE_FIELDS:
                sample[el.tag] = _num(el.text)
            else:
                sample[el.tag] = (el.text or "").strip()
        samples.append(sample)

    return {"path": str(path), "header": header, "samples": samples}


def normalize_start(value: str) -> str:
    """Normalize a Shearwater startDate to 'YYYY-MM-DD HH:MM:SS'.

    Accepts the export's native 'dd-MM-yyyy HH:mm:ss' as well as ISO
    'yyyy-MM-ddTHH:mm:ss' (files repaired for Garmin Connect upload)."""
    value = value.strip().replace("T", " ")
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4}) (\d{2}:\d{2}:\d{2})$", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}"
    return value


def load_xml_dir(xml_dir: Path) -> dict[str, dict]:
    """Map normalized start wall-clock -> parsed XML for every export.

    Keyed by start time, not dive number: the on-unit dive counter can be
    reset, so numbers repeat across a collection."""
    dives = {}
    for path in sorted(xml_dir.glob("*.xml")):
        parsed = parse_xml(path)
        dives[normalize_start(parsed["header"]["startDate"])] = parsed
    return dives
