#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "config_mode.h"

static usbnet_profile_t saved;
static unsigned saves;
void profile_defaults(usbnet_profile_t *p) { memset(p, 0, sizeof(*p)); p->mode = 0; }
bool profile_valid(const usbnet_profile_t *p) { return p->mode <= 1; }
bool profile_save(const usbnet_profile_t *p) { saved = *p; saves++; return true; }
bool profile_load(usbnet_profile_t *p) { (void)p; return false; }

static void call(uint8_t op, uint16_t offset, const uint8_t *data, uint8_t length, uint8_t out[64])
{
    uint8_t request[64] = {0};
    request[0] = 0xc7; request[1] = op; request[2] = 42;
    request[3] = length; request[4] = offset & 0xff; request[5] = offset >> 8;
    if (data) memcpy(request + 6, data, length);
    config_mode_handle(request, out);
    assert(out[0] == 0xc7 && out[1] == (op | 0x80) && out[2] == 42);
}

int main(void)
{
    usbnet_profile_t initial = {0};
    initial.mode = 0; initial.vid = 0x1a86;
    config_mode_init(&initial, 0);
    uint8_t out[64];
    call(1, 0, NULL, 0, out);
    assert(out[3] == 0 && (out[8] | (out[9] << 8)) == sizeof(initial));
    call(2, 0, NULL, 3, out);
    assert(out[3] == 0 && out[7] == 0 && out[8] == 0x86 && out[9] == 0x1a);

    usbnet_profile_t next = initial;
    next.mode = 1; next.pid = 0x1234;
    call(3, 0, NULL, 0, out); assert(out[3] == 0);
    const uint8_t *bytes = (const uint8_t *)&next;
    for (uint16_t offset = 0; offset < sizeof(next); offset += 58) {
        uint8_t count = (sizeof(next) - offset) < 58 ? sizeof(next) - offset : 58;
        call(4, offset, bytes + offset, count, out);
        assert(out[3] == 0);
    }
    call(5, 0, NULL, 0, out);
    assert(out[3] == 0 && saves == 1 && saved.mode == 1 && saved.pid == 0x1234);
    call(2, 0, NULL, 57, out);
    assert(out[3] == 0 && memcmp(out + 7, &next, 57) == 0);
    call(4, 0, bytes, 1, out);
    assert(out[3] == 1); /* no write outside BEGIN */
    puts("config mode protocol passed");
    return 0;
}
