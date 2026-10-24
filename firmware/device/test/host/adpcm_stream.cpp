// Mac check: a recording coded as it comes, in blocks of any size
// (adpcm::StreamCoder), gives the same bytes as the whole of it coded at once
// (as notes.cpp did before notes were written while recorded) — byte for
// byte, but for one thing: halving an odd number of samples, the stream also
// codes the last one (its centre is in the recording), where the whole-
// recording coder took n / 2 and left it out.
//
//   c++ -std=c++17 -O2 -o /tmp/adpcm_stream test/host/adpcm_stream.cpp && /tmp/adpcm_stream
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../../src/adpcm.h"

static std::vector<uint8_t> whole(const std::vector<int16_t>& pcm, bool halve) {
  const size_t kept = halve ? (pcm.size() + 1) / 2 : pcm.size();  // (see above)
  adpcm::State st;
  std::vector<uint8_t> out;
  auto at = [&](size_t i) { return halve ? adpcm::halved(pcm.data(), pcm.size(), i) : pcm[i]; };
  for (size_t i = 0; i < kept; i += 2) {
    uint8_t b = adpcm::encode(st, at(i));
    if (i + 1 < kept) b |= adpcm::encode(st, at(i + 1)) << 4;
    out.push_back(b);
  }
  return out;
}

static std::vector<uint8_t> streamed(const std::vector<int16_t>& pcm, bool halve, size_t block) {
  adpcm::StreamCoder c;
  c.halve = halve;
  std::vector<uint8_t> out;
  auto sink = [&](const uint8_t* b, size_t n) { out.insert(out.end(), b, b + n); };
  for (size_t i = 0; i < pcm.size(); i += block) c.push(pcm.data() + i, std::min(block, pcm.size() - i), sink);
  c.finish(sink);
  return out;
}

int main() {
  int fails = 0;
  srand(7);
  for (size_t len : {0u, 1u, 2u, 3u, 19u, 20u, 21u, 39u, 40u, 511u, 512u, 513u, 1023u, 16000u, 48001u, 160000u}) {
    std::vector<int16_t> pcm(len);
    for (size_t i = 0; i < len; ++i) {
      pcm[i] = (int16_t)(9000 * std::sin(i * 0.031) + 4000 * std::sin(i * 0.47) + (rand() % 2001 - 1000));
    }
    for (bool halve : {true, false}) {
      const auto want = whole(pcm, halve);
      for (size_t block : {1u, 7u, 512u, 1000u, 1024u, 5000u}) {
        const auto got = streamed(pcm, halve, block);
        if (got != want) {
          ++fails;
          printf("FAIL len=%zu halve=%d block=%zu: %zu vs %zu bytes\n", len, halve, block, got.size(), want.size());
        }
      }
    }
  }
  printf(fails ? "%d failures\n" : "all equal\n", fails);
  return fails ? 1 : 0;
}
