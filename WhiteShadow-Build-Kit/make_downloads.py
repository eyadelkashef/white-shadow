#!/usr/bin/env python3
"""Copies the finished files from dist/ into site/downloads/ and writes site/downloads/latest.json + SHA256SUMS,
so the website's Download button hands out the real setup file straight from your own web host."""
import hashlib, json, re, shutil, time
from pathlib import Path

root = Path(__file__).resolve().parent
dist, out = root / "dist", root / "site" / "downloads"
KNOWN = [("windows-installer", "WhiteShadow-Setup.exe"), ("windows-portable", "WhiteShadow-windows.exe"),
         ("macos", "WhiteShadow-macos.zip"), ("linux", "WhiteShadow-linux")]
version = re.search(r'^APP, VER = "White Shadow", "([^"]+)"', (root / "WhiteShadowLauncher.py").read_text(encoding="utf-8"), re.M).group(1)
notes = (root / "release-notes.txt").read_text(encoding="utf-8").strip() if (root / "release-notes.txt").exists() else ""
out.mkdir(parents=True, exist_ok=True)
files, sums = {}, []
for key, name in KNOWN:
    src = dist / name
    if not src.is_file():
        continue
    shutil.copy2(src, out / name)
    h = hashlib.sha256((out / name).read_bytes()).hexdigest()
    files[key] = {"name": name, "size": src.stat().st_size, "sha256": h, "url": f"downloads/{name}"}
    sums.append(f"{h}  {name}")
if not files:
    raise SystemExit("Nothing to publish: build the app first (no files found in dist/).")
(out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
(out / "latest.json").write_text(json.dumps({"version": version, "date": time.strftime("%Y-%m-%d"), "notes": notes,
                                              "sums": "downloads/SHA256SUMS", "files": files}, indent=2), encoding="utf-8")
print(f"Published {len(files)} file(s) for version {version} into {out}")
for k, f in files.items():
    print(f"  {k:17} {f['name']}  {f['size'] / 1048576:.1f} MB  sha256 {f['sha256'][:16]}...")
