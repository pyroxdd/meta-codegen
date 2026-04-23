#!/bin/bash
# Exit on error
set -e

# Create build directory and configure
cmake -B build -G "Ninja" -DCMAKE_BUILD_TYPE=Release

# Build the project
cmake --build build
