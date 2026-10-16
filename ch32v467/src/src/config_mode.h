#ifndef USBNET_CONFIG_MODE_H
#define USBNET_CONFIG_MODE_H
#include <stdint.h>
#include "profile.h"
void config_mode_init(const usbnet_profile_t *active, uint8_t saved);
void config_mode_handle(const uint8_t request[64], uint8_t response[64]);
#endif
