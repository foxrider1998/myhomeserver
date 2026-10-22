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
        """Mencari port USB serial yang aktif, prioritaskan /dev/ttyUSB* untuk konverter RS485."""
        usb_candidates = sorted(glob.glob("/dev/ttyUSB*"))
        if usb_candidates:
            return usb_candidates[0]
        acm_candidates = sorted(glob.glob("/dev/ttyACM*"))
        if acm_candidates:
            return acm_candidates[0]
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

    def scan_online_slaves(self, candidate_ids: List[int] = None) -> List[int]:
        """Memindai slave address (default 1 s/d 8) untuk menemukan pack baterai ZTE yang aktif di bus RS485."""
        candidates = candidate_ids or list(range(1, 9))
        online = []
        for sid in candidates:
            pack_data = self.read_single_slave(sid, retries=1)
            if pack_data:
                online.append(sid)
            time.sleep(0.04)
        return online

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

                # Reg 2 (0x0082): Suhu Lingkungan BMS (Signed int16, 0.1°C)
                env_temp = round(((regs[2] - 65536) if regs[2] > 32767 else regs[2]) * 0.1, 1) if len(regs) > 2 else 28.0

                # Reg 3 (0x0083): Arus Baterai (Signed int16, 0.01A: + = Pengisian/Charge, - = Pengosongan/Discharge)
                curr_raw = (regs[3] - 65536) if len(regs) > 3 and regs[3] > 32767 else (regs[3] if len(regs) > 3 else 0)
                curr_a = round(curr_raw * 0.01, 2)
                batt_watt = int(round(pack_v * curr_a))

                # 15 Sel LiFePO4 (Regs 4 s/d 18: 0x0084 s/d 0x0092, 0.001V)
                cell_voltages = []
                for r in regs[4:19]:
                    if 2000 <= r <= 4300:
                        cell_voltages.append(round(r * 0.001, 3))

                max_cell_v = max(cell_voltages) if cell_voltages else 0.0
                min_cell_v = min(cell_voltages) if cell_voltages else 0.0
                max_cell_idx = (cell_voltages.index(max_cell_v) + 1) if cell_voltages else 0
                min_cell_idx = (cell_voltages.index(min_cell_v) + 1) if cell_voltages else 0
                cell_delta_mv = round((max_cell_v - min_cell_v) * 1000, 1) if cell_voltages else 0.0

                # 4 Sensor Suhu Sel Baterai (Regs 19, 20, 21, 22: 0x0093 s/d 0x0096, 0.1°C)
                c_temps = []
                for r in regs[19:23]:
                    if 0 <= r <= 1000:
                        c_temps.append(round(r * 0.1, 1))
                if not c_temps:
                    c_temps = [env_temp]
                avg_temp = round(sum(c_temps) / len(c_temps), 1)

                # Reg 23 (0x0097): Arus Busbar (Signed int16, 0.01A)
                bus_curr = round(((regs[23] - 65536) if regs[23] > 32767 else regs[23]) * 0.01, 2) if len(regs) > 23 else 0.0

                # Reg 24 (0x0098): Remaining Capacity % (SOC Riil Baterai, 0-100%)
                soc_pct = int(regs[24]) if len(regs) > 24 and 0 <= regs[24] <= 100 else 68

                # Reg 25 (0x0099): Battery Health (SOH Riil Baterai, 0-100%)
                soh_pct = int(regs[25]) if len(regs) > 25 and 0 <= regs[25] <= 100 else 98

                # Reg 26 (0x009A): Cycle count
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
                    "busbar_current": bus_curr,
                    "soc": soc_pct,
                    "soh": soh_pct,
                    "env_temp_c": env_temp,
                    "temperatures": c_temps,
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
                if isinstance(e, (serial.SerialException, OSError)):
                    try:
                        if self.ser:
                            self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
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
            # Jika semua pack tidak merespon, reset serial agar connect() memeriksa ulang port pada cycle berikutnya
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None
            return None

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        pack_count = len(packs)

        total_current = round(sum(p["pack_current"] for p in packs.values()), 2)
        total_busbar_current = round(sum(p.get("busbar_current", 0.0) for p in packs.values()), 2)
        total_power = sum(p["pack_power_watt"] for p in packs.values())
        avg_pack_v = round(sum(p["pack_voltage"] for p in packs.values()) / pack_count, 2)
        avg_busbar_v = round(sum(p["busbar_voltage"] for p in packs.values()) / pack_count, 2)
        avg_soc = int(round(sum(p["soc"] for p in packs.values()) / pack_count))
        avg_soh = int(round(sum(p.get("soh", 100) for p in packs.values()) / pack_count))
        avg_env_temp = round(sum(p.get("env_temp_c", 28.0) for p in packs.values()) / pack_count, 1)
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
            "connected_slaves": sorted([int(k) for k in packs.keys() if str(k).isdigit()]),
            "connected_slave_ids": ", ".join(str(s) for s in sorted([int(k) for k in packs.keys() if str(k).isdigit()])),
            "packs": packs,
            # Top-level aggregated fields (compatible with existing consumers)
            "pack_voltage": avg_pack_v,
            "busbar_voltage": avg_busbar_v,
            "pack_current": total_current,
            "busbar_current": total_busbar_current,
            "pack_power_watt": total_power,
            "soc": avg_soc,
            "soh": avg_soh,
            "env_temp_c": avg_env_temp,
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


CONFIG_FILE = "/home/ares/Documents/myserver/data/bms_config.json"

def load_bms_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Error loading {CONFIG_FILE}: {e}")
    return {}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ZTE FB100B3 Multi-BMS RS485 Modbus Reader")
    parser.add_argument("--port", type=str, default="/dev/ttyUSB0", help="Serial port (misal /dev/ttyUSB0)")
    parser.add_argument("--slaves", type=str, default="2,5", help="Comma-separated Modbus Slave Addresses (default 2,5)")
    parser.add_argument("--loop", action="store_true", help="Jalankan sebagai service polling berkala")
    parser.add_argument("--interval", type=float, default=2.5, help="Interval polling dalam detik")
    args = parser.parse_args()

    cfg = load_bms_config()
    slaves = None
    if cfg.get("system", {}).get("slave_ids"):
        try:
            slaves = [int(s.strip()) for s in str(cfg["system"]["slave_ids"]).split(",") if s.strip().isdigit()]
        except Exception:
            pass

    if not slaves:
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
        # 1. Probing pack baterai yang aktif saat startup
        logger.info("Memindai pack baterai yang tersambung di RS485 (probe slave 1-8)...")
        detected = reader.scan_online_slaves(list(range(1, 9)))
        if detected:
            logger.info(f"Otomatis mendeteksi pack tersambung: Slave {detected} ({len(detected)} pack, {len(detected)*100}Ah)")
            reader.slaves = detected
            try:
                cur_cfg = load_bms_config()
                if "system" not in cur_cfg:
                    cur_cfg["system"] = {}
                cur_cfg["system"]["slave_ids"] = ", ".join(str(s) for s in detected)
                cur_cfg["system"]["pack_count"] = len(detected)
                cur_cfg["system"]["nominal_capacity_ah"] = len(detected) * 100
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur_cfg, f, indent=2)
                logger.info(f"Default pack berhasil di-update dari pack tersambung: Slave {detected}")
            except Exception as e:
                logger.warning(f"Gagal update bms_config.json: {e}")
        else:
            logger.warning(f"Scan awal tidak mendeteksi pack baru, menggunakan default/konfigurasi: {reader.slaves}")

        logger.info(f"Memulai loop pembacaan BMS ZTE FB100B3 (Slaves: {reader.slaves}, Interval: {args.interval}s)...")
        last_scan_ts = time.time()
        while True:
            try:
                # Dynamic config update
                cur_cfg = load_bms_config()
                if cur_cfg.get("system", {}).get("slave_ids"):
                    try:
                        cfg_slaves = [int(s.strip()) for s in str(cur_cfg["system"]["slave_ids"]).split(",") if s.strip().isdigit()]
                        if cfg_slaves and cfg_slaves != reader.slaves:
                            logger.info(f"Memperbarui Slave IDs dari konfigurasi: {cfg_slaves}")
                            reader.slaves = cfg_slaves
                    except Exception:
                        pass
                poll_interval = float(cur_cfg.get("system", {}).get("polling_interval_sec", args.interval))

                data = reader.read_all_telemetry()

                # Periodic rescan (tiap 10 menit, atau jika data None selama > 45 detik)
                now_ts = time.time()
                should_rescan = (now_ts - last_scan_ts > 600.0) or (data is None and (now_ts - last_scan_ts > 45.0))
                if should_rescan:
                    last_scan_ts = now_ts
                    auto_slaves = reader.scan_online_slaves(list(range(1, 9)))
                    if auto_slaves and auto_slaves != reader.slaves:
                        logger.info(f"Perubahan pack tersambung terdeteksi! Memperbarui slave: {reader.slaves} -> {auto_slaves}")
                        reader.slaves = auto_slaves
                        try:
                            cur_cfg = load_bms_config()
                            if "system" not in cur_cfg:
                                cur_cfg["system"] = {}
                            cur_cfg["system"]["slave_ids"] = ", ".join(str(s) for s in auto_slaves)
                            cur_cfg["system"]["pack_count"] = len(auto_slaves)
                            cur_cfg["system"]["nominal_capacity_ah"] = len(auto_slaves) * 100
                            with open(CONFIG_FILE, "w") as f:
                                json.dump(cur_cfg, f, indent=2)
                        except Exception as ce:
                            logger.warning(f"Gagal update bms_config.json: {ce}")
                if data:
                    # Check alarms against config
                    alarms = []
                    cp = cur_cfg.get("cell_protection", {})
                    tp = cur_cfg.get("thermal_protection", {})
                    max_c = data.get("cell_max_v", 0.0)
                    min_c = data.get("cell_min_v", 0.0)
                    delta_c = data.get("cell_delta_mv", 0.0)
                    temp_c = data.get("avg_cell_temp_c", 25.0)

                    if max_c >= cp.get("ovp_v", 3.65):
                        alarms.append(f"CRITICAL: Sel {data.get('cell_max_idx', 1)} mencapai OVP ({max_c}V)")
                    elif max_c >= cp.get("ov_alarm_v", 3.55):
                        alarms.append(f"WARNING: Sel {data.get('cell_max_idx', 1)} tinggi ({max_c}V)")

                    if min_c <= cp.get("uvp_v", 2.50):
                        alarms.append(f"CRITICAL: Sel {data.get('cell_min_idx', 1)} mencapai UVP ({min_c}V)")
                    elif min_c <= cp.get("uv_alarm_v", 2.80):
                        alarms.append(f"WARNING: Sel {data.get('cell_min_idx', 1)} lemah ({min_c}V)")

                    if delta_c >= cp.get("balance_delta_mv", 30) * 2:
                        alarms.append(f"WARNING: Deviasi sel tinggi (Δ {delta_c} mV)")

                    if temp_c >= tp.get("high_temp_charge_cutoff_c", 55):
                        alarms.append(f"CRITICAL: Suhu sel sangat panas ({temp_c}°C)")
                    elif temp_c >= tp.get("high_temp_charge_alarm_c", 48):
                        alarms.append(f"WARNING: Suhu sel hangat ({temp_c}°C)")

                    data["alarms"] = alarms
                    reader.last_data = data
                    reader.save_state_file()
                    p_info = " | ".join([f"P{p['slave_id']}:{p['pack_voltage']}V/{p['pack_current']}A/{p['soc']}%" for p in data["packs"].values()])
                    alarm_info = f" | Alarms: {len(alarms)}" if alarms else ""
                    logger.info(f"Packs ({data['pack_count']}): {p_info} | Tot: {data['pack_current']}A ({data['pack_power_watt']}W) | SOC: {data['soc']}% | Δ {data['cell_delta_mv']}mV | {data['avg_cell_temp_c']}°C{alarm_info}")
                else:
                    logger.warning("BMS timeout / tidak ada respon slave...")
            except Exception as e:
                logger.error(f"Error loop: {e}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
