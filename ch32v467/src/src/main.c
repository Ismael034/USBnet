/* CH32V467 USBnet: bridge fixed 64-byte vendor HID reports between USB1/USB2. */
#include <string.h>
#include "board.h"
#include "ch32v4x7.h"
#include "debug.h"
#include "led.h"
#include "usb_driver.h"
#include "descriptor.h"
#include "profile.h"
#include "config_mode.h"

#define REPORT_SIZE 64
#define QUEUE_SIZE 128
#define CREDIT_MAGIC 0x56
#define FLOW_MAGIC 0x57
#define QUIET_FRAME 0x04
#define ACTIVITY_TICKS 200 /* Configuration mode runs every 100 us. */
#define RELAY_ACTIVITY_TICKS 2000 /* Keep the relay LED pulse near 20 ms. */
#define CONFIG_BLINK_TICKS 2500 /* 250 ms: LED2 blinks at ~2 Hz in configuration mode */
#define EP_IN  0x82
#define EP_OUT 0x03
#define BULK_IN 0x84
#define BULK_OUT 0x05
#define BULK_PACKET 512
#define BULK_OUT_BYTES 4096
#define BULK_TX_FRAMES 127 /* Stay below the host's 8192-byte bulk read. */
#define RELAY_LOOP_DELAY_US 10
#define CHECK(x) do { if (!(x)) for (;;) { led_ctrl(15); Delay_Ms(100); led_ctrl(0); Delay_Ms(100); } } while (0)

typedef struct {
    usbd_handle_t *h;
    __attribute__((aligned(4))) uint8_t out[USBNET_RELAY_REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t rx[QUEUE_SIZE][REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t tx[USBNET_RELAY_REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t feat[QUEUE_SIZE][REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t feat_out[USBNET_TUNNEL_FEATURE_LEN + 1];
    __attribute__((aligned(4))) uint8_t bulk_out[BULK_OUT_BYTES];
    __attribute__((aligned(4))) uint8_t bulk_tx[BULK_TX_FRAMES * REPORT_SIZE];
    volatile uint8_t head, tail, count, stopped, tx_busy;
    volatile uint8_t bulk_enabled, bulk_tx_busy;
    volatile uint8_t feat_head, feat_tail, feat_count, feat_sending;
} port_t;

static port_t ports[2];
static uint8_t hid_idle[2];
static uint8_t hid_protocol[2] = { 1, 1 };
static uint8_t zero_report[USBNET_RELAY_REPORT_SIZE];
static uint8_t control_report[2][USBNET_TUNNEL_FEATURE_LEN + 1];
static usbnet_profile_t active_profile;
static uint8_t configuration_mode;
static uint8_t config_reply_pending;
static uint16_t activity_ticks[2];
static uint16_t config_blink_ticks;
static uint8_t config_led_on;
static uint8_t shown_leds;
static volatile uint32_t flow_token;
static volatile uint16_t flow_credit_pending;
static volatile uint8_t relay_feature_enabled;

static void activity_service(void)
{
    uint8_t bits = 0;
    if (configuration_mode) {
        if (config_blink_ticks) {
            config_blink_ticks--;
        } else {
            config_blink_ticks = CONFIG_BLINK_TICKS;
            config_led_on ^= 1;
        }
        if (config_led_on) bits |= 2;
    }
    if (activity_ticks[0]) { bits |= 4; activity_ticks[0]--; }
    if (activity_ticks[1]) { bits |= 8; activity_ticks[1]--; }
    if (bits != shown_leds) { led_ctrl(bits); shown_leds = bits; }
}

static unsigned port_index(usbd_handle_t *h) { return h == ports[0].h ? 0 : 1; }
static uint16_t ep_in_size(unsigned i) {
    if (!configuration_mode && i == 1 && active_profile.mode == USBNET_PROFILE_EMULATE)
        return active_profile.input_size;
    return configuration_mode ? REPORT_SIZE : USBNET_RELAY_IN_SIZE;
}
static uint16_t ep_out_size(unsigned i) {
    if (!configuration_mode && i == 1 && active_profile.mode == USBNET_PROFILE_EMULATE)
        return active_profile.output_size;
    return configuration_mode ? REPORT_SIZE : USBNET_RELAY_REPORT_SIZE;
}

static uint8_t emulated(unsigned i)
{
    return i == 1 && !configuration_mode && active_profile.mode == USBNET_PROFILE_EMULATE;
}

/* Report ID of the tunnel feature on port i, or -1 when there is none. */
static int tunnel_id(unsigned i)
{
    return emulated(i) ? descriptor_tunnel_id(1) : 0;
}

static uint8_t is_tunnel(unsigned i, uint16_t wValue)
{
    int id = tunnel_id(i);
    return (wValue >> 8) == 3 && id >= 0 && (wValue & 0xff) == id;
}

static void reset_callback(usbd_handle_t *h, uint32_t parameter)
{
    (void)parameter;
    port_t *p = &ports[port_index(h)];
    p->head = p->tail = p->count = p->stopped = p->tx_busy = 0;
    p->bulk_enabled = p->bulk_tx_busy = 0;
    p->feat_head = p->feat_tail = p->feat_count = p->feat_sending = 0;
    flow_token = 0;
    flow_credit_pending = 0;
    if (port_index(h) == 1) relay_feature_enabled = 0;
    hid_idle[port_index(h)] = 0;
    hid_protocol[port_index(h)] = 1;
    unsigned i = port_index(h);
    CHECK(usbd_endp_open(h, EP_IN, USBD_ENDP_FEATURE_NORMAL, ep_in_size(i)) == USB_RST_OK);
    CHECK(usbd_endp_open(h, EP_OUT, USBD_ENDP_FEATURE_NORMAL, ep_out_size(i)) == USB_RST_OK);
    CHECK(usbd_download(h, EP_OUT, p->out, ep_out_size(i)) == USB_RST_OK);
    if (!configuration_mode && (i == 0 || active_profile.mode == USBNET_PROFILE_RELAY)) {
        CHECK(usbd_endp_open(h, BULK_IN,
                             USBD_ENDP_FEATURE_NORMAL | USBD_ENDP_FEATURE_BURST,
                             BULK_PACKET) == USB_RST_OK);
        CHECK(usbd_endp_open(h, BULK_OUT,
                             USBD_ENDP_FEATURE_NORMAL | USBD_ENDP_FEATURE_BURST,
                             BULK_PACKET) == USB_RST_OK);
        CHECK(usbd_download(h, BULK_OUT, p->bulk_out, BULK_OUT_BYTES) == USB_RST_OK);
    }
}

static usb_rst_e hid_setup(usbd_handle_t *h, const usb_req_t *req, void **buf, size_t *size)
{
    unsigned i = port_index(h);
    if (req->bmRequestType == 0x81 && req->bRequest == USB_REQ_CODE_GET_DESCRIPTOR) {
        switch (req->wValue >> 8) {
        case 0x21: *buf = (void *)descriptor_hid(h); *size = USB_MIN(req->wLength, 9); return USB_RST_OK;
        case 0x22: {
            size_t length;
            *buf = (void *)descriptor_report(h, &length);
            *size = USB_MIN(req->wLength, length);
            return USB_RST_OK;
        }
        }
    }
    if (req->bmRequestType == 0x21) {
        if (req->bRequest == 0x09) { /* SET_REPORT */
            uint8_t rtype = req->wValue >> 8;
            size_t limit = (rtype == 3) ? USBNET_TUNNEL_FEATURE_LEN + (tunnel_id(i) > 0) : ep_out_size(i);
            /* The emulated device's own reports are accepted and ignored. */
            if (emulated(i) && !is_tunnel(i, req->wValue)) limit = sizeof(control_report[i]);
            /* Check actual payload after reception: most wide reports are padding.
             * USB1 must also accept a new flow marker when its queue is full. */
            if ((rtype == 2 || rtype == 3) && req->wLength <= limit) {
                *buf = control_report[i]; *size = req->wLength; return USB_RST_OK;
            }
        }
        if (req->bRequest == 0x0a) { /* SET_IDLE */
            hid_idle[i] = req->wValue >> 8; *size = 0; return USB_RST_OK;
        }
        if (req->bRequest == 0x0b) { /* SET_PROTOCOL */
            hid_protocol[i] = req->wValue; *size = 0; return USB_RST_OK;
        }
    }
    if (req->bmRequestType == 0xa1) {
        if (req->bRequest == 0x01) { /* GET_REPORT */
            if (is_tunnel(i, req->wValue)) {
                ports[i].bulk_enabled = 0;
                if (i == 1 && !configuration_mode && active_profile.mode == USBNET_PROFILE_RELAY)
                    relay_feature_enabled = 1;
                unsigned skip = tunnel_id(i) > 0;
                uint8_t *out = ports[i].feat_out;
                out[0] = req->wValue & 0xff;
                unsigned max = req->wLength > skip ? (req->wLength - skip) / REPORT_SIZE : 0;
                if (max > USBNET_TUNNEL_FRAMES) max = USBNET_TUNNEL_FRAMES;
                /* Frames leave the queue only once the host completes the transfer. */
                unsigned n = 0;
                uint8_t tail = ports[i].feat_tail;
                while (n < max && n < ports[i].feat_count) {
                    memcpy(out + skip + n * REPORT_SIZE, ports[i].feat[tail], REPORT_SIZE);
                    tail = (tail + 1) % QUEUE_SIZE;
                    n++;
                }
                ports[i].feat_sending = n;
                /* Send only the queued frames; an empty reply is one zero frame. */
                if (!n) memset(out + skip, 0, REPORT_SIZE);
                *buf = out;
                *size = USB_MIN(req->wLength, skip + (n ? n : 1) * REPORT_SIZE);
                return USB_RST_OK;
            }
            zero_report[0] = req->wValue & 0xff;
            *buf = zero_report;
            *size = USB_MIN(req->wLength, (req->wValue >> 8) == 3 ? sizeof(zero_report) : ep_in_size(i));
            return USB_RST_OK;
        }
        if (req->bRequest == 0x02) { /* GET_IDLE */
            *buf = &hid_idle[i]; *size = USB_MIN(req->wLength, 1); return USB_RST_OK;
        }
        if (req->bRequest == 0x03) { /* GET_PROTOCOL */
            *buf = &hid_protocol[i]; *size = USB_MIN(req->wLength, 1); return USB_RST_OK;
        }
    }
    return USB_RST_FAILED;
}

static void hid_status(usbd_handle_t *h, const usb_req_t *req, void *buf, size_t size)
{
    (void)buf; (void)size;
    unsigned i = port_index(h);
    port_t *p = &ports[i];
    if (req->bmRequestType != 0xa1 || req->bRequest != 0x01 || !p->feat_sending) return;
    unsigned n = p->feat_sending;
    p->feat_sending = 0;
    p->feat_tail = (p->feat_tail + n) % QUEUE_SIZE;
    p->feat_count -= n;
    if (i == 1 && flow_token) flow_credit_pending += n;
}

/* Count payload frames before changing the queue, so a report is atomic. */
static size_t payload_frames(const uint8_t *data, size_t size)
{
    size_t count = 0;
    for (size_t off = 0; off < size; off += REPORT_SIZE) {
        size_t n = size - off;
        if (n > REPORT_SIZE) n = REPORT_SIZE;
        for (size_t k = 0; k < n; k++) {
            if (data[off + k]) { count++; break; }
        }
    }
    return count;
}

/* Caller has checked capacity for every nonzero frame. */
static void enqueue_block(port_t *p, const uint8_t *data, size_t size)
{
    size_t off = 0;
    while (off < size) {
        size_t n = size - off;
        if (n > REPORT_SIZE) n = REPORT_SIZE;
        uint8_t nonzero = 0;
        for (size_t k = 0; k < n; k++) {
            if (data[off + k]) { nonzero = 1; break; }
        }
        if (nonzero) {
            if (n < REPORT_SIZE) memset(p->rx[p->head], 0, REPORT_SIZE);
            memcpy(p->rx[p->head], data + off, n);
            p->head = (p->head + 1) % QUEUE_SIZE;
            p->count++;
        }
        off += n;
    }
}

static uint8_t handle_flow_marker(port_t *p, const uint8_t *data, size_t size)
{
    if (p != &ports[0] || size < REPORT_SIZE || data[0] != FLOW_MAGIC || data[1] != 1)
        return 0;
    flow_token = (uint32_t)data[2] | ((uint32_t)data[3] << 8) |
                 ((uint32_t)data[4] << 16) | ((uint32_t)data[5] << 24);
    flow_credit_pending = 0;
    /* A new response supersedes any frames left by a timed-out client. */
    p->head = p->tail = p->count = 0;
    ports[1].feat_head = ports[1].feat_tail = ports[1].feat_count = ports[1].feat_sending = 0;
    if (p->stopped) {
        p->stopped = 0;
        usbd_endp_set_response(p->h, EP_OUT, ENDP_RESP_ACK);
    }
    return 1;
}

static usb_rst_e hid_control_data(usbd_handle_t *h, const usb_req_t *req, void *buf, size_t size)
{
    if (req->bRequest != 0x09) return USB_RST_OK;
    unsigned i = port_index(h);
    port_t *p = &ports[i];
    if (emulated(i)) {
        if (!is_tunnel(i, req->wValue)) return USB_RST_OK;
        if (tunnel_id(i) > 0) { buf = (uint8_t *)buf + 1; size--; }
    }
    if (handle_flow_marker(p, (const uint8_t *)buf, size)) return USB_RST_OK;
    /* Refuse the whole control report rather than accepting a prefix and
     * silently dropping later frames when the Mac has not drained the queue. */
    if (payload_frames((const uint8_t *)buf, size) > (size_t)(QUEUE_SIZE - p->count))
        return USB_RST_FAILED;
    enqueue_block(p, (const uint8_t *)buf, size);
    return USB_RST_OK;
}

static void credit_service(void)
{
    port_t *p = &ports[1];
    if (!flow_credit_pending || p->count == QUEUE_SIZE) return;
    uint8_t n = flow_credit_pending > 255 ? 255 : (uint8_t)flow_credit_pending;
    uint8_t *frame = p->rx[p->head];
    memset(frame, 0, REPORT_SIZE);
    frame[0] = CREDIT_MAGIC;
    frame[1] = n;
    frame[2] = flow_token & 0xff;
    frame[3] = (flow_token >> 8) & 0xff;
    frame[4] = (flow_token >> 16) & 0xff;
    frame[5] = (flow_token >> 24) & 0xff;
    p->head = (p->head + 1) % QUEUE_SIZE;
    p->count++;
    flow_credit_pending -= n;
}

static endp_resp_e received(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    (void)ep;
    port_t *p = &ports[port_index(h)];
    /* Output reports for the emulated device (keyboard LEDs) are not tunnel data. */
    if (emulated(port_index(h))) return ENDP_RESP_ACK;
    if (handle_flow_marker(p, (const uint8_t *)buf, size)) return ENDP_RESP_ACK;
    if (p == &ports[0] && !configuration_mode && active_profile.mode == USBNET_PROFILE_EMULATE) {
        /* Keep OUT available for the next flow marker after a timed-out reply.
         * An unexpected overflow drops only this report, never queued data. */
        if (payload_frames((const uint8_t *)buf, size) <= (size_t)(QUEUE_SIZE - p->count))
            enqueue_block(p, (const uint8_t *)buf, size);
        return ENDP_RESP_ACK;
    }
    /* Keep a whole report together; zero padding does not use queue slots. */
    if (payload_frames((const uint8_t *)buf, size) > (size_t)(QUEUE_SIZE - p->count)) {
        p->stopped = 1;
        return ENDP_RESP_NAK;
    }
    enqueue_block(p, (const uint8_t *)buf, size);
    if (p->count == QUEUE_SIZE) {
        p->stopped = 1;
        return ENDP_RESP_NAK;
    }
    return ENDP_RESP_ACK;
}

static endp_resp_e sent(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    (void)ep; (void)buf;
    unsigned i = port_index(h);
    ports[i].tx_busy = 0;
    /* Credit the server once the client has consumed each relayed response
     * frame. The emulated path credits through GET_REPORT; the relay path
     * carries responses over the interrupt IN endpoint, so the credit is
     * generated here when that transfer completes. Without this the receive
     * queue can fill and the server's HID writes fail. */
    if (i == 1 && !configuration_mode && active_profile.mode == USBNET_PROFILE_RELAY &&
        !relay_feature_enabled &&
        flow_token) {
        flow_credit_pending += (uint16_t)(size / REPORT_SIZE);
    }
    return ENDP_RESP_NAK;
}

static endp_resp_e bulk_received(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    port_t *p = &ports[port_index(h)];
    p->bulk_enabled = 1;
    endp_resp_e response = received(h, ep, buf, size);
    if (p->count > QUEUE_SIZE - BULK_OUT_BYTES / REPORT_SIZE) {
        p->stopped = 1;
        return ENDP_RESP_NAK;
    }
    return response;
}

static endp_resp_e bulk_sent(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    (void)ep; (void)buf; (void)size;
    ports[port_index(h)].bulk_tx_busy = 0;
    return ENDP_RESP_NAK;
}

static void forward(port_t *source, port_t *dest)
{
    unsigned di = port_index(dest->h);
    unsigned moved = 0;
    uint8_t data_activity = 0;
    if (!source->count ||
        usbd_is_enum(source->h) != USB_TRUE || usbd_is_enum(dest->h) != USB_TRUE) return;
    if (!configuration_mode && dest->bulk_enabled) {
        if (dest->bulk_tx_busy) return;
        unsigned n = source->count < BULK_TX_FRAMES ? source->count : BULK_TX_FRAMES;
        /* A short final USB packet completes a host read immediately. */
        if (n % (BULK_PACKET / REPORT_SIZE) == 0) n--;
        uint8_t tail = source->tail;
        for (unsigned j = 0; j < n; j++) {
            if (source->rx[tail][0] != CREDIT_MAGIC &&
                !(source->rx[tail][1] & QUIET_FRAME)) data_activity = 1;
            memcpy(dest->bulk_tx + j * REPORT_SIZE, source->rx[tail], REPORT_SIZE);
            tail = (tail + 1) % QUEUE_SIZE;
        }
        dest->bulk_tx_busy = 1;
        if (usbd_upload(dest->h, BULK_IN, dest->bulk_tx, n * REPORT_SIZE) != USB_RST_OK) {
            dest->bulk_tx_busy = 0;
            return;
        }
        source->tail = tail;
        source->count -= n;
        moved = n;
    } else if (!configuration_mode && (di == 0 ||
        (di == 1 && (active_profile.mode == USBNET_PROFILE_EMULATE || relay_feature_enabled)))) {
        /* Both the server port and emulated client batch frames in features. */
        /* A few frames per pass keep interrupts off only briefly. */
        while (source->count && dest->feat_count < QUEUE_SIZE && moved < 16) {
            if (source->rx[source->tail][0] != CREDIT_MAGIC &&
                !(source->rx[source->tail][1] & QUIET_FRAME)) data_activity = 1;
            memcpy(dest->feat[dest->feat_head], source->rx[source->tail], REPORT_SIZE);
            dest->feat_head = (dest->feat_head + 1) % QUEUE_SIZE;
            dest->feat_count++;
            source->tail = (source->tail + 1) % QUEUE_SIZE;
            source->count--;
            moved++;
        }
    } else {
        if (dest->tx_busy) return;
        unsigned max_frames = (unsigned)((ep_in_size(di) + REPORT_SIZE - 1) / REPORT_SIZE);
        if (max_frames == 0) max_frames = 1;
        unsigned n = 0;
        uint8_t tail = source->tail;
        while (n < max_frames && n < source->count) {
            if (source->rx[tail][0] != CREDIT_MAGIC &&
                !(source->rx[tail][1] & QUIET_FRAME)) data_activity = 1;
            memcpy(dest->tx + n * REPORT_SIZE, source->rx[tail], REPORT_SIZE);
            tail = (tail + 1) % QUEUE_SIZE;
            n++;
        }
        dest->tx_busy = 1;
        if (usbd_upload(dest->h, EP_IN, dest->tx, n * REPORT_SIZE) != USB_RST_OK) {
            dest->tx_busy = 0;
            return;
        }
        source->tail = tail;
        source->count -= n;
        moved = n;
    }
    if (moved && data_activity)
        activity_ticks[source == &ports[0] ? 0 : 1] = RELAY_ACTIVITY_TICKS;
    if (source->stopped && source->count <=
        QUEUE_SIZE - (source->bulk_enabled ? BULK_OUT_BYTES / REPORT_SIZE : 1)) {
        source->stopped = 0;
        usbd_endp_set_response(source->h, source->bulk_enabled ? BULK_OUT : EP_OUT, ENDP_RESP_ACK);
    }
}

static void config_service(void)
{
    port_t *p = &ports[0];
    if (!config_reply_pending && !p->tx_busy && p->count) {
        uint8_t request[REPORT_SIZE];
        usbd_interrupt_ctrl(p->h, USB_FALSE);
        memcpy(request, p->rx[p->tail], REPORT_SIZE);
        p->tail = (p->tail + 1) % QUEUE_SIZE;
        p->count--;
        if (p->stopped) {
            p->stopped = 0;
            usbd_endp_set_response(p->h, EP_OUT, ENDP_RESP_ACK);
        }
        usbd_interrupt_ctrl(p->h, USB_TRUE);
        activity_ticks[0] = ACTIVITY_TICKS;
        config_mode_handle(request, p->tx);
        config_reply_pending = 1;
    }
    if (config_reply_pending && !p->tx_busy && usbd_is_enum(p->h) == USB_TRUE) {
        usbd_interrupt_ctrl(p->h, USB_FALSE);
        p->tx_busy = 1;
        if (usbd_upload(p->h, EP_IN, p->tx, REPORT_SIZE) == USB_RST_OK) {
            config_reply_pending = 0;
            activity_ticks[1] = ACTIVITY_TICKS;
        } else {
            p->tx_busy = 0;
        }
        usbd_interrupt_ctrl(p->h, USB_TRUE);
    }
}

static void init_port(unsigned i)
{
    port_t *p = &ports[i];
    p->h = board_usbd_init(i);
    CHECK(p->h != NULL);
    CHECK(usbd_drv_init(p->h) == USB_RST_OK);
    CHECK(usbd_enable(p->h) == USB_RST_OK);
    CHECK(usbd_register_event_callback(p->h, USBD_CB_EVENT_RESET, reset_callback) == USB_RST_OK);
    CHECK(usbd_register_req_callback(p->h, 0x80, USB_REQ_CODE_GET_DESCRIPTOR,
                                     get_device_desc, NULL, NULL) == USB_RST_OK);
    CHECK(usbd_register_itf_callback(p->h, 0, hid_setup, hid_control_data, hid_status) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, EP_OUT, received) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, EP_IN, sent) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, BULK_OUT, bulk_received) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, BULK_IN, bulk_sent) == USB_RST_OK);
    CHECK(usbd_open(p->h, USB_SPEED_HIGH, USB_FALSE) == USB_RST_OK);
}

int main(void)
{
    board_init();
    led_init();

    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOA, ENABLE);
    GPIO_InitTypeDef button = {0};
    button.GPIO_Pin = GPIO_Pin_3;
    button.GPIO_Mode = GPIO_Mode_IPD;
    GPIO_Init(GPIOA, &button);
    Delay_Ms(5);
    configuration_mode = GPIO_ReadInputDataBit(GPIOA, GPIO_Pin_3) == Bit_SET;
    uint8_t saved = profile_load(&active_profile);

    if (configuration_mode) {
        config_led_on = 1;
        config_blink_ticks = CONFIG_BLINK_TICKS;
        shown_leds = 0xff; /* force the first activity_service() to drive LED2 */
        descriptor_set_config_mode(0);
        config_mode_init(&active_profile, saved);
        init_port(0);
        for (;;) { config_service(); activity_service(); Delay_Us(100); }
    }

    if (active_profile.mode == USBNET_PROFILE_EMULATE)
        descriptor_set_emulated(1, &active_profile);
    else
        descriptor_set_relay(1);
    descriptor_set_relay(0);
    init_port(0);
    init_port(1);
    for (;;) {
        /* Keep interrupts off only while moving queue indices and scheduling DMA. */
        usbd_interrupt_ctrl(ports[0].h, USB_FALSE);
        usbd_interrupt_ctrl(ports[1].h, USB_FALSE);
        forward(&ports[0], &ports[1]);
        forward(&ports[1], &ports[0]);
        credit_service();
        usbd_interrupt_ctrl(ports[1].h, USB_TRUE);
        usbd_interrupt_ctrl(ports[0].h, USB_TRUE);
        activity_service();
        Delay_Us(RELAY_LOOP_DELAY_US);
    }
}
