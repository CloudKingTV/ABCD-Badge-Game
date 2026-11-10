// badgesim: runs a Solana OS Lua app on the badge's own Lua (the firmware's
// main/lua/vendor, 32-bit numbers) with the firmware's limits: a 48 KB heap,
// 200,000 VM instructions per callback, the same library set and boot order
// as main/lua/lua_host.c. It is not the firmware: everything a badge does
// outside Lua (the screen, LEDs, radio, timers, the clock) is asked of the
// driver on the other end of stdin/stdout (tools/sim/sim.py), one JSON line
// per message. storage, json, require and fs.read/load are handled here, the
// way the firmware does them, because they live in the app's heap.
//
//   driver -> sim   {"op":"boot","dir":"app","storage":"{}"}
//                   {"op":"call","fn":"on_tick","args":"[50]","n":1}
//                   {"op":"callref","ref":7,"args":"[]","n":0,"unref":true}
//                   {"op":"unref","ref":7}   {"op":"close"}
//   sim -> driver   {"api":"ui.label","args":[...]}     (while a call runs)
//   driver -> sim   {"n":1,"ret":[...],"steps":0}  or  {"err":"message"}
//   sim -> driver   {"done":true,"ok":true,"ret":...,"mem":..,"peak":..,"steps":..}
//
// Functions cross as {"__fn":ref}, UI objects as {"__obj":id}; the driver
// hands back a new object as {"__new":{"id":..,"kind":"ui.label","bytes":n}},
// where bytes is what the real object costs the app's heap.

#define _GNU_SOURCE
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"
#include "lua_priv.h"

#define HEAP_MAX     (48 * 1024)
#define STEPS_MAX    200000
#define HOOK_EVERY   1000
#define STORAGE_MAX  (16 * 1024)
#define FS_READ_MAX  (16 * 1024)

static struct {
    lua_State *L;
    size_t mem, peak;
    bool mem_hit, killed;
    unsigned steps, steps_peak;
    int store_ref;
    char dir[512];
} S;

static lua_State *C;        // the control state: decodes the driver's lines

// ---------------------------------------------------------------------------
// The firmware's limits (lua_host.c)

int lh_cstack_check(void)
{
    return 0;
}

static void *app_alloc(void *ud, void *ptr, size_t osize, size_t nsize)
{
    (void)ud;
    size_t old = ptr ? osize : 0;
    if (nsize == 0) {
        free(ptr);
        S.mem -= old;
        return NULL;
    }
    if (nsize > old && S.mem - old + nsize > HEAP_MAX) {
        S.mem_hit = true;
        return NULL;
    }
    void *p = realloc(ptr, nsize);
    if (p) {
        S.mem = S.mem - old + nsize;
        if (S.mem > S.peak) {
            S.peak = S.mem;
        }
    }
    return p;
}

static void *ctl_alloc(void *ud, void *ptr, size_t osize, size_t nsize)
{
    (void)ud;
    (void)osize;
    if (nsize == 0) {
        free(ptr);
        return NULL;
    }
    return realloc(ptr, nsize);
}

static void step_hook(lua_State *L, lua_Debug *ar);

static int over_budget(lua_State *L, int level)
{
    S.killed = true;
    lua_sethook(L, step_hook, LUA_MASKCOUNT, 1);
    luaL_where(L, level);
    lua_pushliteral(L, "Script took too long");
    lua_concat(L, 2);
    return lua_error(L);
}

static void step_hook(lua_State *L, lua_Debug *ar)
{
    (void)ar;
    S.steps += HOOK_EVERY;
    if (S.steps > STEPS_MAX) {
        over_budget(L, 0);
    }
}

void lh_charge_steps(lua_State *L, unsigned int n)
{
    if (lua_getallocf(L, NULL) != app_alloc) {
        return;     // the control state
    }
    S.steps += n;
    if (S.steps > STEPS_MAX) {
        over_budget(L, 1);
    }
}

void lh_thread_new(lua_State *L1)
{
    if (lua_getallocf(L1, NULL) == app_alloc) {
        lua_sethook(L1, step_hook, LUA_MASKCOUNT, HOOK_EVERY);
    }
}

// ---------------------------------------------------------------------------
// Lines in and out

typedef struct {
    char *p;
    size_t len, cap;
} sb_t;

static void sb_put(sb_t *b, const char *s, size_t n)
{
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        while (cap < b->len + n + 1) {
            cap *= 2;
        }
        b->p = realloc(b->p, cap);
        if (!b->p) {
            fprintf(stderr, "badgesim: out of host memory\n");
            exit(2);
        }
        b->cap = cap;
    }
    memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = 0;
}

static void sb_puts(sb_t *b, const char *s)
{
    sb_put(b, s, strlen(s));
}

static void sb_printf(sb_t *b, const char *fmt, ...)
{
    char tmp[128];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    sb_puts(b, tmp);
}

static void sb_str(sb_t *b, const char *s, size_t n)
{
    sb_put(b, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') {
            char e[2] = {'\\', (char)c};
            sb_put(b, e, 2);
        } else if (c < 0x20 || c == 0x7f) {
            sb_printf(b, "\\u%04x", c);
        } else {
            sb_put(b, (const char *)&c, 1);
        }
    }
    sb_put(b, "\"", 1);
}

static char *read_line(void)
{
    static char *line;
    static size_t cap;
    ssize_t n = getline(&line, &cap, stdin);
    if (n < 0) {
        exit(0);
    }
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
        line[--n] = 0;
    }
    return line;
}

static void send_line(sb_t *b)
{
    sb_put(b, "\n", 1);
    fwrite(b->p, 1, b->len, stdout);
    fflush(stdout);
    b->len = 0;
}

// A Lua value as JSON for the driver. Functions become registry references,
// UI objects their ids.
static void emit(lua_State *L, int idx, sb_t *b, int depth)
{
    idx = lua_absindex(L, idx);
    switch (lua_type(L, idx)) {
    case LUA_TBOOLEAN:
        sb_puts(b, lua_toboolean(L, idx) ? "true" : "false");
        break;
    case LUA_TNUMBER:
        if (lua_isinteger(L, idx)) {
            sb_printf(b, "%lld", (long long)lua_tointeger(L, idx));
        } else {
            double v = (double)lua_tonumber(L, idx);
            if (v != v || v - v != 0) {
                sb_puts(b, "null");
            } else {
                sb_printf(b, "%.9g", v);
            }
        }
        break;
    case LUA_TSTRING: {
        size_t n;
        const char *s = lua_tolstring(L, idx, &n);
        sb_str(b, s, n);
        break;
    }
    case LUA_TFUNCTION:
        lua_pushvalue(L, idx);
        sb_printf(b, "{\"__fn\":%d}", luaL_ref(L, LUA_REGISTRYINDEX));
        break;
    case LUA_TUSERDATA:
        sb_printf(b, "{\"__obj\":%d}", *(int *)lua_touserdata(L, idx));
        break;
    case LUA_TTABLE: {
        if (depth > 12 || !lua_checkstack(L, 4)) {
            sb_puts(b, "null");
            break;
        }
        lua_Unsigned n = lua_rawlen(L, idx), keys = 0;
        lua_pushnil(L);
        while (lua_next(L, idx)) {
            keys++;
            lua_pop(L, 1);
        }
        if (n > 0 && keys == n) {
            sb_put(b, "[", 1);
            for (lua_Unsigned i = 1; i <= n; i++) {
                if (i > 1) {
                    sb_put(b, ",", 1);
                }
                lua_rawgeti(L, idx, (lua_Integer)i);
                emit(L, -1, b, depth + 1);
                lua_pop(L, 1);
            }
            sb_put(b, "]", 1);
            break;
        }
        sb_put(b, "{", 1);
        bool first = true;
        lua_pushnil(L);
        while (lua_next(L, idx)) {
            if (!first) {
                sb_put(b, ",", 1);
            }
            first = false;
            if (lua_type(L, -2) == LUA_TSTRING) {
                size_t kn;
                const char *k = lua_tolstring(L, -2, &kn);
                sb_str(b, k, kn);
            } else if (lua_isinteger(L, -2)) {
                sb_printf(b, "\"%lld\"", (long long)lua_tointeger(L, -2));
            } else {
                sb_puts(b, "\"?\"");
            }
            sb_put(b, ":", 1);
            emit(L, -1, b, depth + 1);
            lua_pop(L, 1);
        }
        sb_put(b, "}", 1);
        break;
    }
    default:
        sb_puts(b, "null");
    }
}

// ---------------------------------------------------------------------------
// The bridge: an API call the driver answers

static int method_factory(lua_State *L);

static void push_kind_meta(lua_State *L, const char *kind)
{
    if (luaL_newmetatable(L, kind)) {
        lua_newtable(L);                        // the methods, made as they're used
        lua_newtable(L);
        lua_pushstring(L, kind);
        lua_pushcclosure(L, method_factory, 1);
        lua_setfield(L, -2, "__index");
        lua_setmetatable(L, -2);
        lua_setfield(L, -2, "__index");
        lua_pushliteral(L, "UI object");
        lua_setfield(L, -2, "__metatable");
    }
}

static int read_reply(lua_State *L)
{
    char *line = read_line();
    const char *err = lh_json_decode(L, line, strlen(line));
    if (err) {
        return luaL_error(L, "sim: bad reply (%s)", err);
    }
    int t = lua_gettop(L);
    if (lua_getfield(L, t, "err") != LUA_TNIL) {
        return luaL_error(L, "%s", lua_tostring(L, -1));
    }
    lua_pop(L, 1);
    lua_getfield(L, t, "steps");
    unsigned steps = (unsigned)lua_tointeger(L, -1);
    lua_pop(L, 1);
    if (steps) {
        lh_charge_steps(L, steps);
    }
    lua_getfield(L, t, "n");
    int n = (int)lua_tointeger(L, -1);
    lua_pop(L, 1);
    lua_getfield(L, t, "ret");
    int r = lua_gettop(L);
    luaL_checkstack(L, n + 6, "too many results");
    for (int i = 1; i <= n; i++) {
        if (lua_rawgeti(L, r, i) != LUA_TTABLE) {
            continue;
        }
        int v = lua_gettop(L);
        if (lua_getfield(L, v, "__new") != LUA_TTABLE) {
            lua_pop(L, 1);
            continue;
        }
        int nw = lua_gettop(L);
        lua_getfield(L, nw, "id");
        int id = (int)lua_tointeger(L, -1);
        lua_getfield(L, nw, "bytes");
        int bytes = (int)lua_tointeger(L, -1);
        lua_getfield(L, nw, "uv");
        int uv = (int)lua_tointeger(L, -1);
        lua_pop(L, 3);
        int *u = lua_newuserdatauv(L, sizeof(int) + (size_t)(bytes > 0 ? bytes : 0), uv);
        *u = id;
        lua_getfield(L, nw, "kind");
        push_kind_meta(L, lua_tostring(L, -1));
        lua_setmetatable(L, -3);
        lua_pop(L, 1);                          // kind
        lua_replace(L, v);                      // the object, where the reply's table was
        lua_settop(L, v);
    }
    return n;
}

static sb_t s_out;

static int bridge(lua_State *L)
{
    size_t nn;
    const char *name = lua_tolstring(L, lua_upvalueindex(1), &nn);
    int n = lua_gettop(L);
    s_out.len = 0;
    sb_puts(&s_out, "{\"api\":");
    sb_str(&s_out, name, nn);
    sb_puts(&s_out, ",\"args\":[");
    for (int i = 1; i <= n; i++) {
        if (i > 1) {
            sb_put(&s_out, ",", 1);
        }
        if (lua_isnil(L, i)) {
            sb_puts(&s_out, "null");
        } else {
            emit(L, i, &s_out, 0);
        }
    }
    sb_printf(&s_out, "],\"n\":%d}", n);
    send_line(&s_out);
    return read_reply(L);
}

// methods[key] for a UI object kind: "ui.label:text"
static int method_factory(lua_State *L)
{
    lua_pushfstring(L, "%s:%s", lua_tostring(L, lua_upvalueindex(1)), luaL_checkstring(L, 2));
    lua_pushcclosure(L, bridge, 1);
    lua_pushvalue(L, 2);
    lua_pushvalue(L, -2);
    lua_rawset(L, 1);
    return 1;
}

// module[key]: "ui.label"
static int module_factory(lua_State *L)
{
    lua_pushfstring(L, "%s.%s", lua_tostring(L, lua_upvalueindex(1)), luaL_checkstring(L, 2));
    lua_pushcclosure(L, bridge, 1);
    lua_pushvalue(L, 2);
    lua_pushvalue(L, -2);
    lua_rawset(L, 1);
    return 1;
}

static void open_module(lua_State *L, const char *name)
{
    lua_newtable(L);
    lua_newtable(L);
    lua_pushstring(L, name);
    lua_pushcclosure(L, module_factory, 1);
    lua_setfield(L, -2, "__index");
    lua_setmetatable(L, -2);
    lua_setglobal(L, name);
}

// ---------------------------------------------------------------------------
// What the firmware keeps in the app's own heap: print, storage, files

static int l_print(lua_State *L)
{
    luaL_Buffer b;
    int n = lua_gettop(L);
    luaL_buffinit(L, &b);
    for (int i = 1; i <= n; i++) {
        if (i > 1) {
            luaL_addchar(&b, '\t');
        }
        luaL_tolstring(L, i, NULL);
        luaL_addvalue(&b);
    }
    luaL_pushresult(&b);
    size_t len;
    const char *text = lua_tolstring(L, -1, &len);
    s_out.len = 0;
    sb_puts(&s_out, "{\"api\":\"print\",\"args\":[");
    sb_str(&s_out, text, len);
    sb_puts(&s_out, "],\"n\":1}");
    send_line(&s_out);
    lua_settop(L, 0);
    read_reply(L);
    return 0;
}

static int l_collectgarbage(lua_State *L)
{
    const char *opt = luaL_optstring(L, 1, "collect");
    luaL_argcheck(L, !strcmp(opt, "count"), 1, "only \"count\" is available");
    lua_pushnumber(L, (lua_Number)lua_gc(L, LUA_GCCOUNT) + (lua_Number)lua_gc(L, LUA_GCCOUNTB) / 1024);
    return 1;
}

static int l_get(lua_State *L)
{
    luaL_checkstring(L, 1);
    lua_rawgeti(L, LUA_REGISTRYINDEX, S.store_ref);
    lua_pushvalue(L, 1);
    lua_rawget(L, -2);
    return 1;
}

static int l_set(lua_State *L)
{
    luaL_checkstring(L, 1);
    int type = lua_type(L, 2);
    luaL_argcheck(L, type == LUA_TNIL || type == LUA_TNONE || type == LUA_TNUMBER || type == LUA_TSTRING
                  || type == LUA_TBOOLEAN || type == LUA_TTABLE, 2, "storage takes numbers, strings, booleans and tables");
    lua_settop(L, 2);
    lua_rawgeti(L, LUA_REGISTRYINDEX, S.store_ref);
    int t = lua_gettop(L);
    lua_pushvalue(L, 1);
    lua_rawget(L, t);
    lua_pushvalue(L, 1);
    lua_pushvalue(L, 2);
    lua_rawset(L, t);
    lh_jbuf_t b = { .max = STORAGE_MAX };
    if (!lh_json_encode(L, t, &b)) {
        const char *err = b.err;
        free(b.p);
        lua_pushvalue(L, 1);
        lua_pushvalue(L, -2);
        lua_rawset(L, t);
        return luaL_error(L, "storage: %s", !strcmp(err, "too big") ? "full (16 KB per app)" : err);
    }
    s_out.len = 0;
    sb_puts(&s_out, "{\"api\":\"storage.save\",\"args\":[");
    sb_str(&s_out, b.p ? b.p : "{}", b.len);
    sb_puts(&s_out, "],\"n\":1}");
    free(b.p);
    send_line(&s_out);
    lua_settop(L, 0);
    read_reply(L);
    return 0;
}

static int l_remove(lua_State *L)
{
    lua_settop(L, 1);
    lua_pushnil(L);
    return l_set(L);
}

// A bundled file's path -> the host's, or an error for one a badge refuses.
static void file_path(lua_State *L, int idx, char *out, size_t n)
{
    const char *rel = luaL_checkstring(L, idx);
    bool ok = rel[0] && rel[0] != '/' && !strstr(rel, "..") && strlen(rel) <= 64;
    for (const char *p = rel; ok && *p; p++) {
        ok = (*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '_' || *p == '-' || *p == '.' || *p == '/';
    }
    luaL_argcheck(L, ok, idx, "a bundled file's path, like \"levels/1.json\"");
    snprintf(out, n, "%s/%s", S.dir, rel);
}

static char *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *p = malloc((size_t)n + 1);
    *len = fread(p, 1, (size_t)n, f);
    p[*len] = 0;
    fclose(f);
    return p;
}

// Pushes the compiled chunk of a bundled .lua file; 0 ok, 1 missing, 2 broken (message pushed).
static int compile_file(lua_State *L, const char *rel)
{
    char path[640];
    snprintf(path, sizeof(path), "%s/%s", S.dir, rel);
    size_t len;
    char *src = slurp(path, &len);
    if (!src) {
        return 1;
    }
    lh_charge_steps(L, 500 + (unsigned)(len / 4));
    char chunk[96];
    snprintf(chunk, sizeof(chunk), "=%s", rel);
    int rc = luaL_loadbufferx(L, src, len, chunk, "t");
    free(src);
    return rc == LUA_OK ? 0 : 2;
}

static int l_require(lua_State *L)
{
    size_t len;
    const char *name = luaL_checklstring(L, 1, &len);
    char rel[80];
    luaL_argcheck(L, len && len < 60, 1, "module names are lowercase a-z 0-9 _ - with dots between folders");
    for (size_t i = 0; i < len; i++) {
        rel[i] = name[i] == '.' ? '/' : name[i];
    }
    memcpy(rel + len, ".lua", 5);
    lua_settop(L, 1);
    lua_getfield(L, LUA_REGISTRYINDEX, "sim.loaded");
    if (lua_getfield(L, 2, name) != LUA_TNIL) {
        return 1;
    }
    lua_pop(L, 1);
    int rc = compile_file(L, rel);
    if (rc == 1) {
        return luaL_error(L, "module \"%s\" not found: the app has no %s", name, rel);
    }
    if (rc == 2) {
        return lua_error(L);
    }
    lua_pushvalue(L, 1);
    lua_pushstring(L, rel);
    lua_call(L, 2, 1);
    if (lua_isnil(L, -1)) {
        lua_pop(L, 1);
        lua_pushboolean(L, 1);
    }
    lua_pushvalue(L, -1);
    lua_setfield(L, 2, name);
    return 1;
}

static int l_fs_load(lua_State *L)
{
    char path[640];
    file_path(L, 1, path, sizeof(path));
    int rc = compile_file(L, lua_tostring(L, 1));
    if (rc == 1) {
        lua_pushnil(L);
        lua_pushliteral(L, "not found");
        return 2;
    }
    if (rc == 2) {
        lua_pushnil(L);
        lua_insert(L, -2);
        return 2;
    }
    return 1;
}

static int l_fs_read(lua_State *L)
{
    char path[640];
    file_path(L, 1, path, sizeof(path));
    size_t len;
    char *data = slurp(path, &len);
    if (!data) {
        lua_pushnil(L);
        lua_pushliteral(L, "not found");
        return 2;
    }
    lua_Integer off = luaL_optinteger(L, 2, 0), want = luaL_optinteger(L, 3, (lua_Integer)len - off);
    if (off < 0 || want > FS_READ_MAX || (lua_isnoneornil(L, 3) && (lua_Integer)len - off > FS_READ_MAX)) {
        free(data);
        return luaL_error(L, "fs.read: at most 16 KB at a time");
    }
    if (off > (lua_Integer)len) {
        off = (lua_Integer)len;
    }
    if (want < 0 || off + want > (lua_Integer)len) {
        want = (lua_Integer)len - off;
    }
    lh_charge_steps(L, 500 + (unsigned)(want / 4));
    lua_pushlstring(L, data + off, (size_t)want);
    free(data);
    return 1;
}

static int l_fs_exists(lua_State *L)
{
    char path[640];
    file_path(L, 1, path, sizeof(path));
    FILE *f = fopen(path, "rb");
    lh_charge_steps(L, 500);
    lua_pushboolean(L, f != NULL);
    if (f) {
        fclose(f);
    }
    return 1;
}

// ---------------------------------------------------------------------------
// Boot, as lua_host.c's boot(): the code first, then the libraries

typedef struct {
    const char *storage;
    int seed;
} boot_t;

static int boot(lua_State *L)
{
    boot_t *bt = lua_touserdata(L, 1);
    char path[640];
    snprintf(path, sizeof(path), "%s/main.lua", S.dir);
    size_t len;
    char *src = slurp(path, &len);
    if (!src) {
        return luaL_error(L, "main.lua is missing");
    }
    int rc = luaL_loadbufferx(L, src, len, "=main.lua", "t");
    free(src);
    if (rc != LUA_OK) {
        return lua_error(L);
    }
    lua_setfield(L, LUA_REGISTRYINDEX, "solana.main");
    lua_gc(L, LUA_GCCOLLECT);

    static const luaL_Reg libs[] = {
        {LUA_GNAME, luaopen_base}, {LUA_COLIBNAME, luaopen_coroutine}, {LUA_STRLIBNAME, luaopen_string},
        {LUA_TABLIBNAME, luaopen_table}, {LUA_MATHLIBNAME, luaopen_math}, {LUA_UTF8LIBNAME, luaopen_utf8},
    };
    for (size_t i = 0; i < sizeof(libs) / sizeof(libs[0]); i++) {
        lua_pushcfunction(L, libs[i].func);
        lua_call(L, 0, 1);
        if (i == 0) {
            lua_pop(L, 1);
        } else {
            lua_setglobal(L, libs[i].name);
        }
    }
    static const char *const drop[] = {"dofile", "loadfile", "load", "warn"};
    for (size_t i = 0; i < sizeof(drop) / sizeof(drop[0]); i++) {
        lua_pushnil(L);
        lua_setglobal(L, drop[i]);
    }
    lua_register(L, "collectgarbage", l_collectgarbage);
    lua_register(L, "print", l_print);

    lua_getglobal(L, LUA_MATHLIBNAME);
    lua_getfield(L, -1, "randomseed");
    lua_pushinteger(L, bt->seed);
    lua_pushinteger(L, 7);
    lua_call(L, 2, 0);
    lua_pop(L, 1);

    static const char *const mods[] = {"ui", "led", "timer", "sys", "badge", "mesh", "wifi"};
    for (size_t i = 0; i < sizeof(mods) / sizeof(mods[0]); i++) {
        open_module(L, mods[i]);
    }
    lua_getglobal(L, "ui");
    lua_pushinteger(L, 320);
    lua_setfield(L, -2, "W");
    lua_pushinteger(L, 218);
    lua_setfield(L, -2, "H");
    lua_pop(L, 1);
    lua_getglobal(L, "led");
    lua_pushinteger(L, 8);
    lua_setfield(L, -2, "count");
    lua_pop(L, 1);
    static const luaL_Reg storage[] = {{"get", l_get}, {"set", l_set}, {"remove", l_remove}, {NULL, NULL}};
    luaL_newlib(L, storage);
    lua_setglobal(L, "storage");
    lh_open_json(L);
    static const luaL_Reg fs[] = {{"load", l_fs_load}, {"read", l_fs_read}, {"exists", l_fs_exists}, {NULL, NULL}};
    luaL_newlib(L, fs);
    lua_setglobal(L, "fs");
    lua_newtable(L);
    lua_setfield(L, LUA_REGISTRYINDEX, "sim.loaded");
    lua_register(L, "require", l_require);

    const char *err = lh_json_decode(L, bt->storage, strlen(bt->storage));
    if (err || !lua_istable(L, -1)) {
        lua_settop(L, 1);
        lua_newtable(L);
    }
    S.store_ref = luaL_ref(L, LUA_REGISTRYINDEX);

    lua_gc(L, LUA_GCCOLLECT);
    lua_gc(L, LUA_GCGEN);
    lua_gc(L, LUA_GCPARAM, LUA_GCPMINORMUL, 10);
    return 0;
}

static int run_main(lua_State *L)
{
    lua_getfield(L, LUA_REGISTRYINDEX, "solana.main");
    lua_pushnil(L);
    lua_setfield(L, LUA_REGISTRYINDEX, "solana.main");
    lua_call(L, 0, 0);
    return 0;
}

// ---------------------------------------------------------------------------
// Callbacks from the driver

typedef struct {
    const char *fn;
    int ref;
    const char *args;
    int n;
    bool nofn;
    sb_t ret;
} call_t;

static int do_call(lua_State *L)
{
    call_t *c = lua_touserdata(L, 1);
    lua_settop(L, 0);
    if (c->fn) {
        lua_getglobal(L, c->fn);
    } else {
        lua_rawgeti(L, LUA_REGISTRYINDEX, c->ref);
    }
    if (!lua_isfunction(L, -1)) {
        c->nofn = true;
        return 0;
    }
    if (c->n > 0) {
        const char *err = lh_json_decode(L, c->args, strlen(c->args));
        if (err) {
            return luaL_error(L, "sim: bad args (%s)", err);
        }
        luaL_checkstack(L, c->n + 2, "too many arguments");
        for (int i = 1; i <= c->n; i++) {
            lua_rawgeti(L, 2, i);
        }
        lua_remove(L, 2);
    }
    lua_call(L, c->n, 1);
    emit(L, -1, &c->ret, 0);
    return 0;
}

static sb_t s_done;

static void finish(int rc, const call_t *c)
{
    lua_State *L = S.L;
    if (S.steps > S.steps_peak) {
        S.steps_peak = S.steps;
    }
    s_done.len = 0;
    bool ok = rc == LUA_OK && !S.killed;
    sb_printf(&s_done, "{\"done\":true,\"ok\":%s", ok ? "true" : "false");
    if (!ok) {
        const char *msg = L && lua_type(L, -1) == LUA_TSTRING ? lua_tostring(L, -1) : "(error is not a string)";
        bool oom = rc == LUA_ERRMEM || (S.mem_hit && !strcmp(msg, "not enough memory"));
        sb_puts(&s_done, ",\"err\":");
        sb_str(&s_done, msg, strlen(msg));
        sb_printf(&s_done, ",\"oom\":%s,\"killed\":%s", oom ? "true" : "false", S.killed ? "true" : "false");
    }
    if (c && c->ret.len) {
        sb_puts(&s_done, ",\"ret\":");
        sb_put(&s_done, c->ret.p, c->ret.len);
    }
    sb_printf(&s_done, ",\"nofn\":%s,\"mem\":%u,\"peak\":%u,\"steps\":%u,\"steps_peak\":%u}",
              c && c->nofn ? "true" : "false", (unsigned)S.mem, (unsigned)S.peak, S.steps, S.steps_peak);
    if (L) {
        lua_settop(L, 0);
    }
    send_line(&s_done);
}

static int callback(lua_CFunction fn, void *arg)
{
    lua_State *L = S.L;
    S.steps = 0;
    S.killed = false;
    S.mem_hit = false;
    lua_sethook(L, step_hook, LUA_MASKCOUNT, HOOK_EVERY);
    lua_pushcfunction(L, fn);
    lua_pushlightuserdata(L, arg);
    return lua_pcall(L, 1, 0, 0);
}

static const char *ctl_str(const char *key)
{
    lua_getfield(C, 1, key);
    return lua_tostring(C, -1);
}

static int ctl_int(const char *key)
{
    lua_getfield(C, 1, key);
    return (int)lua_tointeger(C, -1);
}

int main(void)
{
    C = lua_newstate(ctl_alloc, NULL, 1);
    for (;;) {
        char *line = read_line();
        lua_settop(C, 0);
        if (lh_json_decode(C, line, strlen(line)) || !lua_istable(C, 1)) {
            fprintf(stderr, "badgesim: bad line: %.80s\n", line);
            return 2;
        }
        const char *op = ctl_str("op");
        if (!op) {
            fprintf(stderr, "badgesim: no op: %.80s\n", line);
            return 2;
        }
        if (!strcmp(op, "boot")) {
            snprintf(S.dir, sizeof(S.dir), "%s", ctl_str("dir"));
            boot_t bt = { .storage = ctl_str("storage"), .seed = ctl_int("seed") };
            S.L = lua_newstate(app_alloc, NULL, (unsigned)ctl_int("seed"));
            lua_sethook(S.L, step_hook, LUA_MASKCOUNT, HOOK_EVERY);
            lua_pushcfunction(S.L, boot);
            lua_pushlightuserdata(S.L, &bt);
            int rc = lua_pcall(S.L, 1, 0, 0);
            if (rc == LUA_OK) {
                rc = callback(run_main, NULL);
            }
            finish(rc, NULL);
        } else if (!strcmp(op, "call") || !strcmp(op, "callref")) {
            call_t c = { .fn = op[4] ? NULL : ctl_str("fn"), .ref = ctl_int("ref"), .args = ctl_str("args"),
                         .n = ctl_int("n") };
            bool unref = (lua_getfield(C, 1, "unref"), lua_toboolean(C, -1));
            if (!c.args) {
                c.args = "[]";
            }
            int rc = callback(do_call, &c);
            if (unref) {
                luaL_unref(S.L, LUA_REGISTRYINDEX, c.ref);
            }
            finish(rc, &c);
            free(c.ret.p);
        } else if (!strcmp(op, "unref")) {
            luaL_unref(S.L, LUA_REGISTRYINDEX, ctl_int("ref"));
        } else if (!strcmp(op, "close")) {
            if (S.L) {
                call_t c = { .fn = "on_exit", .args = "[]" };
                int rc = callback(do_call, &c);
                finish(rc, &c);
                free(c.ret.p);
                lua_close(S.L);
                S.L = NULL;
            }
            return 0;
        } else {
            fprintf(stderr, "badgesim: unknown op %s\n", op);
            return 2;
        }
    }
}
