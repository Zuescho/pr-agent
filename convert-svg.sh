#!/usr/bin/env bash
# Render the release thumbnail SVG to a 1200x630 PNG (LinkedIn/social size).
# Requires librsvg: `brew install librsvg` (provides rsvg-convert).
set -euo pipefail

SVG="${1:-pr-agent-release-thumbnail.svg}"
PNG="${2:-pr-agent-linkedin-thumbnail.png}"

rsvg-convert -w 1200 -h 630 "$SVG" -o "$PNG"
echo "Rendered $PNG from $SVG"
