#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Timo Heimonen <timo.heimonen@proton.me>
"""Create <name>-fixed.stx from the Ultima V (Atari ST, 1988) boot disk image.

Fixes the inn's "Leave a companion", which pushes all shop prices to the maximum.

When a companion stays at an inn, the game removes the member from the party
and keeps the active member index in step: ``if (active > left) active--``.
The comparison is unsigned, so the normal value $FF ("no active member")
becomes $FE.  Every shop then takes member #254 as the buyer.  Its record lies
outside the party roster and its intelligence reads as 0, and merchant prices
are ``base + base * (100 - 3 * INT) / 100``, so every price becomes 2 x base.
Karma is not changed by any of this.

The fix compares the low bytes as signed values (``cmp.b`` / ``ble.s`` instead
of ``cmp.l`` / ``bls.s``), so $FF (-1) is never decremented.  It is 6 bytes in
AUTO\\U5.PRG, keeps the size of the code it replaces and contains no absolute
address, so the relocation table is not changed.  The 68000 source is
``src/inn_fix.s``.

The patched U5.PRG is written back into the same sector of the STX image
(Pasti) in place.  The image also holds a raw read-track dump of every track;
if the original sector appears there exactly once with a valid data CRC, that
copy and its CRC are updated too.  Nothing else in the image changes.  Only
the image with SHA-256
8ea59c11c3c862f3fdcbc83f9ad2af0eda175df0918523d573bdb9f0b8f39c1d is accepted;
the input file is never modified.  Only the Python standard library is required.
"""

import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# The only supported input: Ultima V - Warriors of Destiny (Disk 1 of 3).STX,
# the boot disk, Pasti STX image.
STX_SHA256 = "8ea59c11c3c862f3fdcbc83f9ad2af0eda175df0918523d573bdb9f0b8f39c1d"
# The fixed image produced from it.
FIXED_STX_SHA256 = "cae03bb83c04a77355343fb93d107a5895c264acb141affb09201f8e5f7a9a32"

PRG_PATH = "AUTO/U5.PRG"
PRG_SHA256 = "e1ea68a2b76d0194f42fad88081d4dd1e2afdc45270c0dba83e484250a7b9089"
FIXED_PRG_SHA256 = "caa78c3f0fb021f5d3658e802682d08a7d4d93ed3766e62c04395ccbae10406d"
ASM_SOURCE = Path(__file__).resolve().parent / "src" / "inn_fix.s"

# Addresses are text segment offsets of U5.PRG.
PATCHES = [
    {
        "name": "inn, Leave a companion: $FF (no active member) is not decremented",
        "addr": 0x36D9E,
        "old": "b0aefff6 6308",     # cmp.l (-10,a6),d0 / bls.s
        "new": "b02efff9 6f08",     # cmp.b (-7,a6),d0  / ble.s
    },
]


class PatchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Pasti STX image

def crc16(data, crc=0xFFFF):
    """CRC-CCITT as computed by the WD1772."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class STXImage:
    """Sector data of a Pasti (STX) image, patched in place."""

    def __init__(self, raw):
        self.img = bytearray(raw)
        self.sectors = {}             # (track, side, sector) -> file offset of the data
        self.plain = set()            # sectors that can be rewritten safely
        self.track_image = {}         # (track, side) -> (file offset, size) of the read-track dump
        self._parse()

    def _parse(self):
        img = self.img
        if img[:4] != b"RSY\0" or struct.unpack_from("<H", img, 4)[0] != 3:
            raise PatchError("not a Pasti STX image (version 3)")
        pos = 16
        for _ in range(img[10]):
            rsize, fuzzy, nsec, flags, _tlen, tnum, _ttype = struct.unpack_from("<IIHHHBB", img, pos)
            track, side = tnum & 0x7F, tnum >> 7
            if not flags & 1:
                raise PatchError(f"track {track}/{side}: no sector descriptors")
            q = pos + 16
            base = q + 16 * nsec + fuzzy
            if flags & 0x40:
                skip = 2 if flags & 0x80 else 0     # optional sync offset word
                size, = struct.unpack_from("<H", img, base + skip)
                self.track_image[(track, side)] = (base + skip + 2, size)
            seen = {}
            for i in range(nsec):
                off, _bit, _time, t, h, sec, size, _crc, fdc, _ = struct.unpack_from(
                    "<IHHBBBBHBB", img, q + 16 * i)
                key = (track, side, sec)
                seen[sec] = seen.get(sec, 0) + 1
                self.sectors[key] = base + off
                # a normal 512-byte sector: ID matches the track, no FDC flags
                if size == 2 and t == track and h == side and fdc == 0:
                    self.plain.add(key)
            for sec, n in seen.items():
                if n > 1:
                    self.plain.discard((track, side, sec))
            pos += rsize
        if pos != len(img):
            raise PatchError("STX image has trailing data")

    def read(self, key):
        if key not in self.sectors:
            raise PatchError(f"sector {key} not found")
        off = self.sectors[key]
        return bytes(self.img[off:off + 512])

    def write(self, key, data):
        """Rewrite a sector; returns True if the read-track dump was updated too."""
        if key not in self.plain or len(data) != 512:
            raise PatchError(f"sector {key} is not a plain sector")
        old = self.read(key)
        off = self.sectors[key]
        self.img[off:off + 512] = data
        if key[:2] not in self.track_image:
            return False
        start, size = self.track_image[key[:2]]
        dump = bytes(self.img[start:start + size])
        k = dump.find(old)
        if k < 0 or dump.find(old, k + 1) >= 0 or k + 514 > size:
            return False
        if struct.unpack_from(">H", dump, k + 512)[0] != crc16(b"\xa1\xa1\xa1\xfb" + old):
            return False
        self.img[start + k:start + k + 512] = data
        struct.pack_into(">H", self.img, start + k + 512, crc16(b"\xa1\xa1\xa1\xfb" + data))
        return True


# ---------------------------------------------------------------------------
# FAT12 volume (boot sector on track 0, side 0, sector 1)

class Volume:
    def __init__(self, stx):
        self.stx = stx
        boot = stx.read((0, 0, 1))
        (self.bps, self.spc, self.res, self.nfats, self.ndirs, _nsects, _md, self.spf,
         self.spt, self.heads) = struct.unpack_from("<HBHBHHBHHH", boot, 11)
        if self.bps != 512 or self.heads not in (1, 2):
            raise PatchError("unexpected boot sector parameters")
        self.fat = b"".join(self.read_sector(self.res + i) for i in range(self.spf))
        self.root = self.res + self.nfats * self.spf
        self.first_data = self.root + self.ndirs * 32 // self.bps

    def sector_key(self, lsn):
        track, s = divmod(lsn, self.spt * self.heads)
        return track, s // self.spt, s % self.spt + 1

    def read_sector(self, lsn):
        return self.stx.read(self.sector_key(lsn))

    def chain(self, cluster):
        lsns = []
        while 2 <= cluster < 0xFF0:
            first = self.first_data + (cluster - 2) * self.spc
            lsns += range(first, first + self.spc)
            o = cluster * 3 // 2
            v = self.fat[o] | self.fat[o + 1] << 8
            cluster = v >> 4 if cluster & 1 else v & 0xFFF
            if len(lsns) > 2 * 1440:
                raise PatchError("FAT chain loop")
        return lsns

    def find(self, path):
        """Return (list of logical sectors, size) of a file, path like 'AUTO/U5.PRG'."""
        lsns = list(range(self.root, self.first_data))
        parts = path.upper().split("/")
        for depth, part in enumerate(parts):
            raw = b"".join(self.read_sector(s) for s in lsns)
            for i in range(0, len(raw), 32):
                e = raw[i:i + 32]
                if e[0] == 0:
                    raise PatchError(f"{path!r} not found on disk")
                if e[0] == 0xE5 or e[11] & 0x08:
                    continue
                name = e[:8].decode("latin-1").rstrip()
                ext = e[8:11].decode("latin-1").rstrip()
                if (f"{name}.{ext}" if ext else name) != part:
                    continue
                cluster, size = struct.unpack_from("<HI", e, 26)
                is_dir = bool(e[11] & 0x10)
                if depth < len(parts) - 1:
                    if not is_dir:
                        raise PatchError(f"{part!r} is not a folder")
                    lsns = self.chain(cluster)
                    break
                if is_dir:
                    raise PatchError(f"{part!r} is a folder")
                lsns = self.chain(cluster)
                if len(lsns) * self.bps < size:
                    raise PatchError(f"{path}: cluster chain shorter than the file")
                return lsns[:(size + self.bps - 1) // self.bps], size
            else:
                raise PatchError(f"{path!r} not found on disk")
        raise PatchError(f"{path!r} not found on disk")

    def read_file(self, lsns, size):
        return b"".join(self.read_sector(s) for s in lsns)[:size]

    def write_file(self, lsns, data):
        """Overwrite a file of the same size in place; returns [(sector key, dump updated)]."""
        touched = []
        for i, lsn in enumerate(lsns):
            key = self.sector_key(lsn)
            old = self.stx.read(key)
            chunk = data[i * self.bps:(i + 1) * self.bps]
            new = chunk + old[len(chunk):]          # keep the slack after the end of file
            if new != old:
                touched.append((key, self.stx.write(key, new)))
        return touched


# ---------------------------------------------------------------------------
# TOS executable

def parse_prg(prg):
    """Return (text size, relocation offsets)."""
    magic, tsize, dsize, _bsize, ssize = struct.unpack_from(">HIIII", prg)
    if magic != 0x601A:
        raise PatchError("not a TOS executable")
    p = 0x1C + tsize + dsize + ssize
    off, = struct.unpack_from(">I", prg, p)
    p += 4
    relocs = []
    while off:
        relocs.append(off)
        step = prg[p]
        p += 1
        while step == 1:
            off += 254
            step = prg[p]
            p += 1
        if step == 0:
            break
        off += step
    if p != len(prg):
        raise PatchError("relocation table does not end at the end of the program")
    return tsize, relocs


def apply_patches(prg):
    """Return the patched program (same size, same relocation table)."""
    tsize, relocs = parse_prg(prg)
    out = bytearray(prg)
    for p in PATCHES:
        old = bytes.fromhex(p["old"].replace(" ", ""))
        new = bytes.fromhex(p["new"].replace(" ", ""))
        a = p["addr"]
        if len(old) != len(new) or not 0 <= a <= tsize - len(new):
            raise PatchError(f"{p['name']}: bad patch entry")
        if prg[0x1C + a:0x1C + a + len(old)] != old:
            raise PatchError(f"{p['name']}: original bytes do not match")
        for r in relocs:
            if r < a + len(new) and a < r + 4:
                raise PatchError(f"{p['name']}: relocation at {r:#x} inside the patch")
        out[0x1C + a:0x1C + a + len(new)] = new
    if parse_prg(bytes(out))[1] != relocs:
        raise PatchError("relocation table changed")
    return bytes(out)


def build_fixed_image(raw, log=print):
    """Patch the original STX image `raw`.

    Returns (fixed image, original program, patched program).
    """
    digest = hashlib.sha256(raw).hexdigest()
    if digest != STX_SHA256:
        raise PatchError(f"input SHA-256 {digest} is not the supported Ultima V boot disk "
                         f"({STX_SHA256})")
    stx = STXImage(raw)
    vol = Volume(stx)
    lsns, size = vol.find(PRG_PATH)
    prg = vol.read_file(lsns, size)
    if hashlib.sha256(prg).hexdigest() != PRG_SHA256:
        raise PatchError(f"{PRG_PATH} does not match the supported version")

    patched = apply_patches(prg)
    if hashlib.sha256(patched).hexdigest() != FIXED_PRG_SHA256:
        raise PatchError("patched program differs from the expected one")
    for q in PATCHES:
        log(f"patched {q['name']}: text {q['addr']:#07x}")

    touched = vol.write_file(lsns, patched)
    for key, dump in touched:
        log(f"{PRG_PATH}: track {key[0]} side {key[1]} sector {key[2]} rewritten"
            + (", read-track copy updated" if dump else ""))

    # Re-parse the result from scratch and verify it end to end.
    fixed = bytes(stx.img)
    check = STXImage(fixed)
    vol2 = Volume(check)
    lsns2, size2 = vol2.find(PRG_PATH)
    if vol2.read_file(lsns2, size2) != patched:
        raise PatchError(f"verification failed: {PRG_PATH} does not read back")
    allowed = []
    for key, _dump in touched:
        allowed.append((check.sectors[key], 512))
        allowed.append(check.track_image.get(key[:2], (0, 0)))
    for i, (a, b) in enumerate(zip(raw, fixed)):
        if a != b and not any(o <= i < o + n for o, n in allowed):
            raise PatchError(f"verification failed: unexpected change at {i:#x}")
    if hashlib.sha256(fixed).hexdigest() != FIXED_STX_SHA256:
        raise PatchError("verification failed: output SHA-256 differs from the expected image")
    return fixed, prg, patched


def parse_srec(text):
    """Return {address: byte} from Motorola S-records (S1/S2/S3 data only)."""
    data = {}
    for line in text.split():
        kind = line[1]
        if kind not in "123":
            continue
        alen = {"1": 2, "2": 3, "3": 4}[kind]
        rec = bytes.fromhex(line[2:])
        addr = int.from_bytes(rec[1:1 + alen], "big")
        for i, b in enumerate(rec[1 + alen:-1]):
            data[addr + i] = b
    return data


def check_asm_source(original_prg, patched_prg, asm=ASM_SOURCE, vasm="vasmm68k_mot"):
    """Assemble inn_fix.s and compare it with the patch table.

    Returns None if vasm is not installed.  Otherwise every byte the source
    defines must equal the patched program, and every changed text byte must
    come from the source.
    """
    exe = shutil.which(vasm)
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "inn_fix.srec"
        subprocess.run([exe, "-quiet", "-no-opt", "-Fsrec", "-s28", "-o", str(out), str(asm)],
                       check=True)
        src = parse_srec(out.read_text())

    tsize = struct.unpack_from(">I", original_prg, 2)[0]
    for addr, b in src.items():
        if addr >= tsize or patched_prg[0x1C + addr] != b:
            raise PatchError(f"{asm.name} differs from the patch table at {addr:#x}")
    for addr in range(tsize):
        if original_prg[0x1C + addr] != patched_prg[0x1C + addr] and addr not in src:
            raise PatchError(f"patched byte at text offset {addr:#x} is not in {asm.name}")
    return len(src)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help=f"original STX image (SHA-256 {STX_SHA256})")
    ap.add_argument("-o", "--output",
                    help="output image (default: <input name>-fixed.stx next to the input)")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output) if args.output else src.with_name(src.stem + "-fixed.stx")
    if dst.exists() and dst.resolve() == src.resolve():
        raise PatchError("refusing to overwrite the input image")

    raw = src.read_bytes()
    print(f"input:  {src}")
    print(f"        SHA-256 {hashlib.sha256(raw).hexdigest()}")
    fixed, original, patched = build_fixed_image(raw)

    checked = check_asm_source(original, patched)
    if checked is None:
        print("note:   vasmm68k_mot not found, src/inn_fix.s not cross-checked")
    else:
        print(f"source: src/inn_fix.s assembles to the same {checked} bytes")

    dst.write_bytes(fixed)
    print(f"output: {dst}")
    print(f"        SHA-256 {hashlib.sha256(fixed).hexdigest()}")


if __name__ == "__main__":
    try:
        main()
    except PatchError as e:
        sys.exit(f"error: {e}")
