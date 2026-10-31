"""
hl7v2.py - minimal HL7 v2.x (ER7, "pipe and hat") parser.

Covers what an ORU^R01 adapter needs: segments, fields, repetitions,
components, sub-components, the escape sequences of HL7 v2 section
2.7, and MLLP framing. It does not validate messages against a v2
profile; the adapter checks the segments it relies on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

MLLP_START, MLLP_END = b"\x0b", b"\x1c\x0d"


@dataclass
class Encoding:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"


@dataclass
class Segment:
    name: str
    fields: list[str]              # fields[1] is <name>-1 (for MSH, MSH-1 is the field separator)
    enc: Encoding = field(default_factory=Encoding)

    def raw(self, n: int) -> str:
        return self.fields[n] if n < len(self.fields) else ""

    def reps(self, n: int) -> list[str]:
        v = self.raw(n)
        return [r for r in v.split(self.enc.repetition)] if v else []

    def comp(self, n: int, c: int = 1, rep: int = 0) -> str:
        """Component c (1-based) of repetition rep of field n, unescaped."""
        reps = self.reps(n)
        if rep >= len(reps):
            return ""
        comps = reps[rep].split(self.enc.component)
        return unescape(comps[c - 1], self.enc) if c - 1 < len(comps) else ""

    def components(self, n: int, rep: int = 0) -> list[str]:
        reps = self.reps(n)
        if rep >= len(reps):
            return []
        return [unescape(x, self.enc) for x in reps[rep].split(self.enc.component)]


@dataclass
class Message:
    segments: list[Segment]
    enc: Encoding

    def first(self, name: str) -> Optional[Segment]:
        return next((s for s in self.segments if s.name == name), None)

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]


_ESC_MAP = {"F": "field", "S": "component", "T": "subcomponent", "R": "repetition", "E": "escape"}


def unescape(value: str, enc: Encoding) -> str:
    if enc.escape not in value:
        return value
    e = re.escape(enc.escape)

    def repl(m):
        code = m.group(1)
        if code in _ESC_MAP:
            return getattr(enc, _ESC_MAP[code])
        if code == ".br":
            return "\n"
        if code.startswith("X") and len(code) > 1:
            try:
                return bytes.fromhex(code[1:]).decode("latin-1")
            except ValueError:
                return ""
        return ""  # unsupported escapes (highlighting etc.) are dropped
    return re.sub(f"{e}([^{e}]*){e}", repl, value)


class HL7ParseError(ValueError):
    pass


def parse(text: str) -> Message:
    text = text.strip("\x0b\x1c\r\n ")
    if not text.startswith("MSH"):
        raise HL7ParseError("message does not start with an MSH segment")
    fs = text[3]
    enc_chars = text[4:8] if text[7:8] != fs else text[4:7]
    enc = Encoding(field=fs, component=enc_chars[0], repetition=enc_chars[1],
                   escape=enc_chars[2], subcomponent=enc_chars[3] if len(enc_chars) > 3 else "&")
    segments = []
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.strip():
            continue
        if line.startswith("MSH"):
            parts = line.split(fs)
            # MSH-1 is the field separator itself; MSH-2 the encoding characters
            fields = ["MSH", fs] + parts[1:]
        else:
            fields = line.split(fs)
        name = fields[0]
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2}", name):
            raise HL7ParseError(f"invalid segment name {name!r}")
        segments.append(Segment(name, fields, enc))
    return Message(segments, enc)


def ts(value: str):
    """HL7 v2 DTM (YYYY[MM[DD[HH[MM[SS[.S]]]]]][+/-ZZZZ]) -> date/datetime/None."""
    from datetime import date, datetime, timedelta, timezone

    if not value:
        return None
    m = re.fullmatch(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?", value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    if h is None:
        return date(int(y), int(mo or 1), int(d or 1))
    tzinfo = None
    if tz:
        sign = 1 if tz[0] == "+" else -1
        tzinfo = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
    return datetime(int(y), int(mo), int(d), int(h), int(mi or 0), int(s or 0), tzinfo=tzinfo)


def frame(message: str) -> bytes:
    return MLLP_START + message.replace("\n", "\r").encode("utf-8") + MLLP_END
