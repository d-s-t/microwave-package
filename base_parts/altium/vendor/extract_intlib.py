"""Pull the embedded source libraries out of a compiled .IntLib.

An IntLib is an OLE compound file; pywin32 is already a dependency and
Windows' own structured-storage API reads those, so no new package.
"""
import sys, pythoncom
from win32com.storagecon import (
    STGM_READ, STGM_SHARE_EXCLUSIVE, STGTY_STORAGE, STGTY_STREAM)

MODE = STGM_READ | STGM_SHARE_EXCLUSIVE


def walk(storage, prefix=""):
    out = []
    for stat in storage.EnumElements(0, None, 0):
        name, kind = stat[0], stat[1]
        path = f"{prefix}/{name}" if prefix else name
        if kind == STGTY_STORAGE:
            out.append((path, "storage", 0))
            out += walk(storage.OpenStorage(name, None, MODE, None, 0), path)
        elif kind == STGTY_STREAM:
            out.append((path, "stream", stat[2]))
    return out


def read_stream(storage, parts):
    node = storage
    for p in parts[:-1]:
        node = node.OpenStorage(p, None, MODE, None, 0)
    stream = node.OpenStream(parts[-1], None, MODE, 0)
    chunks = []
    while True:
        data = stream.Read(1 << 20)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


src, want, dest = sys.argv[1], sys.argv[2], sys.argv[3]
root = pythoncom.StgOpenStorage(src, None, MODE)
for path, kind, size in walk(root):
    print(f"  {kind:8} {size:>9}  {path}")
data = read_stream(root, want.split("/"))
with open(dest, "wb") as f:
    f.write(data)
print(f"\nwrote {len(data)} bytes to {dest}")
print("first bytes:", data[:8].hex(" "))
