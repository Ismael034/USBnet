/* Separate CH32V467 hardware-test image: LEDs, button, and HID echo per USB port. */
#include <string.h>
#include "board.h"
#include "ch32v4x7.h"
#include "debug.h"
#include "led.h"
#include "usb_driver.h"
#include "descriptor.h"

#define REPORT_SIZE 64
#define QUEUE_SIZE 8
#define EP_IN 0x82
#define EP_OUT 0x03
#define CHECK(x) do { if (!(x)) for (;;) { led_ctrl(15); Delay_Ms(100); led_ctrl(0); Delay_Ms(100); } } while (0)

typedef struct {
    usbd_handle_t *h;
    __attribute__((aligned(4))) uint8_t out[REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t rx[QUEUE_SIZE][REPORT_SIZE];
    __attribute__((aligned(4))) uint8_t tx[REPORT_SIZE];
    volatile uint8_t head, tail, count, stopped, tx_busy;
} test_port_t;

static test_port_t ports[2];
static uint8_t hid_idle[2];
static uint8_t hid_protocol[2] = { 1, 1 };
static uint8_t control_report[2][REPORT_SIZE];
static uint8_t zero_report[REPORT_SIZE];
static unsigned port_index(usbd_handle_t *h) { return h == ports[0].h ? 0 : 1; }

static void reset_callback(usbd_handle_t *h, uint32_t parameter)
{
    (void)parameter;
    test_port_t *p = &ports[port_index(h)];
    p->head = p->tail = p->count = p->stopped = p->tx_busy = 0;
    CHECK(usbd_endp_open(h, EP_IN, USBD_ENDP_FEATURE_NORMAL, REPORT_SIZE) == USB_RST_OK);
    CHECK(usbd_endp_open(h, EP_OUT, USBD_ENDP_FEATURE_NORMAL, REPORT_SIZE) == USB_RST_OK);
    CHECK(usbd_download(h, EP_OUT, p->out, REPORT_SIZE) == USB_RST_OK);
}

static usb_rst_e hid_setup(usbd_handle_t *h, const usb_req_t *req, void **buf, size_t *size)
{
    unsigned i = port_index(h);
    if (req->bmRequestType == 0x81 && req->bRequest == USB_REQ_CODE_GET_DESCRIPTOR) {
        switch (req->wValue >> 8) {
        case 0x21: *buf = (void *)hid_desc; *size = USB_MIN(req->wLength, 9); return USB_RST_OK;
        case 0x22: *buf = (void *)report_desc; *size = USB_MIN(req->wLength, 27); return USB_RST_OK;
        }
    }
    if (req->bmRequestType == 0x21) {
        if (req->bRequest == 0x09 && (req->wValue >> 8) == 2 &&
            req->wLength == REPORT_SIZE && ports[i].count < QUEUE_SIZE) {
            *buf = control_report[i]; *size = REPORT_SIZE; return USB_RST_OK;
        }
        if (req->bRequest == 0x0a) { hid_idle[i] = req->wValue >> 8; *size = 0; return USB_RST_OK; }
        if (req->bRequest == 0x0b) { hid_protocol[i] = req->wValue; *size = 0; return USB_RST_OK; }
    }
    if (req->bmRequestType == 0xa1) {
        if (req->bRequest == 0x01) { *buf = zero_report; *size = USB_MIN(req->wLength, REPORT_SIZE); return USB_RST_OK; }
        if (req->bRequest == 0x02) { *buf = &hid_idle[i]; *size = USB_MIN(req->wLength, 1); return USB_RST_OK; }
        if (req->bRequest == 0x03) { *buf = &hid_protocol[i]; *size = USB_MIN(req->wLength, 1); return USB_RST_OK; }
    }
    return USB_RST_FAILED;
}

static usb_rst_e hid_control_data(usbd_handle_t *h, const usb_req_t *req, void *buf, size_t size)
{
    if (req->bRequest != 0x09) return USB_RST_OK;
    test_port_t *p = &ports[port_index(h)];
    if (size != REPORT_SIZE || p->count == QUEUE_SIZE) return USB_RST_FAILED;
    memcpy(p->rx[p->head], buf, REPORT_SIZE);
    p->head = (p->head + 1) % QUEUE_SIZE;
    p->count++;
    return USB_RST_OK;
}

static endp_resp_e received(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    (void)ep;
    test_port_t *p = &ports[port_index(h)];
    if (size == REPORT_SIZE && p->count < QUEUE_SIZE) {
        memcpy(p->rx[p->head], buf, REPORT_SIZE);
        p->head = (p->head + 1) % QUEUE_SIZE;
        p->count++;
    }
    if (p->count == QUEUE_SIZE) { p->stopped = 1; return ENDP_RESP_NAK; }
    return ENDP_RESP_ACK;
}

static endp_resp_e sent(usbd_handle_t *h, usb_endp_t ep, void *buf, size_t size)
{
    (void)ep; (void)buf; (void)size;
    ports[port_index(h)].tx_busy = 0;
    return ENDP_RESP_NAK;
}

static void service(test_port_t *p)
{
    usbd_interrupt_ctrl(p->h, USB_FALSE);
    if (p->count && !p->tx_busy && usbd_is_enum(p->h) == USB_TRUE) {
        memcpy(p->tx, p->rx[p->tail], REPORT_SIZE);
        p->tx_busy = 1;
        if (usbd_upload(p->h, EP_IN, p->tx, REPORT_SIZE) == USB_RST_OK) {
            p->tail = (p->tail + 1) % QUEUE_SIZE;
            p->count--;
            if (p->stopped) {
                p->stopped = 0;
                usbd_endp_set_response(p->h, EP_OUT, ENDP_RESP_ACK);
            }
        } else p->tx_busy = 0;
    }
    usbd_interrupt_ctrl(p->h, USB_TRUE);
}

static void init_port(unsigned i)
{
    test_port_t *p = &ports[i];
    p->h = board_usbd_init(i);
    CHECK(p->h != NULL);
    CHECK(usbd_drv_init(p->h) == USB_RST_OK);
    CHECK(usbd_enable(p->h) == USB_RST_OK);
    CHECK(usbd_register_event_callback(p->h, USBD_CB_EVENT_RESET, reset_callback) == USB_RST_OK);
    CHECK(usbd_register_req_callback(p->h, 0x80, USB_REQ_CODE_GET_DESCRIPTOR,
                                     get_device_desc, NULL, NULL) == USB_RST_OK);
    CHECK(usbd_register_itf_callback(p->h, 0, hid_setup, hid_control_data, NULL) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, EP_OUT, received) == USB_RST_OK);
    CHECK(usbd_register_data_callback(p->h, EP_IN, sent) == USB_RST_OK);
    CHECK(usbd_open(p->h, USB_SPEED_HIGH, USB_FALSE) == USB_RST_OK);
}

int main(void)
{
    board_init();
    led_init();
    for (uint32_t led = 1; led <= 8; led <<= 1) { led_ctrl(led); Delay_Ms(200); }
    led_ctrl(15); Delay_Ms(1000); led_ctrl(0);

    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOA, ENABLE);
    GPIO_InitTypeDef button = {0};
    button.GPIO_Pin = GPIO_Pin_3;
    button.GPIO_Mode = GPIO_Mode_IPD;
    GPIO_Init(GPIOA, &button);
    init_port(0);
    init_port(1);

    uint32_t tick = 0;
    for (;;) {
        service(&ports[0]);
        service(&ports[1]);
        uint32_t leds = ((tick / 2500) & 1) ? 1 : 0;
        if (usbd_is_enum(ports[0].h) == USB_TRUE) leds |= 2;
        if (usbd_is_enum(ports[1].h) == USB_TRUE) leds |= 4;
        if (GPIO_ReadInputDataBit(GPIOA, GPIO_Pin_3) == Bit_SET) leds |= 8;
        led_ctrl(leds);
        Delay_Us(100);
        tick++;
    }
}
