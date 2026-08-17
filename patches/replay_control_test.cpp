// Drives the real ReplayControl class (compiled straight from the patched
// tree - not a copy) through the cases that matter for scrubbing a replay.
// It has no STK dependencies beyond <atomic>/<mutex>, so it links standalone.
#include "replay/replay_control.hpp"

#include <cstdio>
#include <cmath>
#include <thread>
#include <vector>

static int g_fail = 0;

static void check(const char *what, bool ok, const char *detail = "")
{
    printf("  %-52s %s%s%s\n", what, ok ? "ok" : "FAIL",
           detail[0] ? "  " : "", detail);
    if (!ok) g_fail++;
}

static bool near(double a, double b, double eps = 1e-6)
{
    return std::fabs(a - b) < eps;
}

// one tick at STK's default 120Hz physics rate
static const double TICK = 1.0 / 120.0;

int main()
{
    ReplayControl::create();
    ReplayControl *rc = ReplayControl::get();

    printf("default state\n");
    check("disabled until the CLI flag switches it on", !rc->isEnabled());
    check("starts playing", rc->isPlaying());
    check("starts at rate 1", near(rc->getRate(), 1.0));
    check("starts at t=0", near(rc->getTime(), 0.0));

    printf("\nrate 1 must count up exactly like the stock clock\n");
    {
        rc->reset();
        double stock = 0.0;
        for (int i = 0; i < 1200; i++)      // 10 seconds
        {
            rc->advance(TICK);
            stock += TICK;
        }
        check("10s of ticks matches a plain accumulator",
              near(rc->getTime(), stock, 1e-9));
    }

    printf("\npause\n");
    {
        rc->reset();
        for (int i = 0; i < 120; i++) rc->advance(TICK);
        double t = rc->getTime();
        rc->setPlaying(false);
        for (int i = 0; i < 600; i++) rc->advance(TICK);
        check("time does not move while paused", near(rc->getTime(), t));
        rc->setPlaying(true);
        for (int i = 0; i < 120; i++) rc->advance(TICK);
        check("resumes from where it paused", near(rc->getTime(), t + 1.0, 1e-9));
    }

    printf("\nrate\n");
    {
        rc->reset();
        rc->setRate(0.25);
        for (int i = 0; i < 480; i++) rc->advance(TICK);   // 4s real
        check("0.25x covers a quarter of the time", near(rc->getTime(), 1.0, 1e-9));

        rc->reset();
        rc->setRate(4.0);
        for (int i = 0; i < 120; i++) rc->advance(TICK);   // 1s real
        check("4x covers four times the time", near(rc->getTime(), 4.0, 1e-9));

        rc->setRate(0.0);
        check("a rate of 0 is refused (would fake a pause)", rc->getRate() > 0.0);
        rc->setRate(-5.0);
        check("a negative rate is refused", rc->getRate() > 0.0);
        rc->setRate(1000.0);
        check("an absurd rate is clamped", rc->getRate() <= 16.0);
    }

    printf("\nseek\n");
    {
        rc->reset();
        rc->setDuration(90.0);
        rc->seek(45.0);
        check("seeks to the requested time", near(rc->getTime(), 45.0));
        rc->seek(-10.0);
        check("clamps below zero", near(rc->getTime(), 0.0));
        rc->seek(1e6);
        check("clamps past the end", near(rc->getTime(), 90.0));

        // the case patch 0001 exists for: jump back after reaching the end
        rc->seek(90.0);
        rc->seek(20.0);
        check("can seek backwards from the end", near(rc->getTime(), 20.0));
    }

    printf("\nend of replay\n");
    {
        rc->reset();
        rc->setDuration(2.0);
        for (int i = 0; i < 600; i++) rc->advance(TICK);   // 5s of ticks
        check("holds at the end rather than running past",
              near(rc->getTime(), 2.0));
        check("stops playing at the end", !rc->isPlaying());
        rc->setPlaying(true);
        check("pressing play at the end restarts from the top",
              near(rc->getTime(), 0.0));
    }

    printf("\nunknown duration (nothing has told it the length yet)\n");
    {
        rc->reset();                        // duration 0
        rc->seek(500.0);
        check("no upper clamp when the length isn't known",
              near(rc->getTime(), 500.0));
        for (int i = 0; i < 120; i++) rc->advance(TICK);
        check("keeps counting with no known end",
              near(rc->getTime(), 501.0, 1e-9));
    }

    printf("\ndirty flag (so the server can report a jump immediately)\n");
    {
        rc->reset();
        rc->pollDirty();                    // clear
        rc->advance(TICK);
        check("ordinary playback does not set it", !rc->pollDirty());
        rc->seek(5.0);
        check("a seek sets it", rc->pollDirty());
        check("polling clears it", !rc->pollDirty());
        rc->setPlaying(false);
        check("pause sets it", rc->pollDirty());
        rc->setRate(2.0);
        check("a rate change sets it", rc->pollDirty());
    }

    printf("\nthread safety (commands arrive from the socket thread)\n");
    {
        rc->reset();
        rc->setDuration(1000.0);
        std::vector<std::thread> ts;
        for (int i = 0; i < 4; i++)
        {
            ts.emplace_back([rc, i]()
            {
                for (int n = 0; n < 20000; n++)
                {
                    rc->seek((i * 20000 + n) % 900);
                    rc->setRate(1.0 + (n % 4));
                    rc->setPlaying(n % 2 == 0);
                    (void)rc->getTime();
                }
            });
        }
        // main thread keeps ticking, as it would in the game
        for (int n = 0; n < 40000; n++) rc->advance(TICK);
        for (auto &t : ts) t.join();
        double t = rc->getTime();
        check("no crash or torn state under concurrent access",
              t >= 0.0 && t <= 1000.0);
    }

    ReplayControl::destroy();
    printf("\n%s\n", g_fail ? "FAILURES" : "all checks passed");
    return g_fail ? 1 : 0;
}
