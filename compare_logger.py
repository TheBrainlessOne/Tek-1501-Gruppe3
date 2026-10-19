"""
Log the SCD41 and a Comet T6540 side by side, into one CSV file.

The Pi reads the SCD41 over I2C and the Comet over the network (from the
Comet's own web server), every 5 seconds, so both readings in a row are
taken at the same moment.

Output: files/compare_<date>_<time>.csv  ->  graph it with compare_plot.py

Before the first run:
  1. sudo apt install python3-smbus2   (if not installed already)
  2. Check COMET_IP below matches your Comet (find it with: sudo nmap -sn 10.0.0.0/24,
     look for a MAC address starting with 00:20:4A).
  3. Test the Comet connection on its own:
         python compare_logger.py --test-comet
     and check the numbers match what the Comet's display / web page shows.

Run:
    python compare_logger.py            # runs until Ctrl+C
    python compare_logger.py --hours 2  # stops after 2 hours
"""

import csv
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from smbus2 import SMBus, i2c_msg

COMET_IP = "10.0.0.36"
INTERVAL_S = 5
I2C_BUS = 1
ADDRESS = 0x62  # SCD41
OUTPUT_FOLDER = Path(__file__).parent / "files"


# SCD41 commands (from the datasheet)
CMD_START_PERIODIC = 0x21B1
CMD_STOP_PERIODIC = 0x3F86
CMD_READ_MEASUREMENT = 0xEC05
CMD_DATA_READY = 0xE4B8
CMD_WAKE_UP = 0x36F6


def crc8(data):
    """Sensirion CRC-8 (polynomial 0x31, init 0xFF)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class SCD41:
    def __init__(self, bus_number=I2C_BUS, address=ADDRESS):
        self.bus = SMBus(bus_number)
        self.address = address

    def _send(self, command):
        self.bus.i2c_rdwr(i2c_msg.write(self.address, [command >> 8, command & 0xFF]))

    def _read_words(self, command, n_words):
        """Send a command and read n 16-bit words, checking each word's CRC."""
        self._send(command)
        time.sleep(0.002)
        msg = i2c_msg.read(self.address, n_words * 3)
        self.bus.i2c_rdwr(msg)
        raw = list(msg)
        words = []
        for i in range(0, len(raw), 3):
            msb, lsb, crc = raw[i], raw[i + 1], raw[i + 2]
            if crc8([msb, lsb]) != crc:
                raise IOError("CRC error - reading corrupted, skipped")
            words.append((msb << 8) | lsb)
        return words

    def start(self):
        try:
            self._send(CMD_WAKE_UP)
        except OSError:
            pass  # the sensor doesn't acknowledge wake-up; that's normal
        time.sleep(0.03)
        try:
            self._send(CMD_STOP_PERIODIC)
        except OSError:
            pass
        time.sleep(0.5)
        self._send(CMD_START_PERIODIC)

    def stop(self):
        try:
            self._send(CMD_STOP_PERIODIC)
            time.sleep(0.5)
        finally:
            self.bus.close()

    def data_ready(self):
        (status,) = self._read_words(CMD_DATA_READY, 1)
        return (status & 0x07FF) != 0

    def read(self):
        """Return (co2_ppm, temperature_c, humidity_pct)."""
        co2, raw_t, raw_rh = self._read_words(CMD_READ_MEASUREMENT, 3)
        temperature = -45 + 175 * raw_t / 65535
        humidity = 100 * raw_rh / 65535
        return co2, temperature, humidity


def _http_get(path):
    with urllib.request.urlopen(f"http://{COMET_IP}/{path}", timeout=4) as r:
        return r.read().decode("utf-8", errors="replace")


def _to_number(text):
    try:
        return float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None  # e.g. "n/a" or "Error"


def _channels_from_xml(text):
    """Comet values.xml: each channel has <name>, <unit> and <aval> (or <value>)."""
    channels = []
    for el in ET.fromstring(text).iter():
        children = {c.tag.lower(): (c.text or "") for c in el}
        if "unit" in children and ("aval" in children or "value" in children):
            value = _to_number(children.get("aval", children.get("value")))
            if value is not None:
                channels.append((children.get("name", ""), value, children["unit"]))
    return channels


def _channels_from_json(text):
    channels = []

    def walk(obj):
        if isinstance(obj, dict):
            keys = {k.lower(): v for k, v in obj.items()}
            if "unit" in keys and ("aval" in keys or "value" in keys):
                value = _to_number(keys.get("aval", keys.get("value")))
                if value is not None:
                    channels.append((str(keys.get("name", "")), value, str(keys["unit"])))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(json.loads(text))
    return channels


def _channels_from_html(text):
    """Last resort: pick numbers followed by a unit out of the main web page."""
    text = text.replace("&deg;", "°").replace("&#176;", "°")
    text = re.sub(r"<[^>]+>", " ", text)
    pattern = r"(-?\d+(?:[.,]\d+)?)\s*(°C|%RH|% RH|%|ppm)"
    return [("", _to_number(v), unit) for v, unit in re.findall(pattern, text)]


def comet_channels():
    """Return a list of (name, value, unit) for everything the Comet measures."""
    for path, parser in [("values.xml", _channels_from_xml),
                         ("values.json", _channels_from_json),
                         ("", _channels_from_html)]:
        try:
            channels = parser(_http_get(path))
            if channels:
                return channels
        except Exception:
            continue
    raise IOError(f"Could not read any values from the Comet at {COMET_IP}")


def comet_read():
    """Return (co2_ppm, temperature_c, humidity_pct) from the Comet."""
    co2 = temperature = humidity = None
    for name, value, unit in comet_channels():
        unit_l, name_l = unit.lower(), name.lower()
        if "ppm" in unit_l and co2 is None:
            co2 = value
        elif "%" in unit_l and humidity is None:
            humidity = value
        elif "c" in unit_l and "dew" not in name_l and temperature is None:
            temperature = value  # first °C channel; dew point comes later
    if None in (co2, temperature, humidity):
        raise IOError(f"Missing values from Comet (CO2={co2}, T={temperature}, RH={humidity})")
    return int(round(co2)), temperature, humidity


def test_comet():
    print(f"Reading Comet at http://{COMET_IP} ...")
    print("Everything the Comet reports:")
    for name, value, unit in comet_channels():
        print(f"  {name or '(no name)':<25} {value} {unit}")
    print("\nWhat the script will log:")
    for _ in range(3):
        co2, t, rh = comet_read()
        print(f"  CO2: {co2} ppm   Temp: {t:.1f} °C   Hum: {rh:.1f} %")
        time.sleep(2)
    print("Compare these with the Comet's display or web page.")


def main():
    args = sys.argv[1:]
    if "--test-comet" in args:
        test_comet()
        return
    hours = float(args[args.index("--hours") + 1]) if "--hours" in args else None

    OUTPUT_FOLDER.mkdir(exist_ok=True)
    path = OUTPUT_FOLDER / time.strftime("compare_%Y-%m-%d_%H-%M.csv")
    columns = ["Timestamp", "Elapsed_Time_s",
               "SCD41_CO2_PPM", "SCD41_Temperature_C", "SCD41_Humidity_Pct",
               "Comet_CO2_PPM", "Comet_Temperature_C", "Comet_Humidity_Pct"]

    scd = SCD41()
    scd.start()
    print(f"Logging to {path}  (Ctrl+C to stop)")

    start = time.time()
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        try:
            while hours is None or time.time() - start < hours * 3600:
                time.sleep(INTERVAL_S)
                try:
                    if not scd.data_ready():
                        continue
                    s_co2, s_t, s_rh = scd.read()
                except (OSError, IOError) as e:
                    print(f"SCD41 read error: {e}")
                    continue
                if s_co2 == 0:
                    continue
                try:
                    c_co2, c_t, c_rh = comet_read()
                except (OSError, IOError) as e:
                    print(f"Comet read error: {e}")
                    c_co2 = c_t = c_rh = ""

                elapsed = round(time.time() - start, 1)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([ts, elapsed, s_co2, round(s_t, 2), round(s_rh, 2), c_co2, c_t, c_rh])
                f.flush()
                print(f"[{ts}] CO2 {s_co2} / {c_co2} ppm | "
                      f"Temp {s_t:.1f} / {c_t} °C | Hum {s_rh:.1f} / {c_rh} %   (SCD41 / Comet)")
        except KeyboardInterrupt:
            pass
        finally:
            scd.stop()
            print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
