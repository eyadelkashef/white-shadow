White Shadow Launcher
A Minecraft launcher that players download from your website and run like any other app - no Python, no browser, no setup.

How players get it (Windows)
They open your website and press Download for Windows - the browser immediately downloads WhiteShadow-Setup.exe, and the page shows the next steps.
They open that file (if Windows warns "protected your PC": More info → Run anyway - the app is not code-signed yet) and click Install.
White Shadow is installed like any other launcher: Start Menu entry, optional desktop shortcut, listed in Settings → Apps for uninstalling. No administrator rights, no Python, no Microsoft account or Microsoft Store. The installer also sets up the small Windows web component the app window uses (Microsoft WebView2) if the PC doesn't already have it, so the app runs right after installing. macOS: unzip, drag to Applications, right-click → Open once. Linux: chmod +x, run.
How you get the setup file onto your website
Option A - your own web hosting (simplest):

On a Windows PC with Python (and internet), double-click build.bat. It builds the app, builds WhiteShadow-Setup.exe (installing Inno Setup via winget the first time) and copies everything into site\downloads\ with a checksum list.
Upload the whole site folder to any web host (Netlify, Cloudflare Pages, your own server...). Done: the Download button now serves that setup file.
New version: raise the version number in WhiteShadowLauncher.py (APP, VER = ...), optionally describe it in release-notes.txt, run build.bat, upload site again.
Option B - GitHub builds everything for you (also makes macOS and Linux files, no Windows PC needed):

Create a GitHub repository and upload this folder (keep .github).
Settings → Pages → Source: GitHub Actions; then Actions → site → Run workflow.
git tag v1.0.0 then git push --tags: the release workflow builds the Windows setup + portable exe, macOS app and Linux file and publishes them; the website shows the buttons automatically (it uses its own downloads folder first, GitHub Releases otherwise). Do not commit the .exe files into the repository itself - GitHub Releases is where they belong.
What the app is
The launcher engine is Python; the interface runs in a native app window (Microsoft WebView2 on Windows - the app installs it itself if it is missing - and WebKit on macOS) with the logo as its icon. If a native window isn't available (Linux), it opens in its own app window of an installed Chromium-based browser (Chrome, Chromium, Edge, Brave) instead. Set WS_BROWSER=1 to force that mode.

Honest notes
The apps are unsigned, so Windows SmartScreen / macOS Gatekeeper warn on first run (More info → Run anyway / right-click → Open). Removing the warning needs a paid code-signing certificate.
macOS build is Apple Silicon (arm64). Linux needs a Chromium-based browser installed.
Accounts are offline accounts (singleplayer, LAN, servers that allow them). No Microsoft sign-in yet.
Settings, worlds, mods and saved skins live in one data folder (movable in Settings → Storage).
classic/ holds the older Tk version and early prototypes (not part of the product).
Before launching publicly read MARKET_CHECKLIST.md.
