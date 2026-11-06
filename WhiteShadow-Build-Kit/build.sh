#!/usr/bin/env bash
# Builds a standalone app for the OS you run it on. Python is needed only on THIS machine.
set -e
if [ "$(uname)" = "Darwin" ]; then
  python3 -m pip install --upgrade pyinstaller pillow certifi pywebview
  python3 -m PyInstaller --noconfirm --clean --windowed --name WhiteShadow --icon icon.icns --collect-all webview WhiteShadowLauncher.py
  (cd dist && rm -f WhiteShadow-macos.zip && ditto -c -k --keepParent WhiteShadow.app WhiteShadow-macos.zip)
  python3 make_downloads.py
  echo "Done: dist/WhiteShadow.app  (zipped as dist/WhiteShadow-macos.zip)"
else
  python3 -m pip install --upgrade pyinstaller pillow certifi
  python3 -m PyInstaller --noconfirm --clean --onefile --name WhiteShadow WhiteShadowLauncher.py
  chmod +x dist/WhiteShadow
  mv dist/WhiteShadow dist/WhiteShadow-linux
  python3 make_downloads.py
  echo "Done: dist/WhiteShadow-linux"
fi
