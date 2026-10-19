"""
SCD41 air-quality monitor for Raspberry Pi: real-time graph + CSV logging.

Reads CO2, temperature and humidity from the SCD41 every 5 seconds
(directly over I2C, CRC-checked, converted with the datasheet formulas),
shows them on a live graph with the ideal indoor ranges shaded, and saves
every reading to a new CSV file in the "files" folder.

Setup (once):
    Enable I2C:  sudo raspi-config  ->  Interface Options  ->  I2C
    sudo apt install python3-smbus2 python3-matplotlib

Run (needs the Pi's desktop, e.g. via Raspberry Pi Connect screen sharing):
    python scd41_monitor.py              # runs until you close the window
    python scd41_monitor.py --hours 2    # stops by itself after 2 hours

Without a screen (remote shell), log only, no graph:
    python scd41_monitor.py --no-graph --hours 2

The CSV files work with plot_csv.py for graphs afterwards.
"""

import csv
import sys
import time
from pathlib import Path

from smbus2 import SMBus, i2c_msg

# ---- Settings -----------------------------------------------------------
I2C_BUS = 1
ADDRESS = 0x62
OUTPUT_FOLDER = Path(__file__).parent / "files"

# (label, unit, line colour, ideal low, ideal high)
# Change the ideal values here if you use different guidelines.
QUANTITIES = [
    ("CO₂", "ppm", "#2ca02c", 400, 1000),
    ("Temperature", "°C", "#d62728", 20, 24),
    ("Humidity", "%", "#1f77b4", 40, 60),
]
# -------------------------------------------------------------------------

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


def run(hours, show_graph):
    OUTPUT_FOLDER.mkdir(exist_ok=True)
    csv_path = OUTPUT_FOLDER / time.strftime("scd41_%Y-%m-%d_%H-%M.csv")
    csv_file = open(csv_path, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["Timestamp", "Elapsed_Time_s", "CO2_PPM", "Temperature_C", "Humidity_Pct"])
    csv_file.flush()

    sensor = SCD41()
    print("Starting SCD41...")
    sensor.start()
    print(f"Logging to {csv_path}  (first reading in about 5 seconds)")

    start = time.time()
    times, series = [], [[], [], []]

    def take_reading():
        """Read the sensor if a new value is ready. Returns True if a reading was added."""
        try:
            if not sensor.data_ready():
                return False
            co2, temp, hum = sensor.read()
        except (OSError, IOError) as e:
            print(f"Read error: {e}")
            return False
        if co2 == 0:  # startup glitch
            return False
        elapsed = round(time.time() - start, 1)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([ts, elapsed, co2, round(temp, 2), round(hum, 2)])
        csv_file.flush()
        print(f"[{ts}] CO2: {co2} ppm | Temp: {temp:.1f} °C | Hum: {hum:.1f} %")
        times.append(elapsed / 60)
        for values, v in zip(series, (co2, temp, hum)):
            values.append(v)
        return True

    def time_is_up():
        return hours is not None and time.time() - start > hours * 3600

    try:
        if not show_graph:
            while not time_is_up():
                take_reading()
                time.sleep(1)
            return

        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation

        fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
        fig.canvas.manager.set_window_title("SCD41 Air Quality Monitor")
        lines = []
        for ax, (label, unit, colour, low, high) in zip(axes, QUANTITIES):
            ax.axhspan(low, high, color="tab:green", alpha=0.08)
            ax.axhline(low, color="tab:green", linestyle="--", linewidth=0.8, alpha=0.6)
            ax.axhline(high, color="tab:green", linestyle="--", linewidth=0.8, alpha=0.6)
            (line,) = ax.plot([], [], color=colour, linewidth=1.8)
            lines.append(line)
            ax.set_ylabel(f"{label} ({unit})")
            ax.grid(True, alpha=0.3)
            ax.set_title(f"{label}: waiting for first reading…", loc="left", fontsize=10)
        axes[-1].set_xlabel("Elapsed time (min)")
        fig.suptitle("SCD41 – live air quality  (green = ideal range)")
        fig.tight_layout()

        def update(_frame):
            if time_is_up():
                print(f"Reached {hours} hours - stopping.")
                plt.close(fig)
                return lines
            if not take_reading():
                return lines
            x_max = max(times[-1], 1)
            for ax, line, values, (label, unit, _c, low, high) in zip(axes, lines, series, QUANTITIES):
                line.set_data(times, values)
                current = values[-1]
                status = "OK" if low <= current <= high else ("too low" if current < low else "too high")
                ax.set_title(f"{label}: {current:.1f} {unit}  ({status})" if unit != "ppm"
                             else f"{label}: {current:.0f} {unit}  ({status})", loc="left", fontsize=10)
                lo, hi = min(min(values), low), max(max(values), high)
                pad = (hi - lo) * 0.08 or 1
                ax.set_ylim(lo - pad, hi + pad)
            axes[-1].set_xlim(0, x_max * 1.02)
            return lines

        # check for a new reading every second; the sensor delivers one every 5 s
        _ani = FuncAnimation(fig, update, interval=1000, cache_frame_data=False)
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        csv_file.close()
        sensor.stop()
        print(f"\nSaved {csv_path}")


def main():
    args = sys.argv[1:]
    hours = float(args[args.index("--hours") + 1]) if "--hours" in args else None
    run(hours, show_graph="--no-graph" not in args)


if __name__ == "__main__":
    main()
