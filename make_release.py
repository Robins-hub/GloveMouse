"""Packages dist/GloveMouse.exe for distribution.

Creates release/GloveMouse-windows.zip containing the app, its license, the
licenses of every bundled third-party component, and the README, plus
release/SHA256SUMS.txt so downloads can be verified.
"""
import glob
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "dist", "GloveMouse.exe")
OUT = os.path.join(ROOT, "release")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SKIP = {"pip", "setuptools", "wheel", "pip-licenses", "prettytable", "wcwidth"}
LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def package_licenses():
    """License info for every installed package, read with Python's own
    importlib.metadata (no external tool, always UTF-8)."""
    from importlib import metadata
    seen, parts = set(), []
    dists = sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower())
    for d in dists:
        name = d.metadata["Name"]
        if not name or name.lower() in seen or name.lower() in SKIP:
            continue
        seen.add(name.lower())
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        lic = lic.strip().splitlines()[0] if lic.strip() else ""
        if not lic:
            cls = [c.split("::")[-1].strip() for c in (d.metadata.get_all("Classifier") or [])
                   if c.startswith("License ::")]
            lic = ", ".join(cls) or "see license text below"
        url = d.metadata.get("Home-page") or ""
        if not url:
            for u in d.metadata.get_all("Project-URL") or []:
                url = u.split(",", 1)[-1].strip()
                break
        parts.append(f"\n\n{'=' * 70}\n{name} {d.version}\nLicense: {lic}\n{url}\n{'=' * 70}\n")
        for f in d.files or []:
            if ".dist-info" in str(f) and f.name.upper().startswith(LICENSE_PREFIXES):
                try:
                    with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                        parts.append(f"\n--- {f.name} ---\n" + fh.read())
                except Exception:
                    pass
    return "".join(parts)


def third_party_licenses():
    parts = ["GloveMouse bundles the following third-party software.\n"
             "Their licenses are reproduced below.\n", package_licenses()]
    extra = [("Python", os.path.join(sys.base_prefix, "LICENSE.txt"))]
    extra += [("Tcl", p) for p in glob.glob(os.path.join(sys.base_prefix, "tcl", "tcl8*", "license.terms"))]
    extra += [("Tk", p) for p in glob.glob(os.path.join(sys.base_prefix, "tcl", "tk8*", "license.terms"))]
    for name, path in extra:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"\n\n{'=' * 70}\n{name}\n{'=' * 70}\n{f.read()}")
    return "".join(parts)


def main():
    if not os.path.isfile(EXE):
        sys.exit("dist/GloveMouse.exe not found - run PyInstaller first.")
    os.makedirs(OUT, exist_ok=True)
    zip_path = os.path.join(OUT, "GloveMouse-windows.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(EXE, "GloveMouse/GloveMouse.exe")
        z.write(os.path.join(ROOT, "LICENSE"), "GloveMouse/LICENSE.txt")
        z.write(os.path.join(ROOT, "README.md"), "GloveMouse/README.md")
        z.writestr("GloveMouse/THIRD_PARTY_LICENSES.txt", third_party_licenses())
    with open(os.path.join(OUT, "SHA256SUMS.txt"), "w") as f:
        f.write(f"{sha256(EXE)}  GloveMouse.exe\n{sha256(zip_path)}  GloveMouse-windows.zip\n")
    print("Release files written to", OUT)


if __name__ == "__main__":
    main()
