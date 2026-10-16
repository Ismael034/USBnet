# CH32V467WEU6 USBnet firmware and separate hardware-test image.
# Local WCH "RISC-V Embedded GCC15" (riscv32-wch-elf) under ignored toolchain/.
# Override for another install, e.g. MounRiver Studio:
#   make -C ch32v467 TOOLCHAIN_DIR="/path/to/RISC-V Embedded GCC15/bin"
TOOLCHAIN_DIR ?= $(CURDIR)/../toolchain/riscv32-wch-elf-gcc15/bin
CROSS ?= $(TOOLCHAIN_DIR)/riscv32-wch-elf-
CC := "$(CROSS)gcc"
OBJCOPY := "$(CROSS)objcopy"
SIZE := "$(CROSS)size"

SRC := src/src/main.c src/src/profile.c src/src/config_mode.c \
  src/vendor/Core/core_riscv.c src/vendor/Debug/debug.c \
  src/vendor/Peripheral/src/ch32v4x7_gpio.c \
  src/vendor/Peripheral/src/ch32v4x7_flash.c \
  src/vendor/Peripheral/src/ch32v4x7_rcc.c \
  src/vendor/Peripheral/src/ch32v4x7_usart.c \
  src/vendor/board/board.c src/vendor/board/led.c \
  src/vendor/board/system_ch32v4x7.c src/vendor/src/descriptor.c \
  src/vendor/usb_driver/port/usbhs1_dcp.c \
  src/vendor/usb_driver/port/usbhs2_dcp.c \
  src/vendor/usb_driver/src/usbd_driver_core.c
ASM := src/vendor/Startup/startup_ch32v4x7.S
INC := -Isrc/src -Isrc/vendor/Core -Isrc/vendor/Debug -Isrc/vendor/Peripheral/inc \
  -Isrc/vendor/board -Isrc/vendor/src \
  -Isrc/vendor/usb_driver/include -Isrc/vendor/usb_driver/port
ARCH := -march=rv32imacxw -mabi=ilp32
CFLAGS := $(ARCH) -O2 -g -ffunction-sections -fdata-sections -fno-common \
  -Wall -Wextra -Wno-unused-parameter -MMD -MP $(INC)
LDFLAGS := $(ARCH) -nostartfiles -Wl,--gc-sections \
  -T src/vendor/Ld/Link.ld --specs=nano.specs --specs=nosys.specs
OBJ := $(patsubst %.c,build/%.o,$(SRC)) $(patsubst %.S,build/%.o,$(ASM))
TEST_OBJ := $(filter-out build/src/src/main.o build/src/src/profile.o \
  build/src/src/config_mode.o build/src/vendor/Peripheral/src/ch32v4x7_flash.o \
  build/src/vendor/src/descriptor.o,$(OBJ)) \
  build/src/src/test_main.o build/test/descriptor.o
-include $(OBJ:.o=.d) $(TEST_OBJ:.o=.d)

# The -include above defines object targets, so name the goal explicitly to
# keep `make -C ch32v467` (and `make test`) building the images.
.DEFAULT_GOAL := all

.PHONY: all test clean
all: build/ch32v467.hex build/ch32v467.bin
test: build/ch32v467_test.hex build/ch32v467_test.bin

build/ch32v467.elf: $(OBJ) src/vendor/Ld/Link.ld
	$(CC) $(OBJ) $(LDFLAGS) -Wl,-Map,build/ch32v467.map -o $@
	$(SIZE) $@

build/ch32v467_test.elf: $(TEST_OBJ) src/vendor/Ld/Link.ld
	$(CC) $(TEST_OBJ) $(LDFLAGS) -Wl,-Map,build/ch32v467_test.map -o $@
	$(SIZE) $@

build/ch32v467.hex: build/ch32v467.elf
	$(OBJCOPY) -O ihex $< $@

build/ch32v467.bin: build/ch32v467.elf
	$(OBJCOPY) -O binary $< $@

build/ch32v467_test.hex: build/ch32v467_test.elf
	$(OBJCOPY) -O ihex $< $@

build/ch32v467_test.bin: build/ch32v467_test.elf
	$(OBJCOPY) -O binary $< $@

build/test/descriptor.o: src/vendor/src/descriptor.c src/vendor/src/descriptor.h
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -DUSBNET_TEST_FIRMWARE -c $< -o $@

build/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

build/%.o: %.S
	@mkdir -p $(dir $@)
	$(CC) $(ARCH) -c $< -o $@

clean:
	rm -rf build
