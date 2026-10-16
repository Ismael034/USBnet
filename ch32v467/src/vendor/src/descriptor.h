#ifndef USBNET_DESCRIPTOR_H
#define USBNET_DESCRIPTOR_H
#include <stdint.h>
#include "usb_driver.h"
#include "profile.h"
#define CONFIG_DESC_SIZE 41
#define BULK_CONFIG_DESC_SIZE (CONFIG_DESC_SIZE + 23)

/* Vendor feature report used by the emulated client and USB1 server.
 * Sized as a whole number of 64-byte USBnet frames. */
#define USBNET_TUNNEL_FEATURE_LEN 4096
#define USBNET_TUNNEL_FRAMES (USBNET_TUNNEL_FEATURE_LEN / 64)

/* Relay output reports pack 16 frames. The interrupt input stays 64 bytes;
 * widening it proved unreliable, so USB1 reads requests by feature report. */
#define USBNET_RELAY_REPORT_SIZE 1024
#define USBNET_RELAY_FRAMES (USBNET_RELAY_REPORT_SIZE / 64)
#define USBNET_RELAY_IN_SIZE 64
extern const desc_device_t device_desc;
extern const desc_qua_t qua_desc;
extern const uint8_t config_hs_desc[];
extern const uint8_t config_fs_desc[];
extern const uint8_t hid_desc[];
extern const uint8_t report_desc[];
usb_rst_e get_device_desc(usbd_handle_t *h, const usb_req_t *req, void **buf, size_t *size);
void descriptor_set_config_mode(unsigned port);
void descriptor_set_relay(unsigned port);
void descriptor_set_emulated(unsigned port, const usbnet_profile_t *profile);
/* Report ID of the tunnel feature report, or -1 when there is none. */
int descriptor_tunnel_id(unsigned port);
const uint8_t *descriptor_hid(usbd_handle_t *h);
const uint8_t *descriptor_report(usbd_handle_t *h, size_t *length);
#endif
