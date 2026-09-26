#!/usr/bin/env python3
"""Write a small PNG, for exercising the evidence path without a real image.

Kept as generated output rather than a committed binary: a checked-in fixture
would be a blob in the repository that nobody can review in a diff.

Uses only the standard library, so it runs on a bare CI runner with no
imaging package installed.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib


def png_bytes(width: int, height: int) -> bytes:
    """Encode a gradient as a truecolour PNG."""
    rows = []

    for y in range(height):
        # Each scanline is prefixed with its filter type; 0 means unfiltered.
        row = bytearray([0])

        for x in range(width):
            row += bytes(
                (
                    (x * 255) // max(1, width - 1),
                    (y * 255) // max(1, height - 1),
                    128,
                )
            )

        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", crc)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Path to write the PNG to.")
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=320)
    args = parser.parse_args()

    if args.width < 1 or args.height < 1:
        print("width and height must be positive", file=sys.stderr)
        return 2

    data = png_bytes(args.width, args.height)

    with open(args.output, "wb") as handle:
        handle.write(data)

    print(f"wrote {args.output} ({len(data)} bytes, {args.width}x{args.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
