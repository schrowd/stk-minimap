// Standalone check of the GhostController index search, before and after the
// seek patch.  The real class needs a whole World/Camera/kart graph to
// instantiate, so the index logic is lifted verbatim here and driven directly.
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <string>
#include <fstream>
#include <sstream>
#include <algorithm>

// ---- verbatim from ghost_controller.cpp, pre-patch -----------------------
static void update_old(unsigned int &idx, float t, const std::vector<float> &at)
{
    if (t != 0.0f)
    {
        while (idx + 1 < at.size() && t >= at[idx + 1])
            idx++;
    }
}

// ---- verbatim from ghost_controller.cpp, post-patch ----------------------
static void update_new(unsigned int &idx, float t, const std::vector<float> &at)
{
    while (idx > 0 && t < at[idx])
        idx--;

    if (t != 0.0f)
    {
        while (idx + 1 < at.size() && t >= at[idx + 1])
            idx++;
    }
}

// The index the definition demands: the largest i with at[i] <= t (and 0 when
// t is before the first sample).  Computed independently of both loops.
static unsigned int reference(float t, const std::vector<float> &at)
{
    unsigned int best = 0;
    for (unsigned int i = 0; i < at.size(); i++)
        if (at[i] <= t) best = i;
    return best;
}

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "need a .replay path\n"); return 2; }

    // pull the time column (field 0) out of a real replay
    std::vector<float> times;
    std::ifstream in(argv[1]);
    std::string line;
    bool started = false;
    while (std::getline(in, line))
    {
        if (!started)
        {
            if (line.rfind("size:", 0) == 0) started = true;
            continue;
        }
        std::istringstream ss(line);
        float t;
        if (ss >> t)
        {
            if (times.empty() || times.back() != t)   // addReplayTime dedupes
                times.push_back(t);
        }
    }
    if (times.size() < 10) { fprintf(stderr, "no usable times\n"); return 2; }
    printf("replay: %zu distinct times, %.2f .. %.2f\n",
           times.size(), times.front(), times.back());

    int fail_old = 0, fail_new = 0;

    // 1. ordinary forward playback: both must agree with the reference
    {
        unsigned int io = 0, in_ = 0;
        for (float t = 0.0f; t <= times.back() + 0.5f; t += 0.01f)
        {
            update_old(io, t, times);
            update_new(in_, t, times);
            unsigned int r = reference(t, times);
            if (io != r) fail_old++;
            if (in_ != r) fail_new++;
        }
        printf("forward playback   : old %s, new %s\n",
               fail_old ? "MISMATCH" : "ok", fail_new ? "MISMATCH" : "ok");
    }

    // 2. seeks: jump all over the file, including backwards and to 0
    {
        unsigned int io = 0, in_ = 0;
        int bad_old = 0, bad_new = 0, checked = 0;
        srand(1);
        for (int n = 0; n < 20000; n++)
        {
            float t = (float)(rand() / (double)RAND_MAX) * times.back();
            if (n % 7 == 0) t = 0.0f;                    // seek to the start
            if (n % 11 == 0) t = times.back();           // seek to the end
            update_old(io, t, times);
            update_new(in_, t, times);
            unsigned int r = reference(t, times);
            checked++;
            if (io != r) bad_old++;
            if (in_ != r) bad_new++;
        }
        printf("random seeking     : old %d/%d wrong, new %d/%d wrong\n",
               bad_old, checked, bad_new, checked);
        fail_old += bad_old; fail_new += bad_new;
    }

    // 3. the specific case that matters: play to the end, then rewind
    {
        unsigned int io = 0, in_ = 0;
        update_old(io, times.back(), times);
        update_new(in_, times.back(), times);
        float back = times[times.size() / 4];
        update_old(io, back, times);
        update_new(in_, back, times);
        unsigned int r = reference(back, times);
        printf("rewind from end    : reference=%u  old=%u %s  new=%u %s\n",
               r, io, io == r ? "ok" : "STUCK", in_, in_ == r ? "ok" : "STUCK");
        if (io != r) fail_old++;
        if (in_ != r) fail_new++;
    }

    printf("\nold implementation: %s\n", fail_old ? "FAILS on seeks (expected)"
                                                  : "no failures");
    printf("new implementation: %s\n", fail_new ? "STILL BROKEN"
                                                : "matches reference in all cases");
    return fail_new ? 1 : 0;
}
