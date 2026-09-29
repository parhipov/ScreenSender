# ScreenSender

Shows the Windows desktop in a phone browser and lets you click, scroll and type on it from the phone.
Works over the LAN or from the internet through a forwarded port.

## Run

```
pip install -r requirements.txt
copy config.example.json config.json   # then edit username / password / public_ip
python server.py
```

If `config.json` is missing it is created with defaults on first start.
The startup output lists the LAN and internet URLs and the login.

## Using it

- **Tap**: left click. **Double tap**: double click. **Long press**: right click.
- **Pinch**: zoom in; then drag with one finger to look around. Only the visible part of the screen
  is transferred while zoomed in.
- **Two fingers up/down (or sideways)**: mouse wheel on the PC. A mouse wheel in a desktop browser does the same.
- **🎚 Эконом / Обычно / Чётко** picks the picture quality (bandwidth); remembered by the browser.
- **Клики** toggles mouse input (handy while looking around).
- **🖥 1** switches the monitor (shown only with several monitors): 1, 2, … and then all of them together.
- **⌨** opens the keyboard panel: text typed on the phone goes straight to the PC (any language),
  plus Esc / Tab / arrows / Enter / Alt+Tab / copy / Ctrl+V / undo buttons.
  Ctrl, Alt, Shift and Win are sticky: tap Ctrl, then a letter, to get Ctrl+letter;
  tap an active modifier again to send it alone (e.g. Win opens Start).
- **Paste from the phone**: long-press the text field → Paste (or **📋 С телефона**, which reads the
  clipboard directly over HTTPS). The text is put into the PC clipboard and pasted with Ctrl+V,
  so long texts and code arrive instantly and without editors' auto-indent. Ctrl+V in a desktop browser does the same.
- The corner shows the capture method, updates per second and KB/s.

## Config

| key | meaning |
|---|---|
| `host`, `port` | listen address, default `0.0.0.0:8005` |
| `https` | `true` serves HTTPS with a self-signed certificate (see below) |
| `public_ip` | your external IP, only used to print the internet URL (and put into the certificate) |
| `username`, `password` | login for the web page |
| `monitor` | monitor at start: `1`, `2`, … or `0` = all together; switchable from the page |
| `capture` | `auto` (default): DXGI desktop duplication, GDI when it is unavailable; `dxgi` or `gdi` to force one |
| `fps` | capture rate; the phone gets updates no faster than this |
| `max_width` | the picture is scaled down to this width; lower it for a slow uplink (e.g. `1280`) |
| `tile_size` | the picture is split into tiles of this size; only changed tiles are sent |
| `draw_cursor` | draw the PC mouse pointer on the picture |
| `allow_input` | allow clicks, scrolling and typing from the page |

## HTTPS

With `"https": true` the server makes `cert.pem` / `key.pem` on first start (valid 800 days, made
again when expired) and serves HTTPS only. The phone browser then warns about an unknown certificate
once; tap "proceed" and it is remembered. To get rid of the warning install the certificate on the
phone as a trusted root: open `https://<pc>:8005/cert`, then Android: Settings → Security → Install
a certificate → CA certificate; iPhone: install the downloaded profile, then Settings → General →
About → Certificate Trust Settings → enable full trust.

HTTPS also unlocks reading the phone clipboard for **📋 С телефона**. Delete `cert.pem` to make a new
certificate (e.g. after the LAN IP changed).

## Notes

- Only the changed parts of the screen travel: a still screen costs nothing, typing costs a few KB.
  Small pieces go as WebP, big changes as one JPEG. Once the screen is still for a moment the
  changed parts are sent again in high quality, so text is crisp after scrolling.
- DXGI capture (`dxcam`) takes a 4K frame in about 5 ms and knows when nothing changed. It cannot
  see the lock screen or UAC prompts; GDI is used for half a minute after such a failure.
- Mouse and keyboard input use the Windows API; on other systems the page is view-only.
- Plain HTTP sends the password and the picture unencrypted; turn on `https` or put a reverse proxy
  (e.g. Caddy) in front if you use it over the internet.
- UAC prompts and the lock screen accept input only when the server runs as administrator.
