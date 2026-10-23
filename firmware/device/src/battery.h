// How fast the battery charges or drains, and what that means in time.
//
// There's no fuel gauge to ask for the current, so the rate comes from the
// level over the clock — through the deep sleeps too, which is where most of
// the time goes (the samples live in RTC memory, which sleep keeps). A new
// charging state starts over: the old rate says nothing about the new one.
#pragma once

#include <Arduino.h>

namespace battery {

void begin();
// From loop(): a sample a minute while awake.
void poll();
// A sample now (before sleep, so the next wake has the long end of the window).
void sample();
bool charging();
// Minutes left on battery, or until full — -1 while there isn't enough
// history to say (or the state doesn't apply); 0 to full = it's full.
int minutesLeft();
int minutesToFull();

}  // namespace battery
