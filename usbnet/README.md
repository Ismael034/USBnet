# USBnet host tools

This directory is the Python [`uv`](https://docs.astral.sh/uv/) project for the USBnet Client and Server programs. It installs a single `usbnet` command with flags for selecting its function.

```sh
uv sync

# Run on the computer connected to the USBnet Server port:
uv run usbnet --server

# Run on the computer connected to the USBnet Client port:
uv run usbnet --client
```

`--client` auto-detects relay (bulk) or emulated HID mode.

Set the browser or application HTTP and HTTPS proxy on the Client computer to `127.0.0.1:8080`. Override the local proxy port with `uv run usbnet --client --port 9090`.

Board configuration uses the same command:

```sh
uv run usbnet --config list
uv run usbnet --config get
uv run usbnet --config set --help
```

Build installable source and wheel distributions with:

```sh
uv build
```

The artifacts are written to `dist/`.

See the repository [README](../README.md) for firmware build and flash instructions.
