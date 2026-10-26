#!/bin/sh
# Build the sandbox image: ./docker/build.sh
set -eu
cd "$(dirname "$0")"
docker build -t repopilot-pytest .
