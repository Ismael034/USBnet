/* USBnet vendor HID descriptors, one identical device on each USB controller. */
#include <string.h>
#include "board.h"
#include "descriptor.h"
#include "usbhs2_dcp.h"

const desc_device_t device_desc = {
    .bLength = 18, .bDescriptorType = USB_DESC_DEVICE, .bcdUSB = 0x0200,
    .bDeviceClass = 0, .bDeviceSubClass = 0, .bDeviceProtocol = 0,
    .bMaxPacketSize0 = 64, .idVendor = 0x1A86,
#ifdef USBNET_TEST_FIRMWARE
    .idProduct = 0xFE52,
#else
    .idProduct = 0xFE51,
#endif
    .bcdDevice = 0x0100, .iManufacturer = 1, .iProduct = 2,
    .iSerialNumber = 0, .bNumConfigurations = 1,
};

const desc_qua_t qua_desc = {
    .bLength = 10, .bDescriptorType = USB_DESC_DEVICE_QUALIFIER,
    .bcdUSB = 0x0200, .bDeviceClass = 0, .bDeviceSubClass = 0,
    .bDeviceProtocol = 0, .bMaxPacketSize0 = 64, .bNumConfigurations = 1,
    .bReserved = 0,
};

/* Vendor usage avoids protected keyboard/mouse usages in WebHID. No report ID. */
const uint8_t report_desc[] = {
    0x06, 0x00, 0xff,       /* Usage Page: vendor 0xff00 */
    0x09, 0x01,             /* Usage 1 */
    0xa1, 0x01,             /* Application collection */
    0x15, 0x00,             /* Logical minimum 0 */
    0x26, 0xff, 0x00,       /* Logical maximum 255 */
    0x75, 0x08,             /* 8-bit fields */
    0x95, 0x40,             /* 64 fields */
    0x09, 0x02, 0x81, 0x02, /* Input report */
    0x95, 0x40,
    0x09, 0x03, 0x91, 0x02, /* Output report */
    0xc0,
};

/* Relay client: 64-byte interrupt input, 1024-byte output, and a batched
 * tunnel feature report. */
const uint8_t relay_report_desc[] = {
    0x06, 0x00, 0xff,       /* Usage Page: vendor 0xff00 */
    0x09, 0x01,             /* Usage 1 */
    0xa1, 0x01,             /* Application collection */
    0x15, 0x00,             /* Logical minimum 0 */
    0x26, 0xff, 0x00,       /* Logical maximum 255 */
    0x75, 0x08,             /* 8-bit fields */
    0x95, 0x40,             /* 64 fields */
    0x09, 0x02, 0x81, 0x02, /* Input report */
    0x96, 0x00, 0x04,       /* 1024 fields */
    0x09, 0x03, 0x91, 0x02, /* Output report */
    0x96, USBNET_TUNNEL_FEATURE_LEN & 0xff, USBNET_TUNNEL_FEATURE_LEN >> 8, /* tunnel feature */
    0x09, 0x04, 0xb1, 0x02,
    0xc0,
};

/* USB1 server: GET_REPORT batches requests and credits into the feature report.
 * The interrupt input descriptor remains 64 bytes; the new server polls the
 * feature report instead. */
static const uint8_t server_report_desc[] = {
    0x06, 0x00, 0xff,       /* Usage Page: vendor 0xff00 */
    0x09, 0x01,             /* Usage 1 */
    0xa1, 0x01,             /* Application collection */
    0x15, 0x00,             /* Logical minimum 0 */
    0x26, 0xff, 0x00,       /* Logical maximum 255 */
    0x75, 0x08,             /* 8-bit fields */
    0x95, 0x40,             /* 64-byte input report */
    0x09, 0x02, 0x81, 0x02,
    0x96, 0x00, 0x04,       /* 1024-byte output report */
    0x09, 0x03, 0x91, 0x02,
    0x96, USBNET_TUNNEL_FEATURE_LEN & 0xff, USBNET_TUNNEL_FEATURE_LEN >> 8, /* tunnel feature */
    0x09, 0x04, 0xb1, 0x02,
    0xc0,
};

const uint8_t hid_desc[] = {
    9, 0x21, 0x11, 0x01, 0, 1, 0x22,
    U16_TO_U8S_LE(sizeof(report_desc)),
};

#define CONFIG_BODY \
    9, 0x02, U16_TO_U8S_LE(CONFIG_DESC_SIZE), 1, 1, 0, 0x80, 0x32, \
    9, 0x04, 0, 0, 2, 0x03, 0, 0, 0, \
    9, 0x21, 0x11, 0x01, 0, 1, 0x22, U16_TO_U8S_LE(sizeof(report_desc)), \
    7, 0x05, 0x82, 0x03, U16_TO_U8S_LE(64), 1, \
    7, 0x05, 0x03, 0x03, U16_TO_U8S_LE(64), 1

const uint8_t config_hs_desc[] = { CONFIG_BODY };
const uint8_t config_fs_desc[] = { CONFIG_BODY };
static uint8_t other_speed_desc[2][BULK_CONFIG_DESC_SIZE];
static uint16_t string_storage[2][64];
static desc_device_t runtime_device[2];
static desc_qua_t runtime_qualifier[2];
static uint8_t runtime_config[2][BULK_CONFIG_DESC_SIZE];
static uint8_t runtime_config_len[2];
static uint8_t runtime_hid[2][9];
static const uint8_t *runtime_report[2];
static uint16_t runtime_report_len[2];
static const char *runtime_strings[2][4];
static uint8_t runtime_enabled[2];
static int16_t tunnel_id[2];

/* Vendor feature report appended inside the emulated device's own collection.
 * It carries the USBnet tunnel so Windows keeps seeing a single HID device. */
static uint8_t emulated_report[USBNET_REPORT_DESC_MAX + 24];

/*
 * Splice a vendor-page feature report into a cloned report descriptor, just
 * before the outermost application collection closes. A feature report is a
 * distinct HID report type, so when the source descriptor uses no report IDs we
 * add none either (no interference with its input/output reports). When it does
 * use report IDs, the first unused ID is claimed for the feature report.
 * Returns the new length, or 0 when the descriptor cannot be extended.
 */
static uint16_t build_tunnel_report(const uint8_t *src, uint16_t len, uint8_t *dst, uint8_t *id)
{
    if (len == 0 || len > USBNET_REPORT_DESC_MAX) return 0;

    int end = -1;
    for (int i = len - 1; i >= 0; i--) {
        if (src[i] == 0xc0) { end = i; break; }
    }
    if (end < 0) return 0;

    uint8_t used[256] = {0};
    int uses_ids = 0;
    for (uint16_t i = 0; i < len;) {
        uint8_t prefix = src[i];
        uint8_t size = prefix & 0x03;
        if (size == 3) size = 4;
        if (prefix == 0xfe) { /* long item */
            if (i + 2 >= len) break;
            i += 3 + src[i + 1];
            continue;
        }
        if (((prefix >> 2) & 0x03) == 1 && ((prefix >> 4) & 0x0f) == 8 && size == 1 && i + 1 < len) {
            used[src[i + 1]] = 1;
            uses_ids = 1;
        }
        i += 1 + size;
    }

    uint8_t tunnel_id = 0;
    if (uses_ids) {
        for (int id = 1; id < 256; id++) {
            if (!used[id]) { tunnel_id = (uint8_t)id; break; }
        }
        if (tunnel_id == 0) return 0;
    }

    *id = tunnel_id;
    uint16_t total = len + (uses_ids ? 19u : 17u);
    if (total > USBNET_REPORT_DESC_MAX) return 0;

    memcpy(dst, src, (size_t)end);
    uint8_t *p = dst + end;
    if (uses_ids) { *p++ = 0x85; *p++ = tunnel_id; }   /* Report ID (unused) */
    *p++ = 0x06; *p++ = 0x00; *p++ = 0xff;             /* Usage Page (vendor) */
    *p++ = 0x09; *p++ = 0x01;                          /* Usage 1 */
    *p++ = 0x15; *p++ = 0x00;                          /* Logical Minimum 0 */
    *p++ = 0x26; *p++ = 0xff; *p++ = 0x00;             /* Logical Maximum 255 */
    *p++ = 0x75; *p++ = 0x08;                          /* Report Size 8 */
    *p++ = 0x96; *p++ = USBNET_TUNNEL_FEATURE_LEN & 0xff;
    *p++ = (USBNET_TUNNEL_FEATURE_LEN >> 8) & 0xff;    /* Report Count (bytes) */
    *p++ = 0xb1; *p++ = 0x02;                          /* Feature (Data,Var,Abs) */
    memcpy(p, src + end, (size_t)(len - end));
    return total;
}

static unsigned port_number(usbd_handle_t *h) { return h == &usbhs2d_handle ? 1 : 0; }

static void init_runtime(unsigned port)
{
    if (port > 1) return;
    runtime_device[port] = device_desc;
    runtime_qualifier[port] = qua_desc;
    memcpy(runtime_config[port], config_hs_desc, CONFIG_DESC_SIZE);
    runtime_config_len[port] = CONFIG_DESC_SIZE;
    memcpy(runtime_hid[port], hid_desc, sizeof(hid_desc));
    runtime_report[port] = report_desc;
    runtime_report_len[port] = sizeof(report_desc);
    runtime_strings[port][1] = "USBnet";
#ifdef USBNET_TEST_FIRMWARE
    runtime_strings[port][2] = "USBnet Hardware Test";
#else
    runtime_strings[port][2] = "USBnet HID Link";
#endif
    runtime_strings[port][3] = "";
    runtime_enabled[port] = 1;
}

void descriptor_set_config_mode(unsigned port)
{
    if (port > 1) return;
    init_runtime(port);
    runtime_device[port].idProduct = 0xfe53;
    runtime_strings[port][2] = "USBnet Configuration";
}

void descriptor_set_relay(unsigned port)
{
    if (port > 1) return;
    init_runtime(port);
    runtime_device[port].bcdDevice = 0x0101;
    runtime_strings[port][2] = port == 0 ? "USBnet Server" : "USBnet Client";
    uint8_t *config = runtime_config[port];
    runtime_report[port] = port == 0 ? server_report_desc : relay_report_desc;
    runtime_report_len[port] = port == 0 ? sizeof(server_report_desc) : sizeof(relay_report_desc);
    config[25] = runtime_report_len[port] & 0xff;
    config[26] = runtime_report_len[port] >> 8;
    config[31] = USBNET_RELAY_IN_SIZE & 0xff;
    config[32] = USBNET_RELAY_IN_SIZE >> 8;
    config[38] = USBNET_RELAY_REPORT_SIZE & 0xff;
    config[39] = USBNET_RELAY_REPORT_SIZE >> 8;
    runtime_hid[port][7] = runtime_report_len[port] & 0xff;
    runtime_hid[port][8] = runtime_report_len[port] >> 8;
    /* Interface 1 carries raw USBnet frames; HID interface 0 remains available. */
    static const uint8_t bulk_interface[] = {
        9, 0x04, 1, 0, 2, 0xff, 0, 0, 0,
        7, 0x05, 0x84, 0x02, 0x00, 0x02, 0,
        7, 0x05, 0x05, 0x02, 0x00, 0x02, 0,
    };
    memcpy(config + CONFIG_DESC_SIZE, bulk_interface, sizeof(bulk_interface));
    config[2] = BULK_CONFIG_DESC_SIZE;
    config[3] = 0;
    config[4] = 2;
    runtime_config_len[port] = BULK_CONFIG_DESC_SIZE;
}

void descriptor_set_emulated(unsigned port, const usbnet_profile_t *profile)
{
    if (port > 1 || !profile_valid(profile)) return;
    init_runtime(port);
    desc_device_t *device = &runtime_device[port];
    device->idVendor = profile->vid;
    device->idProduct = profile->pid;
    device->bcdDevice = profile->bcd_device;
    device->bDeviceClass = profile->device_class;
    device->bDeviceSubClass = profile->device_subclass;
    device->bDeviceProtocol = profile->device_protocol;
    runtime_qualifier[port].bDeviceClass = profile->device_class;
    runtime_qualifier[port].bDeviceSubClass = profile->device_subclass;
    runtime_qualifier[port].bDeviceProtocol = profile->device_protocol;
    device->iSerialNumber = profile->serial[0] ? 3 : 0;
    uint8_t *config = runtime_config[port];
    config[7] = profile->attributes;
    config[8] = profile->max_power_units;
    config[15] = profile->interface_subclass;
    config[16] = profile->interface_protocol;
    config[31] = profile->input_size;
    config[32] = 0;
    config[33] = profile->interval;
    config[38] = profile->output_size;
    config[39] = 0;
    config[40] = profile->interval;

    uint8_t id = 0;
    uint16_t report_len = build_tunnel_report(profile->report_desc, profile->report_len, emulated_report, &id);
    tunnel_id[port] = report_len ? id : -1;
    if (report_len == 0) {
        report_len = profile->report_len;
        memcpy(emulated_report, profile->report_desc, profile->report_len);
    }
    config[25] = report_len & 0xff;
    config[26] = report_len >> 8;
    runtime_hid[port][7] = report_len & 0xff;
    runtime_hid[port][8] = report_len >> 8;
    runtime_report[port] = emulated_report;
    runtime_report_len[port] = report_len;
    runtime_strings[port][1] = profile->manufacturer;
    runtime_strings[port][2] = profile->product;
    runtime_strings[port][3] = profile->serial;
}

int descriptor_tunnel_id(unsigned port)
{
    return port > 1 ? -1 : tunnel_id[port];
}

const uint8_t *descriptor_hid(usbd_handle_t *h)
{
    unsigned port = port_number(h);
    return runtime_enabled[port] ? runtime_hid[port] : hid_desc;
}

const uint8_t *descriptor_report(usbd_handle_t *h, size_t *length)
{
    unsigned port = port_number(h);
    *length = runtime_enabled[port] ? runtime_report_len[port] : sizeof(report_desc);
    return runtime_enabled[port] ? runtime_report[port] : report_desc;
}

usb_rst_e get_device_desc(usbd_handle_t *h, const usb_req_t *req, void **buf, size_t *size)
{
    unsigned port = port_number(h);
    switch (req->wValue >> 8) {
    case USB_DESC_DEVICE:
        *buf = (void *)(runtime_enabled[port] ? &runtime_device[port] : &device_desc);
        *size = USB_MIN(req->wLength, sizeof(device_desc));
        return USB_RST_OK;
    case USB_DESC_CONFIGURATION:
        *buf = (void *)(runtime_enabled[port] ? runtime_config[port] :
                        (usbd_get_speed(h) == USB_SPEED_HIGH ? config_hs_desc : config_fs_desc));
        *size = USB_MIN(req->wLength, runtime_enabled[port] ? runtime_config_len[port] : CONFIG_DESC_SIZE);
        return USB_RST_OK;
    case USB_DESC_STRING: {
        uint8_t index = req->wValue & 0xff;
        uint16_t *out = string_storage[h == &usbhs2d_handle ? 1 : 0];
        size_t length = 0;
        if (index == 0) {
            out[1] = 0x0409;
            length = 1;
        } else {
            const char *strings[] = {
                "", "USBnet",
#ifdef USBNET_TEST_FIRMWARE
                "USBnet Hardware Test",
#else
                "USBnet HID Link",
#endif
            };
            if (index >= 4 || (!runtime_enabled[port] && index >= 3)) return USB_RST_FAILED;
            const char *value = runtime_enabled[port] ? runtime_strings[port][index] : strings[index];
            if (value == NULL) return USB_RST_FAILED;
            length = USB_MIN(strlen(value), 63);
            for (size_t i = 0; i < length; i++) out[i + 1] = value[i];
        }
        out[0] = (uint16_t)((USB_DESC_STRING << 8) | (2 + length * 2));
        *buf = out;
        *size = USB_MIN(req->wLength, 2 + length * 2);
        return USB_RST_OK;
    }
    case USB_DESC_DEVICE_QUALIFIER:
        *buf = (void *)(runtime_enabled[port] ? &runtime_qualifier[port] : &qua_desc);
        *size = USB_MIN(req->wLength, sizeof(qua_desc));
        return USB_RST_OK;
    case USB_DESC_OTHER_SPEED_CONFIG: {
        uint8_t *out = other_speed_desc[h == &usbhs2d_handle ? 1 : 0];
        memcpy(out, runtime_enabled[port] ? runtime_config[port] :
                    (usbd_get_speed(h) == USB_SPEED_HIGH ? config_fs_desc : config_hs_desc),
                    runtime_enabled[port] ? runtime_config_len[port] : CONFIG_DESC_SIZE);
        out[1] = USB_DESC_OTHER_SPEED_CONFIG;
        if (runtime_enabled[port] && runtime_config_len[port] == BULK_CONFIG_DESC_SIZE) {
            out[CONFIG_DESC_SIZE + 13] = 64;
            out[CONFIG_DESC_SIZE + 14] = 0;
            out[CONFIG_DESC_SIZE + 20] = 64;
            out[CONFIG_DESC_SIZE + 21] = 0;
        }
        *buf = out;
        *size = USB_MIN(req->wLength, runtime_enabled[port] ? runtime_config_len[port] : CONFIG_DESC_SIZE);
        return USB_RST_OK;
    }
    default:
        return USB_RST_FAILED;
    }
}
