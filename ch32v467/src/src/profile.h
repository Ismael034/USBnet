#ifndef USBNET_PROFILE_H
#define USBNET_PROFILE_H
#include <stdbool.h>
#include <stdint.h>

#define USBNET_PROFILE_SIZE 675
#define USBNET_REPORT_DESC_MAX 512
#define USBNET_STRING_SIZE 48
#define USBNET_PROFILE_RELAY 0
#define USBNET_PROFILE_EMULATE 1

/* Packed, versioned on-flash and WebHID wire format; little-endian fields. */
typedef struct __attribute__((packed)) {
    uint8_t mode;
    uint16_t vid, pid, bcd_device;
    uint8_t device_class, device_subclass, device_protocol;
    uint8_t interface_subclass, interface_protocol;
    uint8_t interval, max_power_units, attributes;
    uint8_t input_size, output_size;
    uint16_t report_len;
    char manufacturer[USBNET_STRING_SIZE];
    char product[USBNET_STRING_SIZE];
    char serial[USBNET_STRING_SIZE];
    uint8_t report_desc[USBNET_REPORT_DESC_MAX];
} usbnet_profile_t;

typedef char profile_size_must_match[(sizeof(usbnet_profile_t) == USBNET_PROFILE_SIZE) ? 1 : -1];

void profile_defaults(usbnet_profile_t *profile);
bool profile_valid(const usbnet_profile_t *profile);
bool profile_load(usbnet_profile_t *profile);
bool profile_save(const usbnet_profile_t *profile);

#endif
