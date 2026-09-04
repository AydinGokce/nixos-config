#!/usr/bin/env python3
"""Clear the executable bit on PT_GNU_STACK for the given ELF shared objects.

Old PyTorch 1.12 wheels ship libtorch_*.so marked with an executable stack
(readelf shows `GNU_STACK ... RWE`). On kernels that refuse to grant an exec
stack at dlopen time, `import torch` then fails with:

    ImportError: libtorch_cpu.so: cannot enable executable stack as shared
    object requires: Invalid argument

patchelf 0.15 (this nixpkgs) has no --clear-execstack and execstack isn't
packaged, so we clear the PF_X bit in PT_GNU_STACK ourselves. Accepts file
paths and globs (recursive ** supported). Pure stdlib; safe/no-op on files
that don't request an exec stack.
"""
import glob
import os
import struct
import sys

PT_GNU_STACK = 0x6474E551
PF_X = 0x1


def patch(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if data[:4] != b"\x7fELF":
        return False
    ei_class, ei_data = data[4], data[5]
    en = "<" if ei_data == 1 else ">"
    if ei_class == 2:  # ELF64: p_type@0, p_flags@4
        e_phoff = struct.unpack_from(en + "Q", data, 0x20)[0]
        e_phentsize = struct.unpack_from(en + "H", data, 0x36)[0]
        e_phnum = struct.unpack_from(en + "H", data, 0x38)[0]
        flags_off = 4
    else:  # ELF32: p_flags@24
        e_phoff = struct.unpack_from(en + "I", data, 0x1C)[0]
        e_phentsize = struct.unpack_from(en + "H", data, 0x2A)[0]
        e_phnum = struct.unpack_from(en + "H", data, 0x2C)[0]
        flags_off = 24
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from(en + "I", data, off)[0] == PT_GNU_STACK:
            fo = off + flags_off
            p_flags = struct.unpack_from(en + "I", data, fo)[0]
            if p_flags & PF_X:
                struct.pack_into(en + "I", data, fo, p_flags & ~PF_X)
                with open(path, "wb") as f:
                    f.write(data)
                return True
            return False
    return False


def main():
    n = 0
    for arg in sys.argv[1:]:
        for path in glob.glob(arg, recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                if patch(path):
                    n += 1
                    print("cleared execstack:", path)
            except Exception as e:  # noqa: BLE001
                print("skip", path, e, file=sys.stderr)
    print(f"clear_execstack: patched {n} file(s)")


if __name__ == "__main__":
    main()
