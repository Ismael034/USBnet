/* Fixed vendor-HID configuration protocol on USB1 while USER is held at boot. */
#include <string.h>
#include "config_mode.h"

#define CONFIG_MAGIC 0xc7
#define CMD_INFO 1
#define CMD_READ 2
#define CMD_BEGIN 3
#define CMD_WRITE 4
#define CMD_COMMIT 5
#define STATUS_OK 0
#define STATUS_BAD_REQUEST 1
#define STATUS_BAD_PROFILE 2
#define STATUS_FLASH_ERROR 3

static usbnet_profile_t current;
static usbnet_profile_t staged;
static uint16_t staged_next;
static uint8_t staging;
static uint8_t saved_profile;

void config_mode_init(const usbnet_profile_t *active, uint8_t saved)
{
    memcpy(&current, active, sizeof(current));
    saved_profile = saved;
    staging = 0;
    staged_next = 0;
}

void config_mode_handle(const uint8_t request[64], uint8_t response[64])
{
    memset(response, 0, 64);
    response[0] = CONFIG_MAGIC;
    response[1] = request[1] | 0x80;
    response[2] = request[2];
    response[3] = STATUS_BAD_REQUEST;
    if (request[0] != CONFIG_MAGIC) return;
    uint16_t offset = (uint16_t)request[4] | ((uint16_t)request[5] << 8);
    uint8_t length = request[3];
    switch (request[1]) {
    case CMD_INFO:
        response[3] = STATUS_OK;
        response[4] = 5;
        response[7] = 1; /* wire format version */
        response[8] = sizeof(usbnet_profile_t) & 0xff;
        response[9] = sizeof(usbnet_profile_t) >> 8;
        response[10] = saved_profile;
        response[11] = current.mode;
        return;
    case CMD_READ:
        if (length > 57 || offset > sizeof(current) || length > sizeof(current) - offset) return;
        response[3] = STATUS_OK;
        response[4] = length;
        response[5] = offset & 0xff;
        response[6] = offset >> 8;
        memcpy(response + 7, (const uint8_t *)&current + offset, length);
        return;
    case CMD_BEGIN:
        memset(&staged, 0, sizeof(staged));
        staged_next = 0;
        staging = 1;
        response[3] = STATUS_OK;
        return;
    case CMD_WRITE:
        if (!staging || length > 58 || offset != staged_next ||
            offset > sizeof(staged) || length > sizeof(staged) - offset) return;
        memcpy((uint8_t *)&staged + offset, request + 6, length);
        staged_next += length;
        response[3] = STATUS_OK;
        return;
    case CMD_COMMIT:
        if (!staging || staged_next != sizeof(staged)) return;
        if (!profile_valid(&staged)) { response[3] = STATUS_BAD_PROFILE; return; }
        if (!profile_save(&staged)) { response[3] = STATUS_FLASH_ERROR; return; }
        memcpy(&current, &staged, sizeof(current));
        saved_profile = 1;
        staging = 0;
        response[3] = STATUS_OK;
        return;
    default:
        return;
    }
}
