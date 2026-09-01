#!/bin/bash
# One version, derived in one place. Source this; don't run it.
#
# Four files used to declare a version independently — the engine's __init__,
# the FastAPI app, the Xcode project, and the package builder — and none of
# them moved. Four different packages went out calling themselves 1.0, which
# makes "which one have you got?" unanswerable. That is the only question
# that matters once more than one person has a copy, and the first walkthrough
# will ask it.
#
# The marketing version is a human decision, so it stays where a human edits
# it: the engine's __version__. The build number is not a decision at all, so
# it is counted rather than typed — git's commit count only ever increases,
# which is precisely what macOS needs to tell an upgrade from a downgrade.
#
# Every name here is prefixed. The first version of this file exported a bare
# BUILD, which is also what both callers already called their build
# *directory* — sourcing it silently repointed the package build at a folder
# named "225". A shared file gets to own a namespace, not a common word.

# BASH_SOURCE is bash-only. Under zsh it is empty, dirname turns that into
# ".", and the root resolves one directory too high — where there is no
# __init__ to read and possibly no repo at all. The old code answered that
# with defaults and reported version 0.0.0, which is the same silent-wrong
# -answer shape as the installer's "found python3" and the package's
# "Installation Successful". Locate, then check, then refuse.
_version_self="${BASH_SOURCE[0]:-$0}"
VERSION_ROOT="${VERSION_ROOT:-$(cd "$(dirname "$_version_self")/.." 2>/dev/null && pwd)}"

if [ ! -f "$VERSION_ROOT/backend/lifeline/__init__.py" ]; then
  echo "version.sh: can't find the repo from '$_version_self'." >&2
  echo "  Source it from bash, or set VERSION_ROOT before sourcing." >&2
  return 1 2>/dev/null || exit 1
fi

VERSION_MARKETING="$(sed -n 's/^__version__ = "\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' \
                   "$VERSION_ROOT/backend/lifeline/__init__.py")"
if [ -z "$VERSION_MARKETING" ]; then
  echo "version.sh: no __version__ in $VERSION_ROOT/backend/lifeline/__init__.py" >&2
  return 1 2>/dev/null || exit 1
fi

VERSION_BUILD="$(git -C "$VERSION_ROOT" rev-list --count HEAD 2>/dev/null || echo 0)"
VERSION_SHA="$(git -C "$VERSION_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
VERSION_FULL="$VERSION_MARKETING.$VERSION_BUILD"

# A build made from uncommitted work cannot be identified afterwards: the sha
# names a commit that does not contain what actually shipped. Worth saying out
# loud rather than refusing — testing a change before committing it is normal,
# and handing that build to someone else is what isn't.
if [ -n "$(git -C "$VERSION_ROOT" status --porcelain 2>/dev/null)" ]; then
  VERSION_DIRTY=1
else
  VERSION_DIRTY=0
fi
