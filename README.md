# ScreenSender

Shows the Windows desktop in a phone browser and lets you click and type on it from the phone.
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

- **Tap**: left click. **Double tap**: double click. **Long press**: right click. Pinch to zoom.
- **Клики** toggles mouse input (handy while zooming around).
- **🖥 1** switches the monitor (shown only with several monitors): 1, 2, … and then all of them together.
- **⌨** opens the keyboard panel: text typed on the phone goes straight to the PC (any language),
  plus Esc / Tab / arrows / Enter / Alt+Tab / copy / Ctrl+V / undo buttons.
  Ctrl, Alt, Shift and Win are sticky: tap Ctrl, then a letter, to get Ctrl+letter;
  tap an active modifier again to send it alone (e.g. Win opens Start).
- **Paste from the phone**: long-press the text field → Paste (or **📋 С телефона**, which reads the
  clipboard directly over HTTPS). The text is put into the PC clipboard and pasted with Ctrl+V,
  so long texts and code arrive instantly and without editors' auto-indent. Ctrl+V in a desktop browser does the same.

## Config

| key | meaning |
|---|---|
| `host`, `port` | listen address, default `0.0.0.0:8005` |
| `public_ip` | your external IP, only used to print the internet URL |
| `username`, `password` | login for the web page |
| `monitor` | monitor at start: `1`, `2`, … or `0` = all together; switchable from the page |
| `fps`, `jpeg_quality`, `max_width` | picture rate and size; lower them for a slow uplink (e.g. `1280` / `40`) |
| `tile_size` | the picture is split into tiles of this size; only changed tiles are sent |
| `draw_cursor` | draw the PC mouse pointer on the picture |
| `allow_input` | allow clicks and typing from the page |

## Notes

- Only the changed parts of the screen travel: a static screen costs nothing, typing costs a few KB.
  When more than half of the tiles change the whole frame is sent as one JPEG.

- Mouse and keyboard input use the Windows API; on other systems the page is view-only.
- The traffic is plain HTTP: the password and the picture are not encrypted.
  Put an HTTPS reverse proxy (e.g. Caddy) in front if you use it over the internet often.
- UAC prompts and the lock screen accept input only when the server runs as administrator.
