// Checks how many times ReplayPlay::loadFile() would call readKartData() for a
// given replay, before and after the blank-line fix.  It must be exactly the
// number of karts the file's own header declares - one call too many indexes
// past m_kart_list and, because the "size:" line is validated only *after*
// that indexing, throws std::out_of_range and aborts the game.
//
// The header-skip arithmetic and the read loop are lifted from
// src/replay/replay_play.cpp so this measures the real behaviour.
#include <cstdio>
#include <cstring>
#include <cctype>
#include <string>

struct Header { int version = 0; int num_kart = 0; bool has_info = false; };

static Header readHeader(const char *path)
{
    Header h;
    FILE *fd = fopen(path, "r");
    if (!fd) return h;
    char s[1024];
    while (fgets(s, 1023, fd))
    {
        if (!strncmp(s, "version:", 8))      sscanf(s, "version: %d", &h.version);
        else if (!strncmp(s, "kart:", 5))    h.num_kart++;
        else if (!strncmp(s, "info:", 5))    h.has_info = true;
        else if (!strncmp(s, "size:", 5))    break;
    }
    fclose(fd);
    return h;
}

// Returns the number of readKartData() calls loadFile() would make.
// skip_blank = false reproduces stock 1.5; true is the patched behaviour.
static int countKartDataCalls(const char *path, const Header &h, bool skip_blank)
{
    FILE *fd = fopen(path, "r");
    if (!fd) return -1;
    char s[1024];

    unsigned int lines_to_skip = (h.version == 3) ? 7 : 10;
    lines_to_skip += (h.version == 3) ? h.num_kart : 2 * h.num_kart;
    lines_to_skip += h.has_info ? 1 : 0;
    for (unsigned int i = 0; i < lines_to_skip; i++)
        if (!fgets(s, 1023, fd)) break;

    int calls = 0;
    while (!feof(fd))
    {
        if (fgets(s, 1023, fd) == NULL) break;

        if (skip_blank)
        {
            bool is_blank = true;
            for (const char *p = s; *p != '\0'; p++)
                if (!isspace((unsigned char)*p)) { is_blank = false; break; }
            if (is_blank) continue;
        }

        // readKartData(): consumes its "size: N" line then N data rows
        calls++;
        unsigned int size = 0;
        if (sscanf(s, "size: %u", &size) != 1)
        {
            // stock aborts here via Log::fatal - but only *after* it has
            // already indexed m_kart_list, which is the actual crash
            fclose(fd);
            return calls;
        }
        for (unsigned int i = 0; i < size; i++)
            if (!fgets(s, 1023, fd)) break;
    }
    fclose(fd);
    return calls;
}

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: %s <replay files...>\n", argv[0]); return 2; }

    int broken_before = 0, broken_after = 0, checked = 0;
    for (int i = 1; i < argc; i++)
    {
        Header h = readHeader(argv[i]);
        if (h.num_kart == 0) continue;
        int before = countKartDataCalls(argv[i], h, false);
        int after  = countKartDataCalls(argv[i], h, true);
        checked++;

        const char *base = strrchr(argv[i], '/');
        base = base ? base + 1 : argv[i];

        if (before != h.num_kart || after != h.num_kart)
        {
            printf("  %-46s karts=%d  stock=%d %s  patched=%d %s\n",
                   base, h.num_kart,
                   before, before == h.num_kart ? "ok" : "<-- CRASH",
                   after,  after  == h.num_kart ? "ok" : "<-- CRASH");
        }
        if (before != h.num_kart) broken_before++;
        if (after  != h.num_kart) broken_after++;
    }

    printf("\n%d replays checked\n", checked);
    printf("  stock 1.5 : %d would call readKartData too many times (crash)\n",
           broken_before);
    printf("  patched   : %d\n", broken_after);
    return broken_after ? 1 : 0;
}
