"""Protocol discovery for the HeadRush Remote Editor.

The Remote Editor is a web app the device serves over Wi-Fi. Its wire
protocol is undocumented, so this package derives it from three independent
sources and cross-checks them:

* :mod:`acro.protocol.capture` - static analysis of the served bundle
* :mod:`acro.protocol.recorder` - live HTTP/WebSocket traffic while a human
  drives the real editor
* :mod:`acro.protocol.firmware` - read-only static analysis of the firmware image

All three are observation only. Nothing here writes to, patches, or reflashes
the device.
"""
