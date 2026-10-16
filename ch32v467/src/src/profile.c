/* Two-slot internal CodeFlash profile store. Magic is programmed last. */
#include <stddef.h>
#include <string.h>
#include "profile.h"
#include "ch32v4x7_flash.h"

#define PROFILE_MAGIC 0x31474643u /* CFG1 */
#define PROFILE_PAGE_A 0x0808E000u
#define PROFILE_PAGE_B 0x0808F000u
#define PROFILE_PAGE_SIZE 4096u

typedef struct {
    uint32_t magic;
    uint32_t sequence;
    uint32_t length;
    uint32_t crc;
    usbnet_profile_t profile;
} profile_record_t;

static profile_record_t write_record;
static const uint8_t vendor_report_desc[] = {
    0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01,
    0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08,
    0x95, 0x40, 0x09, 0x02, 0x81, 0x02,
    0x95, 0x40, 0x09, 0x03, 0x91, 0x02, 0xc0,
};

static uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t size)
{
    for (size_t i = 0; i < size; i++) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return crc;
}

static uint32_t record_crc(const profile_record_t *record)
{
    uint32_t crc = 0xffffffffu;
    crc = crc32_update(crc, (const uint8_t *)&record->sequence, sizeof(record->sequence));
    crc = crc32_update(crc, (const uint8_t *)&record->length, sizeof(record->length));
    crc = crc32_update(crc, (const uint8_t *)&record->profile, sizeof(record->profile));
    return ~crc;
}

void profile_defaults(usbnet_profile_t *profile)
{
    memset(profile, 0, sizeof(*profile));
    profile->mode = USBNET_PROFILE_RELAY;
    profile->vid = 0x1a86;
    profile->pid = 0xfe51;
    profile->bcd_device = 0x0100;
    profile->interval = 1;
    profile->max_power_units = 50;
    profile->attributes = 0x80;
    profile->input_size = 64;
    profile->output_size = 64;
    profile->report_len = sizeof(vendor_report_desc);
    memcpy(profile->report_desc, vendor_report_desc, sizeof(vendor_report_desc));
    memcpy(profile->manufacturer, "USBnet", 7);
    memcpy(profile->product, "USBnet HID Device", 18);
}

static bool valid_string(const char *value)
{
    bool terminated = false;
    for (size_t i = 0; i < USBNET_STRING_SIZE; i++) {
        uint8_t c = (uint8_t)value[i];
        if (c == 0) { terminated = true; break; }
        if (c < 0x20 || c > 0x7e) return false;
    }
    return terminated;
}

bool profile_valid(const usbnet_profile_t *profile)
{
    if (profile->mode > USBNET_PROFILE_EMULATE ||
        profile->vid == 0 || profile->pid == 0 ||
        profile->report_len == 0 || profile->report_len > USBNET_REPORT_DESC_MAX ||
        profile->input_size == 0 || profile->input_size > 64 ||
        profile->output_size == 0 || profile->output_size > 64 ||
        profile->interval == 0 || profile->interval > 16 ||
        (profile->attributes & 0x80) == 0 ||
        !valid_string(profile->manufacturer) ||
        !valid_string(profile->product) ||
        !valid_string(profile->serial)) return false;
    return true;
}

static bool record_valid(const profile_record_t *record)
{
    return record->magic == PROFILE_MAGIC &&
           record->length == sizeof(usbnet_profile_t) &&
           record->crc == record_crc(record) &&
           profile_valid(&record->profile);
}

static const profile_record_t *slot(uint32_t address)
{
    return (const profile_record_t *)(uintptr_t)address;
}

bool profile_load(usbnet_profile_t *profile)
{
    const profile_record_t *a = slot(PROFILE_PAGE_A);
    const profile_record_t *b = slot(PROFILE_PAGE_B);
    bool va = record_valid(a), vb = record_valid(b);
    if (!va && !vb) { profile_defaults(profile); return false; }
    const profile_record_t *newest = (!va || (vb && (int32_t)(b->sequence - a->sequence) > 0)) ? b : a;
    memcpy(profile, &newest->profile, sizeof(*profile));
    return true;
}

bool profile_save(const usbnet_profile_t *profile)
{
    if (!profile_valid(profile) || sizeof(write_record) > PROFILE_PAGE_SIZE) return false;
    const profile_record_t *a = slot(PROFILE_PAGE_A);
    const profile_record_t *b = slot(PROFILE_PAGE_B);
    bool va = record_valid(a), vb = record_valid(b);
    uint32_t destination, sequence;
    if (!va && !vb) { destination = PROFILE_PAGE_A; sequence = 1; }
    else if (!va) { destination = PROFILE_PAGE_A; sequence = b->sequence + 1; }
    else if (!vb) { destination = PROFILE_PAGE_B; sequence = a->sequence + 1; }
    else if ((int32_t)(a->sequence - b->sequence) > 0) {
        destination = PROFILE_PAGE_B; sequence = a->sequence + 1;
    } else { destination = PROFILE_PAGE_A; sequence = b->sequence + 1; }

    memset(&write_record, 0xff, sizeof(write_record));
    write_record.sequence = sequence;
    write_record.length = sizeof(usbnet_profile_t);
    memcpy(&write_record.profile, profile, sizeof(*profile));
    write_record.crc = record_crc(&write_record);

    FLASH_Unlock();
    bool ok = FLASH_ErasePage(destination) == FLASH_COMPLETE;
    for (size_t offset = 4; ok && offset < sizeof(write_record); offset += 4) {
        uint32_t word = 0xffffffffu;
        size_t remaining = sizeof(write_record) - offset;
        memcpy(&word, (const uint8_t *)&write_record + offset, remaining < 4 ? remaining : 4);
        if (FLASH_ProgramWord(destination + offset, word) != FLASH_COMPLETE) ok = false;
    }
    if (ok && memcmp((const void *)(uintptr_t)(destination + 4),
                     (const uint8_t *)&write_record + 4, sizeof(write_record) - 4) != 0) ok = false;
    if (ok && FLASH_ProgramWord(destination, PROFILE_MAGIC) != FLASH_COMPLETE) ok = false;
    FLASH_Lock();
    return ok && record_valid(slot(destination));
}
