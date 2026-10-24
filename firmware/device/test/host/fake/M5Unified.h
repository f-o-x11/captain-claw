// Mac stand-in for M5Unified's speaker and mic, as audio.cpp uses them. The
// speaker plays its queued pieces on the simulated clock, back to back, and
// reads each piece's samples only as they're played — so a sample written
// over before it sounded shows up in what was "heard".
#pragma once
#include <deque>
#include <vector>

#include "Arduino.h"

struct FakeSpeaker {
  struct Piece {
    const int16_t* ptr;
    size_t n;
    uint32_t rate;
    uint32_t startMs;  // when it began to sound
    size_t done;       // samples heard
  };
  std::deque<Piece> q;
  std::vector<int16_t> heard;
  bool enabled = false;
  bool begin() { return enabled = true; }
  void end() { enabled = false; q.clear(); }
  bool isEnabled() const { return enabled; }
  void setVolume(uint8_t) {}
  void tone(float, uint32_t, int = -1, bool = true) {}
  size_t isPlaying(uint8_t ch) const { return ch == 0 ? q.size() : 0; }
  void stop(uint8_t ch) {
    if (ch != 0) return;
    advance();
    q.clear();
  }
  bool playRaw(const int16_t* p, size_t n, uint32_t rate, bool, uint32_t, int ch, bool) {
    if (ch != 0) return true;
    if (q.size() >= 2) return false;
    q.push_back({p, n, rate, q.empty() ? g_simMs : 0, 0});
    return true;
  }
  // Up to the clock: what's due of the front piece is heard, then the next.
  void advance() {
    while (!q.empty()) {
      Piece& f = q.front();
      const size_t due = (size_t)((uint64_t)(g_simMs - f.startMs) * f.rate / 1000);
      const size_t upTo = due < f.n ? due : f.n;
      for (; f.done < upTo; ++f.done) heard.push_back(f.ptr[f.done]);
      if (f.done < f.n) return;
      const uint32_t endMs = f.startMs + (uint32_t)((uint64_t)f.n * 1000 / f.rate);
      q.pop_front();
      if (!q.empty()) q.front().startMs = endMs;
    }
  }
};

struct FakeMic {
  bool begin() { return true; }
  void end() {}
  size_t isRecording() const { return 0; }
  bool record(int16_t*, size_t, uint32_t) { return true; }
};

struct FakeM5 {
  FakeSpeaker Speaker;
  FakeMic Mic;
};
extern FakeM5 M5;
