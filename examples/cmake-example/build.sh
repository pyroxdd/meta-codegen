#!/usr/bin/env bash
set -e

cd "$(dirname "${BASH_SOURCE[0]}")"
rm -f build/CMakeCache.txt
cmake -S . -B build -G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build
