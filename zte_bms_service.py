#!/usr/bin/env python3
"""
ZTE Birubatt BB-48 FB100B3 LiFePO4 Multi-Pack BMS Reader
Membaca telemetri baterai ZTE ZXDC48 FB100B3 via RS485 Modbus-RTU (FC 04, Baud 9600 Even).
Mendukung multi-slave (misal Slave ID 2 dan Slave ID 5).
"""

import os
import sys
import time
import json
import glob
import struct
import logging
from typing import Dict, Any, Optional, List

try:
    import serial
except ImportError:
    print("Error: pyserial belum terinstall. Jalankan: pip install pyserial")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [ZTE-BMS] %(message)s"
)
logger = logging.getLogger("ZTE-BMS")

STATE_FILE = "/home/ares/Documents/myserver/data/bms_state.json"


def calculate_crc16(data: bytes) -> bytes:
    """Modbus RTU CRC16 calculation."""
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)


def check_crc16(data: bytes) -> bool:
    """Verifikasi apakah CRC16 pada frame respon valid."""
    if len(data) < 4:
        return False
    expected = data[-2:]
    calculated = calculate_crc16(data[:-2])
    return expected == calculated


class ZTEBmsReader:
    def __init__(self, port: str = None, baudrate: int = 9600, slaves: List[int] = None, timeout: float = 0.6):
        self.port = port
        self.baudrate = baudrate
        self.slaves = slaves or [2, 5]
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self.last_data: Dict[str, Any] = {}
        self.cached_packs: Dict[str, Dict[str, Any]] = {}
        self.pack_last_seen: Dict[str, float] = {}

    def auto_detect_port(self) -> Optional[str]:
        """Mencari port USB serial yang aktif (/dev/ttyUSB*)."""
        candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        if candidates:
            return candidates[0]
        return None

    def connect(self) -> bool:
        if not self.port or not os.path.exists(self.port):
            detected = self.auto_detect_port()
            if detected:
                self.port = detected
            else:
                return False

        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout
            )
            return True
        except Exception as e:
            logger.error(f"Gagal membuka port {self.port}: {e}")
            return False

    def read_single_slave(self, slave_id: int, retries: int = 2) -> Optional[Dict[str, Any]]:
        """Membaca 32 input register dari slave tertentu (FC 04, start 0x0080, count 32) dengan retry."""
        if not self.ser or not self.ser.is_open:
            if not self.connect():
                return None

        pdu = struct.pack(">BBHH", slave_id, 0x04, 0x0080, 32)
        frame = pdu + calculate_crc16(pdu)
        expected_bytes = 1 + 1 + 1 + (32 * 2) + 2  # 69 bytes

        for attempt in range(retries):
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
                time.sleep(0.06)
                self.ser.write(frame)
                resp = self.ser.read(expected_bytes)

                if len(resp) < expected_bytes or resp[0] != slave_id or resp[1] != 0x04:
                    if attempt < retries - 1:
                        time.sleep(0.08)
                        continue
                    return None

                if not check_crc16(resp):
                    logger.warning(f"CRC mismatch pada respon BMS Slave {slave_id} (attempt {attempt+1})")
                    if attempt < retries - 1:
                        time.sleep(0.08)
                        continue
                    return None

                byte_cnt = resp[2]
                raw = resp[3:3 + byte_cnt]
                regs = [struct.unpack(">H", raw[i:i+2])[0] for i in range(0, len(raw), 2)]

                pack_v = round(regs[0] * 0.01, 2)
                bus_v = round(regs[1] * 0.01, 2)
                curr = (regs[2] - 65536) if regs[2] > 32767 else regs[2]
                curr_a = round(curr * 0.01, 2)
                batt_watt = int(round(pack_v * curr_a))

                # 15 Sel LiFePO4 (Regs 4 s/d 18)
                cell_voltages = []
                for r in regs[4:19]:
                    if 2000 <= r <= 4300:
                        cell_voltages.append(round(r * 0.001, 3))

                max_cell_v = max(cell_voltages) if cell_voltages else 0.0
                min_cell_v = min(cell_voltages) if cell_voltages else 0.0
                max_cell_idx = (cell_voltages.index(max_cell_v) + 1) if cell_voltages else 0
                min_cell_idx = (cell_voltages.index(min_cell_v) + 1) if cell_voltages else 0
                cell_delta_mv = round((max_cell_v - min_cell_v) * 1000, 1) if cell_voltages else 0.0

                # Suhu Sensor 1, 2, 3 (Regs 20, 21, 22)
                t1 = round(regs[20] * 0.1, 1) if len(regs) > 20 else 28.0
                t2 = round(regs[21] * 0.1, 1) if len(regs) > 21 else 28.0
                t3 = round(regs[22] * 0.1, 1) if len(regs) > 22 else 28.0
                avg_temp = round((t1 + t2 + t3) / 3.0, 1)

                # SOC (Reg 25)
                soc_pct = int(regs[25]) if len(regs) > 25 and regs[25] <= 100 else 98
                # Cycle count (Reg 26)
                cycle_count = int(regs[26]) if len(regs) > 26 else 0

                balance_status = "🟢 Prima & Seimbang" if cell_delta_mv < 30 else ("🟡 Cukup Seimbang" if cell_delta_mv < 80 else "🔴 Butuh Balancing")

                pack_name = f"Baterai {self.slaves.index(slave_id)+1} (Slave {slave_id})" if slave_id in self.slaves else f"Baterai (Slave {slave_id})"

                result = {
                    "slave_id": slave_id,
                    "name": pack_name,
                    "connected": True,
                    "pack_voltage": pack_v,
                    "busbar_voltage": bus_v,
                    "pack_current": curr_a,
                    "pack_power_watt": batt_watt,
                    "soc": soc_pct,
                    "temperatures": [t1, t2, t3],
                    "avg_cell_temp_c": avg_temp,
                    "cycle_count": cycle_count,
                    "cell_count": len(cell_voltages),
                    "cell_voltages": cell_voltages,
                    "cell_max_v": max_cell_v,
                    "cell_min_v": min_cell_v,
                    "cell_max_idx": max_cell_idx,
                    "cell_min_idx": min_cell_idx,
                    "cell_delta_mv": cell_delta_mv,
                    "balance_status": balance_status
                }
                self.cached_packs[str(slave_id)] = result
                self.pack_last_seen[str(slave_id)] = time.time()
                return result
            except Exception as e:
                logger.debug(f"Error baca Slave {slave_id} (attempt {attempt+1}): {e}")
                if attempt < retries - 1:
                    time.sleep(0.08)

        return None

    def read_all_telemetry(self) -> Optional[Dict[str, Any]]:
        """Membaca semua slave baterai yang terpasang dan mengagregasi hasilnya."""
        packs = {}
        all_cells = []
        all_temps = []
        now_ts = time.time()

        for slave_id in self.slaves:
            pack_data = self.read_single_slave(slave_id)
            if not pack_data and (now_ts - self.pack_last_seen.get(str(slave_id), 0) < 15.0):
                pack_data = self.cached_packs.get(str(slave_id))

            if pack_data:
                packs[str(slave_id)] = pack_data
                all_cells.extend(pack_data["cell_voltages"])
                all_temps.extend(pack_data["temperatures"])
            time.sleep(0.08)  # Turnaround delay pada RS-485 bus

        if not packs:
            return None

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        pack_count = len(packs)

        total_current = round(sum(p["pack_current"] for p in packs.values()), 2)
        total_power = sum(p["pack_power_watt"] for p in packs.values())
        avg_pack_v = round(sum(p["pack_voltage"] for p in packs.values()) / pack_count, 2)
        avg_busbar_v = round(sum(p["busbar_voltage"] for p in packs.values()) / pack_count, 2)
        avg_soc = int(round(sum(p["soc"] for p in packs.values()) / pack_count))
        avg_temp = round(sum(all_temps) / max(1, len(all_temps)), 1)
        max_cycle = max(p["cycle_count"] for p in packs.values())

        max_cell_v = max(all_cells) if all_cells else 0.0
        min_cell_v = min(all_cells) if all_cells else 0.0
        max_delta_mv = round((max_cell_v - min_cell_v) * 1000, 1) if all_cells else 0.0

        overall_balance = "🟢 Prima & Seimbang" if max_delta_mv < 30 else ("🟡 Cukup Seimbang" if max_delta_mv < 80 else "🔴 Butuh Balancing")

        aggregated = {
            "timestamp": now_str,
            "connected": True,
            "port": self.port,
            "pack_count": pack_count,
            "nominal_capacity_ah": pack_count * 100,
            "nominal_energy_kwh": round(pack_count * 4.8, 1),
            "battery_model": f"ZTE Birubatt FB100B3 ({pack_count}x Pack • {pack_count*100}Ah LiFePO4)",
            "packs": packs,
            # Top-level aggregated fields (compatible with existing consumers)
            "pack_voltage": avg_pack_v,
            "busbar_voltage": avg_busbar_v,
            "pack_current": total_current,
            "pack_power_watt": total_power,
            "soc": avg_soc,
            "avg_cell_temp_c": avg_temp,
            "temperatures": all_temps,
            "cycle_count": max_cycle,
            "cell_count": len(all_cells),
            "cell_voltages": all_cells,
            "cell_max_v": max_cell_v,
            "cell_min_v": min_cell_v,
            "cell_delta_mv": max_delta_mv,
            "balance_status": overall_balance
        }

        self.last_data = aggregated
        return aggregated

    def save_state_file(self):
        """Menyimpan data terkini ke file JSON untuk diakses Web Dashboard."""
        if not self.last_data:
            return
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        try:
            with open(STATE_FILE + ".tmp", "w") as f:
                json.dump(self.last_data, f, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        except Exception as e:
            logger.debug(f"Gagal menulis {STATE_FILE}: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ZTE FB100B3 Multi-BMS RS485 Modbus Reader")
    parser.add_argument("--port", type=str, default=None, help="Serial port (misal /dev/ttyUSB0)")
    parser.add_argument("--slaves", type=str, default="2,5", help="Comma-separated Modbus Slave Addresses (default 2,5)")
    parser.add_argument("--loop", action="store_true", help="Jalankan sebagai service polling berkala")
    parser.add_argument("--interval", type=float, default=2.5, help="Interval polling dalam detik")
    args = parser.parse_args()

    slaves = [int(s.strip()) for s in args.slaves.split(",") if s.strip().isdigit()]
    if not slaves:
        slaves = [2, 5]

    reader = ZTEBmsReader(port=args.port, slaves=slaves)

    if not args.loop:
        res = reader.read_all_telemetry()
        if res:
            print(json.dumps(res, indent=2))
            reader.save_state_file()
        else:
            print("Gagal membaca telemetri BMS")
    else:
        logger.info(f"Memulai loop pembacaan BMS ZTE FB100B3 (Slaves: {slaves}, Interval: {args.interval}s)...")
        while True:
            try:
                data = reader.read_all_telemetry()
                if data:
                    reader.save_state_file()
                    p_info = " | ".join([f"P{p['slave_id']}:{p['pack_voltage']}V/{p['pack_current']}A/{p['soc']}%" for p in data["packs"].values()])
                    logger.info(f"Packs ({data['pack_count']}): {p_info} | Tot: {data['pack_current']}A ({data['pack_power_watt']}W) | SOC: {data['soc']}% | Δ {data['cell_delta_mv']}mV | {data['avg_cell_temp_c']}°C")
                else:
                    logger.warning("BMS timeout / tidak ada respon slave...")
            except Exception as e:
                logger.error(f"Error loop: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
