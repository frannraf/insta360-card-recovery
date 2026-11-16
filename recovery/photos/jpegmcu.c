/* jpegmcu: bit-exact structural check of a baseline JPEG's entropy-coded scan.
 *
 * Decodes every Huffman symbol of every block (no IDCT), counts MCUs, and requires that
 *   - every restart interval ends exactly at its RSTn marker (n counting 0..7), with only
 *     1-bit padding before it,
 *   - the last interval ends exactly at EOI,
 *   - no Huffman code is invalid and no block has more than 64 coefficients.
 * Prints one JSON line. Exit 0 = ok, 1 = scan error, 2 = unsupported/parse error.
 * Usage: jpegmcu <file> [max_bytes]   (the file must start with SOI; data after EOI ignored)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

typedef struct { int present; int maxcode[18]; int mincode[17]; int valptr[17]; uint8_t vals[256]; } huff_t;
static huff_t dc[4], ac[4];
static const uint8_t *d; static size_t n;
static size_t p; static int nbits; static uint8_t cur; static int marker_hit;

static int get_bit(void) {
  if (nbits == 0) {
    for (;;) {
      if (marker_hit) return 0;
      if (p + 1 >= n) { marker_hit = 1; return 0; }
      uint8_t b = d[p];
      if (b == 0xFF) {
        uint8_t b2 = d[p + 1];
        if (b2 == 0x00) { cur = 0xFF; p += 2; break; }
        if (b2 == 0xFF) { p += 1; continue; }
        marker_hit = 1; return 0;
      }
      cur = b; p += 1; break;
    }
    nbits = 8;
  }
  nbits--;
  return (cur >> nbits) & 1;
}
static int get_bits(int k) { int v = 0; while (k--) v = (v << 1) | get_bit(); return v; }

static int decode(const huff_t *h) {
  int code = 0;
  for (int l = 1; l <= 16; l++) {
    code = (code << 1) | get_bit();
    if (h->maxcode[l] >= 0 && code <= h->maxcode[l]) return h->vals[h->valptr[l] + code - h->mincode[l]];
  }
  return -1;
}

static void build(huff_t *h, const uint8_t *bits, const uint8_t *vals, int nv) {
  memset(h, 0, sizeof *h);
  h->present = 1;
  memcpy(h->vals, vals, nv);
  int code = 0, k = 0;
  for (int l = 1; l <= 16; l++) {
    if (bits[l - 1]) {
      h->valptr[l] = k; h->mincode[l] = code;
      code += bits[l - 1]; k += bits[l - 1];
      h->maxcode[l] = code - 1;
    } else h->maxcode[l] = -1;
    code <<= 1;
  }
}

static void fail(const char *what, long mcu, long expected, int rst, int code) {
  printf("{\"ok\": false, \"error\": \"%s\", \"error_offset\": %zu, \"error_mcu\": %ld, \"expected_mcus\": %ld, "
         "\"rst_seen\": %d}\n", what, p, mcu, expected, rst);
  exit(code);
}

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: jpegmcu file [max_bytes]\n"); return 2; }
  FILE *f = fopen(argv[1], "rb"); if (!f) { perror("open"); return 2; }
  fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
  if (argc > 2 && atol(argv[2]) < sz) sz = atol(argv[2]);
  uint8_t *buf = malloc(sz + 2); if (fread(buf, 1, sz, f) != (size_t)sz) { perror("read"); return 2; }
  buf[sz] = 0; buf[sz + 1] = 0; fclose(f);
  d = buf; n = sz;
  if (n < 4 || d[0] != 0xFF || d[1] != 0xD8) fail("no SOI", 0, 0, 0, 2);
  int W = 0, H = 0, nc = 0, cid[4], ch[4], cv[4], dri = 0, hmax = 1, vmax = 1;
  int ns = 0, sc[4], std_[4], sta[4];
  p = 2;
  for (;;) {
    if (p + 4 > n || d[p] != 0xFF) fail("marker sync", 0, 0, 0, 2);
    uint8_t m = d[p + 1];
    if (m == 0xFF) { p++; continue; }
    int len = (d[p + 2] << 8) | d[p + 3];
    const uint8_t *s = d + p + 4; int sl = len - 2;
    if (m == 0xC0 || m == 0xC1) {
      H = (s[1] << 8) | s[2]; W = (s[3] << 8) | s[4]; nc = s[5];
      if (nc > 4) fail("too many components", 0, 0, 0, 2);
      for (int i = 0; i < nc; i++) {
        cid[i] = s[6 + 3 * i]; ch[i] = s[7 + 3 * i] >> 4; cv[i] = s[7 + 3 * i] & 15;
        if (ch[i] > hmax) hmax = ch[i];
        if (cv[i] > vmax) vmax = cv[i];
      }
    } else if (m >= 0xC2 && m <= 0xCF && m != 0xC4 && m != 0xC8 && m != 0xCC) {
      fail("not baseline", 0, 0, 0, 2);
    } else if (m == 0xC4) {
      int q = 0;
      while (q < sl) {
        int tc = s[q] >> 4, th = s[q] & 15; const uint8_t *bits = s + q + 1; int tot = 0;
        for (int i = 0; i < 16; i++) tot += bits[i];
        if (th > 3 || tot > 256) fail("bad DHT", 0, 0, 0, 2);
        build(tc ? &ac[th] : &dc[th], bits, s + q + 17, tot);
        q += 17 + tot;
      }
    } else if (m == 0xDD) {
      dri = (s[0] << 8) | s[1];
    } else if (m == 0xDA) {
      ns = s[0];
      for (int i = 0; i < ns; i++) { sc[i] = s[1 + 2 * i]; std_[i] = s[2 + 2 * i] >> 4; sta[i] = s[2 + 2 * i] & 15; }
      p += 2 + len;
      break;
    }
    p += 2 + len;
  }
  if (!W || !H || ns != nc || ns < 1) fail("unsupported scan layout", 0, 0, 0, 2);
  int bh[4], bv[4];
  for (int i = 0; i < ns; i++) {
    int k; for (k = 0; k < nc && cid[k] != sc[i]; k++);
    if (k == nc) fail("scan component not in frame", 0, 0, 0, 2);
    bh[i] = ns == 1 ? 1 : ch[k]; bv[i] = ns == 1 ? 1 : cv[k];
    if (!dc[std_[i]].present || !ac[sta[i]].present) fail("missing Huffman table", 0, 0, 0, 2);
  }
  long mx = (W + 8 * hmax - 1) / (8 * hmax), my = (H + 8 * vmax - 1) / (8 * vmax);
  long total = mx * my;
  size_t scan_start = p;
  nbits = 0; marker_hit = 0;
  int rst = 0;
  for (long mcu = 0; mcu < total; mcu++) {
    if (dri && mcu > 0 && mcu % dri == 0) {
      if (marker_hit) fail("marker reached before restart interval ended", mcu, total, rst, 1);
      if (nbits && (cur & ((1 << nbits) - 1)) != (1 << nbits) - 1) fail("non-1 padding before RST", mcu, total, rst, 1);
      nbits = 0;
      while (p < n && d[p] == 0xFF && d[p + 1] == 0xFF) p++;
      if (!(p + 1 < n && d[p] == 0xFF && d[p + 1] == 0xD0 + (rst & 7))) fail("RST marker not where the interval ends", mcu, total, rst, 1);
      p += 2; rst++;
    }
    for (int c = 0; c < ns; c++)
      for (int b = 0; b < bh[c] * bv[c]; b++) {
        int s = decode(&dc[std_[c]]);
        if (s < 0 || s > 11) fail("bad DC code", mcu, total, rst, 1);
        get_bits(s);
        for (int k = 1; k < 64;) {
          int rs = decode(&ac[sta[c]]);
          if (rs < 0) fail("bad AC code", mcu, total, rst, 1);
          int r = rs >> 4, sz2 = rs & 15;
          if (sz2 == 0) { if (r == 15) { k += 16; if (k > 64) fail("ZRL past end of block", mcu, total, rst, 1); continue; } break; }
          k += r;
          if (k > 63) fail("more than 64 coefficients", mcu, total, rst, 1);
          get_bits(sz2); k++;
        }
        if (marker_hit) fail("marker reached inside an interval", mcu, total, rst, 1);
      }
  }
  if (nbits && (cur & ((1 << nbits) - 1)) != (1 << nbits) - 1) fail("non-1 padding before EOI", total, total, rst, 1);
  nbits = 0;
  /* some encoders put a final RSTn right before EOI */
  int trailing_rst = 0;
  if (p + 1 < n && d[p] == 0xFF && d[p + 1] == 0xD0 + (rst & 7)) { p += 2; trailing_rst = 1; }
  while (p < n && d[p] == 0xFF && d[p + 1] == 0xFF) p++;
  if (!(p + 1 < n && d[p] == 0xFF && d[p + 1] == 0xD9)) fail("EOI not where the last interval ends", total, total, rst, 1);
  printf("{\"ok\": true, \"width\": %d, \"height\": %d, \"mcus\": %ld, \"restart_interval\": %d, \"rst\": %d, "
         "\"trailing_rst\": %d, \"scan_start\": %zu, \"eoi\": %zu}\n", W, H, total, dri, rst, trailing_rst, scan_start, p);
  return 0;
}
