// Mac stand-in for what audio.cpp takes from Arduino: a simulated clock.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>

extern uint32_t g_simMs;
inline uint32_t millis() { return g_simMs; }
void delay(uint32_t ms);

template <class A, class B> inline auto min(A a, B b) -> decltype(a < b ? a : b) { return a < b ? a : b; }
template <class A, class B> inline auto max(A a, B b) -> decltype(a > b ? a : b) { return a > b ? a : b; }

#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
inline void* heap_caps_malloc(size_t n, int) { return malloc(n); }
