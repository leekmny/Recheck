#include <stdint.h>

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

/* The caller supplies at least len live bytes. A quote uses two LE bytes. */
EXPORT int32_t parcel_quote(int32_t weight, uint8_t *out, uint32_t len) {
    if (weight < 0 || weight > 100) return -1;
    if (len < 2 || !out) return -2;
    uint16_t fee = (uint16_t)(100 + 15 * weight);
    out[0] = (uint8_t)fee;
    out[1] = (uint8_t)(fee >> 8);
    return 0;
}
