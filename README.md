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
- **⌨** opens the keyboard panel: text typed on the phone goes straight to the PC (any language),
  plus Esc / Tab / arrows / Enter / Alt+Tab / copy / paste / undo buttons.
  Ctrl, Alt, Shift and Win are sticky: tap Ctrl, then a letter, to get Ctrl+letter;
  tap an active modifier again to send it alone (e.g. Win opens Start).

## Config

| key | meaning |
|---|---|
| `host`, `port` | listen address, default `0.0.0.0:8005` |
| `public_ip` | your external IP, only used to print the internet URL |
| `username`, `password` | login for the web page |
| `monitor` | `1` = primary monitor, `0` = all monitors together |
| `fps`, `jpeg_quality`, `max_width` | picture rate and size; lower them for a slow uplink (e.g. `1280` / `40`) |
| `draw_cursor` | draw the PC mouse pointer on the picture |
| `allow_input` | allow clicks and typing from the page |

## Notes

- Mouse and keyboard input use the Windows API; on other systems the page is view-only.
- The traffic is plain HTTP: the password and the picture are not encrypted.
  Put an HTTPS reverse proxy (e.g. Caddy) in front if you use it over the internet often.
- UAC prompts and the lock screen accept input only when the server runs as administrator.
