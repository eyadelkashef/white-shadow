# Launch checklist - what is ready and what you still have to do

## Ready in this package
- The launcher app, the Windows setup program (with EULA, wizard art, WebView2 setup), macOS and Linux builds, GitHub workflows that build and publish them, and the one-click `build.bat`.
- The download website: OS-aware Download button that serves the setup file, next-steps panel, checksums, install guide, FAQ, changelog, Privacy Policy, Terms, 404 page, security headers (`site/_headers`).

## Tested in my environment
- The interface and server in a real browser engine (all pages, dialogs, installs with dependencies, skins, servers, settings, security rules).
- The website (every OS, failure states, real file download, links, security headers).
- Native-window start-up logic and the WebView2 install, with stand-ins.

## NOT tested (needs a real Windows/macOS machine) - do these first
1. Run `build.bat`, install the result on a clean Windows 10 and Windows 11 PC (or VM), launch vanilla and Fabric, install a mod with dependencies, join a server, uninstall.
2. Confirm the native window opens (WebView2). If it ever doesn't, set the environment variable `WS_BROWSER=1` to compare with the browser-window mode and send me the log (`launcher.log` in the data folder).
3. Test the macOS and Linux builds on real machines (GitHub builds them, nobody has run them yet).

## Before you charge for it or promote it widely
1. **Code signing.** Unsigned apps show "Windows protected your PC" and get flagged by antivirus more often. Buy/obtain a signing certificate (e.g. Azure Artifact Signing, Certum, SSL.com) and sign `WhiteShadow-windows.exe` and `WhiteShadow-Setup.exe`. macOS needs an Apple Developer ID and notarization. Tip: packaging the app as a folder ("onedir") inside the installer triggers fewer antivirus false positives than a single self-extracting exe.
2. **Legal review.** Read the Minecraft EULA and Commercial Usage Guidelines; check the name "White Shadow" is free to use in your country; never use Minecraft logos or game assets. Have a lawyer review `EULA.txt`, `site/terms.html` and `site/privacy.html` (add your governing law and company details). Selling access to Minecraft content or charging for features of the game can break Mojang's rules - a free launcher is the safest model.
3. **Offline accounts.** The launcher uses offline accounts only. That is fine for singleplayer / LAN / servers that allow it, but it is a legal and reputation risk to market it as a way to play without owning Minecraft. Safer positioning: "for Minecraft owners". Adding official **Microsoft sign-in** (register an app in Microsoft Entra and request Minecraft services approval) is the biggest step toward a credible, long-lasting product.
4. **Support.** Put a real support address in `site/config.js` (`CONTACT_EMAIL`) and consider a community server and an issue tracker.
5. **Updates.** Installed copies tell users about new versions and open the release page. A silent auto-updater is a later improvement.

## Known limits
macOS build is Apple Silicon only; Linux needs a Chromium-based browser; no Microsoft sign-in; unsigned until you sign it.
