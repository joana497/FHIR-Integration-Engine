#!/usr/bin/env python3
"""
send_oru_mllp.py - send HL7 v2 messages to the engine over MLLP and report the ACKs.

  python scripts/send_oru_mllp.py samples/oru                    # every *.hl7 in a folder
  python scripts/send_oru_mllp.py results/<run>/generated/oru --repeat 3
  python scripts/send_oru_mllp.py msg.hl7 --host localhost --port 2575

Each file holds one message; segment separators may be CR, LF or CRLF
(they are normalised to CR, as HL7 v2 requires). Writes mllp_acks.csv
with the ACK code (AA/AR/AE) and the round-trip time of every message.
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, results_dir, write_csv  # noqa: E402

SB, EB = b"\x0b", b"\x1c\x0d"


def send(sock: socket.socket, er7: str, timeout: float) -> str:
    sock.sendall(SB + er7.encode("utf-8") + EB)
    sock.settimeout(timeout)
    buf = b""
    while EB not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("connection closed before the ACK")
        buf += chunk
    return buf[buf.index(SB) + 1: buf.index(EB)].decode("utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .hl7 file or a folder of them")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2575)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()

    p = Path(a.path)
    files = sorted(p.glob("*.hl7")) if p.is_dir() else [p]
    if not files:
        sys.exit("no .hl7 files found")
    rows, codes = [], {}
    with socket.create_connection((a.host, a.port), timeout=a.timeout) as sock:
        for rep in range(a.repeat):
            for f in files:
                er7 = re.sub(r"\r\n|\n|\r", "\r", f.read_text(encoding="utf-8").strip()) + "\r"
                t0 = time.perf_counter()
                ack = send(sock, er7, a.timeout)
                ms = (time.perf_counter() - t0) * 1000
                msa = next((s for s in ack.split("\r") if s.startswith("MSA")), "MSA||")
                code = msa.split("|")[1]
                codes[code] = codes.get(code, 0) + 1
                rows.append({"file": f.name, "repeat": rep + 1, "ack_code": code, "rtt_ms": round(ms, 1),
                             "msa": msa[:200]})
                print(f"{f.name:28s} {code}  {ms:8.1f} ms  {msa[:90]}")
    out = results_dir("mllp")
    write_csv(out / "mllp_acks.csv", rows)
    log(f"{len(rows)} messages: {codes}  ->  {out / 'mllp_acks.csv'}")


if __name__ == "__main__":
    main()
