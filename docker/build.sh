#!/bin/sh
# Build the sandbox images: ./docker/build.sh
set -eu
cd "$(dirname "$0")"
docker build -t repopilot-pytest .
docker build -t repopilot-node -f Dockerfile.node .
