#!/usr/bin/env bash
# Builds tools/sim/badgesim from a checkout of solana-foundation/solana-os
# (its Lua 5.5 in main/lua/vendor and its lua_json.c), as a 32-bit program so
# that the heap an app uses matches what it uses on the badge's 32-bit chip.
#   tools/sim/build.sh <path to solana-os>     (needs gcc with 32-bit support: gcc-multilib)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fw="${1:?usage: build.sh <path to a solana-os checkout>}"
vendor="$fw/main/lua/vendor"
srcs=()
for f in lapi lauxlib lbaselib lcode lcorolib lctype ldebug ldo ldump lfunc lgc llex lmathlib lmem \
         lobject lopcodes lparser lstate lstring lstrlib ltable ltablib ltm lundump lutf8lib lvm lzio; do
    srcs+=("$vendor/$f.c")
done
# lua_json.c includes "lua_priv.h" from its own folder first, so build a copy
# next to our stand-in for that header.
build="$here/build"
mkdir -p "$build"
cp "$fw/main/lua/lua_json.c" "$here/shim/lua_priv.h" "$build/"
gcc -m32 -O1 -g -w -I"$build" -I"$vendor" "${srcs[@]}" "$build/lua_json.c" "$here/badgesim.c" \
    -lm -o "$here/badgesim"
echo "built $here/badgesim"
