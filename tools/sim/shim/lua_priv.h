// Stand-in for Solana OS's main/lua/lua_priv.h, just enough to compile the
// firmware's own lua_json.c into the test rig (tools/sim/badgesim.c).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "lua.h"
#include "lauxlib.h"

#define LH_JSON_DEPTH 16
#define LH_STORAGE_MAX (16 * 1024)

typedef struct {
    char *p;
    size_t len, cap, max;
    const char *err;
} lh_jbuf_t;

bool lh_json_encode(lua_State *L, int idx, lh_jbuf_t *b);
const char *lh_json_decode(lua_State *L, const char *s, size_t len);
void lh_push_jbuf(lua_State *L, lh_jbuf_t *b);
void lh_open_json(lua_State *L);

void lh_charge_steps(lua_State *L, unsigned int n);
