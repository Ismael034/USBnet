inc = include_directories(
  'src/src', 'src/vendor/Core', 'src/vendor/Debug', 'src/vendor/Peripheral/inc',
  'src/vendor/board', 'src/vendor/src',
  'src/vendor/usb_driver/include', 'src/vendor/usb_driver/port'
)

common = files(
  'src/vendor/Core/core_riscv.c',
  'src/vendor/Debug/debug.c',
  'src/vendor/Peripheral/src/ch32v4x7_gpio.c',
  'src/vendor/Peripheral/src/ch32v4x7_rcc.c',
  'src/vendor/Peripheral/src/ch32v4x7_usart.c',
  'src/vendor/board/board.c',
  'src/vendor/board/led.c',
  'src/vendor/board/system_ch32v4x7.c',
  'src/vendor/src/descriptor.c',
  'src/vendor/usb_driver/port/usbhs1_dcp.c',
  'src/vendor/usb_driver/port/usbhs2_dcp.c',
  'src/vendor/usb_driver/src/usbd_driver_core.c',
  'src/vendor/Startup/startup_ch32v4x7.S'
)

main_firmware = executable('ch32v467', files('src/src/main.c'), common,
  files('src/src/profile.c', 'src/src/config_mode.c',
        'src/vendor/Peripheral/src/ch32v4x7_flash.c'),
  include_directories : inc,
  name_suffix : 'elf',
  link_args : ['-T', meson.current_source_dir() / 'src/vendor/Ld/Link.ld'])

test_firmware = executable('ch32v467_test', files('src/src/test_main.c'), common,
  include_directories : inc,
  c_args : ['-DUSBNET_TEST_FIRMWARE'],
  name_suffix : 'elf',
  build_by_default : false,
  link_args : ['-T', meson.current_source_dir() / 'src/vendor/Ld/Link.ld'])

objcopy = find_program('riscv32-wch-elf-objcopy')
custom_target('ch32v467.hex', input : main_firmware, output : 'ch32v467.hex',
  command : [objcopy, '-O', 'ihex', '@INPUT@', '@OUTPUT@'], build_by_default : true)
custom_target('ch32v467.bin', input : main_firmware, output : 'ch32v467.bin',
  command : [objcopy, '-O', 'binary', '@INPUT@', '@OUTPUT@'], build_by_default : true)
custom_target('ch32v467_test.hex', input : test_firmware, output : 'ch32v467_test.hex',
  command : [objcopy, '-O', 'ihex', '@INPUT@', '@OUTPUT@'], build_by_default : false)
custom_target('ch32v467_test.bin', input : test_firmware, output : 'ch32v467_test.bin',
  command : [objcopy, '-O', 'binary', '@INPUT@', '@OUTPUT@'], build_by_default : false)
