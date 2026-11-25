#!/bin/sh
# Build the image and tag it with the version from app/version.py and "latest".
#   ./scripts/build.sh
set -e
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/version.py)
[ -n "$VERSION" ] || { echo "Could not read version from app/version.py" >&2; exit 1; }
docker build --build-arg VERSION="$VERSION" -t "gamelib:$VERSION" -t gamelib:latest .
echo
echo "Built gamelib:$VERSION (also tagged gamelib:latest)"
docker image ls gamelib
