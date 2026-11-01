# SafeDrive AI prototype

This is a local ESP32, Flask, and browser dashboard prototype. It is not a
replacement for emergency services or a certified safety system.

## Start the server

Copy `telegram_config.example.py` to `telegram_config.py` and enter your
Telegram credentials there. `telegram_config.py` is excluded from Git; never
commit or share it. The bot token previously shared in chat must be revoked
with BotFather. Put only the newly generated token in your local config.

```powershell
python -m pip install -r requirements.txt
Copy-Item telegram_config.example.py telegram_config.py
# Edit telegram_config.py and enter your new bot token and chat ID.
python app.py
```

In Telegram, open your bot and press **Start** so it can message you. Restart
Flask after changing `telegram_config.py`.

Open `http://127.0.0.1:5000/` in a browser on the same computer and sign in
with an admin account. The dashboard polls the Flask server for telemetry.

## Configure the ESP32

In `ardunio/ardunio.ino`, set `WIFI_SSID`, `WIFI_PASSWORD`, and
`BACKEND_BASE`. `BACKEND_BASE` must use the IPv4 address of the computer
running Flask, for example `http://192.168.1.20:5000`; do not use
`127.0.0.1` there. The ESP32 and computer must be on the same network, and
the Windows firewall must allow inbound TCP port 5000.

Install ESP32 board support and these Arduino libraries: Adafruit MPU6050,
Adafruit Unified Sensor, and TinyGPSPlus. Upload the sketch and use Serial
Monitor at **115200 baud**.

## Operation

The ESP32 posts live readings once per second. Collision, flame, or smoke
detection starts a 10-second hardware verification period. Touching GPIO 4
during that period cancels the event; otherwise the ESP32 posts a confirmed
incident. Flask finds nearby hospital and police stations, then sends the
configured Telegram dispatches automatically with the incident location map
link. An unconfirmed telemetry request does not trigger a dispatch. Press **C**
on the admin dashboard to send a confirmed crash test, or **F** to send a fire
test. Both tests send a Telegram alert when configured. Use **Space** to cancel
false alarms.

The admin dashboard also has a **Send SOS to Telegram** button for a manual
alert. It sends the latest vehicle location and telemetry to
`TELEGRAM_CHAT_ID`; confirm the prompt before sending. Configure
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `telegram_config.py`. The dashboard displays
configuration and delivery errors if the alert cannot be sent.

Driver accounts create a sign-in password during registration. On their first
visit to the driver panel, they must also create a separate vehicle access
password; later visits require that password before the monitor can start.
The vehicle password is stored as a server-side hash. **Stop camera and lock
panel** stops the camera and requires the vehicle password again.

Each drowsiness episode after two seconds of continuous eye closure adds one
of five alert stages. Three continuous seconds of normal eye detection clears
the stages. At stage five, the browser monitor stops and stays locked until
the driver confirms the vehicle is safely stopped and explicitly restarts
monitoring. This prototype does not control or stop a vehicle; it is not a
substitute for safe driving practices or a certified vehicle safety system.

Only configure `TELEGRAM_POLICE_CHAT_ID` when an agency has explicitly
authorised that channel for testing or dispatch.
