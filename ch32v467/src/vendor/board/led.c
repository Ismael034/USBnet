#include "ch32v4x7.h"
#include "led.h"

/* LED1..4: PA9, PA8, PB10, PB11; anodes through RN1, cathodes to GND. */
usb_rst_e led_init(void)
{
    GPIO_InitTypeDef gpio = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOA | RCC_PB2Periph_GPIOB, ENABLE);
    gpio.GPIO_Mode = GPIO_Mode_Out_PP;
    gpio.GPIO_Speed = GPIO_Speed_High;
    gpio.GPIO_Pin = GPIO_Pin_8 | GPIO_Pin_9;
    GPIO_Init(GPIOA, &gpio);
    gpio.GPIO_Pin = GPIO_Pin_10 | GPIO_Pin_11;
    GPIO_Init(GPIOB, &gpio);
    led_ctrl(0);
    return USB_RST_OK;
}

void led_ctrl(uint32_t bits)
{
    GPIO_WriteBit(GPIOA, GPIO_Pin_9, (bits & 1) ? Bit_SET : Bit_RESET);
    GPIO_WriteBit(GPIOA, GPIO_Pin_8, (bits & 2) ? Bit_SET : Bit_RESET);
    GPIO_WriteBit(GPIOB, GPIO_Pin_10, (bits & 4) ? Bit_SET : Bit_RESET);
    GPIO_WriteBit(GPIOB, GPIO_Pin_11, (bits & 8) ? Bit_SET : Bit_RESET);
}
