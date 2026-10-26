/*
 * NeonMiner.exe — автономный Windows-клиент майнинга Monero (XMR).
 *
 * Один файл, без зависимостей:
 *   - встроенный HTTP-сервер на 127.0.0.1 (панель зашита в бинарник);
 *   - полная проверка Monero-адреса (Base58 + Keccak-256 checksum);
 *   - генерация config.json для официального XMRig;
 *   - загрузка релиза XMRig через WinHTTP + обязательная проверка SHA256;
 *   - запуск/остановка майнера ТОЛЬКО по явной команде пользователя.
 *
 * Собирается кросс-компиляцией: zig cc -target x86_64-windows-gnu.
 * Тот же исходник собирается под Linux (--selftest / --host 0.0.0.0) для тестов.
 *
 * Никаких скрытых автозапусков, служб, реестра и чужих машин.
 */

#ifndef _WIN32
# ifndef _POSIX_C_SOURCE
#  define _POSIX_C_SOURCE 200809L
# endif
#endif

#ifdef _WIN32
# define WIN32_LEAN_AND_MEAN
# include <winsock2.h>
# include <ws2tcpip.h>
# include <windows.h>
# include <winhttp.h>
# include <shellapi.h>
# include <io.h>
# define popen  _popen
# define pclose _pclose
typedef SOCKET sock_t;
#else
# include <sys/types.h>
# include <sys/socket.h>
# include <netinet/in.h>
# include <arpa/inet.h>
# include <netdb.h>
# include <unistd.h>
# include <signal.h>
# include <errno.h>
# include <sys/stat.h>
# include <sys/time.h>
typedef int sock_t;
# define INVALID_SOCKET (-1)
# define SOCKET_ERROR   (-1)
# define closesocket    close
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <ctype.h>
#include <time.h>
#include <math.h>

#include "embed.h"

#define APP_VERSION "1.1.0"
#define DEFAULT_PORT 8765
#define PICO 1000000000000.0

#if defined(__GNUC__) || defined(__clang__)
# define NM_MAYBE_UNUSED __attribute__((unused))
#else
# define NM_MAYBE_UNUSED
#endif

/* ============================ динамический буфер ============================ */
typedef struct { char *p; size_t len, cap; } Buf;

static void buf_init(Buf *b) { b->cap = 4096; b->len = 0; b->p = (char*)malloc(b->cap); b->p[0] = 0; }
static void buf_free(Buf *b) { free(b->p); b->p = NULL; b->len = b->cap = 0; }
static void buf_reserve(Buf *b, size_t extra) {
    if (b->len + extra + 1 <= b->cap) return;
    while (b->len + extra + 1 > b->cap) b->cap *= 2;
    b->p = (char*)realloc(b->p, b->cap);
}
static void buf_puts(Buf *b, const char *s) { size_t n = strlen(s); buf_reserve(b, n); memcpy(b->p + b->len, s, n); b->len += n; b->p[b->len] = 0; }
static void buf_putc(Buf *b, char c) { buf_reserve(b, 1); b->p[b->len++] = c; b->p[b->len] = 0; }
static void buf_printf(Buf *b, const char *fmt, ...) {
    char tmp[8192]; va_list ap; va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap); va_end(ap);
    if (n < 0) return;
    if ((size_t)n >= sizeof(tmp)) n = (int)sizeof(tmp) - 1;
    buf_reserve(b, (size_t)n); memcpy(b->p + b->len, tmp, (size_t)n); b->len += (size_t)n; b->p[b->len] = 0;
}
static void json_escape(Buf *b, const char *s) {
    if (!s) return;
    for (const unsigned char *p = (const unsigned char*)s; *p; ++p) {
        switch (*p) {
            case '"':  buf_puts(b, "\\\""); break;
            case '\\': buf_puts(b, "\\\\"); break;
            case '\n': buf_puts(b, "\\n");  break;
            case '\r': buf_puts(b, "\\r");  break;
            case '\t': buf_puts(b, "\\t");  break;
            default:
                if (*p < 0x20) buf_printf(b, "\\u%04x", *p);
                else buf_putc(b, (char)*p);
        }
    }
}

/* ============================ парсинг JSON (наш собственный плоский формат) ============================ */
static const char *find_key(const char *json, const char *key) {
    char needle[128]; snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *p = strstr(json, needle);
    if (!p) return NULL;
    p += strlen(needle);
    while (*p && isspace((unsigned char)*p)) ++p;
    if (*p != ':') return NULL;
    ++p;
    while (*p && isspace((unsigned char)*p)) ++p;
    return p;
}
static int json_str(const char *json, const char *key, char *out, size_t n) {
    const char *p = find_key(json, key);
    if (!p || *p != '"') return 0;
    ++p; size_t i = 0;
    while (*p && *p != '"' && i + 1 < n) {
        if (*p == '\\' && p[1]) {
            ++p;
            switch (*p) { case 'n': out[i++]='\n'; break; case 'r': out[i++]='\r'; break;
                          case 't': out[i++]='\t'; break; default: out[i++]=*p; break; }
            ++p;
        } else out[i++] = *p++;
    }
    out[i] = 0; return 1;
}
static double json_num(const char *json, const char *key, double def) {
    const char *p = find_key(json, key);
    if (!p || !(*p == '-' || *p == '+' || *p == '.' || isdigit((unsigned char)*p))) return def;
    char *end; double v = strtod(p, &end);
    return (end == p) ? def : v;
}
static int json_bool(const char *json, const char *key) {
    const char *p = find_key(json, key);
    return p && !strncmp(p, "true", 4);
}
static void url_decode(char *s) {
    char *o = s;
    for (char *p = s; *p; ++p) {
        if (*p == '+') *o++ = ' ';
        else if (*p == '%' && isxdigit((unsigned char)p[1]) && isxdigit((unsigned char)p[2])) {
            char h[3] = { p[1], p[2], 0 }; *o++ = (char)strtol(h, NULL, 16); p += 2;
        } else *o++ = *p;
    }
    *o = 0;
}
static void query_value(const char *query, const char *key, char *out, size_t n) {
    out[0] = 0;
    if (!query) return;
    char buf[4096]; snprintf(buf, sizeof(buf), "%s", query);
    for (char *tok = strtok(buf, "&"); tok; tok = strtok(NULL, "&")) {
        char *eq = strchr(tok, '='); if (!eq) continue;
        *eq = 0; if (strcmp(tok, key)) continue;
        snprintf(out, n, "%s", eq + 1); url_decode(out); return;
    }
}

/* ============================ SHA-256 ============================ */
typedef struct { uint32_t h[8]; uint64_t len; unsigned char block[64]; size_t used; } Sha256;
static const uint32_t K256[64] = {
 0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
 0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
 0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
 0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
 0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
 0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
 0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
 0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2 };
#define ROR32(x,n) (((x) >> (n)) | ((x) << (32-(n))))
static void sha_compress(Sha256 *c, const unsigned char *b) {
    uint32_t w[64]; int i;
    for (i = 0; i < 16; ++i) w[i] = ((uint32_t)b[i*4]<<24)|((uint32_t)b[i*4+1]<<16)|((uint32_t)b[i*4+2]<<8)|b[i*4+3];
    for (; i < 64; ++i) { uint32_t s0=ROR32(w[i-15],7)^ROR32(w[i-15],18)^(w[i-15]>>3);
                          uint32_t s1=ROR32(w[i-2],17)^ROR32(w[i-2],19)^(w[i-2]>>10);
                          w[i]=w[i-16]+s0+w[i-7]+s1; }
    uint32_t a=c->h[0],bb=c->h[1],cc=c->h[2],d=c->h[3],e=c->h[4],f=c->h[5],g=c->h[6],h=c->h[7];
    for (i = 0; i < 64; ++i) {
        uint32_t S1=ROR32(e,6)^ROR32(e,11)^ROR32(e,25), ch=(e&f)^((~e)&g);
        uint32_t t1=h+S1+ch+K256[i]+w[i];
        uint32_t S0=ROR32(a,2)^ROR32(a,13)^ROR32(a,22), mj=(a&bb)^(a&cc)^(bb&cc);
        uint32_t t2=S0+mj;
        h=g; g=f; f=e; e=d+t1; d=cc; cc=bb; bb=a; a=t1+t2;
    }
    c->h[0]+=a;c->h[1]+=bb;c->h[2]+=cc;c->h[3]+=d;c->h[4]+=e;c->h[5]+=f;c->h[6]+=g;c->h[7]+=h;
}
static void sha_init(Sha256 *c) {
    static const uint32_t H[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    memcpy(c->h,H,sizeof(H)); c->len=0; c->used=0;
}
static void sha_update(Sha256 *c, const unsigned char *d, size_t n) {
    c->len += n;
    while (n) { size_t take = 64 - c->used; if (take > n) take = n;
        memcpy(c->block + c->used, d, take); c->used += take; d += take; n -= take;
        if (c->used == 64) { sha_compress(c, c->block); c->used = 0; } }
}
static void sha_final(Sha256 *c, unsigned char out[32]) {
    uint64_t bits = c->len * 8; c->block[c->used++] = 0x80;
    if (c->used > 56) { memset(c->block+c->used,0,64-c->used); sha_compress(c,c->block); c->used=0; }
    memset(c->block+c->used,0,56-c->used);
    for (int i=0;i<8;++i) c->block[56+i]=(unsigned char)(bits>>(56-8*i));
    sha_compress(c,c->block);
    for (int i=0;i<8;++i) { out[i*4]=(unsigned char)(c->h[i]>>24); out[i*4+1]=(unsigned char)(c->h[i]>>16);
                            out[i*4+2]=(unsigned char)(c->h[i]>>8); out[i*4+3]=(unsigned char)c->h[i]; }
}
static void sha_hex(const unsigned char d[32], char out[65]) {
    static const char *hx="0123456789abcdef";
    for (int i=0;i<32;++i) { out[i*2]=hx[d[i]>>4]; out[i*2+1]=hx[d[i]&15]; } out[64]=0;
}
NM_MAYBE_UNUSED static int sha256_file(const char *path, char hex[65], char *err, size_t errn) {
    FILE *f = fopen(path, "rb"); if (!f) { snprintf(err,errn,"не открыть файл %s",path); return 0; }
    Sha256 c; sha_init(&c); unsigned char b[65536]; size_t n;
    while ((n=fread(b,1,sizeof(b),f))>0) sha_update(&c,b,n);
    fclose(f); unsigned char d[32]; sha_final(&c,d); sha_hex(d,hex); return 1;
}
static int sha256_bytes(const unsigned char *data, size_t n, char hex[65]) {
    Sha256 c; sha_init(&c); sha_update(&c,data,n); unsigned char d[32]; sha_final(&c,d); sha_hex(d,hex); return 1;
}

/* ============================ Keccak-256 (НЕ SHA3) ============================ */
static const uint64_t KRC[24] = {
 0x0000000000000001ULL,0x0000000000008082ULL,0x800000000000808aULL,0x8000000080008000ULL,
 0x000000000000808bULL,0x0000000080000001ULL,0x8000000080008081ULL,0x8000000000008009ULL,
 0x000000000000008aULL,0x0000000000000088ULL,0x0000000080008009ULL,0x000000008000000aULL,
 0x000000008000808bULL,0x800000000000008bULL,0x8000000000008089ULL,0x8000000000008003ULL,
 0x8000000000008002ULL,0x8000000000000080ULL,0x000000000000800aULL,0x800000008000000aULL,
 0x8000000080008081ULL,0x8000000000008080ULL,0x0000000080000001ULL,0x8000000080008008ULL };
static const int KROT[5][5] = {
 {0,36,3,41,18},{1,44,10,45,2},{62,6,43,15,61},{28,55,25,21,56},{27,20,39,8,14} };
#define ROL64(x,n) (((n)==0)?(x):(((x)<<(n))|((x)>>(64-(n)))))
static void keccak_f(uint64_t a[5][5]) {
    for (int rnd=0; rnd<24; ++rnd) {
        uint64_t c[5],d[5],b[5][5];
        for (int x=0;x<5;++x) c[x]=a[x][0]^a[x][1]^a[x][2]^a[x][3]^a[x][4];
        for (int x=0;x<5;++x) d[x]=c[(x+4)%5]^ROL64(c[(x+1)%5],1);
        for (int x=0;x<5;++x) for (int y=0;y<5;++y) a[x][y]^=d[x];
        for (int x=0;x<5;++x) for (int y=0;y<5;++y) b[y][(2*x+3*y)%5]=ROL64(a[x][y],KROT[x][y]);
        for (int x=0;x<5;++x) for (int y=0;y<5;++y) a[x][y]=b[x][y]^((~b[(x+1)%5][y])&b[(x+2)%5][y]);
        a[0][0]^=KRC[rnd];
    }
}
static void keccak256(const unsigned char *data, size_t len, unsigned char out[32]) {
    const size_t rate=136; uint64_t a[5][5]; memset(a,0,sizeof(a));
    unsigned char pad[4096]; size_t n = len;
    if (n+1 > sizeof(pad)) n = sizeof(pad)-1; /* нам нужны только короткие строки */
    memcpy(pad,data,n); pad[n++]=0x01;
    while (n % rate) pad[n++]=0x00;
    pad[n-1] |= 0x80;
    for (size_t off=0; off<n; off+=rate) {
        for (int i=0;i<(int)(rate/8);++i) {
            uint64_t lane=0; for (int j=0;j<8;++j) lane |= (uint64_t)pad[off+i*8+j] << (8*j);
            a[i%5][i/5] ^= lane;
        }
        keccak_f(a);
    }
    for (int i=0;i<4;++i) for (int j=0;j<8;++j) out[i*8+j]=(unsigned char)(a[i%5][i/5]>>(8*j));
}

/* ============================ Monero-адрес ============================ */
static const char *B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
static const int ENC_BLOCK[9] = {0,2,3,5,6,7,9,10,11};
typedef struct {
    int valid, checksum_ok, length;
    char address[256], network[16], kind[16], reason[256], warning[256];
} AddrInfo;
static int b58idx(char c) { const char *p = strchr(B58,c); return p ? (int)(p-B58) : -1; }
/* Кодирование Monero Base58 блоками по 8 байт. Нужно только для selftest:
   генерирует синтетический адрес, чтобы в бинарник не попадал настоящий. */
static void b58_encode(const unsigned char *data, size_t len, char *out, size_t outcap) {
    size_t o=0, pos=0;
    while (pos < len && o + 12 < outcap) {
        size_t rem=len-pos, dec=rem>=8?8:rem;
        size_t enc=(dec==8)?11:(size_t)ENC_BLOCK[dec];
        uint64_t num=0; for(size_t i=0;i<dec;++i) num=(num<<8)|data[pos+i];
        char tmp[16];
        for(size_t i=0;i<enc;++i){ tmp[enc-1-i]=B58[num%58]; num/=58; }
        memcpy(out+o,tmp,enc); o+=enc; pos+=dec;
    }
    out[o]=0;
}
static int decode_b58(const char *s, unsigned char *out, size_t outcap, size_t *outlen) {
    size_t pos=0, size=strlen(s), olen=0;
    while (pos < size) {
        size_t rem = size-pos, enc, dec;
        if (rem >= 11) { enc=11; dec=8; }
        else { enc=rem; dec=0; for (int i=0;i<9;++i) if (ENC_BLOCK[i]==(int)enc) dec=(size_t)i; if (!dec) return 0; }
        uint64_t num=0;
        for (size_t i=0;i<enc;++i) { int v=b58idx(s[pos+i]); if (v<0) return 0; num=num*58+(uint64_t)v; }
        /* проверка переполнения */
        if (dec < 8 && num >= (1ULL << (8*dec))) return 0;
        if (olen + dec > outcap) return 0;
        for (size_t i=0;i<dec;++i) out[olen+i] = (unsigned char)(num >> (8*(dec-1-i)));
        olen += dec; pos += enc;
    }
    *outlen=olen; return 1;
}
static void validate_address(const char *raw, AddrInfo *i) {
    memset(i,0,sizeof(*i));
    char val[512]; snprintf(val,sizeof(val),"%s",raw?raw:"");
    /* убираем пробелы по краям */
    while (*val && isspace((unsigned char)*val)) memmove(val,val+1,strlen(val));
    size_t L=strlen(val); while (L && isspace((unsigned char)val[L-1])) val[--L]=0;
    i->length=(int)L;
    if (!L) { snprintf(i->reason,sizeof(i->reason),"Вставь свой XMR-адрес."); return; }
    /* суффикс .paymentID */
    char *dot = strchr(val,'.'); int has_pid = 0;
    if (dot) { *dot=0; has_pid=1; L=strlen(val); i->length=(int)L; }
    if (L != 95 && L != 106) { snprintf(i->reason,sizeof(i->reason),"Длина адреса %zu симв., нужно 95 или 106.",L); return; }
    for (size_t k=0;k<L;++k) if (b58idx(val[k])<0) { snprintf(i->reason,sizeof(i->reason),"Недопустимый символ в адресе."); return; }
    unsigned char dec[128]; size_t dlen=0;
    if (!decode_b58(val,dec,sizeof(dec),&dlen) || (dlen!=69 && dlen!=77)) {
        snprintf(i->reason,sizeof(i->reason),"Не удалось декодировать Base58."); return; }
    unsigned char kk[32]; keccak256(dec,dlen-4,kk);
    if (memcmp(kk,dec+dlen-4,4)!=0) {
        i->checksum_ok=0;
        snprintf(i->reason,sizeof(i->reason),"Контрольная сумма не совпадает — в адресе опечатка! Монеты уйдут в никуда."); return; }
    i->checksum_ok=1;
    unsigned char tag=dec[0];
    if (tag==18)      { snprintf(i->network,sizeof(i->network),"mainnet"); snprintf(i->kind,sizeof(i->kind),"primary"); }
    else if (tag==42) { snprintf(i->network,sizeof(i->network),"mainnet"); snprintf(i->kind,sizeof(i->kind),"subaddress"); }
    else if (tag==19) { snprintf(i->network,sizeof(i->network),"mainnet"); snprintf(i->kind,sizeof(i->kind),"integrated"); }
    else if (tag==24||tag==25||tag==35||tag==53||tag==54||tag==63) {
        snprintf(i->network,sizeof(i->network),"testnet");
        snprintf(i->reason,sizeof(i->reason),"Это тестовый адрес. Пулы принимают только mainnet."); return;
    } else { snprintf(i->reason,sizeof(i->reason),"Неизвестный префикс сети (%u).",tag); return; }
    if (!strcmp(i->kind,"integrated") && dlen!=77) { snprintf(i->reason,sizeof(i->reason),"Интегрированный адрес должен быть 106 симв."); return; }
    if (strcmp(i->kind,"integrated") && dlen!=69) { snprintf(i->reason,sizeof(i->reason),"Обычный адрес должен быть 95 симв."); return; }
    i->valid=1; snprintf(i->address,sizeof(i->address),"%s",val);
    if (!strcmp(i->kind,"integrated"))
        snprintf(i->warning,sizeof(i->warning),"Интегрированный адрес — часто адрес биржи. Возьми обычный адрес из своего кошелька.");
    else if (has_pid)
        snprintf(i->warning,sizeof(i->warning),"Суффикс .paymentID устарел и игнорируется пулами. Используй просто адрес.");
    else
        snprintf(i->warning,sizeof(i->warning),"Не используй депозитный адрес биржи — только свой кошелёк с приватными ключами.");
}
static void addr_json(Buf *b, const AddrInfo *i) {
    buf_printf(b,"{\"valid\":%s,\"address\":\"",i->valid?"true":"false"); json_escape(b,i->address);
    buf_printf(b,"\",\"network\":\""); json_escape(b,i->network);
    buf_printf(b,"\",\"kind\":\""); json_escape(b,i->kind);
    buf_printf(b,"\",\"length\":%d,\"checksum_ok\":%s,\"reason\":\"",i->length,i->checksum_ok?"true":"false");
    json_escape(b,i->reason); buf_printf(b,"\",\"warning\":\""); json_escape(b,i->warning); buf_puts(b,"\"}");
}

/* ============================ пулы и конфиг ============================ */
static const PoolDef *pool_by_id(const char *id) {
    for (int k=0;k<N_POOLS;++k) if (!strcmp(POOL_DEFS[k].id,id)) return &POOL_DEFS[k];
    return &POOL_DEFS[0];
}
static const PortDef *port_of(const PoolDef *p, int port) {
    for (int k=0;k<p->nports;++k) if (p->ports[k].port==port) return &p->ports[k];
    return &p->ports[0];
}
static const char *host_for(const PoolDef *p, int port) {
    if (!strcmp(p->id,"c3pool") && port==33333) return "mine.c3pool.com";
    return p->host;
}
static void sanitize_worker(const char *in, char *out, size_t n) {
    size_t j=0; for (size_t k=0; in[k] && j+1<n && j<32; ++k) {
        unsigned char c=(unsigned char)in[k];
        out[j++] = (isalnum(c)||c=='.'||c=='_'||c=='-') ? (char)c : '-';
    }
    if (!j) { snprintf(out,n,"rig-01"); return; }
    out[j]=0;
}
static int build_config_json(Buf *b, const char *wallet, const PoolDef *pool, int port,
                             const char *worker, int threads, int donate, int diff) {
    const PortDef *pp = port_of(pool,port);
    const char *host = host_for(pool,port);
    int tls = pp->tls;
    char user[600];
    if (diff>0) snprintf(user,sizeof(user),"%s+%d",wallet,diff); else snprintf(user,sizeof(user),"%s",wallet);
    buf_puts(b,"{\n    \"autosave\": true,\n    \"background\": false,\n    \"colors\": true,\n");
    buf_printf(b,"    \"donate-level\": %d,\n",donate);
    buf_puts(b,"    \"log-file\": null,\n    \"print-time\": 60,\n    \"health-print-time\": 60,\n");
    buf_puts(b,"    \"retries\": 5,\n    \"retry-pause\": 5,\n    \"syslog\": false,\n    \"watch\": true,\n");
    buf_puts(b,"    \"api\": {\"id\": null, \"worker-id\": null},\n");
    buf_puts(b,"    \"http\": {\"enabled\": true, \"host\": \"127.0.0.1\", \"port\": 8080, \"access-token\": null, \"restricted\": true},\n");
    buf_puts(b,"    \"randomx\": {\"init\": -1, \"numa\": true},\n");
    buf_printf(b,"    \"cpu\": {\"enabled\": true, \"huge-pages\": true, \"hw-aes\": null, \"priority\": 2, \"max-threads-hint\": %d, \"asm\": true, \"yield\": true},\n",threads);
    buf_puts(b,"    \"opencl\": false,\n    \"cuda\": false,\n    \"pools\": [\n");
    buf_printf(b,"      {\"algo\": null, \"coin\": %s, \"url\": \"%s:%d\", \"user\": \"%s\", \"pass\": \"%s\", \"rig-id\": \"%s\", \"nicehash\": false, \"keepalive\": true, \"enabled\": true, \"tls\": %s, \"tls-fingerprint\": null, \"daemon\": false}",
        strcmp(pool->id,"moneroocean") ? "\"monero\"" : "null", host, port, user, worker, worker, tls?"true":"false");
    buf_puts(b,",\n");
    if (pool->failover_port)
        buf_printf(b,"      {\"algo\": null, \"coin\": %s, \"url\": \"%s:%d\", \"user\": \"%s\", \"pass\": \"%s\", \"rig-id\": \"%s\", \"nicehash\": false, \"keepalive\": true, \"enabled\": true, \"tls\": %s, \"daemon\": false}\n",
            strcmp(pool->id,"moneroocean") ? "\"monero\"" : "null", pool->failover_host, pool->failover_port,
            user, worker, worker, pool->failover_tls?"true":"false");
    else if (!strcmp(pool->id,"c3pool"))
        buf_printf(b,"      {\"algo\": null, \"coin\": \"monero\", \"url\": \"auto.c3pool.org:80\", \"user\": \"%s\", \"pass\": \"%s\", \"rig-id\": \"%s\", \"nicehash\": false, \"keepalive\": true, \"enabled\": true, \"tls\": false, \"daemon\": false}\n",
            user,worker,worker);
    else
        buf_printf(b,"      {\"algo\": null, \"coin\": \"monero\", \"url\": \"%s:%d\", \"user\": \"%s\", \"pass\": \"%s\", \"rig-id\": \"%s\", \"keepalive\": true, \"enabled\": true, \"tls\": %s, \"daemon\": false}\n",
            pool->host, pool->ports[pool->nports-1].port, user,worker,worker,
            pool->ports[pool->nports-1].tls?"true":"false");
    buf_puts(b,"    ]\n}\n");
    return tls;
}
static void build_cmd_windows(Buf *b, const char *wallet, const PoolDef *pool, int port,
                              const char *worker, int threads, int donate, int diff) {
    const PortDef *pp=port_of(pool,port);
    const char *host=host_for(pool,port);
    buf_printf(b,"xmrig.exe -o %s:%d -u %s -p \"%s\"",host,port,wallet,worker);
    if (strcmp(pool->id,"moneroocean")) buf_puts(b," --coin=monero");
    buf_printf(b," --cpu-max-threads-hint=%d --donate-level=%d%s",threads,donate,pp->tls?" --tls":"");
    (void)diff;
}

/* ============================ калькулятор ============================ */
static void calc_json(Buf *b, const char *body) {
    double hs=json_num(body,"hashrate_hs",0);
    double net=json_num(body,"network_hashrate_hs",0); if (net<=0) net=6.84e9;
    double reward=json_num(body,"block_reward_xmr",0); if (reward<=0) reward=0.6035;
    double fee=json_num(body,"fee_pct",0);
    double thr=json_num(body,"threshold_xmr",0.001); if (thr<=0) thr=0.001;
    double price=json_num(body,"price_eur",0); if (price<=0) price=530.0;
    double watts=json_num(body,"power_watts",65);
    double kwh=json_num(body,"eur_per_kwh",0.36);
    double share = hs>0 ? hs/net : 0;
    double gross = share*720.0*reward;
    double xmr = gross*(1.0-fee/100.0);
    double rev = xmr*price;
    double kwhday = watts*24.0/1000.0;
    double cost = kwhday*kwh;
    double nett = rev-cost;
    double days = xmr>0 ? thr/xmr : -1;
    buf_printf(b,"{\"hashrate_hs\":%.4f,\"network_hashrate_hs\":%.2f,\"network_share\":%.12f,"
        "\"block_reward_xmr\":%.8f,\"fee_pct\":%.4f,\"gross_xmr_per_day\":%.12f,"
        "\"xmr_per_day\":%.12f,\"gross_eur_per_day\":%.8f,\"eur_per_day\":%.8f,"
        "\"eur_per_month\":%.8f,\"power_kwh_per_day\":%.6f,\"power_cost_eur_per_day\":%.8f,"
        "\"net_eur_per_day\":%.8f,\"net_eur_per_month\":%.8f,\"price_eur\":%.4f,"
        "\"eur_per_kwh\":%.4f,\"power_watts\":%.2f,\"days_to_payout\":%.6f,"
        "\"threshold_xmr\":%.8f,\"profitable\":%s}",
        hs,net,share,reward,fee,gross,xmr,gross*price,rev,rev*30.0,kwhday,cost,nett,nett*30.0,
        price,kwh,watts,days,thr,nett>0?"true":"false");
}

/* ============================ глобальное состояние майнера ============================ */
#ifdef _WIN32
static HANDLE g_miner = NULL;
static DWORD  g_pid = 0;
#else
static int g_sim_running = 0;
#endif
static char g_progress[512] = "";
static char g_start_error[512] = "";
static char g_workdir[1024] = "";
static char g_logpath[1200] = "";
static char g_configpath[1200] = "";
static char g_binzip[1200] = "";
static int  g_simulate = 0;

static void set_progress(const char *fmt, ...) {
    va_list ap; va_start(ap,fmt); vsnprintf(g_progress,sizeof(g_progress),fmt,ap); va_end(ap);
    printf("  · %s\n",g_progress); fflush(stdout);
}
NM_MAYBE_UNUSED static int file_exists(const char *p) { FILE *f=fopen(p,"rb"); if(f){fclose(f);return 1;} return 0; }
static void ensure_dir(const char *p) {
#ifdef _WIN32
    CreateDirectoryA(p,NULL);
#else
    mkdir(p,0755);
#endif
}
static void join_path(char *out,size_t n,const char *a,const char *b) {
    snprintf(out,n,"%s%c%s",a,
#ifdef _WIN32
        '\\'
#else
        '/'
#endif
        ,b);
}

/* ============================ HTTP (WinHTTP / curl) ============================ */
#ifdef _WIN32
static wchar_t *utf8_to_wide(const char *s) {
    int n = MultiByteToWideChar(CP_UTF8,0,s,-1,NULL,0);
    wchar_t *w = (wchar_t*)calloc((size_t)n+1,sizeof(wchar_t));
    if (w) MultiByteToWideChar(CP_UTF8,0,s,-1,w,n);
    return w;
}
/* Возвращает 1 при успехе. Если text != NULL — копит ответ в буфер,
   иначе пишет байты в файл dest. */
static int winhttp_get(const char *url, Buf *text, const char *dest, char *err, size_t errn) {
    err[0]=0;
    const char *s=url;
    if (!strncmp(s,"https://",8)) s+=8; else if (!strncmp(s,"http://",7)) s+=7;
    const char *slash=strchr(s,'/');
    char host[256], path[1024];
    if (!slash) { snprintf(host,sizeof(host),"%s",s); snprintf(path,sizeof(path),"/"); }
    else { size_t hl=(size_t)(slash-s); if (hl>=sizeof(host)) hl=sizeof(host)-1;
           memcpy(host,s,hl); host[hl]=0; snprintf(path,sizeof(path),"%s",slash); }
    wchar_t *whost=utf8_to_wide(host), *wpath=utf8_to_wide(path);
    HINTERNET ses=WinHttpOpen(L"NeonMiner/1.1 (+github.com/xmrig/xmrig)",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,WINHTTP_NO_PROXY_NAME,WINHTTP_NO_PROXY_BYPASS,0);
    if (!ses) { snprintf(err,errn,"WinHttpOpen: %lu",GetLastError()); free(whost);free(wpath); return 0; }
    WinHttpSetTimeouts(ses,15000,15000,20000,60000);
    HINTERNET con=WinHttpConnect(ses,whost,INTERNET_DEFAULT_HTTPS_PORT,0);
    HINTERNET req=con?WinHttpOpenRequest(con,L"GET",wpath,NULL,WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,WINHTTP_FLAG_SECURE):NULL;
    BOOL ok = req && WinHttpSendRequest(req,WINHTTP_NO_ADDITIONAL_HEADERS,0,
        WINHTTP_NO_REQUEST_DATA,0,0,0) && WinHttpReceiveResponse(req,NULL);
    if (ok) {
        DWORD code=0, sz=sizeof(code);
        WinHttpQueryHeaders(req,WINHTTP_QUERY_STATUS_CODE|WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX,&code,&sz,WINHTTP_NO_HEADER_INDEX);
        if (code<200||code>=300) { snprintf(err,errn,"HTTP %lu от %s",code,host); ok=FALSE; }
    } else if (req) snprintf(err,errn,"WinHttp send/recv: %lu",GetLastError());
    FILE *f=NULL;
    if (ok && dest) { f=fopen(dest,"wb"); if(!f){snprintf(err,errn,"не создать %s",dest);ok=FALSE;} }
    if (ok) {
        DWORD avail=0;
        while (WinHttpQueryDataAvailable(req,&avail) && avail>0) {
            unsigned char chunk[16384]; DWORD got=0;
            DWORD want = avail>sizeof(chunk)?(DWORD)sizeof(chunk):avail;
            if (!WinHttpReadData(req,chunk,want,&got) || !got) break;
            if (text) buf_reserve(text,got), memcpy(text->p+text->len,chunk,got), text->len+=got, text->p[text->len]=0;
            if (f) fwrite(chunk,1,got,f);
            if (got<want) break;
        }
    }
    if (f) fclose(f);
    if (req) WinHttpCloseHandle(req);
    if (con) WinHttpCloseHandle(con);
    WinHttpCloseHandle(ses); free(whost); free(wpath);
    return ok ? 1 : 0;
}
#endif
/* Кроссплатформенный fallback через curl (и единственный вариант под Linux). */
static int curl_get(const char *url, Buf *text, const char *dest, char *err, size_t errn) {
    char cmd[4096];
    if (dest) snprintf(cmd,sizeof(cmd),"curl -fsSL --connect-timeout 15 --max-time 90 -o \"%s\" \"%s\" 2>&1",dest,url);
    else      snprintf(cmd,sizeof(cmd),"curl -fsSL --connect-timeout 15 --max-time 30 \"%s\" 2>&1",url);
    FILE *p=popen(cmd,"r"); if(!p){snprintf(err,errn,"не запустить curl");return 0;}
    char b[8192]; size_t n; int rc;
    while ((n=fread(b,1,sizeof(b),p))>0) { if(text){buf_reserve(text,n);memcpy(text->p+text->len,b,n);text->len+=n;text->p[text->len]=0;} }
    rc=pclose(p);
    if (rc!=0) { snprintf(err,errn,"curl вернул код %d (нет сети или пул недоступен)",rc); return 0; }
    return 1;
}
static int http_text(const char *url, Buf *out, char *err, size_t errn) {
#ifdef _WIN32
    if (winhttp_get(url,out,NULL,err,errn)) return 1;
    char e2[256];
    if (curl_get(url,out,NULL,e2,sizeof(e2))) { err[0]=0; return 1; }
    if (!err[0]) snprintf(err,errn,"%s",e2);
    return 0;
#else
    return curl_get(url,out,NULL,err,errn);
#endif
}
NM_MAYBE_UNUSED static int http_file(const char *url, const char *dest, char *err, size_t errn) {
#ifdef _WIN32
    if (winhttp_get(url,NULL,dest,err,errn)) return 1;
    char e2[256];
    if (curl_get(url,NULL,dest,e2,sizeof(e2))) { err[0]=0; return 1; }
    if (!err[0]) snprintf(err,errn,"%s",e2);
    return 0;
#else
    return curl_get(url,NULL,dest,err,errn);
#endif
}

/* ============================ запуск процессов ============================ */
#ifdef _WIN32
NM_MAYBE_UNUSED static int run_cmd(const char *cmdline, const char *cwd) {
    STARTUPINFOA si; PROCESS_INFORMATION pi; memset(&si,0,sizeof(si)); memset(&pi,0,sizeof(pi));
    si.cb=sizeof(si); si.dwFlags=STARTF_USESHOWWINDOW; si.wShowWindow=SW_HIDE;
    char *cmd=(char*)malloc(strlen(cmdline)+1); strcpy(cmd,cmdline);
    BOOL ok=CreateProcessA(NULL,cmd,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,cwd,&si,&pi);
    free(cmd);
    if(!ok) return -1;
    WaitForSingleObject(pi.hProcess,120000);
    DWORD code=0; GetExitCodeProcess(pi.hProcess,&code);
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return (int)code;
}
#else
NM_MAYBE_UNUSED static int run_cmd(const char *cmdline, const char *cwd) {
    (void)cwd; return system(cmdline);
}
#endif

/* ============================ сборка URL пула ============================ */
static void pool_url(char *out,size_t n,const PoolDef *p,const char *kind,const char *wallet) {
    char w[512]; snprintf(w,sizeof(w),"%s",wallet);
    const char *tpl = !strcmp(kind,"stats") ? p->stats_path :
                      !strcmp(kind,"payments") ? p->payments_path : p->pool_path;
    const char *q = strstr(tpl,"{wallet}");
    if (q) {
        size_t head = (size_t)(q - tpl);
        snprintf(out, n, "%s%.*s%s%s", p->api_base, (int)head, tpl, w, q + 8);
    } else snprintf(out, n, "%s%s", p->api_base, tpl);
}

/* ============================ демонстрационные данные ============================ */
static const char *demo_for(const char *id) {
    for (int i=0;i<N_DEMOS;++i) if (!strcmp(DEMO_DEFS[i].id,id)) return DEMO_DEFS[i].json;
    return DEMO_DEFS[0].json;
}

/* ============================ mining control ============================ */
static int miner_is_running(void) {
#ifdef _WIN32
    if (!g_miner) return 0;
    return WaitForSingleObject(g_miner,0)==WAIT_TIMEOUT;
#else
    return g_sim_running;
#endif
}
static void miner_stop(void) {
#ifdef _WIN32
    if (g_miner) { TerminateProcess(g_miner,0); CloseHandle(g_miner); g_miner=NULL; g_pid=0; }
#else
    g_sim_running=0;
#endif
}
static int miner_start(const char *wallet,const PoolDef *pool,int port,const char *worker,
                       int threads,int donate,int diff, char *err, size_t errn) {
    err[0]=0; g_start_error[0]=0;
    char cfg[65536]; Buf c; buf_init(&c);
    build_config_json(&c,wallet,pool,port,worker,threads,donate,diff);
    snprintf(cfg,sizeof(cfg),"%s",c.p); buf_free(&c);
    FILE *f=fopen(g_configpath,"wb");
    if(!f){snprintf(err,errn,"не записать config.json: %.900s",g_configpath);return 0;}
    fwrite(cfg,1,strlen(cfg),f); fclose(f);
    set_progress("config.json записан: кошелёк %s, пул %s",wallet,pool->name);

#ifndef _WIN32
    /* Тестовая сборка под Linux: НЕ качает и НЕ запускает майнер. */
    g_simulate=1;
    FILE *lg=fopen(g_logpath,"wb");
    if(lg){fprintf(lg,"[NeonMiner] Linux test build — реальный майнер не запускается.\n");
           fprintf(lg,"[NeonMiner] config.json создан, пул %s, кошелёк %s\n",pool->name,wallet);fclose(lg);}
    set_progress("тестовая Linux-сборка: майнер симулирован (в .exe будет реальный запуск)");
    g_sim_running=1;
    return 1;
#else
    char exe[1200], sumspath[1200], sumsurl[512];
    join_path(exe,sizeof(exe),g_workdir,XMRIG_EXE_RELPATH);
    if (!file_exists(exe)) {
        /* 1. SHA256SUMS официального релиза */
        join_path(sumspath,sizeof(sumspath),g_workdir,"SHA256SUMS");
        snprintf(sumsurl,sizeof(sumsurl),"%s",XMRIG_SUMS_URL);
        Buf sums; buf_init(&sums);
        set_progress("скачиваю SHA256SUMS релиза %s…",XMRIG_VERSION);
        char e1[256];
        if (!http_text(sumsurl,&sums,e1,sizeof(e1))) { snprintf(err,errn,"SHA256SUMS: %s",e1); buf_free(&sums); return 0; }
        /* 2. ожидаемый хэш нужного ассета */
        char expect[65]=""; 
        for (char *line=strtok(sums.p,"\n"); line; line=strtok(NULL,"\n")) {
            char hx[128], name[256]; 
            if (sscanf(line,"%127s %255s",hx,name)!=2) continue;
            if (!strcmp(name,XMRIG_ASSET) || !strcmp(name+1,XMRIG_ASSET)) {
                size_t k=0; for (size_t j=0;hx[j]&&k<64;++j) if(isxdigit((unsigned char)hx[j])) expect[k++]=tolower((unsigned char)hx[j]);
                expect[k]=0; break;
            }
        }
        buf_free(&sums);
        if (strlen(expect)!=64) { snprintf(err,errn,"в SHA256SUMS нет строки для %s",XMRIG_ASSET); return 0; }
        /* 3. сам архив */
        char zip[1200]; join_path(zip,sizeof(zip),g_workdir,XMRIG_ASSET);
        set_progress("скачиваю официальный %s…",XMRIG_ASSET);
        char e2[256];
        if (!http_file(XMRIG_ZIP_URL,zip,e2,sizeof(e2))) { snprintf(err,errn,"загрузка XMRig: %s",e2); return 0; }
        /* 4. проверка SHA256 */
        char got[65]; char e3[256];
        if (!sha256_file(zip,got,e3,sizeof(e3))) { snprintf(err,errn,"%s",e3); return 0; }
        if (strcmp(got,expect)) {
            remove(zip);
            snprintf(err,errn,"SHA256 НЕ совпал! ожидался %s, получен %s. Архив удалён, ничего не запущено.",expect,got);
            return 0;
        }
        set_progress("SHA256 совпал: %s",got);
        /* 5. распаковка: встроенный tar.exe (Windows 10+), запасной вариант PowerShell */
        char cmd[3000];
        snprintf(cmd,sizeof(cmd),"tar.exe -xf \"%s\" -C \"%s\"",zip,g_workdir);
        int rc=run_cmd(cmd,g_workdir);
        if (rc!=0 || !file_exists(exe)) {
            snprintf(cmd,sizeof(cmd),"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \"Expand-Archive -LiteralPath '%s' -DestinationPath '%s' -Force\"",zip,g_workdir);
            rc=run_cmd(cmd,g_workdir);
        }
        if (!file_exists(exe)) { snprintf(err,errn,"не удалось распаковать архив (tar.exe / Expand-Archive), код %d",rc); return 0; }
        set_progress("XMRig распакован и проверен");
    } else set_progress("XMRig уже скачан: %s",exe);

    /* 6. запуск процесса с логом */
    SECURITY_ATTRIBUTES sa; memset(&sa,0,sizeof(sa)); sa.nLength=sizeof(sa); sa.bInheritHandle=TRUE;
    HANDLE hlog=CreateFileA(g_logpath,GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    char cmdline[3000];
    snprintf(cmdline,sizeof(cmdline),"\"%s\" --config=\"%s\"",exe,g_configpath);
    STARTUPINFOA si; PROCESS_INFORMATION pi; memset(&si,0,sizeof(si)); memset(&pi,0,sizeof(pi));
    si.cb=sizeof(si); si.dwFlags=STARTF_USESTDHANDLES|STARTF_USESHOWWINDOW; si.wShowWindow=SW_HIDE;
    si.hStdOutput=hlog; si.hStdError=hlog; si.hStdInput=GetStdHandle(STD_INPUT_HANDLE);
    set_progress("запускаю XMRig…");
    BOOL ok=CreateProcessA(NULL,cmdline,NULL,NULL,TRUE,CREATE_NO_WINDOW,NULL,g_workdir,&si,&pi);
    if (hlog!=INVALID_HANDLE_VALUE) CloseHandle(hlog);
    if (!ok) { snprintf(err,errn,"CreateProcess: %lu",GetLastError()); return 0; }
    g_miner=pi.hProcess; g_pid=pi.dwProcessId; CloseHandle(pi.hThread);
    return 1;
#endif
}

/* ============================ HTTP-сервер ============================ */
/* send()/write() могут отправить меньше, чем попросили — особенно для больших
   JSON-ответов пула. Досылаем остаток, иначе браузер получит битый JSON. */
static int send_all(sock_t c, const char *data, size_t len) {
    size_t off=0;
    while (off < len) {
        size_t chunk = len-off; if (chunk > 1u<<20) chunk = 1u<<20;
#ifdef _WIN32
        int n=send(c,data+off,(int)chunk,0);
#else
        ssize_t n=write(c,data+off,chunk);
#endif
        if (n<=0) return 0;
        off += (size_t)n;
    }
    return 1;
}
static void send_resp(sock_t c,int status,const char *ctype,const char *body,size_t len) {
    char hdr[1024];
    const char *st = status==200?"OK":status==404?"Not Found":status==400?"Bad Request":status==413?"Payload Too Large":"Error";
    int n=snprintf(hdr,sizeof(hdr),
        "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
        "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
        "Referrer-Policy: no-referrer\r\nAccess-Control-Allow-Origin: *\r\n\r\n",status,st,ctype,len);
    if (n>0) send_all(c,hdr,(size_t)n);
    if (len) send_all(c,body,len);
}
static void send_json(sock_t c,int status,const char *json) { send_resp(c,status,"application/json; charset=utf-8",json,strlen(json)); }
static void send_err(sock_t c,int status,const char *msg) {
    Buf b; buf_init(&b); buf_puts(&b,"{\"error\":\""); json_escape(&b,msg); buf_puts(&b,"\"}");
    send_json(c,status,b.p); buf_free(&b);
}
static void send_ok(Buf *b,sock_t c) { send_json(c,200,b->p); }

static void handle_api(sock_t c,const char *method,const char *path,const char *query,const char *body) {
    Buf b; buf_init(&b);

    if (!strcmp(path,"/api/health")) {
        buf_printf(&b,"{\"ok\":true,\"version\":\"%s\",\"platform\":\"%s\",\"pools\":%d,\"default_pool\":\"%s\"}",
            APP_VERSION,
#ifdef _WIN32
            "windows"
#else
            "linux-test"
#endif
            ,N_POOLS,DEFAULT_POOL_ID);
        send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/catalog")) { buf_puts(&b,EMBED_CATALOG_JSON); send_ok(&b,c); goto done; }
    if (!strcmp(path,"/api/release")) { buf_puts(&b,EMBED_RELEASE_JSON); send_ok(&b,c); goto done; }

    if (!strcmp(path,"/api/validate")) {
        char addr[512]=""; query_value(query,"address",addr,sizeof(addr));
        AddrInfo i; validate_address(addr,&i); addr_json(&b,&i); send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/stats")) {
        char pool[64]=""; query_value(query,"pool",pool,sizeof(pool));
        buf_puts(&b,demo_for(pool)); send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/config") && !strcmp(method,"POST")) {
        char wallet[512]="",poolid[64]="",worker[128]="";
        json_str(body,"wallet",wallet,sizeof(wallet));
        json_str(body,"pool",poolid,sizeof(poolid));
        json_str(body,"worker",worker,sizeof(worker));
        char w[64]; sanitize_worker(worker,w,sizeof(w));
        int port=(int)json_num(body,"port",0);
        int threads=(int)json_num(body,"threads_pct",75); if(threads<10)threads=10; if(threads>100)threads=100;
        int donate=(int)json_num(body,"donate_level",1); if(donate<0)donate=0; if(donate>99)donate=99;
        int diff=(int)json_num(body,"fixed_difficulty",0);
        int demo=json_bool(body,"demo");
        const PoolDef *p=pool_by_id(poolid);
        if(!port) port=p->ports[0].port;
        char use_wallet[512];
        if (demo) snprintf(use_wallet,sizeof(use_wallet),"PASTE_YOUR_XMR_ADDRESS_HERE");
        else {
            AddrInfo ai; validate_address(wallet,&ai);
            if(!ai.valid){ send_err(c,400,ai.reason); goto done; }
            snprintf(use_wallet,sizeof(use_wallet),"%s",ai.address);
        }
        Buf cfg; buf_init(&cfg);
        int tls=build_config_json(&cfg,use_wallet,p,port,w,threads,donate,diff);
        Buf cmd; buf_init(&cmd); build_cmd_windows(&cmd,use_wallet,p,port,w,threads,donate,diff);
        buf_printf(&b,"{\"pool\":\"%s\",\"host\":\"%s:%d\",\"tls\":%s,\"demo\":%s,\"config_json\":\"",
            p->id,host_for(p,port),port,tls?"true":"false",demo?"true":"false");
        json_escape(&b,cfg.p); buf_puts(&b,"\",\"cmd_windows\":\""); json_escape(&b,cmd.p);
        buf_puts(&b,"\"}");
        buf_free(&cfg); buf_free(&cmd); send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/calc") && !strcmp(method,"POST")) { calc_json(&b,body); send_ok(&b,c); goto done; }

    if (!strcmp(path,"/api/raw")) {
        char poolid[64]="",kind[32]="",wallet[512]="";
        query_value(query,"pool",poolid,sizeof(poolid));
        query_value(query,"kind",kind,sizeof(kind));
        query_value(query,"wallet",wallet,sizeof(wallet));
        if(!strcmp(kind,"stats")||!strcmp(kind,"payments")) {
            AddrInfo ai; validate_address(wallet,&ai);
            if(!ai.valid){ buf_puts(&b,"{\"ok\":false,\"error\":\"invalid wallet\"}"); send_ok(&b,c); goto done; }
            snprintf(wallet,sizeof(wallet),"%s",ai.address);
        }
        const PoolDef *p=pool_by_id(poolid);
        char url[1024];
        if (!strcmp(kind,"market")) {
            const PoolDef *hv=pool_by_id("hashvault");
            pool_url(url,sizeof(url),hv,"pool","");
        } else pool_url(url,sizeof(url),p,!strcmp(kind,"pool")?"pool":kind,wallet);
        Buf raw; buf_init(&raw); char err[512];
        int ok=http_text(url,&raw,err,sizeof(err));
        if(!ok){ buf_puts(&b,"{\"ok\":false,\"error\":\""); json_escape(&b,err); buf_puts(&b,"\"}"); send_ok(&b,c); buf_free(&raw); goto done; }
        buf_puts(&b,"{\"ok\":true,\"data\":\""); json_escape(&b,raw.p); buf_puts(&b,"\"}");
        send_ok(&b,c); buf_free(&raw); goto done;
    }

    if (!strcmp(path,"/api/status")) {
        int running=miner_is_running();
        buf_printf(&b,"{\"running\":%s,\"pid\":%u,\"version\":\"%s\",\"workdir\":\"",
            running?"true":"false",
#ifdef _WIN32
            (unsigned)g_pid,
#else
            0u,
#endif
            XMRIG_VERSION);
        json_escape(&b,g_workdir); buf_puts(&b,"\",\"log_file\":\""); json_escape(&b,g_logpath);
        buf_puts(&b,"\",\"simulated\":"); buf_puts(&b,
#ifndef _WIN32
            "true"
#else
            "false"
#endif
            ); buf_puts(&b,",\"progress\":\""); json_escape(&b,g_progress);
        buf_puts(&b,"\",\"error\":\""); json_escape(&b,g_start_error); buf_puts(&b,"\"}");
        send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/log")) {
        FILE *f=fopen(g_logpath,"rb");
        if(!f){ send_json(c,200,"{\"lines\":\"// лог пока пуст\"}"); goto done; }
        fseek(f,0,SEEK_END); long sz=ftell(f);
        long start = sz>16000 ? sz-16000 : 0;
        fseek(f,start,SEEK_SET);
        Buf t; buf_init(&t);
        char line[4096]; while(fgets(line,sizeof(line),f)) buf_puts(&t,line);
        fclose(f);
        if(!t.len) buf_puts(&t,"// лог пока пуст");
        buf_puts(&b,"{\"lines\":\""); json_escape(&b,t.p); buf_puts(&b,"\"}");
        buf_free(&t); send_ok(&b,c); goto done;
    }
    if (!strcmp(path,"/api/stop") && !strcmp(method,"POST")) {
        miner_stop(); g_progress[0]=0; set_progress("майнер остановлен");
        send_json(c,200,"{\"ok\":true,\"running\":false}"); goto done;
    }
    if (!strcmp(path,"/api/start") && !strcmp(method,"POST")) {
        if(!json_bool(body,"consent")){ send_err(c,400,"Нужно подтвердить, что это твоя машина."); goto done; }
        if(miner_is_running()){ send_json(c,200,"{\"ok\":true,\"running\":true,\"message\":\"Майнер уже работает.\"}"); goto done; }
        char wallet[512]="",poolid[64]="",worker[128]="";
        json_str(body,"wallet",wallet,sizeof(wallet));
        json_str(body,"pool",poolid,sizeof(poolid));
        json_str(body,"worker",worker,sizeof(worker));
        AddrInfo ai; validate_address(wallet,&ai);
        if(!ai.valid){ send_err(c,400,ai.reason); goto done; }
        char w[64]; sanitize_worker(worker,w,sizeof(w));
        const PoolDef *p=pool_by_id(poolid);
        int port=(int)json_num(body,"port",0); if(!port) port=p->ports[0].port;
        int threads=(int)json_num(body,"threads_pct",75); if(threads<10)threads=10; if(threads>100)threads=100;
        int donate=(int)json_num(body,"donate_level",1); if(donate<0)donate=0; if(donate>99)donate=99;
        int diff=(int)json_num(body,"fixed_difficulty",0);
        char err[1024];
        int ok=miner_start(ai.address,p,port,w,threads,donate,diff,err,sizeof(err));
        if(!ok){ snprintf(g_start_error,sizeof(g_start_error),"%s",err);
            buf_puts(&b,"{\"ok\":false,\"error\":\""); json_escape(&b,err); buf_puts(&b,"\"}"); send_json(c,200,b.p); goto done; }
        snprintf(g_progress,sizeof(g_progress),"Майнер запущен · PID %u · пул %s",
#ifdef _WIN32
            (unsigned)g_pid,
#else
            0u,
#endif
            p->name);
        buf_printf(&b,"{\"ok\":true,\"running\":true,\"message\":\"Майнер запущен. Через пару минут появятся первые шары.\",\"workdir\":\"");
        json_escape(&b,g_workdir); buf_puts(&b,"\"}");
        send_ok(&b,c); goto done;
    }

    send_err(c,404,"Неизвестный endpoint");
done:
    buf_free(&b);
}

static void handle_client(sock_t c) {
    char req[70000]; size_t used=0;
#ifdef _WIN32
    int ms=5000;
    setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,(const char*)&ms,sizeof(ms));
#else
    struct timeval tv; tv.tv_sec=5; tv.tv_usec=0;
    setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));
#endif
    char tmp[8192]; int n;
    while (used+1 < sizeof(req)) {
#ifdef _WIN32
        n=recv(c,tmp,sizeof(tmp),0);
#else
        n=(int)recv(c,tmp,sizeof(tmp),0);
#endif
        if(n<=0) break;
        size_t take=(size_t)n; if(used+take>=sizeof(req)) take=sizeof(req)-used-1;
        memcpy(req+used,tmp,take); used+=take; req[used]=0;
        char *hend=strstr(req,"\r\n\r\n");
        if(hend){
            size_t hlen=(size_t)(hend-req)+4;
            long clen=0; char *cl=strstr(req,"Content-Length:"); if(!cl)cl=strstr(req,"content-length:");
            if(cl) clen=strtol(cl+15,NULL,10);
            if (clen < 0) clen = 0;
            if (clen > 60000) clen = 60000;
            if (used >= hlen + (size_t)clen) break;
        }
    }
    if(!used){ closesocket(c); return; }
    req[used]=0;
    char method[16]="",path[1024]="",query[2048]="";
    if(sscanf(req,"%15s %1023s",method,path)!=2){ closesocket(c); return; }
    char *q=strchr(path,'?'); if(q){ *q=0; snprintf(query,sizeof(query),"%s",q+1); }
    char *body=strstr(req,"\r\n\r\n"); body=body?body+4:"";
    /* статика */
    if(!strcmp(path,"/")||!strcmp(path,"/index.html")) send_resp(c,200,"text/html; charset=utf-8",EMBED_INDEX_HTML,strlen(EMBED_INDEX_HTML));
    else if(!strcmp(path,"/static/style.css")) send_resp(c,200,"text/css; charset=utf-8",EMBED_STYLE_CSS,strlen(EMBED_STYLE_CSS));
    else if(!strcmp(path,"/static/app.js")) send_resp(c,200,"application/javascript; charset=utf-8",EMBED_APP_JS,strlen(EMBED_APP_JS));
    else if(!strcmp(path,"/static/favicon.svg")) send_resp(c,200,"image/svg+xml",EMBED_FAVICON_SVG,strlen(EMBED_FAVICON_SVG));
    else if(!strncmp(path,"/api/",5)) handle_api(c,method,path,query,body);
    else send_err(c,404,"Страница не найдена");
    closesocket(c);
}

/* ============================ selftest ============================ */
static int selftest(void) {
    int fails=0;
#define CHECK(name,cond) do { printf("  [%s] %s\n",(cond)?"OK":"FAIL",name); if(!(cond))++fails; } while(0)
    char h[65];
    sha256_bytes((const unsigned char*)"abc",3,h);
    CHECK("SHA256(abc)", !strcmp(h,"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    unsigned char k[32]; char kh[65];
    keccak256((const unsigned char*)"",0,k);
    for(int i=0;i<32;++i) sprintf(kh+i*2,"%02x",k[i]);
    CHECK("Keccak256(empty)", !strcmp(kh,"c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"));
    keccak256((const unsigned char*)"abc",3,k);
    for(int i=0;i<32;++i) sprintf(kh+i*2,"%02x",k[i]);
    CHECK("Keccak256(abc)", !strcmp(kh,"4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"));
    /* Синтетический, но криптографически корректный адрес. В exe не зашит
       ни один настоящий адрес — пользователь всегда вводит свой сам. */
    unsigned char payload[65]; payload[0]=18;
    for(int i=1;i<65;++i) payload[i]=(unsigned char)(i*37+11);
    unsigned char kk[32]; keccak256(payload,65,kk);
    unsigned char full[69]; memcpy(full,payload,65); memcpy(full+65,kk,4);
    char synth[128], corrupt[128]; b58_encode(full,69,synth,sizeof(synth));
    snprintf(corrupt,sizeof(corrupt),"%s",synth);
    corrupt[40] = (corrupt[40]=='A') ? 'B' : 'A';
    printf("  [i] синтетический тестовый адрес: %.12s… (длина %zu)\n",synth,strlen(synth));
    AddrInfo a,b;
    validate_address(synth,&a); CHECK("валидный XMR-адрес", a.valid && a.checksum_ok && !strcmp(a.network,"mainnet"));
    validate_address(corrupt,&b); CHECK("опечатка в адресе отклонена", !b.valid && !b.checksum_ok);
    const PoolDef *p=pool_by_id("c3pool");
    Buf cfg; buf_init(&cfg);
    build_config_json(&cfg,synth,p,13333,"rig-01",75,1,0);
    CHECK("config.json содержит кошелёк", strstr(cfg.p,synth)!=NULL);
    CHECK("config.json содержит failover", strstr(cfg.p,"auto.c3pool.org:80")!=NULL);
    CHECK("config.json без background", strstr(cfg.p,"\"background\": false")!=NULL);
    buf_free(&cfg);
    Buf cfg2; buf_init(&cfg2);
    build_config_json(&cfg2,synth,p,33333,"rig-01",75,1,0);
    CHECK("TLS-порт C3Pool ведёт на mine.c3pool.com", strstr(cfg2.p,"mine.c3pool.com:33333")!=NULL && strstr(cfg2.p,"\"tls\": true")!=NULL);
    buf_free(&cfg2);
    Buf calc; buf_init(&calc);
    calc_json(&calc,"{\"hashrate_hs\":5000,\"eur_per_kwh\":0.36,\"power_watts\":65}");
    CHECK("калькулятор показывает минус", strstr(calc.p,"\"profitable\":false")!=NULL);
    buf_free(&calc);
    printf("  %s (%d ошибок)\n",fails?"ПРОВАЛ":"ВСЁ ОК",fails);
#undef CHECK
    return fails?1:0;
}

/* ============================ main ============================ */
int main(int argc,char **argv) {
    int port=DEFAULT_PORT, no_browser=0, do_selftest=0, port_given=0;
    const char *host="127.0.0.1";
    for(int i=1;i<argc;++i){
        if(!strcmp(argv[i],"--port") && i+1<argc) { port=atoi(argv[++i]); port_given=1; }
        else if(!strcmp(argv[i],"--host") && i+1<argc) host=argv[++i];
        else if(!strcmp(argv[i],"--no-browser")) no_browser=1;
        else if(!strcmp(argv[i],"--selftest")) do_selftest=1;
        else if(!strcmp(argv[i],"--version")) { printf("NeonMiner %s\n",APP_VERSION); return 0; }
        else if(!strcmp(argv[i],"--help")) {
            printf("NeonMiner %s — локальный клиент майнинга XMR\n",APP_VERSION);
            printf("  --port N        порт локального сервера (по умолчанию %d)\n",DEFAULT_PORT);
            printf("  --host ADDR     адрес привязки (по умолчанию 127.0.0.1)\n");
            printf("  --no-browser    не открывать браузер автоматически\n");
            printf("  --selftest      внутренние проверки SHA256/Keccak/адресов/конфига\n");
            return 0;
        }
    }
    if(do_selftest) return selftest();

#ifdef _WIN32
    WSADATA wsa; if(WSAStartup(MAKEWORD(2,2),&wsa)){ printf("WSAStartup failed\n"); return 1; }
    SetConsoleTitleA("NeonMiner — локальная капалка XMR");
    const char *lad=getenv("LOCALAPPDATA");
    if(lad && *lad) snprintf(g_workdir,sizeof(g_workdir),"%s\\NeonMiner",lad);
    else snprintf(g_workdir,sizeof(g_workdir),"C:\\NeonMiner");
#else
    const char *tmp=getenv("TMPDIR"); if(!tmp||!*tmp) tmp="/tmp";
    snprintf(g_workdir,sizeof(g_workdir),"%s/neonminer-test",tmp);
    signal(SIGPIPE,SIG_IGN);
#endif
    ensure_dir(g_workdir);
    join_path(g_configpath,sizeof(g_configpath),g_workdir,"config.json");
    join_path(g_logpath,sizeof(g_logpath),g_workdir,"xmrig.log");
    join_path(g_binzip,sizeof(g_binzip),g_workdir,XMRIG_ASSET);

    sock_t srv=socket(AF_INET,SOCK_STREAM,0);
    if(srv==INVALID_SOCKET){ printf("socket() failed\n"); return 1; }
    int yes=1; setsockopt(srv,SOL_SOCKET,SO_REUSEADDR,(const char*)&yes,sizeof(yes));
    struct sockaddr_in addr; memset(&addr,0,sizeof(addr));
    addr.sin_family=AF_INET;
    addr.sin_addr.s_addr=!strcmp(host,"0.0.0.0")?htonl(INADDR_ANY):inet_addr(host);
    /* Если порт по умолчанию занят — пробуем соседние. Пользователю не надо ничего настраивать. */
    int first = port_given ? port : DEFAULT_PORT;
    int last  = port_given ? port : DEFAULT_PORT + 25;
    int bound = 0;
    for (int p=first; p<=last; ++p) {
        addr.sin_port=htons((unsigned short)p);
        if (bind(srv,(struct sockaddr*)&addr,sizeof(addr))==0) { port=p; bound=1; break; }
    }
    if(!bound){ printf("не нашёл свободный порт в диапазоне %d..%d (занят %s)\n",first,last,host); return 1; }
    if(listen(srv,16)<0){ printf("listen() failed\n"); return 1; }

    char url[256]; snprintf(url,sizeof(url),"http://127.0.0.1:%d",port);
    printf("======================================================================\n");
    printf("  NEON//MINER %s — автономный клиент XMR\n",APP_VERSION);
    printf("======================================================================\n");
    printf("  Панель      : %s\n",url);
    printf("  Рабочая папка: %s\n",g_workdir);
    printf("  XMRig       : %s (%s)\n",XMRIG_VERSION,XMRIG_ASSET);
    printf("  Пул по умолчанию: %s\n",DEFAULT_POOL_ID);
    printf("  Только своё железо. Скрытого майнинга нет.\n");
    printf("  Остановить  : Ctrl+C (майнер будет остановлен вместе с панелью)\n");
    printf("======================================================================\n");
    fflush(stdout);
    if(!no_browser) {
#ifdef _WIN32
        ShellExecuteA(NULL,"open",url,NULL,NULL,SW_SHOWNORMAL);
#else
        printf("  (Linux-тест: браузер не открываю, открой %s вручную)\n",url);
#endif
    }
    for(;;){
        sock_t c=accept(srv,NULL,NULL);
        if(c==INVALID_SOCKET) continue;
        handle_client(c);
    }
#ifdef _WIN32
    miner_stop(); WSACleanup(); closesocket(srv);
#endif
    return 0;
}
