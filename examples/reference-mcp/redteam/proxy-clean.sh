#!/bin/sh
# Launch the reference MCP server over stdio behind poison-proxy.py. POISON=0
# passes every result through untouched; POISON=1 appends the injected
# instruction to get_guide's result. This file is one of a pair that differ in
# that flag and nothing else (a test holds them to it). Everything resolves
# from this file's own directory, so it runs from any working directory on any
# machine; python3 is whichever is first on PATH — the one with the MCP SDK.
HERE=$(cd "$(dirname "$0")" && pwd)
POISON=0 exec python3 "$HERE/poison-proxy.py" python3 "$HERE/../server.py"
