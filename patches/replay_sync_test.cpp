// Harness for ReplaySyncServer.  Compiles the real src/replay/replay_sync_server.cpp
// and src/replay/replay_control.cpp - only utils/log.hpp and utils/constants.hpp
// are stubbed (testinc/), so what is under test is the shipped code, not a copy.
//
//   g++ -std=c++11 -pthread -I testinc -I <stk>/src
//       replay_sync_test.cpp <stk>/src/replay/replay_sync_server.cpp
//       <stk>/src/replay/replay_control.cpp -o /tmp/replay_sync_test

#include "replay/replay_sync_server.hpp"
#include "replay/replay_control.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

extern const char STK_VERSION[];
const char STK_VERSION[] = "1.5";
bool g_log_quiet = true;

static const uint16_t PORT = 27991;

static int g_pass = 0, g_fail = 0;

static void check(bool ok, const std::string &what)
{
    if (ok) { g_pass++; printf("  ok   %s\n", what.c_str()); }
    else    { g_fail++; printf("  FAIL %s\n", what.c_str()); }
}

//=============================================================================
/** A minimal viewer: connects, and reads whole lines with a deadline. */
class TestClient
{
public:
    int         m_fd;
    std::string m_buf;

    TestClient() : m_fd(-1) {}
    ~TestClient() { close_it(); }

    bool connect_to(uint16_t port)
    {
        m_fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port   = htons(port);
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (connect(m_fd, (struct sockaddr*)&a, sizeof(a)) != 0)
        {
            close_it();
            return false;
        }
        return true;
    }

    void close_it() { if (m_fd >= 0) { close(m_fd); m_fd = -1; } }

    void send_raw(const std::string &s)
    { ::send(m_fd, s.data(), s.size(), 0); }

    /** Next complete line, or "" if none arrived within timeout_ms.
     *  Returns "<EOF>" if the server closed the connection. */
    std::string line(int timeout_ms = 1000)
    {
        auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(timeout_ms);
        for (;;)
        {
            size_t nl = m_buf.find('\n');
            if (nl != std::string::npos)
            {
                std::string l = m_buf.substr(0, nl);
                m_buf.erase(0, nl + 1);
                return l;
            }
            int left = (int)std::chrono::duration_cast
                <std::chrono::milliseconds>
                (deadline - std::chrono::steady_clock::now()).count();
            if (left <= 0) return "";

            struct pollfd p;
            memset(&p, 0, sizeof(p));
            p.fd = m_fd; p.events = POLLIN;
            if (poll(&p, 1, left) <= 0) return "";
            char b[512];
            ssize_t n = recv(m_fd, b, sizeof(b), 0);
            if (n <= 0) return "<EOF>";
            m_buf.append(b, n);
        }
    }

    /** Throws away everything already buffered.  Without this a test reads a
     *  heartbeat from a second ago rather than the current state. */
    void drain()
    {
        m_buf.clear();
        for (;;)
        {
            struct pollfd p;
            memset(&p, 0, sizeof(p));
            p.fd = m_fd; p.events = POLLIN;
            if (poll(&p, 1, 0) <= 0) return;
            char b[4096];
            if (recv(m_fd, b, sizeof(b), 0) <= 0) return;
        }
    }

    /** Current state, read back from the server rather than guessed at.
     *
     *  The settle first is not cosmetic.  The server broadcasts on its own
     *  thread, so a heartbeat can be half-written at the moment drain() looks
     *  and land immediately afterwards - carrying the state from *before* the
     *  command under test.  Waiting longer than the 50 ms poll timeout means
     *  the command has certainly been applied by the time we drain, so any
     *  heartbeat that races us still carries the new value.  The PING then
     *  just saves waiting for the next scheduled one. */
    bool sync_state(double *t, int *playing, double *rate, int timeout_ms=1000)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        drain();
        send_raw("PING\n");
        return next_state(t, playing, rate, timeout_ms);
    }

    /** As sync_state, but waits for a heartbeat instead of asking for one.
     *  Needed when a half-written command is deliberately left in the pipe:
     *  a PING appended to "SEE" would just make the server read "SEEPING". */
    bool quiet_state(double *t, int *playing, double *rate, int timeout_ms=1000)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        drain();
        return next_state(t, playing, rate, timeout_ms);
    }

    /** Skips ahead to the next STATE line, so a test doesn't have to care how
     *  many heartbeats went past first. */
    bool next_state(double *t, int *playing, double *rate, int timeout_ms=1000)
    {
        auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(timeout_ms);
        for (;;)
        {
            int left = (int)std::chrono::duration_cast
                <std::chrono::milliseconds>
                (deadline - std::chrono::steady_clock::now()).count();
            if (left <= 0) return false;
            std::string l = line(left);
            if (l.empty() || l == "<EOF>") return false;
            if (sscanf(l.c_str(), "STATE %lf %d %lf", t, playing, rate) == 3)
                return true;
        }
    }
};

//=============================================================================
/** Stands in for the game's main loop: calls advance() at 120 Hz the way
 *  WorldStatus::updateTime does, on its own thread, while the tests poke the
 *  server from outside. */
class FakeGameLoop
{
public:
    std::atomic_bool m_stop;
    std::atomic<long> m_ticks;
    std::atomic<long> m_max_stall_us;
    std::thread m_thread;

    FakeGameLoop() : m_stop(false), m_ticks(0), m_max_stall_us(0) {}

    void start()
    {
        m_thread = std::thread([this]()
        {
            while (!m_stop)
            {
                auto t0 = std::chrono::steady_clock::now();
                ReplayControl::get()->advance(1.0 / 120.0);
                auto us = std::chrono::duration_cast
                    <std::chrono::microseconds>
                    (std::chrono::steady_clock::now() - t0).count();
                long prev = m_max_stall_us;
                while (us > prev && !m_max_stall_us.compare_exchange_weak(prev,
                       (long)us)) {}
                m_ticks++;
                std::this_thread::sleep_for(std::chrono::microseconds(8333));
            }
        });
    }
    void stop() { m_stop = true; if (m_thread.joinable()) m_thread.join(); }
};

//=============================================================================
int main()
{
    ReplayControl::create();
    ReplayControl::get()->setEnabled(true);
    ReplayControl::get()->setDuration(123.456);
    ReplayControl::get()->setReplayName("hacienda_wr.replay");

    printf("\n1. listener starts\n");
    check(ReplaySyncServer::create(PORT), "create() succeeds");
    check(ReplaySyncServer::get() != NULL, "get() is non-null");

    printf("\n2. loopback only\n");
    {
        // Binding the same port on a non-loopback address must succeed: if
        // the server had used INADDR_ANY this would fail with EADDRINUSE.
        int s = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port   = htons(PORT);
        // 127.0.0.2 is still loopback but a different address, so it proves
        // the bind is to one address rather than the whole interface.
        inet_pton(AF_INET, "127.0.0.2", &a.sin_addr);
        int r = bind(s, (struct sockaddr*)&a, sizeof(a));
        check(r == 0, "another address on the same port is still free");
        close(s);
    }

    printf("\n3. greeting\n");
    TestClient c1;
    check(c1.connect_to(PORT), "viewer connects");
    {
        std::string hello = c1.line();
        check(hello == "HELLO stk 1.5 1", "HELLO: '" + hello + "'");
        std::string rep = c1.line();
        check(rep == "REPLAY hacienda_wr.replay", "REPLAY: '" + rep + "'");
        std::string dur = c1.line();
        check(dur == "DURATION 123.456", "DURATION: '" + dur + "'");
        double t, rate; int playing;
        check(c1.next_state(&t, &playing, &rate), "STATE follows");
        check(playing == 1 && std::fabs(rate - 1.0) < 1e-9,
              "STATE starts playing at rate 1");
    }

    printf("\n4. heartbeat cadence\n");
    {
        auto t0 = std::chrono::steady_clock::now();
        int n = 0;
        double t, rate; int playing;
        while (n < 10 && c1.next_state(&t, &playing, &rate, 500)) n++;
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>
                  (std::chrono::steady_clock::now() - t0).count();
        check(n == 10, "10 STATE lines arrive");
        // 10 at 10 Hz is ~1 s; allow slack for the poll timeout granularity.
        check(ms > 800 && ms < 1600,
              "10 updates took " + std::to_string(ms) + " ms (want ~1000)");
    }

    printf("\n5. the clock runs, and PAUSE stops it\n");
    FakeGameLoop loop;
    loop.start();
    {
        double t1, t2, rate; int playing;
        c1.sync_state(&t1, &playing, &rate);
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        c1.sync_state(&t2, &playing, &rate);
        check(t2 > t1, "time advances while playing");

        double p1, p2;
        c1.send_raw("PAUSE\n");
        c1.sync_state(&p1, &playing, &rate);
        check(playing == 0, "STATE reports paused");
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        c1.sync_state(&p2, &playing, &rate);
        check(p1 == p2, "time is frozen while paused");

        c1.send_raw("PLAY\n");
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        double r1; c1.sync_state(&r1, &playing, &rate);
        check(playing == 1 && r1 > p2, "PLAY resumes");
    }

    printf("\n6. SEEK\n");
    {
        double t, rate; int playing;
        c1.send_raw("PAUSE\nSEEK 42.5\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(t - 42.5) < 0.05, "seek to 42.5 -> " +
              std::to_string(t));

        c1.send_raw("SEEK 9999\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(t - 123.456) < 0.05,
              "seek past the end clamps to the duration -> " +
              std::to_string(t));

        c1.send_raw("SEEK -50\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(t) < 0.05, "seek before the start clamps to 0 -> " +
              std::to_string(t));
    }

    printf("\n7. RATE\n");
    {
        double t, rate; int playing;
        c1.send_raw("RATE 4\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(rate - 4.0) < 1e-6, "rate is 4, got " +
              std::to_string(rate));

        double a, b;
        c1.send_raw("SEEK 0\nPLAY\n");
        c1.sync_state(&a, &playing, &rate);
        auto w0 = std::chrono::steady_clock::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        c1.sync_state(&b, &playing, &rate);
        double wall = std::chrono::duration_cast<std::chrono::milliseconds>
                      (std::chrono::steady_clock::now() - w0).count() / 1000.0;
        double ratio = (b - a) / wall;
        check(ratio > 3.0 && ratio < 5.0,
              "playback runs ~4x wall clock (measured " +
              std::to_string(ratio) + "x)");

        c1.send_raw("PAUSE\nRATE 1000\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(rate - 16.0) < 1e-6, "absurd rate clamps to 16 -> " +
              std::to_string(rate));

        c1.send_raw("RATE 0\n");
        c1.sync_state(&t, &playing, &rate);
        check(rate > 0.0, "a rate of zero is refused -> " +
              std::to_string(rate));

        c1.send_raw("RATE 1\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(rate - 1.0) < 1e-6, "rate back to 1");
    }

    printf("\n8. PING\n");
    {
        c1.send_raw("PING\n");
        double t, rate; int playing;
        // A PING must be answered well inside the 100 ms heartbeat.
        auto t0 = std::chrono::steady_clock::now();
        bool got = c1.next_state(&t, &playing, &rate, 500);
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>
                  (std::chrono::steady_clock::now() - t0).count();
        check(got, "PING is answered with STATE");
        check(ms < 60, "answered in " + std::to_string(ms) + " ms");
    }

    printf("\n9. framing\n");
    {
        double t, rate; int playing;
        // Split across writes: the server must not act on half a command, and
        // must not lose it either.  Nothing may be sent down this connection
        // until the line is finished, so read heartbeats rather than PINGing.
        c1.send_raw("SEEK 5\n");
        c1.sync_state(&t, &playing, &rate);
        c1.send_raw("SEE");
        double before;
        c1.quiet_state(&before, &playing, &rate);
        check(std::fabs(before - 5.0) < 0.05,
              "half a command does nothing yet (still at " +
              std::to_string(before) + ")");
        c1.send_raw("K 77.0\n");
        c1.quiet_state(&t, &playing, &rate);
        check(std::fabs(t - 77.0) < 0.05,
              "and completing it applies it -> " + std::to_string(t));

        // Several commands in one write.
        c1.send_raw("RATE 2\nSEEK 10\nRATE 3\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(t - 10.0) < 0.05 && std::fabs(rate - 3.0) < 1e-6,
              "three commands in one packet all apply, in order");

        // CRLF, as a line-oriented client on Windows might send.
        c1.send_raw("SEEK 20\r\n");
        c1.sync_state(&t, &playing, &rate);
        check(std::fabs(t - 20.0) < 0.05, "CRLF line endings are accepted");
        c1.send_raw("RATE 1\n");
    }

    printf("\n10. unknown input is ignored, not fatal\n");
    {
        double t, rate; int playing;
        // Lower case is deliberately not accepted; the protocol says
        // uppercase verbs, so "seek 1" is just an unknown verb.
        c1.send_raw("FOLLOW 3\nWHAT\n\nseek 1\nSEEK\nSEEK abc\n");
        c1.send_raw("SEEK 33\n");
        check(c1.sync_state(&t, &playing, &rate), "server still talking");
        check(std::fabs(t - 33.0) < 0.05,
              "unknown verbs skipped, the good one applied -> " +
              std::to_string(t));
        check(std::fabs(rate - 1.0) < 1e-6, "rate untouched by junk");

        // Binary junk, and a very long line with no newline at all.
        c1.send_raw(std::string("\x01\x02\xff\x00\x7f", 5));
        c1.send_raw(std::string(20000, 'x'));
        c1.send_raw("\nSEEK 44\n");
        check(c1.sync_state(&t, &playing, &rate, 1500),
              "survives binary junk and a 20 kB line");
        check(std::fabs(t - 44.0) < 0.05, "and still obeys the next command");
    }

    printf("\n11. several viewers\n");
    {
        TestClient c2;
        check(c2.connect_to(PORT), "second viewer connects");
        check(c2.line() == "HELLO stk 1.5 1", "second viewer gets HELLO");

        double t1, t2, rate; int playing;
        c2.send_raw("PAUSE\nSEEK 55\n");
        check(c2.sync_state(&t2, &playing, &rate) &&
              std::fabs(t2 - 55.0) < 0.05, "the viewer that sent it sees it");
        // c1 has sent nothing at all, so anything it knows arrived by
        // broadcast.  quiet_state only ever listens.
        check(c1.quiet_state(&t1, &playing, &rate) &&
              std::fabs(t1 - 55.0) < 0.05,
              "a seek from one viewer reaches the other, unprompted");

        // Over the limit: extra connections are refused, and the ones that
        // are already up must be unaffected.
        std::vector<TestClient*> extra;
        for (int i = 0; i < 6; i++)
        {
            TestClient *c = new TestClient();
            c->connect_to(PORT);
            extra.push_back(c);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        int greeted = 0;
        for (size_t i = 0; i < extra.size(); i++)
            if (extra[i]->line(300) == "HELLO stk 1.5 1") greeted++;
        check(greeted == 2, "only 2 more viewers accepted (cap 4), got " +
              std::to_string(greeted));
        for (size_t i = 0; i < extra.size(); i++) delete extra[i];

        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        c1.send_raw("SEEK 66\n");
        check(c1.sync_state(&t1, &playing, &rate) &&
              std::fabs(t1 - 66.0) < 0.05,
              "original viewer unharmed by the rejected ones");
    }

    printf("\n12. disconnect and reconnect\n");
    {
        c1.close_it();
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        TestClient c3;
        check(c3.connect_to(PORT), "reconnects after everyone left");
        check(c3.line() == "HELLO stk 1.5 1", "greeted again");
        double t, rate; int playing;
        c3.send_raw("SEEK 88\n");
        check(c3.sync_state(&t, &playing, &rate) &&
              std::fabs(t - 88.0) < 0.05, "still working after a reconnect");

        // Vanish without a FIN handshake being read: the server writes into a
        // dead socket.  If SIGPIPE weren't suppressed this would kill us.
        c3.close_it();
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        check(true, "a viewer dying mid-broadcast doesn't kill the process");
    }

    printf("\n13. the game loop never waits on the network\n");
    {
        long ticks_before = loop.m_ticks;
        loop.m_max_stall_us = 0;

        // A viewer that connects and then never reads: its socket buffer
        // fills and the server's send() would block if it could.
        TestClient slow;
        slow.connect_to(PORT);
        std::this_thread::sleep_for(std::chrono::seconds(2));

        long ticks = loop.m_ticks - ticks_before;
        check(ticks > 180, "main loop ran " + std::to_string(ticks) +
              " ticks in 2 s (unimpeded)");
        check(loop.m_max_stall_us < 5000, "longest advance() call was " +
              std::to_string((long)loop.m_max_stall_us) +
              " us (no network wait)");
    }

    printf("\n14. a replay loaded after the viewer connected\n");
    {
        // The case that matters for "pick a replay in-game and the map
        // follows": the viewer is already connected, so it can only learn
        // what is loaded from a broadcast.  Announcing only at connect time
        // leaves it blind here.
        TestClient c6;
        check(c6.connect_to(PORT), "a viewer connects while idle");
        c6.line(); c6.line(); c6.line(); c6.line();   // drain the greeting

        ReplayControl::get()->setDuration(88.5);
        ReplayControl::get()->setReplayName("picked_in_game.replay");

        std::string rep, dur;
        for (int i = 0; i < 40 && (rep.empty() || dur.empty()); i++)
        {
            std::string l = c6.line(200);
            if (l.compare(0, 7, "REPLAY ") == 0)   rep = l;
            if (l.compare(0, 9, "DURATION ") == 0) dur = l;
        }
        check(rep == "REPLAY picked_in_game.replay",
              "REPLAY is broadcast to the already-connected viewer: '" +
              rep + "'");
        check(dur == "DURATION 88.500", "so is the new DURATION: '" + dur + "'");

        // Setting the same name again must not re-announce, or a viewer would
        // reload the file it is already showing every time World::reset runs.
        c6.drain();
        ReplayControl::get()->setReplayName("picked_in_game.replay");
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        bool repeated = false;
        for (int i = 0; i < 8; i++)
        {
            std::string l = c6.line(100);
            if (l.empty()) break;
            if (l.compare(0, 7, "REPLAY ") == 0) repeated = true;
        }
        check(!repeated, "re-setting the same name doesn't re-announce it");

        // A different replay must announce again.
        ReplayControl::get()->setDuration(45.25);
        ReplayControl::get()->setReplayName("second_run.replay");
        std::string rep2;
        for (int i = 0; i < 40 && rep2.empty(); i++)
        {
            std::string l = c6.line(200);
            if (l.compare(0, 7, "REPLAY ") == 0) rep2 = l;
        }
        check(rep2 == "REPLAY second_run.replay",
              "switching replay announces the new one: '" + rep2 + "'");

        // reset() must clear the name, so leaving a replay for an ordinary
        // race doesn't leave the viewer thinking one is still loaded.
        ReplayControl::get()->reset();
        check(ReplayControl::get()->getReplayName().empty(),
              "reset() clears the loaded replay name");
    }

    printf("\n15. shutdown\n");
    {
        TestClient c4;
        c4.connect_to(PORT);
        c4.line();
        loop.stop();
        ReplaySyncServer::destroy();
        check(ReplaySyncServer::get() == NULL, "get() is null after destroy");

        // Drain until BYE or EOF; either is a clean goodbye.
        bool said_bye = false, closed = false;
        for (int i = 0; i < 40; i++)
        {
            std::string l = c4.line(200);
            if (l == "BYE")   { said_bye = true; }
            if (l == "<EOF>") { closed = true; break; }
            if (l.empty())    break;
        }
        check(said_bye, "connected viewer is told BYE");
        check(closed, "and the socket is closed");

        TestClient c5;
        check(!c5.connect_to(PORT), "port is free again");
    }

    ReplayControl::destroy();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
