"""Extract a Splinter Cell Chaos Theory PS2 .UMD archive (FFileManagerArc format, from SC3_ON.ELF 0x1183f0).
Trailer at end-0x800: 5 int32 (?, dir_offset, archive_size, ?, ?). Directory at dir_offset: compact-index count, then
per file: FString name (compact-index length incl. NUL), int32 offset, int32 size, int32 flags. Flag 2 = RC4 with the
file name (without NUL) as key (0x143a50 key schedule, 0x143af0 stream). Usage: umd_extract.py ARCHIVE OUTDIR"""
import os, struct, sys

def compact(d, p):
    b = d[p]; p += 1; neg = b & 0x80; v = b & 0x3f
    if b & 0x40:
        sh = 6
        while True:
            b = d[p]; p += 1; v |= (b & 0x7f) << sh; sh += 7
            if not b & 0x80: break
    return (-v if neg else v), p

def rc4(key, data):
    S = list(range(256)); j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xff; S[i], S[j] = S[j], S[i]
    out = bytearray(data); i = j = 0
    for k in range(len(out)):
        i = (i + 1) & 0xff; j = (j + S[i]) & 0xff; S[i], S[j] = S[j], S[i]
        out[k] ^= S[(S[i] + S[j]) & 0xff]
    return bytes(out)

d = open(sys.argv[1], "rb").read(); outdir = sys.argv[2]; os.makedirs(outdir, exist_ok=True)
_, diroff, _, _, _ = struct.unpack_from("<5i", d, len(d) - 0x800)
n, p = compact(d, diroff)
for _ in range(n):
    ln, p = compact(d, p); name = d[p:p + ln - 1].decode("latin-1"); p += ln
    off, size, flags = struct.unpack_from("<3i", d, p); p += 12
    blob = d[off:off + size]
    if flags & 2: blob = rc4(name.encode("latin-1"), blob)
    safe = name.replace("\\", "/").lstrip("/")
    dst = os.path.join(outdir, safe); os.makedirs(os.path.dirname(dst) or outdir, exist_ok=True)
    open(dst, "wb").write(blob)
    print(f"{flags:2d} {size:9d} {name}")
