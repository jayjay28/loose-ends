#!/bin/bash
# Build the double-click installer (§v3 workstream 1, the last piece).
#
#   ./build-pkg.sh              build, sign and notarize
#   ./build-pkg.sh --unsigned   build only — for testing the install itself
#   ./build-pkg.sh --check      say what's missing and stop
#
# Why a package at all: the curl line works, and the person this product most
# needs to reach told us plainly that he does not want to open a terminal. A
# signed package is the difference between "paste this command" and "double
# click this file", and that difference ended one walkthrough already.
#
# Two certificates are involved and they are not interchangeable:
#   Developer ID Application  signs the app inside the payload
#   Developer ID Installer    signs the .pkg itself
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$HERE/build"
ROOTDIR="$BUILD/root"
PAYLOAD="$ROOTDIR/usr/local/loose-ends"
IDENTIFIER="dev.clyon.looseends.engine"
VERSION="${VERSION:-1.0}"
PROFILE="${NOTARY_PROFILE:-looseends}"
PKG="$BUILD/LooseEnds-$VERSION.pkg"

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*" >&2; }

MODE="${1:-}"

INSTALLER_ID="$(security find-identity -v 2>/dev/null \
                | grep "Developer ID Installer" | head -1 \
                | sed -E 's/.*"(.*)"/\1/' || true)"

if [ "$MODE" = "--check" ] || { [ -z "$INSTALLER_ID" ] && [ "$MODE" != "--unsigned" ]; }; then
  if [ -z "$INSTALLER_ID" ]; then
    warn "no Developer ID Installer certificate in the keychain."
    warn "  This is a *different* certificate from the one that signs the app."
    warn "  Xcode ▸ Settings ▸ Accounts ▸ (your team) ▸ Manage Certificates"
    warn "  ▸ + ▸ Developer ID Installer."
    warn ""
    warn "  Build an unsigned package to test the install itself:"
    warn "    $0 --unsigned"
    [ "$MODE" = "--check" ] && exit 1
    exit 1
  fi
  say "ready: $INSTALLER_ID"
  exit 0
fi

# ------------------------------------------------------------------ payload
say "assembling the payload"
rm -rf "$BUILD"
mkdir -p "$PAYLOAD" "$BUILD"

# Tracked files only, so no database, no venv, no local mess — the same
# discipline publish/export.sh uses for the public repo.
cd "$ROOT"
git archive HEAD backend | tar -x -C "$PAYLOAD"

# The menu bar app rides along, already signed and stapled by mac/sign.sh.
if [ -d "$HERE/../build/Loose Ends.app" ]; then
  cp -R "$HERE/../build/Loose Ends.app" "$PAYLOAD/Loose Ends.app"
  say "including the menu bar app"
else
  warn "no signed menu bar app at mac/build — run mac/sign.sh first."
  warn "continuing without it; the engine will install, the icon won't appear."
fi

# What ships from deploy/ is stated positively: an allowlist cannot be
# outflanked by a new private note landing in that directory, the way naming
# each unwanted file can. Everything else there is ours, not the user's.
find "$PAYLOAD/backend/deploy" -type f \
     ! -name 'install.sh' ! -name 'uninstall.sh' -delete 2>/dev/null || true
rm -f "$PAYLOAD/backend"/*.db* 2>/dev/null || true

# ------------------------------------------------------------------ the pkg
say "building the package"
pkgbuild --root "$ROOTDIR" \
         --scripts "$HERE/scripts" \
         --identifier "$IDENTIFIER" \
         --version "$VERSION" \
         --install-location "/" \
         "$BUILD/component.pkg" >/dev/null

# A distribution package can state a minimum macOS and show a real title,
# where a bare component package shows the identifier and accepts anything.
cat > "$BUILD/distribution.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Loose Ends</title>
    <organization>dev.clyon</organization>
    <domains enable_localSystem="true"/>
    <options customize="never" require-scripts="true" hostArchitectures="arm64,x86_64"/>
    <volume-check>
        <allowed-os-versions><os-version min="13.0"/></allowed-os-versions>
    </volume-check>
    <choices-outline><line choice="default"/></choices-outline>
    <choice id="default"><pkg-ref id="$IDENTIFIER"/></choice>
    <pkg-ref id="$IDENTIFIER" version="$VERSION" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
XML

if [ -n "$INSTALLER_ID" ] && [ "$MODE" != "--unsigned" ]; then
  productbuild --distribution "$BUILD/distribution.xml" \
               --package-path "$BUILD" \
               --sign "$INSTALLER_ID" --timestamp \
               "$PKG" >/dev/null
  say "signed with $INSTALLER_ID"
else
  productbuild --distribution "$BUILD/distribution.xml" \
               --package-path "$BUILD" \
               "$PKG" >/dev/null
  warn "unsigned — Gatekeeper will refuse this on any other Mac."
  say "built: $PKG"
  exit 0
fi

# ---------------------------------------------------------------- notarize
say "notarizing (Apple usually answers in a few minutes)…"
xcrun notarytool submit "$PKG" --keychain-profile "$PROFILE" --wait
say "stapling the ticket"
xcrun stapler staple "$PKG"
xcrun stapler validate "$PKG"

say "done — $PKG"
say "verify like Gatekeeper does:  spctl -a -vvv -t install \"$PKG\""
