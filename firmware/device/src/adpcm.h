// IMA ADPCM: 4 bits a sample, a quarter of 16-bit PCM. Offline voice notes
// are kept this way at 8 kHz (notes.cpp): 4 KB a second, telephone quality,
// which is as low as a note can go and still transcribe well (2-bit codes or
// a lower rate start losing words). Plain C++, so the Mac checks it too.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace adpcm {

static const int16_t kSteps[89] = {
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
    31,    34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
    544,   598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
    2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
    9493,  10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};
static const int8_t kIndexStep[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

// The coder's state; encoder and decoder each keep one, both starting at zero.
struct State {
  int32_t predictor = 0;
  int32_t index = 0;
};

static inline int16_t clamp16(int32_t v) { return v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)v; }

// One code (4 bits) back to a sample.
static inline int16_t decode(State& s, uint8_t code) {
  const int32_t step = kSteps[s.index];
  int32_t diff = step >> 3;
  if (code & 4) diff += step;
  if (code & 2) diff += step >> 1;
  if (code & 1) diff += step >> 2;
  s.predictor = clamp16(code & 8 ? s.predictor - diff : s.predictor + diff);
  s.index += kIndexStep[code & 7];
  s.index = s.index < 0 ? 0 : s.index > 88 ? 88 : s.index;
  return (int16_t)s.predictor;
}

// One sample to a code; runs the decoder too, so both sides stay in step.
static inline uint8_t encode(State& s, int16_t sample) {
  const int32_t step = kSteps[s.index];
  int32_t diff = (int32_t)sample - s.predictor;
  uint8_t code = 0;
  if (diff < 0) {
    code = 8;
    diff = -diff;
  }
  if (diff >= step) code |= 4, diff -= step;
  if (diff >= step >> 1) code |= 2, diff -= step >> 1;
  if (diff >= step >> 2) code |= 1;
  decode(s, code);
  return code;
}

// 16 kHz → 8 kHz: sample `i` of the halved signal, through a half-band
// low-pass (flat to 3.4 kHz, −30 dB from 4.6 kHz) so the hiss above 4 kHz
// doesn't fold back into the voice. A half-band filter's even taps are zero.
static inline int16_t halved(const int16_t* in, size_t n, size_t i) {
  static const int16_t kTaps[10] = {10344, -3226, 1690, -980, 570, -318, 163, -73, 26, -5};  // Q15, odd offsets
  const long c = (long)(2 * i);
  int32_t acc = (c < (long)n ? in[c] : 0) * 16386;
  for (int k = 0; k < 10; ++k) {
    const long d = 2 * k + 1;
    const int32_t a = c - d >= 0 ? in[c - d] : 0;
    const int32_t b = c + d < (long)n ? in[c + d] : 0;
    acc += (a + b) * kTaps[k];
  }
  return clamp16((acc + 16384) >> 15);
}

// A recording coded as it comes, block by block (a note written as it's
// recorded): halved (16 → 8 kHz) over a sliding window — the same samples
// halved() gives over the whole of it — then coded, two codes a byte, low
// first; `out(bytes, n)` takes them. finish() ends it (an odd last code
// alone in its byte), as a whole-recording coder would.
struct StreamCoder {
  bool halve = true;     // 16 kHz in (else coded as it comes)
  State st;
  int16_t win[1024];     // input not yet halved, from sample win0 on
  size_t winLen = 0;
  uint64_t win0 = 0;
  uint64_t samples = 0;  // coded so far (the next halved sample's input is 2 * samples)
  int pending = -1;      // a code waiting for its pair
  uint8_t buf[256];
  size_t bufLen = 0;

  template <class Out> void push(const int16_t* pcm, size_t n, Out&& out) {
    while (n) {
      const size_t room = sizeof(win) / sizeof(win[0]) - winLen;
      const size_t take = n < room ? n : room;
      memcpy(win + winLen, pcm, take * sizeof(int16_t));
      winLen += take;
      pcm += take;
      n -= take;
      run(false, out);
    }
    flush(out);
  }

  template <class Out> void finish(Out&& out) {
    run(true, out);
    if (pending >= 0) {
      put((uint8_t)pending, out);
      pending = -1;
    }
    flush(out);
  }

 private:
  template <class Out> void put(uint8_t b, Out& out) {
    buf[bufLen++] = b;
    if (bufLen == sizeof(buf)) flush(out);
  }
  template <class Out> void flush(Out& out) {
    if (bufLen) out(buf, bufLen);
    bufLen = 0;
  }
  template <class Out> void code(int16_t sample, Out& out) {
    const uint8_t c = encode(st, sample);
    if (pending < 0) {
      pending = c;
      return;
    }
    put((uint8_t)(pending | c << 4), out);
    pending = -1;
  }
  template <class Out> void run(bool final, Out& out) {
    if (!halve) {
      for (size_t i = 0; i < winLen; ++i) code(win[i], out);
      win0 += winLen;
      samples += winLen;
      winLen = 0;
      return;
    }
    for (;;) {
      const uint64_t c = 2 * samples, end = win0 + winLen;
      if (final ? c >= end : c + 19 >= end) break;  // (the taps reach 19 either side)
      code(halved(win, winLen, (size_t)(c - win0) / 2), out);
      ++samples;
    }
    // Keep 20 before the next centre (even, so halved()'s index lines up).
    const uint64_t keep = 2 * samples >= 20 ? 2 * samples - 20 : 0;
    if (keep > win0) {
      const size_t drop = (size_t)(keep - win0);
      memmove(win, win + drop, (winLen - drop) * sizeof(int16_t));
      winLen -= drop;
      win0 = keep;
    }
  }
};

}  // namespace adpcm
