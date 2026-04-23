#!/bin/bash
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    ./out/server.exe
    ./out/client.exe
else
    ./out/server
    ./out/client
fi
