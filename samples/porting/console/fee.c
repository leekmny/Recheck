#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

static long fee(long units) {
    long total = 0;
    for (long i = 0; i < units; ++i) {
        total += i < 3 ? 100 : 75;
    }
    return total;
}

int main(int argc, char **argv) {
    char *end;
    errno = 0;
    long units = argc == 2 ? strtol(argv[1], &end, 10) : -1;
    if (argc != 2 || end == argv[1] || *end || errno || units < 0 || units > 100) {
        fputs("invalid\n", stderr);
        return 2;
    }
    printf("%ld\n", fee(units));
    return 0;
}
