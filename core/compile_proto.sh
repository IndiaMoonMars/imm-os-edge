#!/usr/bin/env bash
# IMM-OS Compile Protobuf Schema
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "Compiling telemetry.proto to Python..."
# Note: ensure grpcio-tools or protobuf-compiler is installed
protoc --python_out=. telemetry.proto

echo "Done! Generated telemetry_pb2.py."
