// Stub for the sync-server harness: the real log.hpp drags in most of STK.
#ifndef HEADER_LOG_HPP
#define HEADER_LOG_HPP
#include <cstdio>
#include <cstdarg>
extern bool g_log_quiet;
class Log
{
    static void emit(const char *lvl, const char *c, const char *f, va_list ap)
    {
        if (g_log_quiet) return;
        fprintf(stderr, "[%s] %s: ", lvl, c);
        vfprintf(stderr, f, ap);
        fprintf(stderr, "\n");
    }
public:
    static void info(const char *c, const char *f, ...)
    { va_list ap; va_start(ap, f); emit("info", c, f, ap); va_end(ap); }
    static void error(const char *c, const char *f, ...)
    { va_list ap; va_start(ap, f); emit("error", c, f, ap); va_end(ap); }
    static void warn(const char *c, const char *f, ...)
    { va_list ap; va_start(ap, f); emit("warn", c, f, ap); va_end(ap); }
};
#endif
