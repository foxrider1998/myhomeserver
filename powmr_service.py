#!/usr/bin/env python3
"""PowMr Hybrid Inverter (Voltronic/Axpert Protocol) Reader & Grafana Server.

Membaca data telemetry real-time dari Inverter PowMr melalui serial RS232 (DB9 port).
Menyediakan:
1. Live Dashboard di Terminal (ANSI Colors).
2. Web Dashboard Grafana-Mode & Riwayat Grafik di browser (https://westpro.my.id).
3. Hermes Agent Management UI & Live AI Console.
4. REST API (/api/state, /api/history, /api/hermes/status).
5. Database SQLite lokal untuk penyimpanan riwayat telemetry.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import http.server
import json
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time
import shutil
import urllib.error
import urllib.parse
import urllib.request

def _load_env_file():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env_file()


try:
    import psutil
except ImportError:
    psutil = None

try:
    import serial
except ImportError:
    print("[ERROR] Modul 'pyserial' belum terpasang. Jalankan: pip install pyserial", file=sys.stderr)
    sys.exit(1)

from history_db import HistoryDatabase
from login_template import HTML_LOGIN_TEMPLATE
import webhook_monitor as wm
import hermes_briefing as hb
import android_leases as al
import mailer
import password_reset as pr
import webmail as wm_mail
import teams_service as ts
import stock_service as ss
from stock_page import PAGE_HTML as SAHAM_PAGE_HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("powmr")

# ==============================================================================
# Voltronic / Axpert Protocol Helpers
# ==============================================================================

def voltronic_crc(cmd: str) -> bytes:
    """Menghitung CRC16-CCITT (XMODEM) untuk protokol Voltronic/Axpert."""
    crc = 0
    for c in cmd:
        byte = ord(c)
        for _ in range(8):
            bit = (byte >> 7) & 1
            crc_bit = (crc >> 15) & 1
            crc = (crc << 1) & 0xFFFF
            if crc_bit ^ bit:
                crc ^= 0x1021
            byte = (byte << 1) & 0xFF
    crc_high = (crc >> 8) & 0xFF
    crc_low = crc & 0xFF
    if crc_high in (0x0A, 0x0D, 0x28):
        crc_high += 1
    if crc_low in (0x0A, 0x0D, 0x28):
        crc_low += 1
    return bytes([crc_high, crc_low])


def build_command(cmd_str: str) -> bytes:
    return cmd_str.encode("ascii") + voltronic_crc(cmd_str) + b"\r"


MODE_NAMES = {
    "P": "Power On",
    "S": "Standby",
    "L": "Line Mode (Grid Bypass)",
    "B": "Battery Mode (Inverter)",
    "F": "Fault Mode",
    "H": "Power Saving",
    "D": "Shutdown",
}


class PowMrInverter:
    def __init__(self, port: str = "/dev/ttyS0", baudrate: int = 2400, timeout: float = 1.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.ratings: dict = {}
        self.last_state: dict = {}
        self.is_connected = False
        self.error_count = 0
        self.query_count = 0

    def connect(self) -> bool:
        with self.lock:
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.timeout,
                )
                self.is_connected = True
                logger.info("Berhasil membuka serial port %s (%d bps)", self.port, self.baudrate)
                return True
            except Exception as e:
                self.is_connected = False
                logger.error("Gagal membuka serial port %s: %s", self.port, e)
                return False

    def close(self):
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.is_connected = False

    def send_query(self, cmd_str: str, max_bytes: int = 140) -> bytes | None:
        with self.lock:
            if not self.ser or not self.ser.is_open:
                return None
            try:
                self.ser.reset_input_buffer()
                pkt = build_command(cmd_str)
                self.ser.write(pkt)
                time.sleep(0.15)
                raw = self.ser.read_until(b"\r", size=max_bytes)
                if not raw or not raw.startswith(b"("):
                    return None
                return raw
            except Exception as e:
                logger.warning("Serial error saat query %s: %s", cmd_str, e)
                return None

    def read_ratings(self) -> dict:
        raw = self.send_query("QPIRI")
        if not raw:
            return {}
        try:
            tokens = raw[1:-3].decode("latin1").strip().split()
            if len(tokens) >= 8:
                self.ratings = {
                    "grid_rated_voltage": float(tokens[0]),
                    "grid_rated_current": float(tokens[1]),
                    "ac_output_rated_voltage": float(tokens[2]),
                    "ac_output_rated_frequency": float(tokens[3]),
                    "ac_output_rated_current": float(tokens[4]),
                    "ac_output_rated_apparent_power": int(tokens[5]),
                    "ac_output_rated_active_power": int(tokens[6]),
                    "battery_rated_voltage": float(tokens[7]),
                }
                return self.ratings
        except Exception as e:
            logger.debug("Error parsing QPIRI: %s", e)
        return {}

    def read_mode(self) -> str:
        raw = self.send_query("QMOD")
        if not raw or len(raw) < 2:
            return "Unknown"
        try:
            code = chr(raw[1])
            return MODE_NAMES.get(code, f"Mode {code}")
        except Exception:
            return "Unknown"

    def read_telemetry(self) -> dict | None:
        raw = self.send_query("QPIGS")
        self.query_count += 1
        if not raw:
            self.error_count += 1
            return None

        try:
            body = raw[1:-3].decode("latin1").strip()
            tokens = body.split()
            if len(tokens) < 16:
                self.error_count += 1
                return None

            grid_v = float(tokens[0])
            grid_hz = float(tokens[1])
            ac_out_v = float(tokens[2])
            ac_out_hz = float(tokens[3])
            ac_out_va = int(tokens[4])
            ac_out_w = int(tokens[5])
            load_pct = int(tokens[6])
            bus_v = int(tokens[7])
            batt_v = float(tokens[8])
            batt_charge_a = int(tokens[9])
            batt_soc = int(tokens[10])
            temp_c = int(tokens[11])
            pv_a = float(tokens[12])
            pv_v = float(tokens[13])
            batt_v_scc = float(tokens[14])
            batt_discharge_a = int(tokens[15])
            
            status_bits = tokens[16] if len(tokens) > 16 else ""
            pv_w = int(tokens[19]) if len(tokens) > 19 else int(round(pv_v * pv_a))

            mode_name = self.read_mode()

            data = {
                "timestamp": datetime.now().isoformat(),
                "mode": mode_name,
                "grid_voltage": grid_v,
                "grid_frequency": grid_hz,
                "ac_output_voltage": ac_out_v,
                "ac_output_frequency": ac_out_hz,
                "ac_output_va": ac_out_va,
                "ac_output_watt": ac_out_w,
                "load_percent": load_pct,
                "bus_voltage": bus_v,
                "battery_voltage": batt_v,
                "battery_charging_current": batt_charge_a,
                "battery_discharge_current": batt_discharge_a,
                "battery_net_current": batt_charge_a - batt_discharge_a,
                "battery_power_watt": int(round(batt_v * (batt_charge_a - batt_discharge_a))),
                "soc": batt_soc,
                "temperature": temp_c,
                "pv_voltage": pv_v,
                "pv_current": pv_a,
                "pv_power_watt": pv_w,
                "status_bits": status_bits,
                "ratings": self.ratings,
            }
            self.last_state = data
            return data
        except Exception as e:
            self.error_count += 1
            logger.error("Error parsing QPIGS: %s", e)
            return None

    def read_settings(self) -> dict:
        raw = self.send_query("QPIRI")
        if not raw:
            return {}
        try:
            body = raw[1:-3].decode("latin1").strip()
            tokens = body.split()
            if len(tokens) >= 23:
                out_prio_map = {"0": "USB (Utility First)", "1": "SUB (Solar First)", "2": "SBU (Solar-Battery-Utility)"}
                chg_prio_map = {"0": "Utility First", "1": "Solar First", "2": "Solar + Utility", "3": "Only Solar"}
                batt_type_map = {"0": "AGM", "1": "Flooded", "2": "User Defined (USE)"}
                input_range_map = {"0": "Appliance (90-280V)", "1": "UPS (170-280V)"}

                out_code = "SUB"
                if int(tokens[16]) == 0: out_code = "USB"
                elif int(tokens[16]) == 2: out_code = "SBU"

                settings = {
                    "grid_rated_voltage": float(tokens[0]),
                    "grid_rated_current": float(tokens[1]),
                    "ac_output_rated_voltage": float(tokens[2]),
                    "ac_output_rated_frequency": float(tokens[3]),
                    "ac_output_rated_current": float(tokens[4]),
                    "ac_output_rated_apparent_power": int(tokens[5]),
                    "ac_output_rated_active_power": int(tokens[6]),
                    "battery_rated_voltage": float(tokens[7]),
                    "back_to_grid_voltage": float(tokens[8]),
                    "cutoff_voltage": float(tokens[9]),
                    "bulk_voltage": float(tokens[10]),
                    "float_voltage": float(tokens[11]),
                    "battery_type": int(tokens[12]),
                    "battery_type_name": batt_type_map.get(tokens[12], f"Type {tokens[12]}"),
                    "max_ac_charge_current": int(tokens[13]),
                    "max_charge_current": int(tokens[14]),
                    "input_voltage_range": int(tokens[15]),
                    "input_voltage_range_name": input_range_map.get(tokens[15], f"Range {tokens[15]}"),
                    "output_priority": int(tokens[16]),
                    "output_priority_code": out_code,
                    "output_priority_name": out_prio_map.get(tokens[16], f"Mode {tokens[16]}"),
                    "charger_priority": int(tokens[17]),
                    "charger_priority_name": chg_prio_map.get(tokens[17], f"Mode {tokens[17]}"),
                    "back_to_battery_voltage": float(tokens[22]) if len(tokens) > 22 else 52.0
                }
                return settings
        except Exception as e:
            logger.warning("Error parsing detailed QPIRI settings: %s", e)
        return {}

    def execute_command(self, cmd_str: str) -> tuple[bool, str]:
        with self.lock:
            if not self.ser or not self.ser.is_open:
                return False, "Serial port inverter tidak terhubung"
            try:
                self.ser.reset_input_buffer()
                pkt = build_command(cmd_str)
                self.ser.write(pkt)
                time.sleep(0.2)
                raw = self.ser.read_until(b"\r", size=40)
                if not raw:
                    return False, "Inverter tidak merespon"
                if b"ACK" in raw:
                    return True, "Berhasil diterapkan ke Inverter (ACK)"
                elif b"NAK" in raw:
                    return False, "Perintah ditolak oleh Inverter (NAK) - Parameter mungkin di luar rentang"
                return False, f"Respon tidak dikenal: {raw.decode('latin1', errors='replace')}"
            except Exception as e:
                return False, f"Serial error: {e}"

    def update_setting(self, param: str, value: Any) -> tuple[bool, str]:
        """Validasi dan kirim perintah setelan yang sesuai ke Inverter PowMr."""
        param = str(param).strip().lower()
        if param == "output_priority":
            val = str(value).upper()
            mapping = {"USB": "POP00", "0": "POP00", "SUB": "POP01", "1": "POP01", "SBU": "POP02", "2": "POP02"}
            cmd = mapping.get(val)
            if not cmd:
                return False, "Nilai output priority tidak valid (Pilihan: SUB, SBU, USB)"
            return self.execute_command(cmd)

        elif param == "charger_priority":
            val = str(value).upper()
            mapping = {"0": "PCP00", "UTILITY_FIRST": "PCP00", "1": "PCP01", "SOLAR_FIRST": "PCP01", "2": "PCP02", "SOLAR_UTILITY": "PCP02", "3": "PCP03", "ONLY_SOLAR": "PCP03"}
            cmd = mapping.get(val)
            if not cmd:
                return False, "Nilai charger priority tidak valid"
            return self.execute_command(cmd)

        elif param == "max_charge_current":
            try:
                val = int(value)
            except Exception:
                return False, "Arus pengisian harus berupa angka integer"
            if val < 10 or val > 120:
                return False, "Arus pengisian total harus antara 10A dan 120A"
            cmd = f"MNCHGC{val:03d}"
            return self.execute_command(cmd)

        elif param == "max_ac_charge_current":
            try:
                val = int(value)
            except Exception:
                return False, "Arus pengisian AC PLN harus berupa angka integer"
            if val < 2 or val > 80:
                return False, "Arus pengisian AC PLN harus antara 2A dan 80A"
            cmd = f"MUCHGC{val:03d}"
            return self.execute_command(cmd)

        elif param == "bulk_voltage":
            try:
                val = round(float(value), 1)
            except Exception:
                return False, "Tegangan bulk harus berupa angka"
            if val < 48.0 or val > 56.4:
                return False, "Tegangan bulk harus antara 48.0V dan 56.4V"
            cmd = f"PCVV{val:.1f}"
            return self.execute_command(cmd)

        elif param == "float_voltage":
            try:
                val = round(float(value), 1)
            except Exception:
                return False, "Tegangan float harus berupa angka"
            if val < 48.0 or val > 55.0:
                return False, "Tegangan float harus antara 48.0V dan 55.0V"
            cmd = f"PBFT{val:.1f}"
            return self.execute_command(cmd)

        elif param == "cutoff_voltage":
            try:
                val = round(float(value), 1)
            except Exception:
                return False, "Tegangan cut-off harus berupa angka"
            if val < 40.0 or val > 48.0:
                return False, "Tegangan cut-off harus antara 40.0V dan 48.0V"
            cmd = f"PSDV{val:.1f}"
            return self.execute_command(cmd)

        elif param == "back_to_grid_voltage":
            try:
                val = round(float(value), 1)
            except Exception:
                return False, "Tegangan back-to-grid harus berupa angka"
            if val < 44.0 or val > 51.0:
                return False, "Tegangan back-to-grid harus antara 44.0V dan 51.0V"
            cmd = f"PBCV{val:.1f}"
            return self.execute_command(cmd)

        elif param == "back_to_battery_voltage":
            try:
                val = round(float(value), 1)
            except Exception:
                return False, "Tegangan back-to-battery harus berupa angka"
            if val < 48.0 or val > 55.0:
                return False, "Tegangan back-to-battery harus antara 48.0V dan 55.0V"
            cmd = f"PBDV{val:.1f}"
            return self.execute_command(cmd)

        return False, f"Parameter '{param}' tidak dikenali"

    def apply_preset(self, preset_name: str) -> dict:
        """Menerapkan konfigurasi preset teruji untuk hardware pengguna."""
        presets = {
            "safe_15s": {
                "name": "Aman LiFePO4 15S Bekas (Rekomendasi)",
                "bulk_voltage": 53.8,
                "float_voltage": 52.5,
                "cutoff_voltage": 44.0,
                "back_to_grid_voltage": 47.0,
                "back_to_battery_voltage": 52.0,
                "max_charge_current": 40,
                "max_ac_charge_current": 10,
                "output_priority": "SBU",
                "charger_priority": "SOLAR_FIRST",
            },
            "performance_15s": {
                "name": "Performa LiFePO4 15S (Kapasitas Maksimal)",
                "bulk_voltage": 54.4,
                "float_voltage": 53.2,
                "cutoff_voltage": 43.5,
                "back_to_grid_voltage": 46.5,
                "back_to_battery_voltage": 52.5,
                "max_charge_current": 50,
                "max_ac_charge_current": 15,
                "output_priority": "SBU",
                "charger_priority": "SOLAR_FIRST",
            },
            "sub_standby": {
                "name": "Bypass SUB / UPS Mode (PLN Utama Siang/Malam)",
                "bulk_voltage": 53.8,
                "float_voltage": 52.5,
                "cutoff_voltage": 44.0,
                "back_to_grid_voltage": 48.0,
                "back_to_battery_voltage": 52.0,
                "max_charge_current": 30,
                "max_ac_charge_current": 20,
                "output_priority": "SUB",
                "charger_priority": "SOLAR_UTILITY",
            }
        }
        target = presets.get(preset_name)
        if not target:
            return {"success": False, "error": f"Preset '{preset_name}' tidak ditemukan"}
        
        results = {}
        all_ok = True
        for k, v in target.items():
            if k == "name":
                continue
            ok, msg = self.update_setting(k, v)
            results[k] = {"success": ok, "message": msg, "target": v}
            if not ok:
                all_ok = False
        return {"success": all_ok, "preset": target["name"], "details": results}


# ==============================================================================
# BMS Configuration & Battery Protection Thresholds
# ==============================================================================

DEFAULT_BMS_CONFIG = {
    "system": {
        "battery_model": "ZTE Birubatt ZXDC48 FB100B3 15S LiFePO4",
        "pack_count": 2,
        "nominal_capacity_ah": 200,
        "cells_per_pack": 15,
        "slave_ids": "2, 5",
        "polling_interval_sec": 3
    },
    "cell_protection": {
        "ovp_v": 3.65,
        "ov_alarm_v": 3.55,
        "balance_start_v": 3.40,
        "balance_delta_mv": 30,
        "uv_alarm_v": 2.80,
        "uvp_v": 2.50
    },
    "pack_protection": {
        "pack_ovp_v": 54.75,
        "pack_float_v": 52.50,
        "pack_reconnect_v": 48.00,
        "pack_uvp_v": 44.00
    },
    "current_limits": {
        "max_charge_a": 40,
        "max_discharge_a": 100
    },
    "thermal_protection": {
        "high_temp_charge_alarm_c": 48,
        "high_temp_charge_cutoff_c": 55,
        "high_temp_discharge_cutoff_c": 65,
        "low_temp_charge_cutoff_c": 0
    },
    "notifications": {
        "notify_on_cell_imbalance": True,
        "notify_on_temp_warning": True,
        "notify_channel": "telegram"
    }
}

def get_default_bms_config() -> dict:
    import copy
    return copy.deepcopy(DEFAULT_BMS_CONFIG)


# ==============================================================================
# Grafana-Mode Web App Dashboard Template
# ==============================================================================

HTML_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Westpro Solar & Home Server — Grafana Mode</title>
  <link rel="manifest" href="/manifest.json">
  <meta name="theme-color" content="#111217">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Westpro Solar">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
  <!-- Chart.js & Marked.js CDN -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked@14.0.0/marked.min.js"></script>
  <style>
    :root {
      --gf-bg: #09090b;
      --gf-canvas: #0f0f13;
      --gf-panel: #131317;
      --gf-panel-hover: #18181e;
      --gf-border: #222228;
      --gf-border-highlight: #32323d;
      --gf-orange: #f59e0b;
      --gf-blue: #38bdf8;
      --gf-green: #10b981;
      --gf-purple: #a855f7;
      --gf-red: #ef4444;
      --gf-text: #f4f4f5;
      --gf-text-muted: #9ca3af;
      --gf-text-dim: #64748b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background-color: var(--gf-bg);
      color: var(--gf-text);
      min-height: 100vh;
      overflow-x: hidden;
    }

    /* TOP NAVBAR */
    .navbar {
      background: var(--gf-canvas);
      border-bottom: 1px solid var(--gf-border);
      padding: 0.75rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .brand-logo {
      width: 32px;
      height: 32px;
      background: #18181d;
      border: 1px solid #33333d;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .brand-title {
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: -0.2px;
      color: #fff;
    }
    .brand-sub {
      font-size: 0.75rem;
      color: var(--gf-text-muted);
      font-family: 'JetBrains Mono', monospace;
    }

    /* SUB NAVBAR (HORIZONTAL FEATURE TABS) */
    .sub-nav-bar {
      background: #0f0f13;
      border-bottom: 1px solid var(--gf-border);
      padding: 0.4rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: flex-start;
      overflow-x: auto;
      scrollbar-width: none;
      position: sticky;
      top: 55px;
      z-index: 99;
    }
    .sub-nav-bar::-webkit-scrollbar {
      display: none;
    }
    .nav-tabs {
      display: flex;
      gap: 0.35rem;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .tab-btn {
      background: transparent;
      border: 1px solid transparent;
      color: #9ca3af;
      padding: 0.38rem 0.85rem;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 500;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      text-decoration: none;
      transition: all 0.15s ease;
      white-space: nowrap;
    }
    .tab-btn:hover {
      color: #f4f4f5;
      background: rgba(255, 255, 255, 0.05);
    }
    .tab-btn.active {
      color: #f4f4f5;
      background: #202026;
      border-color: #353540;
      font-weight: 600;
    }

    .nav-right {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .status-pill {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
      padding: 0.3rem 0.65rem;
      border-radius: 6px;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      background: #18181d;
      border: 1px solid #27272f;
      color: #34d399;
      font-weight: 500;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #10b981;
    }

    /* MAIN CONTAINER */
    .container {
      max-width: 1440px;
      margin: 0 auto;
      padding: 1.5rem;
    }

    /* TIME RANGE SELECTOR BAR */
    .controls-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      flex-wrap: wrap;
      gap: 0.75rem;
    }
    .range-selector {
      display: flex;
      gap: 0.35rem;
      background: var(--gf-panel);
      padding: 0.25rem;
      border-radius: 6px;
      border: 1px solid var(--gf-border);
    }
    .range-btn {
      background: transparent;
      border: none;
      color: var(--gf-text-muted);
      padding: 0.3rem 0.65rem;
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 4px;
      cursor: pointer;
      font-family: 'JetBrains Mono', monospace;
    }
    .range-btn:hover { color: #fff; }
    .range-btn.active { background: #32363e; color: #38bdf8; }

    /* GRAFANA GRID */
    .grafana-grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .panel {
      background: var(--gf-panel);
      border: 1px solid var(--gf-border);
      border-radius: 8px;
      padding: 1.1rem;
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
      transition: border-color 0.15s;
    }
    .panel:hover {
      border-color: var(--gf-border-highlight);
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--gf-text-muted);
      margin-bottom: 0.75rem;
    }
    .col-3 { grid-column: span 3; }
    .col-4 { grid-column: span 4; }
    .col-5 { grid-column: span 5; }
    .col-6 { grid-column: span 6; }
    .col-7 { grid-column: span 7; }
    .col-8 { grid-column: span 8; }
    .col-12 { grid-column: span 12; }

    @media (max-width: 1024px) {
      .col-3 { grid-column: span 6; }
      .col-4 { grid-column: span 6; }
      .col-5 { grid-column: span 12; }
      .col-6 { grid-column: span 12; }
      .col-7 { grid-column: span 12; }
      .col-8 { grid-column: span 12; }
    }
    @media (max-width: 640px) {
      .col-3 { grid-column: span 12; }
      .col-4 { grid-column: span 12; }
    }

    /* STAT CARD (GRAFANA SINGELSTAT) */
    .stat-val {
      font-size: 2.25rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      line-height: 1.1;
      margin-bottom: 0.5rem;
    }
    .stat-sub {
      display: flex;
      justify-content: space-between;
      font-size: 0.8rem;
      color: var(--gf-text-muted);
      font-family: 'JetBrains Mono', monospace;
      padding-top: 0.5rem;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }

    /* SOLAR HERO & WEATHER MODERN STYLING */
    .solar-hero-card {
      background: linear-gradient(165deg, #131722 0%, #0d0f17 100%);
      border: 1px solid rgba(56, 189, 248, 0.25);
      border-radius: 12px;
      padding: 1.25rem;
      position: relative;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.08);
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      gap: 0.65rem;
    }
    .weather-metrics-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.45rem;
      margin: 0.35rem 0;
    }
    @media (max-width: 480px) {
      .weather-metrics-grid {
        grid-template-columns: repeat(2, 1fr);
      }
    }
    .weather-m-box {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.07);
      border-radius: 8px;
      padding: 0.4rem 0.55rem;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: all 0.2s;
    }
    .weather-m-box:hover {
      background: rgba(255, 255, 255, 0.055);
      border-color: rgba(56, 189, 248, 0.25);
    }
    .weather-m-head {
      display: flex;
      align-items: center;
      gap: 0.35rem;
      margin-bottom: 0.15rem;
    }
    .weather-m-icon {
      font-size: 0.85rem;
    }
    .weather-m-lbl {
      font-size: 0.62rem;
      color: #94a3b8;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .weather-m-val {
      font-size: 0.95rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      line-height: 1.2;
    }
    .weather-m-sub {
      font-size: 0.58rem;
      color: #64748b;
      margin-top: 0.1rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* HOURLY STRIP */
    .weather-hourly-strip {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 0.35rem;
      overflow-x: auto;
      padding-bottom: 0.15rem;
    }
    .hourly-box {
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 6px;
      padding: 0.35rem 0.2rem;
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.12rem;
      transition: all 0.2s;
    }
    .hourly-box:hover {
      border-color: rgba(255, 255, 255, 0.15);
      background: rgba(255, 255, 255, 0.04);
    }
    .hourly-box.now {
      border-color: rgba(56, 189, 248, 0.4);
      background: rgba(56, 189, 248, 0.08);
    }
    .hourly-time {
      font-size: 0.62rem;
      font-weight: 700;
      color: #94a3b8;
    }
    .hourly-icon {
      font-size: 1.05rem;
    }
    .hourly-temp {
      font-size: 0.72rem;
      font-weight: 700;
      color: #f1f5f9;
    }
    .hourly-pop {
      font-size: 0.58rem;
      color: #38bdf8;
      font-weight: 600;
    }
    .solar-hero-card::before {
      content: '';
      position: absolute;
      top: -50px;
      right: -50px;
      width: 170px;
      height: 170px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(245, 158, 11, 0.22) 0%, rgba(245, 158, 11, 0) 70%);
      pointer-events: none;
    }
    .hero-weather-main {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin: 0.75rem 0;
    }
    .hero-temp-val {
      font-size: 2.85rem;
      font-weight: 800;
      font-family: 'Outfit', sans-serif;
      line-height: 1;
      color: #fff;
      display: flex;
      align-items: baseline;
      gap: 0.2rem;
    }
    .mt-fold { margin-top: 0.5rem; background: #14161b; border: 1px solid #27272a; border-radius: 8px; padding: 0.4rem 0.75rem; }
    .mt-fold > summary, .mt-fold-panel > summary { cursor: pointer; font-size: 0.78rem; font-weight: 600; color: #a1a1aa; list-style: none; display: flex; align-items: center; gap: 0.5rem; }
    .mt-fold > summary::-webkit-details-marker, .mt-fold-panel > summary::-webkit-details-marker { display: none; }
    .mt-fold > summary::before, .mt-fold-panel > summary::before { content: '▸'; color: #52525b; transition: transform 0.15s; }
    .mt-fold[open] > summary::before, .mt-fold-panel[open] > summary::before { transform: rotate(90deg); }
    .mt-fold > summary:hover, .mt-fold-panel > summary:hover { color: #e4e4e7; }
    .mt-fold-panel > summary { font-size: 0.85rem; color: #e4e4e7; }
    .mt-field { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.68rem; color: #71717a; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600; }
    .mt-field .chat-input { text-transform: none; letter-spacing: 0; font-size: 0.78rem; font-weight: 500; }
    .ws-search { background: #0f1013; border: 1px solid #2d2d35; color: #e4e4e7; border-radius: 8px; padding: 0.3rem 0.65rem; font-size: 0.75rem; width: 210px; outline: none; }
    .ws-search:focus { border-color: #38bdf8; }
    .spectrum-tab { border-radius: 999px !important; padding: 0.28rem 0.8rem !important; font-size: 0.73rem !important; }
    .spectrum-tab.active { background: rgba(56,189,248,0.16) !important; color: #38bdf8 !important; border-color: rgba(56,189,248,0.45) !important; }
    .cfg-row { display: flex; justify-content: space-between; align-items: center; gap: 0.6rem; color: #a1a1aa; }
    .cfg-row b { font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; }
    .quick-chip { border-radius: 999px !important; }
    .lease-bar { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; background: linear-gradient(135deg, rgba(56,189,248,0.07), rgba(16,185,129,0.05)); border: 1px solid rgba(56,189,248,0.2); border-radius: 12px; padding: 0.8rem 1rem; margin-bottom: 1rem; }
    .lease-chip { font-size: 0.62rem; font-weight: 700; padding: 0.1rem 0.45rem; border-radius: 4px; letter-spacing: 0.04em; }
    .lease-mine { background: rgba(52,211,153,0.16); color: #34d399; border: 1px solid rgba(52,211,153,0.35); }
    .lease-free { background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); }
    .lease-busy { background: rgba(245,158,11,0.14); color: #fbbf24; border: 1px solid rgba(245,158,11,0.35); }
    .wm-wrap { display: grid; grid-template-columns: 240px 370px minmax(0, 1fr); gap: 0.85rem; height: calc(100vh - 215px); min-height: 620px; }
    @media (max-width: 1100px) { .wm-wrap { grid-template-columns: 1fr; height: auto; } .wm-list-pane, .wm-read-pane { min-height: 440px; } }
    .wm-side, .wm-list-pane, .wm-read-pane { background: #111216; border: 1px solid #23242c; border-radius: 14px; overflow: hidden; display: flex; flex-direction: column; min-height: 0; box-shadow: 0 4px 20px rgba(0,0,0,0.25); }
    .wm-side { padding: 0.8rem; overflow-y: auto; }
    .wm-folder { width: 100%; text-align: left; background: transparent; color: #94a3b8; border: 1px solid transparent; border-radius: 9px; padding: 0.55rem 0.75rem; font-size: 0.82rem; font-weight: 600; cursor: pointer; display: flex; justify-content: space-between; align-items: center; transition: all 0.15s; }
    .wm-folder:hover { background: rgba(255,255,255,0.04); color: #f1f5f9; }
    .wm-folder.active { background: linear-gradient(135deg, rgba(37,99,235,0.18), rgba(56,189,248,0.1)); color: #38bdf8; border-color: rgba(56,189,248,0.35); }
    .wm-side-title { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b; font-weight: 700; margin: 1rem 0 0.4rem; padding-left: 0.2rem; }
    .wm-addr { display: flex; align-items: center; gap: 0.35rem; padding: 0.42rem 0.6rem; border-radius: 8px; cursor: pointer; font-size: 0.76rem; color: #cbd5e1; border: 1px solid transparent; transition: all 0.15s; }
    .wm-addr:hover { background: rgba(255,255,255,0.04); color: #fff; }
    .wm-addr.active { background: rgba(167,139,250,0.14); border-color: rgba(167,139,250,0.35); color: #c4b5fd; font-weight: 600; }
    .wm-addr .wm-a-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'JetBrains Mono', monospace; }
    .wm-badge { background: #2563eb; color: #fff; font-size: 0.62rem; font-weight: 700; border-radius: 10px; padding: 0.05rem 0.45rem; }
    .wm-mini { background: transparent; border: none; color: #64748b; cursor: pointer; font-size: 0.8rem; padding: 0.1rem 0.3rem; border-radius: 4px; transition: all 0.15s; }
    .wm-mini:hover { color: #f1f5f9; background: rgba(255,255,255,0.08); }
    .wm-toolbar { display: flex; gap: 0.55rem; align-items: center; padding: 0.7rem 0.85rem; border-bottom: 1px solid #202129; background: rgba(0,0,0,0.15); }
    .wm-toolbar input[type=search] { flex: 1; min-width: 0; background: #0c0d10; border: 1px solid #2a2b34; color: #f1f5f9; border-radius: 8px; padding: 0.42rem 0.75rem; font-size: 0.8rem; outline: none; }
    .wm-toolbar input[type=search]:focus { border-color: #38bdf8; }
    #wmList { overflow-y: auto; flex: 1; }
    .wm-item { padding: 0.75rem 0.9rem; border-bottom: 1px solid #1c1d24; cursor: pointer; transition: all 0.15s; position: relative; }
    .wm-item:hover { background: rgba(255,255,255,0.035); }
    .wm-item.active { background: rgba(56,189,248,0.09); border-left: 3px solid #38bdf8; padding-left: calc(0.9rem - 3px); }
    .wm-item.unread::before { content: ""; position: absolute; left: 4px; top: 50%; transform: translateY(-50%); width: 6px; height: 6px; border-radius: 50%; background: #38bdf8; box-shadow: 0 0 8px #38bdf8; }
    .wm-item .wm-from { font-size: 0.8rem; color: #e2e8f0; display: flex; justify-content: space-between; gap: 0.5rem; align-items: center; }
    .wm-item.unread .wm-from, .wm-item.unread .wm-subj { font-weight: 700; color: #fff; }
    .wm-item .wm-subj { font-size: 0.8rem; color: #cbd5e1; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; }
    .wm-item .wm-prev { font-size: 0.72rem; color: #64748b; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.4; }
    .wm-chip { display: inline-block; font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; font-weight: 700; padding: 0.1rem 0.45rem; border-radius: 6px; background: rgba(52,211,153,0.15); color: #34d399; border: 1px solid rgba(52,211,153,0.35); cursor: pointer; margin: 0 0.3rem 0.2rem 0; }
    .wm-read-pane { padding: 0; background: #0f1014; }
    .wm-read-head { padding: 1.1rem 1.3rem; border-bottom: 1px solid #202129; background: #131418; }
    .wm-read-body { flex: 1; overflow-y: auto; padding: 1.1rem 1.3rem; font-size: 0.88rem; color: #cbd5e1; line-height: 1.6; min-height: 0; }
    .wm-empty { margin: auto; color: #64748b; font-size: 0.88rem; text-align: center; padding: 2.5rem; }
    .wm-link { display: flex; gap: 0.4rem; align-items: center; font-size: 0.74rem; margin: 0.25rem 0; padding: 0.2rem 0; }
    .wm-link a { color: #38bdf8; word-break: break-all; text-decoration: none; }
    .wm-link.verify a { color: #fbbf24; font-weight: 600; }
    .wm-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 99998; align-items: center; justify-content: center; padding: 1rem; backdrop-filter: blur(5px); }
    .wm-modal-card { width: 100%; max-width: 620px; background: #16171d; border: 1px solid #2d2e38; border-radius: 16px; padding: 1.3rem 1.4rem; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }
    .wm-modal-card input, .wm-modal-card select, .wm-modal-card textarea { width: 100%; background: #0b0c0f; border: 1px solid #2d2e38; color: #f1f5f9; border-radius: 9px; padding: 0.55rem 0.75rem; font-size: 0.84rem; margin-bottom: 0.65rem; font-family: inherit; outline: none; }
    .wm-modal-card input:focus, .wm-modal-card select:focus, .wm-modal-card textarea:focus { border-color: #38bdf8; }
    .hb-panel { background: linear-gradient(135deg, rgba(56,189,248,0.07) 0%, rgba(167,139,250,0.05) 55%, rgba(16,185,129,0.05) 100%); border: 1px solid rgba(56,189,248,0.2); border-radius: 14px; padding: 1rem 1.15rem 0.8rem; margin-bottom: 1.15rem; box-shadow: 0 8px 28px rgba(0,0,0,0.25); }
    .hb-head { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }
    .hb-avatar { width: 38px; height: 38px; border-radius: 12px; background: linear-gradient(135deg, #38bdf8, #a78bfa); color: #0b0c0e; font-weight: 800; font-size: 1.05rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; box-shadow: 0 4px 14px rgba(56,189,248,0.35); }
    .hb-btn { background: rgba(255,255,255,0.04); color: #a1a1aa; border: 1px solid #353540; border-radius: 8px; padding: 0.32rem 0.7rem; font-size: 0.72rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
    .hb-btn:hover:not(:disabled) { color: #f4f4f5; border-color: #52525b; }
    .hb-btn-primary { background: rgba(56,189,248,0.12); color: #38bdf8; border-color: rgba(56,189,248,0.35); }
    .hb-chips { display: flex; gap: 0.4rem; flex-wrap: wrap; margin: 0.75rem 0 0.7rem; }
    .hb-chips:empty { display: none; }
    .hb-chip-stat { display: inline-flex; align-items: center; gap: 0.35rem; background: rgba(0,0,0,0.28); border: 1px solid #2a2a33; border-radius: 999px; padding: 0.22rem 0.7rem; font-size: 0.72rem; color: #cbd5e1; }
    .hb-chip-stat b { color: #f4f4f5; font-weight: 700; }
    .hb-chip-stat.warn { border-color: rgba(245,158,11,0.4); color: #fbbf24; }
    .hb-chip-stat.ok { border-color: rgba(52,211,153,0.35); color: #34d399; }
    .hb-foot { margin-top: 0.7rem; font-size: 0.64rem; color: #52525b; }
    .hb-body { font-size: 0.83rem; color: #cbd5e1; line-height: 1.55; display: flex; flex-wrap: wrap; gap: 0.8rem; align-items: stretch; }
    .hb-body > .hb-section { flex: 1 1 290px; min-width: 0; background: rgba(255,255,255,0.025); border: 1px solid #27272a; border-left: 3px solid #38bdf8; border-radius: 10px; padding: 0.7rem 0.95rem; margin: 0; }
    .hb-body > .hb-section:first-child { flex: 1 1 100%; border-left-color: #a78bfa; background: linear-gradient(135deg, rgba(167,139,250,0.11), rgba(56,189,248,0.05)); }
    .hb-body > .hb-section:first-child .hb-title { font-size: 1.08rem; color: #f4f4f5; }
    .hb-body > .hb-section:nth-child(3) { border-left-color: #34d399; }
    .hb-body > .hb-section:nth-child(4) { border-left-color: #f59e0b; }
    .hb-body > .hb-section:nth-child(n+5) { border-left-color: #64748b; }
    .hb-collapsed .hb-body > .hb-section:not(:first-child) { display: none; }
    .hb-collapsed .hb-foot { display: none; }
    .hb-body .hb-title { font-size: 0.8rem; font-weight: 700; color: #38bdf8; margin: 0 0 0.3rem; letter-spacing: 0.01em; }
    .hb-body p { margin: 0.2rem 0; }
    .hb-body .hb-joke { border-left: 3px solid #f59e0b; background: rgba(245,158,11,0.08); margin: 0.5rem 0 0.1rem; padding: 0.45rem 0.8rem; border-radius: 0 8px 8px 0; color: #fcd34d; font-style: italic; }
    .hb-body .hb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.6rem; }
    .hb-body .hb-card { background: #18181b; border: 1px solid #27272a; border-radius: 8px; padding: 0.55rem 0.75rem; }
    .hb-body .hb-chip { display: inline-block; font-size: 0.65rem; padding: 0.05rem 0.45rem; border-radius: 10px; background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.25); }
    .hb-body .hb-ok { color: #34d399; }
    .hb-body .hb-warn { color: #fbbf24; }
    .hb-body .hb-muted { color: #71717a; }
    .hb-body .hb-list { margin: 0.2rem 0 0 1.1rem; padding: 0; }
    .hb-body .hb-list li { margin: 0.18rem 0; }
    .hb-body table { border-collapse: collapse; width: 100%; font-size: 0.76rem; }
    .hb-body th, .hb-body td { border-bottom: 1px solid #27272a; padding: 0.3rem 0.5rem; text-align: left; }
    /* Workspace QA: KPI, progres, agenda, task */
    .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.6rem; margin-bottom: 0.85rem; }
    .kpi-card { background: #15161a; border: 1px solid #27272a; border-top: 3px solid var(--kpi, #38bdf8); border-radius: 10px; padding: 0.55rem 0.7rem; }
    .kpi-card .kpi-label { font-size: 0.66rem; color: #71717a; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
    .kpi-card .kpi-value { font-size: 1.55rem; font-weight: 800; color: var(--kpi, #38bdf8); line-height: 1.15; margin-top: 0.1rem; font-family: 'JetBrains Mono', monospace; }
    @media (max-width: 720px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
    .agenda-item { background: #15161a; border: 1px solid #27272a; border-radius: 10px; padding: 0.5rem 0.7rem; display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; transition: border-color 0.15s; }
    .agenda-item:hover { border-color: #3f3f46; }
    .agenda-item.today { border-color: rgba(52,211,153,0.4); background: linear-gradient(135deg, rgba(52,211,153,0.07), #15161a); }
    .agenda-chip { font-size: 0.6rem; font-weight: 700; letter-spacing: 0.04em; padding: 0.08rem 0.4rem; border-radius: 4px; background: rgba(52,211,153,0.18); color: #34d399; margin-right: 0.35rem; }
    #spectrumTaskTableBody tr { transition: background 0.12s; }
    #spectrumTaskTableBody tr:hover { background: rgba(56,189,248,0.05); }
    .forecast-pill-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.5rem;
      margin-top: 0.85rem;
      padding-top: 0.75rem;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }
    .forecast-pill {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 8px;
      padding: 0.45rem 0.35rem;
      text-align: center;
      transition: transform 0.2s, background 0.2s;
    }
    .forecast-pill:hover {
      transform: translateY(-2px);
      background: rgba(255, 255, 255, 0.06);
    }
    .weather-tab-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 0.62rem;
      font-weight: 600;
      padding: 0.18rem 0.45rem;
      border-radius: 4px;
      cursor: pointer;
      transition: all 0.2s ease;
      font-family: inherit;
    }
    .weather-tab-btn:hover {
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.08);
    }
    .weather-tab-btn.active {
      background: #0284c7;
      color: #fff;
      font-weight: 700;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
    }
    .forecast-scroll-strip {
      display: flex;
      gap: 0.45rem;
      overflow-x: auto;
      padding-bottom: 0.35rem;
      scrollbar-width: thin;
      scrollbar-color: rgba(56, 189, 248, 0.3) transparent;
    }
    .forecast-scroll-strip::-webkit-scrollbar {
      height: 4px;
    }
    .forecast-scroll-strip::-webkit-scrollbar-thumb {
      background: rgba(56, 189, 248, 0.3);
      border-radius: 2px;
    }
    .forecast-strip-item {
      min-width: 88px;
      flex-shrink: 0;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 8px;
      padding: 0.4rem 0.35rem;
      text-align: center;
      transition: transform 0.2s, background 0.2s;
    }
    .forecast-strip-item:hover {
      transform: translateY(-2px);
      background: rgba(255, 255, 255, 0.06);
    }
    .forecast-strip-item.next-week {
      border-color: rgba(168, 85, 247, 0.35);
      background: rgba(168, 85, 247, 0.05);
    }

    /* SMART ADVISORY CONTAINER */
    .smart-advisory-box {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.08) 0%, rgba(16, 185, 129, 0.05) 100%);
      border: 1px solid rgba(245, 158, 11, 0.35);
      border-radius: 10px;
      padding: 0.85rem 1.15rem;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      box-shadow: 0 4px 20px rgba(245, 158, 11, 0.06);
    }

    /* ENERGY FLOW ANIMATED DIAGRAM */
    .energy-flow-container {
      background: #0f1117;
      border: 1px solid var(--gf-border);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      display: flex;
      align-items: center;
      justify-content: space-around;
      position: relative;
    }
    .energy-node {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.3rem;
      z-index: 2;
    }
    .energy-circle {
      width: 52px;
      height: 52px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.35rem;
      box-shadow: 0 0 15px rgba(0,0,0,0.4);
      transition: transform 0.2s;
    }
    .energy-circle:hover { transform: scale(1.08); }

    /* BATTERY CELL VISUAL */
    .batt-cell-grid {
      display: flex;
      gap: 3px;
      width: 100%;
      height: 10px;
      background: rgba(0,0,0,0.4);
      padding: 2px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.1);
    }
    .batt-cell-segment {
      flex: 1;
      height: 100%;
      border-radius: 2px;
      background: #27272a;
      transition: background 0.3s;
    }
    .batt-cell-segment.active { background: #10b981; }
    .batt-cell-segment.warn { background: #f59e0b; }
    .batt-cell-segment.danger { background: #ef4444; }

    @keyframes pulseArrow {
      0%, 100% { opacity: 0.3; transform: translateX(0); }
      50% { opacity: 1; transform: translateX(3px); }
    }

    /* PLTS CYCLE ANALYSIS SWITCHER & WEEKLY CAPACITY BOX */
    .cycle-mode-pill-group {
      display: inline-flex;
      background: #141720;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 8px;
      padding: 3px;
      gap: 3px;
    }
    .cycle-mode-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 0.74rem;
      font-weight: 600;
      padding: 0.35rem 0.7rem;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 0.35rem;
    }
    .cycle-mode-btn:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }
    .cycle-mode-btn.active {
      background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%);
      color: #fff;
      box-shadow: 0 2px 8px rgba(2, 132, 199, 0.4);
    }
    .weekly-cap-box {
      background: linear-gradient(145deg, #131722 0%, #0e121a 100%);
      border: 1px solid rgba(56, 189, 248, 0.3);
      border-radius: 12px;
      padding: 1.15rem;
      position: relative;
      overflow: hidden;
      box-shadow: 0 10px 25px rgba(0,0,0,0.4);
    }
    .weekly-cap-box::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 2px;
      background: linear-gradient(90deg, #38bdf8 0%, #10b981 50%, #f59e0b 100%);
    }
    .cap-metric-card {
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 8px;
      padding: 0.75rem;
      transition: transform 0.2s, border-color 0.2s;
    }
    .cap-metric-card:hover {
      transform: translateY(-2px);
      border-color: rgba(56, 189, 248, 0.35);
    }

    /* CHART WRAPPER */
    .chart-container {
      position: relative;
      height: 250px;
      width: 100%;
    }

    /* HERMES AGENT PANEL */
    .hermes-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.25rem 0.6rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-family: 'JetBrains Mono', monospace;
      font-weight: 600;
    }
    .badge-ok { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .badge-warn { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
    .badge-blue { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }

    .event-feed {
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
      max-height: 400px;
      overflow-y: auto;
    }
    .event-item {
      background: rgba(255, 255, 255, 0.02);
      border-left: 3px solid #38bdf8;
      border-radius: 0 4px 4px 0;
      padding: 0.65rem 0.85rem;
      font-size: 0.825rem;
    }
    .event-item.solar { border-left-color: #f59e0b; }
    .event-item.appium { border-left-color: #10b981; }
    .event-item.mail { border-left-color: #a855f7; }
    .event-time { font-size: 0.7rem; color: var(--gf-text-dim); font-family: 'JetBrains Mono', monospace; }

    /* CHAT BOX & BUBBLES */
    .chat-bubble-container {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      max-height: 420px;
      min-height: 280px;
      overflow-y: auto;
      padding: 0.85rem;
      background: #0d0d10;
      border: 1px solid var(--gf-border);
      border-radius: 8px;
      scrollbar-width: thin;
    }
    .chat-bubble {
      max-width: 85%;
      padding: 0.7rem 0.95rem;
      border-radius: 8px;
      font-size: 0.84rem;
      line-height: 1.5;
      word-wrap: break-word;
    }
    .chat-bubble-user {
      align-self: flex-end;
      background: #27272e;
      color: #f4f4f5;
      border: 1px solid #383844;
      border-bottom-right-radius: 2px;
    }
    .chat-bubble-ai {
      align-self: flex-start;
      background: #15151a;
      color: #e4e4e7;
      border: 1px solid #262630;
      border-bottom-left-radius: 2px;
    }
    .chat-bubble-ai p { margin-bottom: 0.45rem; }
    .chat-bubble-ai p:last-child { margin-bottom: 0; }
    .chat-bubble-ai pre {
      background: #09090c;
      border: 1px solid #272730;
      border-radius: 6px;
      padding: 0.6rem 0.8rem;
      overflow-x: auto;
      margin: 0.45rem 0;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8rem;
      color: #38bdf8;
    }
    .chat-bubble-ai code {
      font-family: 'JetBrains Mono', monospace;
      background: rgba(255, 255, 255, 0.06);
      color: #e4e4e7;
      padding: 0.15rem 0.35rem;
      border-radius: 4px;
      font-size: 0.8rem;
    }
    .chat-bubble-meta {
      font-size: 0.68rem;
      color: #71717a;
      margin-top: 0.3rem;
      display: flex;
      justify-content: flex-end;
      gap: 0.4rem;
    }
    .quick-chip {
      background: #18181d;
      border: 1px solid #272730;
      color: #a1a1aa;
      border-radius: 6px;
      padding: 0.3rem 0.65rem;
      font-size: 0.74rem;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s;
    }
    .quick-chip:hover {
      background: #22222a;
      color: #f4f4f5;
      border-color: #383844;
    }
    .spectrum-tab {
      background: #18181d;
      border: 1px solid #272730;
      color: #a1a1aa;
      border-radius: 5px;
      padding: 0.25rem 0.65rem;
      font-size: 0.72rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s;
    }
    .spectrum-tab:hover {
      background: #22222a;
      color: #f4f4f5;
      border-color: #383844;
    }
    .spectrum-tab.active {
      background: #27272a;
      color: #38bdf8;
      border-color: #0284c7;
      font-weight: 600;
    }
    .chat-box {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.65rem;
    }
    .chat-input {
      flex: 1;
      background: #111115;
      border: 1px solid var(--gf-border);
      color: #fff;
      padding: 0.55rem 0.85rem;
      border-radius: 6px;
      font-family: inherit;
      font-size: 0.84rem;
    }
    .chat-input:focus { outline: none; border-color: #3b82f6; }
    .chat-btn {
      background: #2563eb;
      color: #fff;
      border: none;
      padding: 0.55rem 1.15rem;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.825rem;
      transition: background 0.15s;
    }
    .chat-btn:hover { background: #1d4ed8; }

    /* DATA TABLE */
    .raw-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
      font-family: 'JetBrains Mono', monospace;
    }
    .raw-table th {
      background: #20242c;
      color: var(--gf-text-muted);
      text-align: left;
      padding: 0.6rem 0.75rem;
      font-weight: 600;
    }
    .raw-table td {
      padding: 0.5rem 0.75rem;
      border-bottom: 1px solid var(--gf-border);
    }
    .raw-table tr:hover { background: rgba(255, 255, 255, 0.02); }

    /* VIEW SECTIONS */
    .view-section { display: none; }
    .view-section.active { display: block; }

    /* WEBHOOK MONITOR & FULLSCREEN STYLES */
    .wh-filter-btn {
      background: #141720;
      color: #94a3b8;
      border: 1px solid #272a31;
      border-radius: 4px;
      padding: 0.25rem 0.55rem;
      font-size: 0.72rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s;
    }
    .wh-filter-btn:hover { color: #f1f5f9; border-color: #3b82f6; }
    .wh-filter-btn.active {
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      border-color: #38bdf8;
    }
    .wh-badge-method {
      display: inline-block;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.68rem;
      font-weight: 800;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      letter-spacing: 0.05em;
    }
    .badge-post { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-get { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }
    .badge-put { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-delete { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge-patch { background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); }
    .badge-other { background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.4); }

    .wh-req-item {
      background: #141720;
      border: 1px solid #272a31;
      border-radius: 6px;
      padding: 0.55rem 0.75rem;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      transition: all 0.15s;
    }
    .wh-req-item:hover {
      border-color: #38bdf8;
      background: #191e2b;
    }
    .wh-req-item.active {
      border-color: #38bdf8;
      background: rgba(56, 189, 248, 0.12);
      box-shadow: 0 0 10px rgba(56, 189, 248, 0.15);
    }

    .insp-tab-btn {
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      color: #94a3b8;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.4rem 0.75rem;
      cursor: pointer;
      transition: all 0.15s;
    }
    .insp-tab-btn:hover { color: #f1f5f9; }
    .insp-tab-btn.active {
      color: #38bdf8;
      border-bottom-color: #38bdf8;
    }

    .fs-btn {
      background: #272a31;
      color: #f1f5f9;
      border: 1px solid #3f3f46;
      border-radius: 6px;
      padding: 0.35rem 0.75rem;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }
    .fs-btn:hover { background: #3b82f6; color: #fff; border-color: #3b82f6; }

    /* ==============================================================================
       TOPOLOGY SYNOPTIC HUB & INVERTER SETTINGS STYLES
       ============================================================================== */
    .topo-grid-flow {
      display: grid;
      grid-template-columns: 1fr 140px 1.4fr 140px 1fr;
      grid-template-rows: auto auto;
      gap: 0.85rem 0.5rem;
      align-items: center;
      position: relative;
    }
    @media (max-width: 1080px) {
      .topo-grid-flow {
        display: flex;
        flex-direction: column;
        gap: 0.85rem;
      }
      .topo-bridge {
        width: 100% !important;
        margin: 0.25rem 0 !important;
      }
      .topo-inverter-hub {
        max-width: 100% !important;
      }
    }
    .topo-card {
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      transition: all 0.2s;
    }
    .topo-card:hover {
      border-color: rgba(255, 255, 255, 0.2);
      background: rgba(255, 255, 255, 0.04);
    }
    .card-pv { border-left: 3px solid #f59e0b; }
    .card-batt { border-left: 3px solid #10b981; }
    .card-grid { border-right: 3px solid #a855f7; }
    .card-load { border-right: 3px solid #38bdf8; }

    .topo-card-header {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      margin-bottom: 0.4rem;
    }
    .topo-icon-circle {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.15rem;
    }
    .icon-pv { background: rgba(245, 158, 11, 0.15); }
    .icon-batt { background: rgba(16, 185, 129, 0.15); }
    .icon-grid { background: rgba(168, 85, 247, 0.15); }
    .icon-load { background: rgba(56, 189, 248, 0.15); }

    .topo-card-title {
      font-size: 0.8rem;
      font-weight: 700;
    }
    .topo-card-sub {
      font-size: 0.65rem;
      color: #94a3b8;
    }
    .topo-val-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin: 0.35rem 0;
    }
    .topo-big-val {
      font-size: 1.55rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
    }
    .topo-pill-ratio {
      font-size: 0.65rem;
      font-weight: 700;
      background: rgba(245, 158, 11, 0.12);
      color: #fbbf24;
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
    }
    .topo-telemetry-row {
      display: flex;
      justify-content: space-between;
      font-size: 0.68rem;
      color: #94a3b8;
      border-top: 1px dashed rgba(255, 255, 255, 0.06);
      padding-top: 0.35rem;
      margin-top: 0.25rem;
    }

    /* CENTER INVERTER HUB */
    .topo-inverter-hub {
      background: #14161f;
      border: 1px solid #2d3748;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
      border-radius: 14px;
      padding: 1.15rem;
      text-align: center;
      width: 100%;
      position: relative;
    }
    .hub-specs-box {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.5rem;
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 8px;
      padding: 0.6rem;
      text-align: left;
    }
    .hub-spec-item {
      display: flex;
      flex-direction: column;
    }
    .hub-spec-label {
      font-size: 0.62rem;
      color: #94a3b8;
    }
    .hub-spec-val {
      font-size: 0.88rem;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
    }

    /* ANIMATED FLOW BRIDGES & PILLS */
    .topo-bridge {
      position: relative;
      height: 48px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .topo-conduit {
      position: absolute;
      top: 50%;
      left: 0;
      right: 0;
      height: 3px;
      background: rgba(255, 255, 255, 0.08);
      transform: translateY(-50%);
      z-index: 1;
      border-radius: 2px;
    }
    .flow-pill {
      position: relative;
      z-index: 3;
      padding: 0.22rem 0.65rem;
      border-radius: 12px;
      font-size: 0.68rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 0.35rem;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.6);
      background: #181b24;
      border: 1px solid rgba(255, 255, 255, 0.12);
      white-space: nowrap;
    }
    .flow-pill-pv { color: #fbbf24; border-color: rgba(245, 158, 11, 0.4); }
    .flow-pill-batt { color: #34d399; border-color: rgba(16, 185, 129, 0.4); }
    .flow-pill-grid { color: #c084fc; border-color: rgba(168, 85, 247, 0.4); }
    .flow-pill-load { color: #38bdf8; border-color: rgba(56, 189, 248, 0.4); }

    /* PARTICLES ANIMATION */
    .energy-particles {
      position: absolute;
      top: 50%;
      left: 0;
      right: 0;
      height: 5px;
      transform: translateY(-50%);
      background-size: 28px 5px;
      opacity: 0;
      transition: opacity 0.3s;
      z-index: 2;
    }
    .flow-pv-active {
      opacity: 0.95;
      background-image: radial-gradient(circle, #f59e0b 50%, transparent 80%);
      animation: particleSlideR 0.85s linear infinite;
    }
    .flow-grid-active {
      opacity: 0.95;
      background-image: radial-gradient(circle, #a855f7 50%, transparent 80%);
      animation: particleSlideL 1.0s linear infinite;
    }
    .flow-load-active {
      opacity: 0.95;
      background-image: radial-gradient(circle, #38bdf8 50%, transparent 80%);
      animation: particleSlideR 0.9s linear infinite;
    }
    .flow-batt-charge {
      opacity: 0.95;
      background-image: radial-gradient(circle, #10b981 50%, transparent 80%);
      animation: particleSlideL 0.85s linear infinite;
    }
    .flow-batt-discharge {
      opacity: 0.95;
      background-image: radial-gradient(circle, #f97316 50%, transparent 80%);
      animation: particleSlideR 0.9s linear infinite;
    }

    @keyframes particleSlideR {
      0% { background-position: 0 0; }
      100% { background-position: 56px 0; }
    }
    @keyframes particleSlideL {
      0% { background-position: 56px 0; }
      100% { background-position: 0 0; }
    }

    /* INVERTER SETTINGS PAGE */
    .set-grid {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 1.15rem;
    }
    @media (max-width: 960px) {
      .set-grid { grid-template-columns: 1fr; }
    }
    .form-group {
      margin-bottom: 0.95rem;
    }
    .form-label {
      font-size: 0.74rem;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 0.35rem;
      display: flex;
      justify-content: space-between;
    }
    .form-control-input, .form-control-select {
      width: 100%;
      background: #0d0f14;
      border: 1px solid #272833;
      border-radius: 6px;
      color: #f1f5f9;
      padding: 0.5rem 0.75rem;
      font-size: 0.82rem;
      font-family: inherit;
      box-sizing: border-box;
      outline: none;
      transition: border-color 0.2s;
    }
    .form-control-input:focus, .form-control-select:focus {
      border-color: #38bdf8;
    }
    .form-hint {
      font-size: 0.65rem;
      color: #64748b;
      margin-top: 0.25rem;
      line-height: 1.4;
    }
  </style>
</head>
<body>
  <!-- NAVBAR -->
  <nav class="navbar">
    <div class="brand">
      <div class="brand-logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
      </div>
      <div>
        <div class="brand-title">WESTPRO HOME SERVER</div>
        <div class="brand-sub">ThinkCentre M90n &bull; PowMr 6.2kW &bull; Grafana Mode</div>
      </div>
    </div>

    <div class="nav-right" style="display: flex; align-items: center; gap: 0.65rem;">
      <div class="status-pill">
        <div class="status-dot"></div>
        <span id="navModeText">ONLINE</span>
      </div>
      <button onclick="openPermissionModal()" id="navPermissionBtn" style="background: #18181f; border: 1px solid #262630; border-radius: 6px; padding: 0.25rem 0.65rem; font-size: 0.75rem; color: #cbd5e1; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;" title="Pengaturan Izin Mikrofon, Audio Speaker & Notifikasi">
        <span id="navPermissionSummary">Izin Akses</span>
      </button>
      <div id="userProfilePill" onclick="navigateToPage('profile')" style="display: flex; align-items: center; gap: 0.5rem; background: #141418; border: 1px solid var(--gf-border); border-radius: 6px; padding: 0.3rem 0.75rem; font-size: 0.75rem; cursor: pointer;" title="Buka Profil Akun &amp; SPEctrum">
        <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #10b981;"></span>
        <span id="navUserName" style="color: #f1f5f9; font-weight: 600;">__USER_NAME__</span>
        <span id="navUserRole" class="hermes-badge badge-warn" style="font-size: 0.65rem; padding: 0.1rem 0.35rem;">__USER_ROLE__</span>
      </div>
      <button onclick="handleLogout()" style="background: transparent; color: #a1a1aa; border: 1px solid var(--gf-border); border-radius: 6px; padding: 0.35rem 0.65rem; font-size: 0.75rem; font-weight: 500; cursor: pointer; transition: all 0.15s;" title="Keluar dari akun">
        Keluar
      </button>
    </div>
  </nav>

  <!-- FEATURE NAVIGATION BAR (FULL WIDTH TABS) -->
  <div class="sub-nav-bar">
    <div class="nav-tabs">
      <a class="tab-btn active" id="nav-solar" href="/solar" onclick="event.preventDefault(); navigateToPage('solar')">Solar Live</a>
      <a class="tab-btn" id="nav-history" href="/history" onclick="event.preventDefault(); navigateToPage('history')">Riwayat Data</a>
      <a class="tab-btn" id="nav-inverter-settings" href="/inverter-settings" onclick="event.preventDefault(); navigateToPage('inverter-settings')">Setelan Inverter &amp; BMS</a>
      <a class="tab-btn" id="nav-hermes" href="/hermes" onclick="event.preventDefault(); navigateToPage('hermes')">Workspace QA <span id="navHermesBadge" style="display:none; background:#0284c7; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-meeting" href="/meeting" onclick="event.preventDefault(); navigateToPage('meeting')">Notulen Rapat</a>
      <a class="tab-btn" id="nav-android" href="/android" onclick="event.preventDefault(); navigateToPage('android')">Android Remote</a>
      <a class="tab-btn" id="nav-otp" href="/otp" onclick="event.preventDefault(); navigateToPage('otp')">Kotak OTP <span id="navOtpBadge" style="display:none; background:#d97706; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-webhook" href="/webhook" onclick="event.preventDefault(); navigateToPage('webhook')">Webhook Monitor <span id="navWebhookBadge" style="display:none; background:#0284c7; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-webmail" href="/webmail" onclick="event.preventDefault(); navigateToPage('webmail')">Mail</a>
      <a class="tab-btn" id="nav-teams" href="/teams" onclick="event.preventDefault(); navigateToPage('teams')">Teams <span id="navTeamsBadge" style="display:none; background:#7c3aed; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-saham" href="/saham" onclick="event.preventDefault(); navigateToPage('saham')">Saham</a>
      <a class="tab-btn" id="nav-profile" href="/profile" onclick="event.preventDefault(); navigateToPage('profile')">Profil &amp; SPEctrum</a>
      <a class="tab-btn" id="nav-system" href="/system" onclick="event.preventDefault(); navigateToPage('system')">Sistem</a>
      <a class="tab-btn" id="nav-9router" href="/9router" onclick="event.preventDefault(); navigateToPage('9router')">9Router</a>
    </div>
  </div>

  <!-- ==================== STICKY PERMISSION WARNING BANNER ==================== -->
  <div id="stickyPermissionBanner" style="display: none; background: #131418; border-bottom: 1px solid #27272e; padding: 0.6rem 1.5rem; align-items: center; justify-content: space-between; gap: 1rem; position: sticky; top: 96px; z-index: 98;">
    <div style="display: flex; align-items: center; gap: 0.65rem;">
      <div style="color: #f59e0b; font-size: 0.95rem;">●</div>
      <div>
        <div style="font-weight: 600; font-size: 0.825rem; color: #f4f4f5;">Akses notifikasi, mikrofon, dan audio laptop belum lengkap</div>
        <div style="font-size: 0.725rem; color: #9ca3af;">Diperlukan untuk notifikasi peringatan daya surya dan fitur notulen suara rapat (suara Anda & audio lawan bicara).</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0;">
      <button onclick="requestAllPermissions()" style="background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.4rem 0.85rem; font-size: 0.775rem; font-weight: 600; cursor: pointer;">
        Aktifkan Izin
      </button>
      <button onclick="openPermissionModal()" style="background: transparent; color: #d4d4d8; border: 1px solid #3f3f46; border-radius: 6px; padding: 0.4rem 0.7rem; font-size: 0.775rem; cursor: pointer;">
        Detail
      </button>
      <button onclick="dismissPermissionBanner()" style="background: transparent; color: #71717a; border: none; font-size: 1.1rem; cursor: pointer; padding: 0.2rem 0.5rem;" title="Tutup sementara">
        ✕
      </button>
    </div>
  </div>

  <div class="container">
    <!-- CONTROLS BAR -->
    <div class="controls-bar">
      <div style="font-size: 0.825rem; color: var(--gf-text-muted);">
        Domain Publik: <a href="https://westpro.my.id" style="color: #38bdf8; text-decoration: none;" target="_blank">https://westpro.my.id</a> &bull; Serial Port: <span style="color:#34d399;">/dev/ttyS0 (2400 8N1)</span>
      </div>
      <div class="range-selector">
        <button class="range-btn active" onclick="setRange('15m', this)">15m</button>
        <button class="range-btn" onclick="setRange('1h', this)">1h</button>
        <button class="range-btn" onclick="setRange('6h', this)">6h</button>
        <button class="range-btn" onclick="setRange('24h', this)">24h</button>
      </div>
    </div>

    <!-- ==================== HALAMAN 1: SOLAR & INVERTER LIVE ==================== -->
    <div id="page-solar" class="view-section active">
      <div class="page-header" style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 0.5rem;">
        <div>
          <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
            <span>☀️</span> Sistem Pembangkit Listrik Tenaga Surya PowMr 6.2kW
          </h1>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
            Pemantauan real-time fotovoltaik, baterai LiFePO4, estimasi cuaca satelit & anjuran hemat energi.
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <span class="hermes-badge badge-ok" id="solarLiveBadge">● LIVE TELEMETRI</span>
          <span style="font-size: 0.72rem; color: var(--gf-text-muted);" id="ovLastUpdateTop">--</span>
        </div>
      </div>

      <div class="grafana-grid">
        <!-- 1. LEFT HERO: WEATHER & 3D SOLAR SCENE (col-4) -->
        <div class="panel col-4 solar-hero-card" style="min-height: 480px;">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem;">
              <div>
                <div style="display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;">
                  <span style="font-size: 0.7rem; color: #38bdf8; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center; gap: 0.25rem;">
                    <span>📍</span> <span id="heroWeatherLocation">Jetak, Kaliagung, Sentolo</span>
                  </span>
                  <span id="heroWeatherProviderBadge" class="hermes-badge badge-blue" style="font-size: 0.6rem; padding: 0.08rem 0.35rem; cursor: pointer;" title="Sumber Resmi BMKG Republik Indonesia. Klik untuk menyegarkan sekarang." onclick="updateSolarWeatherAndAdvisory(true)">BMKG RESMI 🔄</span>
                </div>
                <div style="font-size: 0.85rem; font-weight: 700; color: #f8fafc; margin-top: 0.25rem; display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap;">
                  <span>📅</span> <span id="heroWeatherDateText">Selasa, 13 Oktober 2026</span>
                  <span id="heroWeatherWeekText" class="hermes-badge badge-purple" style="font-size: 0.62rem; padding: 0.08rem 0.35rem;">Minggu ke-42</span>
                </div>
                <div id="heroWeatherDate" style="font-size: 0.72rem; color: #94a3b8; margin-top: 0.1rem; display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap;">
                  <span>Kulon Progo, D.I. Yogyakarta</span>
                  <span>&bull;</span>
                  <span id="heroLiveClock" style="color: #38bdf8; font-family: monospace; font-weight: 600;">--:--:-- WIB</span>
                  <span>&bull;</span>
                  <span id="heroWeatherSyncStatus" style="color: #10b981; font-size: 0.68rem;" title="Data cuaca BMKG tersinkron otomatis setiap 15 menit. Rekomendasi energi dihitung real-time tiap 2 detik.">● Sinkron Otomatis (BMKG 15m)</span>
                </div>
              </div>
              <span id="heroWeatherCondBadge" class="hermes-badge badge-blue">Cerah Berawan</span>
            </div>

            <!-- MAIN TEMP & ICON -->
            <div class="hero-weather-main">
              <div class="hero-temp-val">
                <span id="heroWeatherTemp">24.0</span><span style="font-size: 1.6rem; color: #94a3b8; font-weight: 500;">°C</span>
              </div>
              <div id="heroWeatherIcon" style="font-size: 3.2rem; filter: drop-shadow(0 4px 12px rgba(245,158,11,0.3));">
                ⛅
              </div>
            </div>

            <div style="display: flex; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.85rem;">
              <span style="font-size: 0.7rem; color: #cbd5e1; background: rgba(255,255,255,0.06); padding: 0.2rem 0.45rem; border-radius: 4px;" id="heroWeatherCloud">
                ☁️ Awan: 78%
              </span>
              <span style="font-size: 0.7rem; color: #cbd5e1; background: rgba(255,255,255,0.06); padding: 0.2rem 0.45rem; border-radius: 4px;" id="heroWeatherHumidity">
                💧 Lembab: 78%
              </span>
              <span style="font-size: 0.7rem; color: #fbbf24; background: rgba(245,158,11,0.1); padding: 0.2rem 0.45rem; border-radius: 4px;" id="heroSunTimes">
                🌅 05:17 • 🌇 17:33
              </span>
            </div>
          </div>

          <!-- KOTAK-KOTAK METRIK CUACA (WEATHER METRICS GRID) -->
          <div class="weather-metrics-grid">
            <div class="weather-m-box" title="Estimasi potensi radiasi energi matahari hari ini untuk pembangkitan PLTS">
              <div class="label">☀️ Est. Panen PLTS</div>
              <div class="val" id="wmEstKwh" style="color: #f59e0b;">-- kWh</div>
              <div class="sub" id="wmPeakWindow">Puncak: 10:00 - 14:00</div>
            </div>

            <div class="weather-m-box" title="Indeks radiasi sinar ultraviolet matahari (BMKG / Satelit)">
              <div class="label">🟣 Indeks UV Surya</div>
              <div class="val" id="wmUvIndex" style="color: #c084fc;">UV --</div>
              <div class="sub" id="wmUvLevel">Tingkat Radiasi</div>
            </div>

            <div class="weather-m-box" title="Peluang terjadinya hujan di Desa Kaliagung menurut kalkulasi BMKG">
              <div class="label">🌧️ Peluang Hujan</div>
              <div class="val" id="wmPrecipProb" style="color: #38bdf8;">--%</div>
              <div class="sub" id="wmPrecipDesc">Presipitasi Rendah</div>
            </div>

            <div class="weather-m-box" title="Kecepatan dan hembusan arah angin permukaan BMKG Kaliagung">
              <div class="label">💨 Angin Permukaan</div>
              <div class="val" id="wmWindSpeed" style="color: #34d399;">-- km/h</div>
              <div class="sub" id="wmWindDir">Arah: --</div>
            </div>

            <div class="weather-m-box" title="Sensasi suhu udara yang dirasakan tubuh manusia & titik embun">
              <div class="label">🌡️ Suhu Terasa (Feels)</div>
              <div class="val" id="wmFeelsLike" style="color: #fb923c;">--°C</div>
              <div class="sub" id="wmDewPoint">Titik Embun: --</div>
            </div>

            <div class="weather-m-box" title="Jarak pandang horizontal dan tekanan barometer BMKG">
              <div class="label">👁️ Visibilitas & Tekanan</div>
              <div class="val" id="wmVisibility" style="color: #f1f5f9; font-size: 0.95rem;">--</div>
              <div class="sub" id="wmPressure">Barometer: -- hPa</div>
            </div>
          </div>

          <!-- PRAKIRAAN JAM-JAMAN BMKG (HOURLY FORECAST STRIP) -->
          <div style="margin: 0.15rem 0;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem;">
              <div style="font-size: 0.68rem; color: #71717a; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;">
                Prakiraan Tiap 3 Jam (BMKG Kaliagung)
              </div>
              <div style="font-size: 0.6rem; color: #38bdf8; font-weight: 500;">
                24 Jam Terkini
              </div>
            </div>
            <div class="weather-hourly-strip" id="weatherHourlyStrip">
              <div style="font-size: 0.68rem; color: #94a3b8; padding: 0.4rem;">Memuat prakiraan cuaca jam-jaman...</div>
            </div>
          </div>

          <!-- 3D ISOMETRIC SOLAR PANEL SVG (COMPACT DECORATIVE) -->
          <div style="margin: 0.15rem 0; text-align: center;">
            <svg viewBox="0 0 400 170" width="100%" height="auto" style="max-height: 65px; filter: drop-shadow(0 6px 12px rgba(0,0,0,0.55));">
              <defs>
                <linearGradient id="panelGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#1e3a8a"/>
                  <stop offset="50%" stop-color="#172554"/>
                  <stop offset="100%" stop-color="#0b1329"/>
                </linearGradient>
                <linearGradient id="frameGrad" x1="0%" y1="0%" x2="0%" y2="100%">
                  <stop offset="0%" stop-color="#94a3b8"/>
                  <stop offset="100%" stop-color="#334155"/>
                </linearGradient>
                <linearGradient id="glintGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%" stop-color="rgba(255,255,255,0.01)"/>
                  <stop offset="48%" stop-color="rgba(255,255,255,0.22)"/>
                  <stop offset="52%" stop-color="rgba(255,255,255,0.03)"/>
                  <stop offset="100%" stop-color="rgba(255,255,255,0.01)"/>
                </linearGradient>
              </defs>
              <polygon points="40,155 360,155 390,162 10,162" fill="#0f172a"/>
              <polygon points="10,160 390,160 375,35 25,35" fill="url(#frameGrad)"/>
              <polygon points="14,156 386,156 372,38 28,38" fill="#0b1329"/>
              <polygon points="18,152 382,152 368,41 32,41" fill="url(#panelGrad)"/>
              <!-- Grid Lines -->
              <line x1="22" y1="68" x2="378" y2="68" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="20" y1="96" x2="380" y2="96" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="19" y1="124" x2="381" y2="124" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="75" y1="41" x2="68" y2="152" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="125" y1="41" x2="120" y2="152" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="175" y1="41" x2="175" y2="152" stroke="#38bdf8" stroke-opacity="0.45" stroke-width="1.2"/>
              <line x1="225" y1="41" x2="225" y2="152" stroke="#38bdf8" stroke-opacity="0.45" stroke-width="1.2"/>
              <line x1="275" y1="41" x2="280" y2="152" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <line x1="325" y1="41" x2="332" y2="152" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.2"/>
              <polygon points="18,152 382,152 368,41 32,41" fill="url(#glintGrad)"/>
            </svg>
          </div>

          <!-- MULTI-RANGE WEATHER FORECAST STRIP -->
          <div>
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem; flex-wrap: wrap; gap: 0.25rem;">
              <div id="weatherRangeTitle" style="font-size: 0.68rem; color: #71717a; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;">
                Prakiraan Cuaca 3 Hari
              </div>
              <div style="display: flex; gap: 2px; background: rgba(0,0,0,0.35); padding: 2px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.06);">
                <button type="button" class="weather-tab-btn active" id="btnWeather3d" onclick="switchWeatherForecastRange('3d')">3 Hari</button>
                <button type="button" class="weather-tab-btn" id="btnWeather7d" onclick="switchWeatherForecastRange('7d')">7 Hari</button>
                <button type="button" class="weather-tab-btn" id="btnWeather14d" onclick="switchWeatherForecastRange('14d')">Minggu Depan</button>
                <button type="button" class="weather-tab-btn" id="btnWeatherMonth" onclick="switchWeatherForecastRange('month')">Bulan Ini</button>
              </div>
            </div>
            <div id="heroForecastContainer" style="min-height: 82px;">
              <div class="forecast-pill-grid" id="heroForecastGrid">
                <div class="forecast-pill">
                  <div style="font-size: 0.65rem; color: #a1a1aa;">Hari Ini</div>
                  <div style="font-size: 1.05rem; margin: 2px 0;">🌦️</div>
                  <div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5;">33° / 22°</div>
                </div>
                <div class="forecast-pill">
                  <div style="font-size: 0.65rem; color: #a1a1aa;">Besok</div>
                  <div style="font-size: 1.05rem; margin: 2px 0;">🌧️</div>
                  <div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5;">34° / 20°</div>
                </div>
                <div class="forecast-pill">
                  <div style="font-size: 0.65rem; color: #a1a1aa;">Lusa</div>
                  <div style="font-size: 1.05rem; margin: 2px 0;">⛅</div>
                  <div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5;">34° / 22°</div>
                </div>
              </div>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.35rem; font-size: 0.6rem; color: #64748b;">
              <span id="heroForecastSourceText">BMKG Stasiun Kaliagung</span>
              <a href="javascript:void(0)" onclick="openWeatherForecastModal()" style="color: #38bdf8; text-decoration: none; font-weight: 600; cursor: pointer;">Lihat Detail 14 Hari & Iklim ↗</a>
            </div>
          </div>
        </div>

        <!-- 2. RIGHT SECTION: METRICS, SMART ADVISORY, & FLOW (col-8) -->
        <div class="col-8" style="display: flex; flex-direction: column; gap: 0.85rem;">
          
          <!-- 4 METRICS CARDS -->
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem;">
            <!-- SOLAR PV -->
            <div class="panel" style="margin: 0; padding: 0.85rem;">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.72rem;">Input Solar PV</span>
                <span style="color: var(--gf-orange); font-size: 0.68rem; font-weight: 700;">☀️ PV</span>
              </div>
              <div class="stat-val" id="ovPvWatt" style="color: var(--gf-orange); font-size: 1.85rem; margin-bottom: 0.35rem;">-- W</div>
              <div class="stat-sub" style="font-size: 0.74rem;">
                <span>V: <b id="ovPvVolt" style="color: #fff;">-- V</b></span>
                <span>A: <b id="ovPvCurr" style="color: #fff;">-- A</b></span>
              </div>
            </div>

            <!-- BATTERY -->
            <div class="panel" style="margin: 0; padding: 0.85rem;">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.72rem;">Baterai LiFePO4</span>
                <span id="ovSocBadge" class="hermes-badge badge-ok" style="font-size: 0.65rem; padding: 0.15rem 0.4rem;">SOC --%</span>
              </div>
              <div class="stat-val" id="ovBattVolt" style="color: var(--gf-green); font-size: 1.85rem; margin-bottom: 0.35rem;">-- V</div>
              <!-- 4-Cell Segment Graphic -->
              <div class="batt-cell-grid" id="battCellGrid" style="margin-bottom: 0.4rem;">
                <div class="batt-cell-segment active"></div>
                <div class="batt-cell-segment active"></div>
                <div class="batt-cell-segment active"></div>
                <div class="batt-cell-segment active"></div>
              </div>
              <div class="stat-sub" style="font-size: 0.74rem;">
                <span>Est. Sisa: <b id="ovBattRuntime" style="color: #34d399;">~18.5 Jam</b></span>
                <span>Net: <b id="ovBattNetWatt" style="color: #fff;">-- W</b></span>
              </div>
              <div id="ovBmsDetails" style="display: none; font-size: 0.68rem; color: #a7f3d0; margin-top: 0.35rem; border-top: 1px dashed rgba(16,185,129,0.25); padding-top: 0.25rem;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                  <span><b id="ovBmsTitle">ZTE BMS</b>: <b id="ovBmsDelta" style="color: #6ee7b7;">Δ -- mV</b> • <span id="ovBmsTemp">--°C</span></span>
                  <a href="javascript:void(0)" onclick="openZteBmsModal()" id="ovBmsLink" style="color: #38bdf8; text-decoration: none; font-weight: 600; cursor: pointer;">Detail Sel ↗</a>
                </div>
              </div>
            </div>

            <!-- AC LOAD -->
            <div class="panel" style="margin: 0; padding: 0.85rem;">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.72rem;">Beban AC Rumah</span>
                <span id="ovLoadPctBadge" style="color: var(--gf-blue); font-size: 0.68rem; font-weight: 700;">Load --%</span>
              </div>
              <div class="stat-val" id="ovLoadWatt" style="color: var(--gf-blue); font-size: 1.85rem; margin-bottom: 0.35rem;">-- W</div>
              <div style="width: 100%; height: 4px; background: rgba(255,255,255,0.08); border-radius: 2px; overflow: hidden; margin-bottom: 0.4rem;">
                <div id="ovLoadBarMini" style="height: 100%; width: 0%; background: #38bdf8; transition: width 0.4s ease;"></div>
              </div>
              <div class="stat-sub" style="font-size: 0.74rem;">
                <span>Teg: <b id="ovAcOutVolt" style="color: #fff;">-- V</b></span>
                <span>VA: <b id="ovAcOutVa" style="color: #fff;">-- VA</b></span>
              </div>
              <div style="font-size: 0.68rem; color: #94a3b8; margin-top: 0.35rem; border-top: 1px dashed rgba(255,255,255,0.08); padding-top: 0.25rem; display: flex; justify-content: space-between;">
                <span title="Daya hidup internal mesin inverter & kipas pendingin">Beban Inv: <b id="ovInvSelfLoss" style="color: #fb923c;">~45 W</b></span>
                <span title="Total konsumsi daya: Rumah + Inverter">Total: <b id="ovGrossLoadWatt" style="color: #38bdf8;">-- W</b></span>
              </div>
            </div>

            <!-- ESG & SAVINGS METER -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #18201a 0%, #111613 100%); border-color: rgba(16,185,129,0.3);">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.72rem; color: #34d399;">ESG & Penghematan</span>
                <span style="font-size: 0.68rem;">🌿 ECO</span>
              </div>
              <div class="stat-val" id="ovSavingsRp" style="color: #34d399; font-size: 1.55rem; margin-bottom: 0.35rem;">Rp 10.500</div>
              <div class="stat-sub" style="font-size: 0.72rem; border-color: rgba(16,185,129,0.15);">
                <span>CO2: <b id="ovCo2Saved" style="color: #a7f3d0;">6.2 kg</b></span>
                <span>Pohon: <b id="ovTreesSaved" style="color: #a7f3d0;">0.3 🌲</b></span>
              </div>
            </div>
          </div>

          <!-- BATTERY ENERGY LIFECYCLE & SUPPORT ESTIMATE (TELEMETRI & DATA KEMARIN) -->
          <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.75rem;">
            <!-- CARD 1: ENERGI TERSIMPAN DI BATERAI (CHARGED) -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #13241b 0%, #0d1712 100%); border-color: rgba(16, 185, 129, 0.35);">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.74rem; color: #34d399; font-weight: 600;">🔋 Energi Tersimpan di Baterai</span>
                <span id="ovBattChgBadge" class="hermes-badge badge-ok" style="font-size: 0.65rem; padding: 0.15rem 0.4rem;">+-- W</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.35rem;">
                <div class="stat-val" id="ovBattChargedKwh" style="color: #34d399; font-size: 1.85rem;">-- kWh</div>
              </div>
              <div class="stat-sub" style="font-size: 0.74rem; border-color: rgba(16,185,129,0.15);">
                <span>Kemarin: <b id="ovBattChargedYest" style="color: #a7f3d0;">-- kWh</b></span>
                <span>Akumulasi: <b id="ovBattChargedWh" style="color: #fff;">-- Wh</b></span>
              </div>
            </div>

            <!-- CARD 2: ENERGI DILEPASKAN DARI BATERAI (DISCHARGED) -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #241d13 0%, #17120a 100%); border-color: rgba(245, 158, 11, 0.35);">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.74rem; color: #fbbf24; font-weight: 600;">⚡ Energi Dilepaskan Baterai</span>
                <span id="ovBattDisBadge" class="hermes-badge badge-warn" style="font-size: 0.65rem; padding: 0.15rem 0.4rem;">-- W</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.35rem;">
                <div class="stat-val" id="ovBattDischargedKwh" style="color: #fbbf24; font-size: 1.85rem;">-- kWh</div>
              </div>
              <div class="stat-sub" style="font-size: 0.74rem; border-color: rgba(245,158,11,0.15);">
                <span>Kemarin: <b id="ovBattDischargedYest" style="color: #fde68a;">-- kWh</b></span>
                <span>DoD Semalam: <b id="ovBattDodYest" style="color: #fff;">--%</b></span>
              </div>
            </div>

            <!-- CARD 3: ESTIMASI WAKTU BATERAI SUPPORT (BERDASARKAN DATA KEMARIN) -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #16222f 0%, #0e1620 100%); border-color: rgba(56, 189, 248, 0.35);">
              <div class="panel-header" style="margin-bottom: 0.3rem;">
                <span style="font-size: 0.74rem; color: #38bdf8; font-weight: 600;">⏱️ Baterai Support Sampai</span>
                <span id="ovBattSupportBadge" class="hermes-badge badge-ok" style="font-size: 0.65rem; padding: 0.15rem 0.4rem;">🟢 Aman Subuh</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.35rem;">
                <div class="stat-val" id="ovBattSupportTarget" style="color: #38bdf8; font-size: 1.45rem; letter-spacing: -0.3px;">--:-- WIB</div>
              </div>
              <div class="stat-sub" style="font-size: 0.74rem; border-color: rgba(56,189,248,0.15);">
                <span>Sisa: <b id="ovBattSupportHours" style="color: #7dd3fc;">-- Jam</b> (<span id="ovBattSupportUsable" style="color: #cbd5e1;">-- kWh</span>)</span>
                <span>Beban Acuan: <b id="ovBattSupportRefLoad" style="color: #fff;" title="Berdasarkan rata-rata pengosongan malam kemarin">~-- W</b></span>
              </div>
            </div>
          </div>

          <!-- SMART SOLAR ADVISORY BOX -->
          <div class="smart-advisory-box">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
              <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span style="font-size: 1.1rem;">💡</span>
                <span style="font-weight: 700; font-size: 0.85rem; color: #fbbf24;" id="advTitle">
                  Anjuran Penggunaan Energi & Prediksi Jam Terik
                </span>
                <span class="hermes-badge badge-warn" id="advStatusTag" style="font-size: 0.65rem; padding: 0.15rem 0.45rem;">
                  🌙 Mode Malam (Baterai Aktif)
                </span>
              </div>
              <div style="font-size: 0.75rem; color: #38bdf8; font-weight: 600; background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.25); border-radius: 4px; padding: 0.2rem 0.55rem;" id="advPeakWindow">
                ⏱️ Puncak Panen: 09:30 - 14:00 WIB (Est. ~5.4 kWh)
              </div>
            </div>
            <div style="font-size: 0.78rem; color: #e2e8f0; line-height: 1.5;" id="advDescription">
              Baterai LiFePO4 pada 83% (48.5V). Dengan konsumsi rumah 80W saat ini, estimasi daya cadangan mampu bertahan hingga ~18-24 jam ke depan (aman melewati subuh tanpa suplai PLN).
            </div>
          </div>

          <!-- ADVANCED SYNOPTIC HYBRID POWER FLOW HUB -->
          <div class="panel" style="margin: 0; padding: 1.15rem; background: #0f1117; border: 1px solid var(--gf-border); border-radius: 12px; position: relative; overflow: hidden;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.6rem;">
              <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span style="font-size: 1.1rem;">⚡</span>
                <span style="font-size: 0.92rem; font-weight: 700; color: #fff;">Diagram Aliran Daya Topologi Hybrid (MPPT &amp; Inverter Hub)</span>
                <span class="hermes-badge badge-blue" id="hubLiveBadge" style="font-size: 0.62rem;">REAL-TIME RS232 + RS485</span>
              </div>
              <div style="font-size: 0.72rem; color: #94a3b8;">
                Mode Aktif: <b id="hubInverterMode" style="color: #38bdf8;">SBU Mode</b> &bull; Suhu Inverter: <b id="hubInvTemp" style="color: #f87171;">--°C</b>
              </div>
            </div>

            <!-- TOPOLOGY GRID (5 COLUMNS: [PV / BATT] <-> [FLOW BRIDGES] <-> [INVERTER HUB] <-> [FLOW BRIDGES] <-> [PLN / RUMAH]) -->
            <div class="topo-grid-flow">
              
              <!-- ROW 1, COL 1: 1. SOLAR PV NODE -->
              <div class="topo-card card-pv" id="cardPv" style="grid-column: 1; grid-row: 1;">
                <div class="topo-card-header">
                  <div class="topo-icon-circle icon-pv">☀️</div>
                  <div>
                    <div class="topo-card-title" style="color: #fbbf24;">Solar PV (Longi)</div>
                    <div class="topo-card-sub">2x 550 Wp &bull; 1100 Wp Total</div>
                  </div>
                </div>
                <div class="topo-val-row">
                  <span class="topo-big-val" style="color: #fbbf24;" id="hubPvWatt">-- W</span>
                  <span class="topo-pill-ratio" id="hubPvRatio">--% Wp</span>
                </div>
                <div class="topo-telemetry-row">
                  <span>Tegangan: <b id="hubPvVolt" style="color:#fff;">-- V</b></span>
                  <span>Arus: <b id="hubPvCurr" style="color:#fff;">-- A</b></span>
                </div>
              </div>

              <!-- ROW 1, COL 2: BRIDGE PV -> INVERTER (Panen MPPT) -->
              <div class="topo-bridge bridge-pv" id="trackPv" style="grid-column: 2; grid-row: 1;">
                <div class="topo-conduit"></div>
                <div class="energy-particles" id="particlesPv"></div>
                <div class="flow-pill flow-pill-pv" id="pillPvFlow">
                  <span>Panen PV</span>
                  <b id="flowBadgePvWatt">+0 W ➔</b>
                </div>
              </div>

              <!-- ROW 1-2, COL 3: CENTER INVERTER POWMR 6.2KW HYBRID MPPT HUB -->
              <div class="topo-inverter-hub" id="cardInverter" style="grid-column: 3; grid-row: 1 / span 2;">
                <div style="display: flex; align-items: center; justify-content: center; width: 64px; height: 64px; border-radius: 50%; background: radial-gradient(circle, #38bdf8 0%, #0284c7 60%, #0369a1 100%); box-shadow: 0 0 25px rgba(56,189,248,0.35); margin: 0 auto 0.5rem; font-size: 1.85rem;">
                  ⚡
                </div>
                <div style="font-size: 0.95rem; font-weight: 800; color: #fff; letter-spacing: 0.3px;">Inverter PowMr 6.2kW</div>
                <div style="font-size: 0.72rem; color: #38bdf8; font-weight: 600; margin: 0.15rem 0 0.5rem;" id="hubInvModeFull">SBU Hybrid Mode (Priority)</div>
                
                <div class="hub-specs-box">
                  <div class="hub-spec-item">
                    <span class="hub-spec-label">Beban Rumah (AC)</span>
                    <span class="hub-spec-val" id="hubInvLoadWatt" style="color: #38bdf8;">-- W</span>
                  </div>
                  <div class="hub-spec-item">
                    <span class="hub-spec-label">Daya Inverter (Idle)</span>
                    <span class="hub-spec-val" id="hubInvSelfLossWatt" style="color: #fb923c;" title="Konsumsi daya hidup inverter internal, logic board, kipas pendingin & rugi efisiensi switching">~45 W</span>
                  </div>
                  <div class="hub-spec-item">
                    <span class="hub-spec-label">Total Beban Sistem</span>
                    <span class="hub-spec-val" id="hubInvGrossWatt" style="color: #34d399;" title="Total daya riil yang ditarik dari gabungan sumber Solar PV dan Baterai (Beban Rumah + Inverter)">-- W</span>
                  </div>
                  <div class="hub-spec-item">
                    <span class="hub-spec-label">Tegangan Bus DC</span>
                    <span class="hub-spec-val" id="hubInvBusVolt" style="color: #fbbf24;">-- V</span>
                  </div>
                </div>

                <div style="margin-top: 0.6rem; font-size: 0.68rem; color: #64748b; display: flex; justify-content: space-around;">
                  <span>Beban: <b id="hubInvLoadPercent" style="color:#10b981;">--%</b></span>
                  <span>Daya Semu: <b id="hubInvLoadVa" style="color:#cbd5e1;">-- VA</b></span>
                  <span>Heatsink: <b id="hubInvHeatsink" style="color:#f87171;">--°C</b></span>
                </div>
              </div>

              <!-- ROW 1, COL 4: BRIDGE PLN -> INVERTER (Suplai / Bypass Grid) -->
              <div class="topo-bridge bridge-grid" id="trackGrid" style="grid-column: 4; grid-row: 1;">
                <div class="topo-conduit"></div>
                <div class="energy-particles" id="particlesGrid"></div>
                <div class="flow-pill flow-pill-grid" id="pillGridFlow">
                  <b id="flowBadgeGridWatt">🠘 PLN Standby</b>
                </div>
              </div>

              <!-- ROW 1, COL 5: 3. GRID PLN NODE -->
              <div class="topo-card card-grid" id="cardGrid" style="grid-column: 5; grid-row: 1;">
                <div class="topo-card-header">
                  <div class="topo-icon-circle icon-grid">🔌</div>
                  <div>
                    <div class="topo-card-title" style="color: #c084fc;">Grid PLN (220V AC)</div>
                    <div class="topo-card-sub" id="hubGridSubTitle">Bypass &amp; Cadangan Darurat</div>
                  </div>
                </div>
                <div class="topo-val-row">
                  <span class="topo-big-val" style="color: #c084fc;" id="hubGridVolt">-- V</span>
                  <span id="hubGridStatusBadge" class="hermes-badge badge-warn" style="font-size: 0.62rem;">STANDBY</span>
                </div>
                <div class="topo-telemetry-row">
                  <span>Frekuensi: <b id="hubGridHz" style="color:#fff;">-- Hz</b></span>
                  <span>Impor PLN: <b id="hubGridWatt" style="color:#cbd5e1;">0 W</b></span>
                </div>
              </div>

              <!-- ROW 2, COL 1: 2. BATTERY BANK NODE -->
              <div class="topo-card card-batt" id="cardBatt" style="grid-column: 1; grid-row: 2;">
                <div class="topo-card-header">
                  <div class="topo-icon-circle icon-batt">🔋</div>
                  <div style="flex: 1;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                      <span class="topo-card-title" style="color: #34d399;">LiFePO4 15S Bank</span>
                      <a href="javascript:void(0)" onclick="openZteBmsModal()" style="color: #38bdf8; text-decoration: none; font-size: 0.65rem; font-weight: 600;">30 Sel ↗</a>
                    </div>
                    <div class="topo-card-sub">2x ZTE FB100B3 &bull; 200 Ah (9.6 kWh)</div>
                  </div>
                </div>
                <div class="topo-val-row">
                  <span class="topo-big-val" style="color: #34d399;" id="hubBattSoc">--%</span>
                  <span id="hubBattFlowDirBadge" class="hermes-badge badge-ok" style="font-size: 0.62rem;">-- W</span>
                </div>
                <!-- Progress bar SOC -->
                <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; overflow: hidden; margin: 0.35rem 0;">
                  <div id="hubBattSocBar" style="width: 98%; height: 100%; background: #10b981; transition: width 0.4s;"></div>
                </div>
                <div class="topo-telemetry-row">
                  <span>Bank: <b id="hubBattVolt" style="color:#fff;">-- V</b></span>
                  <span>Arus: <b id="hubBattCurr" style="color:#38bdf8;">-- A</b></span>
                  <span>Δ Sel: <b id="hubBattDelta" style="color:#fbbf24;">-- mV</b></span>
                </div>
              </div>

              <!-- ROW 2, COL 2: BRIDGE BATT <-> INVERTER (Bidirectional Charge/Discharge) -->
              <div class="topo-bridge bridge-batt" id="trackBatt" style="grid-column: 2; grid-row: 2;">
                <div class="topo-conduit"></div>
                <div class="energy-particles" id="particlesBatt"></div>
                <div class="flow-pill flow-pill-batt" id="pillBattFlow">
                  <span id="pillBattDirText">Dua Arah</span>
                  <b id="flowBadgeBattWatt">-- W</b>
                </div>
              </div>

              <!-- ROW 2, COL 4: BRIDGE INVERTER -> BEBAN RUMAH (Suplai Listrik AC Rumah) -->
              <div class="topo-bridge bridge-load" id="trackLoad" style="grid-column: 4; grid-row: 2;">
                <div class="topo-conduit"></div>
                <div class="energy-particles" id="particlesLoad"></div>
                <div class="flow-pill flow-pill-load" id="pillLoadFlow">
                  <span>Listrik AC</span>
                  <b id="flowBadgeLoadWatt">-- W ➔</b>
                </div>
              </div>

              <!-- ROW 2, COL 5: 4. AC HOME LOAD NODE -->
              <div class="topo-card card-load" id="cardLoad" style="grid-column: 5; grid-row: 2;">
                <div class="topo-card-header">
                  <div class="topo-icon-circle icon-load">🏠</div>
                  <div style="flex: 1;">
                    <div class="topo-card-title" style="color: #60a5fa;">Beban Listrik Rumah</div>
                    <div class="topo-card-sub">Pemakaian Nyata Output AC</div>
                  </div>
                </div>
                <div class="topo-val-row">
                  <span class="topo-big-val" style="color: #60a5fa;" id="hubLoadWatt">-- W</span>
                  <span id="hubLoadSupplyBadge" class="hermes-badge badge-ok" style="font-size: 0.62rem;">100% SOLAR+BATT</span>
                </div>
                <div class="topo-telemetry-row">
                  <span>Output: <b id="hubLoadVolt" style="color:#fff;">-- V</b></span>
                  <span>Frekuensi: <b id="hubLoadHz" style="color:#fff;">-- Hz</b></span>
                  <span>Arus: <b id="hubLoadCurr" style="color:#38bdf8;">-- A</b></span>
                </div>
              </div>

            </div>
          </div>
        </div>

        <!-- 3. REALTIME LIVE AREA CHART (col-8) -->
        <div class="panel col-8">
          <div class="panel-header">
            <span>Grafik Keseimbangan Energi: Solar PV vs Beban AC (Watt)</span>
            <span style="color: var(--gf-text-muted); font-size: 0.7rem;">ENERGY BALANCE &bull; LIVE DARI DB9</span>
          </div>
          <div class="chart-container">
            <canvas id="livePowerChart"></canvas>
          </div>
        </div>

        <!-- 4. INVERTER DIAGNOSTICS & HARDWARE (col-4) -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Diagnostik PowMr 6.2kW Hybrid</span>
            <span style="color: #38bdf8; font-size: 0.72rem; font-weight: 700;">SYS-OK</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.65rem; margin-top: 0.25rem;">
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Suhu Heatsink:</span>
              <b id="ovTemp" style="color: #f87171;">-- °C</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Frekuensi Output AC:</span>
              <b id="ovAcOutHz">-- Hz</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Frekuensi Input PLN:</span>
              <b id="ovGridHz">-- Hz</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Tegangan DC Bus:</span>
              <b id="ovBusVolt" style="color: #cbd5e1;">-- V</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Kapasitas Inverter:</span>
              <b style="color: #38bdf8;">6200 W / 48V (Dual MPPT)</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Port Komunikasi:</span>
              <b style="color: #34d399;">RS232 DB9 /dev/ttyS0 (2400 8N1)</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Update Terakhir:</span>
              <b id="ovLastUpdate" style="color: var(--gf-text-dim);">--</b>
            </div>
          </div>
        </div>

        <!-- 5. SECTION: RIWAYAT & PERKIRAAN TOTAL ENERGI (HARIAN & BULANAN) -->
        <div class="col-12" style="margin-top: 0.5rem;">
          <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 0.85rem; flex-wrap: wrap; gap: 0.5rem;">
            <div>
              <div style="font-size: 1.1rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
                <span>📊</span> Riwayat & Perkiraan Penggunaan Total Daya
                <span class="hermes-badge badge-blue" style="font-size: 0.65rem;">HARIAN & BULANAN</span>
              </div>
              <div style="font-size: 0.78rem; color: var(--gf-text-muted); margin-top: 0.15rem;">
                Akumulasi energi riil dari database telemetri PowMr serta proyeksi konsumsi & panen surya untuk Jetak Kaliagung.
              </div>
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="energyAnalyticsUpdateText" style="font-size: 0.72rem; color: #94a3b8;">Diperbarui: Memuat...</span>
              <button type="button" onclick="fetchEnergyAnalytics()" class="hermes-btn-secondary" style="padding: 0.25rem 0.65rem; font-size: 0.72rem;">🔄 Segarkan</button>
            </div>
          </div>

          <!-- 4 METRIC CARDS FOR TODAY & MONTHLY PROJECTIONS -->
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin-bottom: 0.85rem;">
            <!-- CARD 1: HARI INI - KONSUMSI BEBAN -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #181d28 0%, #10131c 100%); border-color: rgba(56, 189, 248, 0.25);">
              <div class="panel-header" style="margin-bottom: 0.25rem;">
                <span style="font-size: 0.72rem; color: #38bdf8;">Konsumsi Beban Hari Ini</span>
                <span class="hermes-badge badge-blue" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">HARI INI</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.2rem;">
                <div class="stat-val" id="kwhTodayActualLoad" style="color: #38bdf8; font-size: 1.75rem;">-- kWh</div>
              </div>
              <div style="font-size: 0.74rem; color: #cbd5e1; margin-bottom: 0.35rem;">
                Est. Total 24 Jam: <b id="kwhTodayProjectedLoad" style="color: #fff;">~1.85 kWh</b>
              </div>
              <div style="font-size: 0.7rem; color: #94a3b8;">
                Beban Puncak Hari Ini: <b id="kwhTodayPeakLoad" style="color: #fff;">-- W</b>
              </div>
            </div>

            <!-- CARD 2: HARI INI - PRODUKSI SURYA -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #241b11 0%, #171109 100%); border-color: rgba(245, 158, 11, 0.3);">
              <div class="panel-header" style="margin-bottom: 0.25rem;">
                <span style="font-size: 0.72rem; color: #fbbf24;">Produksi Surya Hari Ini</span>
                <span class="hermes-badge badge-warn" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">SOLAR PV</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.2rem;">
                <div class="stat-val" id="kwhTodayActualPv" style="color: #fbbf24; font-size: 1.75rem;">-- kWh</div>
              </div>
              <div style="font-size: 0.74rem; color: #cbd5e1; margin-bottom: 0.35rem;">
                Est. Total Panen: <b id="kwhTodayProjectedPv" style="color: #fde68a;">~4.80 kWh</b>
              </div>
              <div style="font-size: 0.7rem; color: #94a3b8;">
                Est. Penghematan Hari Ini: <b id="kwhTodayProjSavings" style="color: #34d399;">Rp --</b>
              </div>
            </div>

            <!-- CARD 3: BULAN BERJALAN - TOTAL KONSUMSI -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #18201a 0%, #111713 100%); border-color: rgba(16, 185, 129, 0.25);">
              <div class="panel-header" style="margin-bottom: 0.25rem;">
                <span style="font-size: 0.72rem; color: #34d399;" id="monthLoadTitle">Konsumsi Bulan Ini</span>
                <span class="hermes-badge badge-ok" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">BULANAN</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.2rem;">
                <div class="stat-val" id="kwhMonthActualLoad" style="color: #34d399; font-size: 1.75rem;">-- kWh</div>
              </div>
              <div style="font-size: 0.74rem; color: #cbd5e1; margin-bottom: 0.35rem;">
                Proyeksi Akhir Bulan: <b id="kwhMonthProjectedLoad" style="color: #fff;">~55 kWh</b>
              </div>
              <div style="font-size: 0.7rem; color: #94a3b8;">
                Rasio Kemandirian: <b id="kwhSelfSufficiency" style="color: #a7f3d0;">--% Mandiri</b>
              </div>
            </div>

            <!-- CARD 4: BULAN BERJALAN - PENGHEMATAN & SURYA -->
            <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #221a2c 0%, #15111b 100%); border-color: rgba(168, 85, 247, 0.3);">
              <div class="panel-header" style="margin-bottom: 0.25rem;">
                <span style="font-size: 0.72rem; color: #c084fc;">Proyeksi Penghematan Bulanan</span>
                <span style="color: #c084fc; font-size: 0.68rem; font-weight: 700;">💰 SAVINGS</span>
              </div>
              <div style="display: flex; align-items: baseline; gap: 0.35rem; margin-bottom: 0.2rem;">
                <div class="stat-val" id="kwhMonthProjSavings" style="color: #c084fc; font-size: 1.65rem;">Rp --</div>
              </div>
              <div style="font-size: 0.74rem; color: #cbd5e1; margin-bottom: 0.35rem;">
                Proyeksi Produksi: <b id="kwhMonthProjPv" style="color: #e9d5ff;">~125 kWh</b>
              </div>
              <div style="font-size: 0.7rem; color: #94a3b8;">
                Reduksi CO2 Bulan Ini: <b id="kwhMonthProjCo2" style="color: #d8b4fe;">-- kg</b>
              </div>
            </div>
          </div>

          <!-- CHART & HISTORICAL TABLE ROW -->
          <div class="grafana-grid" style="margin-top: 0;">
            <!-- CHART: 7 HARI PERBANDINGAN HARIAN -->
            <div class="panel col-7" style="margin: 0; min-height: 340px;">
              <div class="panel-header">
                <span>Grafik Perbandingan Energi Harian: Surya, Baterai (In/Out) &amp; Beban (kWh)</span>
                <span style="color: #fbbf24; font-size: 0.7rem; font-weight: 600;">7 HARI TERAKHIR</span>
              </div>
              <div class="chart-container" style="height: 270px;">
                <canvas id="energyDailyBarChart"></canvas>
              </div>
            </div>

            <!-- TABLE: RIWAYAT HARIAN -->
            <div class="panel col-5" style="margin: 0; min-height: 340px; display: flex; flex-direction: column;">
              <div class="panel-header">
                <span>Tabel Riwayat Energi Harian</span>
                <span style="color: var(--gf-text-muted); font-size: 0.7rem;">DATA SQLITE AKTUAL</span>
              </div>
              <div style="overflow-x: auto; flex: 1;">
                <table style="width: 100%; border-collapse: collapse; font-size: 0.74rem; text-align: left;">
                  <thead>
                    <tr style="border-bottom: 1px solid rgba(255,255,255,0.1); color: #94a3b8;">
                      <th style="padding: 0.4rem 0.5rem;">Hari & Tanggal</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">☀️ PV (kWh)</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">🔋 Baterai In</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">⚡ Baterai Out</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">🏠 Beban (kWh)</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">Hemat (Rp)</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: center;">Status</th>
                    </tr>
                  </thead>
                  <tbody id="energyDailyTableBody">
                    <tr><td colspan="7" style="text-align: center; padding: 1.5rem; color: #64748b;">Memuat data riwayat...</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 2: RIWAYAT & ANALITIK SIKLUS PLTS ==================== -->
    <div id="page-history" class="view-section">
      <div class="page-header" style="margin-bottom: 1.15rem; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
            <span>📊</span> Dashboard Analisa & Intelijen Siklus PLTS PowMr 6.2kW
          </h1>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
            Evaluasi telemetri SQLite riil: siklus harian, mingguan, & bulanan fotovoltaik surya, efisiensi baterai LiFePO4, serta kalkulasi beban kontinyu maksimal.
          </div>
        </div>
        
        <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
          <!-- 4 SIKLUS PILL SELECTOR -->
          <div class="cycle-mode-pill-group">
            <button type="button" class="cycle-mode-btn active" id="btnModeHarian" onclick="setAnalysisCycleMode('harian')">☀️ Siklus Harian</button>
            <button type="button" class="cycle-mode-btn" id="btnModeMingguan" onclick="setAnalysisCycleMode('mingguan')">📅 Siklus Mingguan</button>
            <button type="button" class="cycle-mode-btn" id="btnModeBulanan" onclick="setAnalysisCycleMode('bulanan')">🗓️ Siklus Bulanan</button>
            <button type="button" class="cycle-mode-btn" id="btnModeTahunan" onclick="setAnalysisCycleMode('tahunan')">📆 Siklus Tahunan</button>
          </div>
          <button type="button" onclick="downloadHistoryCSV()" class="hermes-btn-secondary" style="padding: 0.35rem 0.65rem; font-size: 0.75rem;">📥 Unduh CSV</button>
          <button type="button" onclick="loadPLTSAnalysisDashboard()" class="hermes-btn-primary" style="padding: 0.35rem 0.75rem; font-size: 0.75rem;">🔄 Segarkan</button>
        </div>
      </div>

      <!-- 1. DYNAMIC 4 KPI CARDS (ADAPTS TO HARIAN / MINGGUAN / BULANAN) -->
      <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin-bottom: 1rem;">
        <!-- KPI 1: PV -->
        <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #241b11 0%, #15110a 100%); border-color: rgba(245, 158, 11, 0.3);">
          <div class="panel-header" style="margin-bottom: 0.25rem;">
            <span style="font-size: 0.72rem; color: #fbbf24;" id="anKpiPvTitle">Produksi Surya Hari Ini</span>
            <span class="hermes-badge badge-warn" id="anKpiPvBadge" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">PV SIKLUS</span>
          </div>
          <div class="stat-val" id="anKpiPvVal" style="color: #fbbf24; font-size: 1.75rem; margin-bottom: 0.2rem;">-- kWh</div>
          <div class="stat-sub" style="font-size: 0.74rem;">
            <span id="anKpiPvSub" style="color: #cbd5e1;">Memuat data...</span>
          </div>
        </div>

        <!-- KPI 2: BATERAI -->
        <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #132219 0%, #0d1712 100%); border-color: rgba(16, 185, 129, 0.3);">
          <div class="panel-header" style="margin-bottom: 0.25rem;">
            <span style="font-size: 0.72rem; color: #34d399;" id="anKpiBattTitle">Siklus Baterai LiFePO4</span>
            <span class="hermes-badge badge-ok" id="anKpiBattBadge" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">48V LiFePO4</span>
          </div>
          <div class="stat-val" id="anKpiBattVal" style="color: #34d399; font-size: 1.65rem; margin-bottom: 0.2rem;">-- V</div>
          <div class="stat-sub" style="font-size: 0.74rem;">
            <span id="anKpiBattSub" style="color: #cbd5e1;">Memuat data...</span>
          </div>
        </div>

        <!-- KPI 3: BEBAN -->
        <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #18202d 0%, #0f141f 100%); border-color: rgba(56, 189, 248, 0.3);">
          <div class="panel-header" style="margin-bottom: 0.25rem;">
            <span style="font-size: 0.72rem; color: #38bdf8;" id="anKpiLoadTitle">Konsumsi Beban Rumah</span>
            <span class="hermes-badge badge-blue" id="anKpiLoadBadge" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">LOAD BEBAN</span>
          </div>
          <div class="stat-val" id="anKpiLoadVal" style="color: #38bdf8; font-size: 1.75rem; margin-bottom: 0.2rem;">-- kWh</div>
          <div class="stat-sub" style="font-size: 0.74rem;">
            <span id="anKpiLoadSub" style="color: #cbd5e1;">Memuat data...</span>
          </div>
        </div>

        <!-- KPI 4: KEUANGAN & LINGKUNGAN -->
        <div class="panel" style="margin: 0; padding: 0.85rem; background: linear-gradient(145deg, #221a2c 0%, #15111b 100%); border-color: rgba(168, 85, 247, 0.3);">
          <div class="panel-header" style="margin-bottom: 0.25rem;">
            <span style="font-size: 0.72rem; color: #c084fc;" id="anKpiFinTitle">Nilai Penghematan Listrik</span>
            <span style="color: #c084fc; font-size: 0.65rem; font-weight: 700;" id="anKpiFinBadge">💰 SAVINGS</span>
          </div>
          <div class="stat-val" id="anKpiFinVal" style="color: #c084fc; font-size: 1.65rem; margin-bottom: 0.2rem;">Rp --</div>
          <div class="stat-sub" style="font-size: 0.74rem;">
            <span id="anKpiFinSub" style="color: #cbd5e1;">Memuat data...</span>
          </div>
        </div>
      </div>

      <!-- 2. SPECIAL DEDICATED CARD: BEBAN KONTINYU MAKSIMAL & KAPASITAS MINGGUAN -->
      <div class="weekly-cap-box" style="margin-bottom: 1rem;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 0.85rem;">
          <div>
            <div style="font-size: 0.95rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
              <span>⚡</span> Analisa Kapasitas & Beban Kontinyu Maksimal (Evaluasi Berkala 1 Minggu Sekali)
            </div>
            <div style="font-size: 0.76rem; color: #94a3b8; margin-top: 0.2rem;" id="wkAnalysisPeriodText">
              Dihitung otomatis dari 22.800+ sampel telemetri SQLite 7 hari terakhir: mengukur batas aman baterai LiFePO4 48V 100Ah dan inverter PowMr 6.2kW.
            </div>
          </div>
          <span class="hermes-badge badge-ok" id="wkAnalysisStatusBadge">🟢 SURPLUS KAPASITAS TINGGI</span>
        </div>

        <!-- 4 SUB-METRICS GRID -->
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin-bottom: 0.85rem;">
          <div class="cap-metric-card">
            <div style="font-size: 0.7rem; color: #94a3b8;">Beban Rata-Rata 7 Hari</div>
            <div style="font-size: 1.35rem; font-weight: 700; color: #38bdf8; font-family: monospace; margin: 0.2rem 0;" id="wkAvgLoad">94.3 W</div>
            <div style="font-size: 0.68rem; color: #64748b;">
              Load: <b id="wkAvgPct" style="color: #cbd5e1;">1.8%</b> • Puncak: <b id="wkPeakLoad" style="color: #cbd5e1;">332 W</b>
            </div>
          </div>

          <div class="cap-metric-card">
            <div style="font-size: 0.7rem; color: #94a3b8;">Beban Kontinyu 24 Jam Maksimal</div>
            <div style="font-size: 1.35rem; font-weight: 700; color: #fbbf24; font-family: monospace; margin: 0.2rem 0;" id="wkSafe24h">320 W</div>
            <div style="font-size: 0.68rem; color: #64748b;">
              Batas aman offgrid LiFePO4 (DoD 80% 12h)
            </div>
          </div>

          <div class="cap-metric-card" style="border-color: rgba(16, 185, 129, 0.4); background: rgba(16, 185, 129, 0.05);">
            <div style="font-size: 0.7rem; color: #34d399; font-weight: 600;">Sisa Kapasitas 24 Jam Aman</div>
            <div style="font-size: 1.35rem; font-weight: 800; color: #34d399; font-family: monospace; margin: 0.2rem 0;" id="wkRemaining24h">+225.7 W</div>
            <div style="font-size: 0.68rem; color: #a7f3d0;">
              Peralatan baru yang AMAN dinyalakan 24/7
            </div>
          </div>

          <div class="cap-metric-card">
            <div style="font-size: 0.7rem; color: #94a3b8;">Beban Kontinyu Siang Hari (PV)</div>
            <div style="font-size: 1.35rem; font-weight: 700; color: #c084fc; font-family: monospace; margin: 0.2rem 0;" id="wkSafeDaytime">2,500 W</div>
            <div style="font-size: 0.68rem; color: #64748b;">
              Inverter Max: 6,200W (90%: 5,580W)
            </div>
          </div>
        </div>

        <!-- PROGRESS BAR HEADROOM -->
        <div style="background: rgba(0,0,0,0.3); padding: 0.75rem 0.85rem; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); margin-bottom: 0.75rem;">
          <div style="display: flex; justify-content: space-between; font-size: 0.72rem; margin-bottom: 0.35rem;">
            <span style="color: #cbd5e1;">Pemanfaatan Beban Kontinyu 24 Jam: <b id="wkHeadroomPctText" style="color: #38bdf8;">29.5%</b></span>
            <span style="color: #34d399;">Ruang Bebas Aman: <b id="wkFreePctText">70.5%</b></span>
          </div>
          <div style="width: 100%; height: 8px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden;">
            <div id="wkHeadroomProgressBar" style="width: 29.5%; height: 100%; background: linear-gradient(90deg, #10b981 0%, #38bdf8 100%); transition: width 0.5s ease;"></div>
          </div>
        </div>

        <!-- INSIGHT TEXT -->
        <div style="font-size: 0.76rem; color: #cbd5e1; line-height: 1.5; background: rgba(255,255,255,0.02); padding: 0.6rem 0.8rem; border-radius: 6px;" id="wkRecommendationDesc">
          💡 <b>Rekomendasi Cerdas Mingguan:</b> Penggunaan listrik rumah Anda sangat hemat (rata-rata 94W / 1.8% dari 6.2kW). Anda masih memiliki ruang aman untuk menyalakan beban kontinyu 24 jam sebesar <b>~225 Watt</b> (contoh: Kulkas Inverter ~80W + Homelab/Server Tambahan ~70W) tanpa khawatir kehabisan baterai di malam hari. Di siang hari terik (09:30 - 14:00 WIB), beban dapat dinaikkan hingga <b>2.500 Watt</b> (AC 1/2 PK ~350W, pompa air ~250W, mesin cuci) murni dari tenaga surya.
        </div>
      </div>

      <!-- 3. MULTI-CHART SECTION -->
      <div class="grafana-grid" style="margin-top: 0;">
        <!-- CHART 1: MAIN CYCLE (col-8) -->
        <div class="panel col-8">
          <div class="panel-header">
            <span id="anMainCycleChartTitle">Grafik Siklus Diurnal 24 Jam: Solar PV vs Beban Output vs Baterai (Watt)</span>
            <span style="color: #38bdf8; font-size: 0.7rem; font-weight: 600;" id="anMainCycleBadge">SIKLUS 24 JAM</span>
          </div>
          <div class="chart-container" style="height: 270px;">
            <canvas id="anMainCycleChart"></canvas>
          </div>
        </div>

        <!-- CHART 2: BATTERY CYCLE DYNAMICS (col-4) -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Siklus Baterai: Tegangan (V) & Kapasitas (SOC %)</span>
            <span style="color: #10b981; font-size: 0.7rem; font-weight: 600;">LiFePO4 48V</span>
          </div>
          <div class="chart-container" style="height: 270px;">
            <canvas id="histBattChart"></canvas>
          </div>
        </div>

        <!-- CHART 3: VOLTAGE (col-6) -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Tegangan AC: Output 220V Inverter vs Grid PLN Input (V)</span>
            <span style="color: #a855f7; font-size: 0.7rem; font-weight: 600;">220V KONTINYU</span>
          </div>
          <div class="chart-container" style="height: 250px;">
            <canvas id="histVoltChart"></canvas>
          </div>
        </div>

        <!-- CHART 4: TEMPERATURE (col-6) -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Suhu Operasional Heatsink Inverter PowMr (°C)</span>
            <span style="color: #ef4444; font-size: 0.7rem; font-weight: 600;">TERMAL SYS</span>
          </div>
          <div class="chart-container" style="height: 250px;">
            <canvas id="histTempChart"></canvas>
          </div>
        </div>

        <!-- 4. DATA LOG TABLE (col-12) -->
        <div class="panel col-12">
          <div class="panel-header" style="flex-wrap: wrap; gap: 0.5rem;">
            <span>Tabel Log Telemetri SQLite Aktual (100% Data Riil DB9)</span>
            <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
              <span style="font-size: 0.72rem; color: #94a3b8;">Filter Rentang:</span>
              <div style="display: flex; gap: 2px;">
                <button type="button" class="range-btn active" onclick="setRange('15m', this)">15m</button>
                <button type="button" class="range-btn" onclick="setRange('1h', this)">1h</button>
                <button type="button" class="range-btn" onclick="setRange('6h', this)">6h</button>
                <button type="button" class="range-btn" onclick="setRange('24h', this)">24h</button>
              </div>
              <button type="button" onclick="downloadHistoryCSV()" style="background: #202026; color: #34d399; border: 1px solid #353540; border-radius: 4px; padding: 0.25rem 0.65rem; cursor: pointer; font-size: 0.75rem; font-weight: 600;">Unduh CSV</button>
              <button type="button" onclick="fetchHistoryData()" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; border-radius: 4px; padding: 0.25rem 0.6rem; cursor: pointer; font-size: 0.75rem;">Refresh</button>
            </div>
          </div>
          <div style="overflow-x: auto; max-height: 380px;">
            <table class="raw-table">
              <thead>
                <tr>
                  <th>Waktu</th>
                  <th>Mode Inverter</th>
                  <th>PV Daya</th>
                  <th>PV Volt</th>
                  <th>Baterai V</th>
                  <th>SOC</th>
                  <th>Charge</th>
                  <th>Beban W</th>
                  <th>AC Out V</th>
                  <th>Grid V</th>
                  <th>Suhu</th>
                </tr>
              </thead>
              <tbody id="histTableBody">
                <tr><td colspan="11" style="text-align: center; color: var(--gf-text-muted);">Memuat riwayat...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 2B: SETELAN INVERTER & BMS ==================== -->
    <div id="page-inverter-settings" class="view-section">
      <div class="page-header" style="margin-bottom: 1.15rem; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
            <span>⚙️</span> Setelan Inverter PowMr 6.2kW &amp; Manajemen BMS ZTE
          </h1>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
            Kontrol parameter serial RS232: prioritas beban (SBU/SUB/USB), batas arus cas, kurva voltase LiFePO4 15S, dan kalibrasi proteksi sel.
          </div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center;">
          <span class="hermes-badge badge-ok" id="setInverterStatusBadge">● INVERTER RS232 AKTIF</span>
          <button type="button" class="fs-btn" onclick="loadInverterSettings()" style="font-size: 0.75rem; padding: 0.35rem 0.75rem;">
            🔄 Segarkan Parameter
          </button>
        </div>
      </div>

      <!-- 1. HARDWARE PROFILE & 1-CLICK OPTIMIZATION PRESET BANNER -->
      <div class="panel" style="background: linear-gradient(135deg, rgba(56,189,248,0.08) 0%, rgba(16,185,129,0.06) 100%); border: 1px solid rgba(56,189,248,0.25); border-radius: 12px; padding: 1.15rem; margin-bottom: 1.15rem;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 1rem;">
          <div>
            <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem;">
              <span style="font-size: 1.1rem;">🛡️</span>
              <span style="font-size: 0.95rem; font-weight: 700; color: #f1f5f9;">Profil Hardware &amp; Mode Proteksi Baterai Bekas</span>
              <span class="hermes-badge badge-warn" style="font-size: 0.65rem;">SAFE-VOLTAGE ACTIVE</span>
            </div>
            <div style="font-size: 0.78rem; color: #cbd5e1; line-height: 1.6; max-width: 820px;">
              <b>Hardware Terpasang:</b> 2x Panel Surya Longi 550 Wp (<b>1100 Wp</b>) &bull; 2x Pack Baterai ZTE Birubatt FB100B3 LiFePO4 15S (<b>200 Ah / 9.6 kWh</b>, kondisi bekas deviasi &Delta; ~ 100 mV).<br>
              <span style="color: #94a3b8;">
                ⚠️ <i>Baterai bekas dengan deviasi antar-sel &Delta; ~ 100 mV dihindarkan dari tegangan over-charge (&gt;54.5V) agar tidak trip BMS, serta cut-off dinaikkan ke 44.0V agar sel terlemah tidak anjlok ke bawah 2.50V. Arus dibatasi 40A (0.2C) agar baterai tetap dingin dan awet.</i>
              </span>
            </div>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.5rem; min-width: 260px;">
            <button type="button" onclick="applyInverterPreset('SBU')" style="background: linear-gradient(135deg, #0284c7, #0369a1); color: #fff; border: 1px solid #38bdf8; border-radius: 6px; padding: 0.55rem 0.95rem; font-size: 0.76rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; box-shadow: 0 4px 12px rgba(2,132,199,0.35);">
              <span>⚡</span> 1-Klik Terapkan Profil SBU (Hemat Listrik)
            </button>
            <button type="button" onclick="applyInverterPreset('SUB')" style="background: rgba(255,255,255,0.05); color: #cbd5e1; border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; padding: 0.45rem 0.95rem; font-size: 0.72rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
              <span>☀️</span> 1-Klik Profil SUB (Solar Siang, Cadangan)
            </button>
          </div>
        </div>
      </div>

      <!-- 2. MAIN SETTINGS GRID -->
      <div class="set-grid">
        
        <!-- LEFT: INVERTER LIVE SETTINGS FORM -->
        <div class="panel" style="margin: 0; padding: 1.25rem;">
          <div class="panel-header" style="margin-bottom: 1rem; border-bottom: 1px solid #22222d; padding-bottom: 0.5rem;">
            <span style="font-weight: 700; color: #fff;">Form Konfigurasi Parameter Inverter PowMr</span>
            <span style="font-size: 0.7rem; color: #38bdf8;">Kirim Langsung ke EEPROM Inverter</span>
          </div>

          <form id="inverterSettingsForm" onsubmit="event.preventDefault(); saveInverterSettings();">
            
            <!-- 1. OUTPUT SOURCE PRIORITY -->
            <div class="form-group">
              <label class="form-label">
                <span>1. Prioritas Suplai Beban Rumah (Output Source Priority)</span>
                <b id="lblCurOutputPrio" style="color:#38bdf8;">--</b>
              </label>
              <select class="form-control-select" id="inpOutputPriority">
                <option value="SBU">SBU — Solar ➔ Baterai ➔ PLN (Rekomendasi Hemat Tagihan Listrik)</option>
                <option value="SUB">SUB — Solar ➔ PLN ➔ Baterai (Solar Siang, Malam PLN, Baterai Cadangan)</option>
                <option value="USB">USB — PLN First (Bypass PLN Utama, Baterai Darurat)</option>
              </select>
              <div class="form-hint">
                <b>SBU</b> memprioritaskan pemakaian tenaga surya dan baterai untuk memotong beban PLN di malam hari hingga tegangan baterai mencapai batas aman Back-to-Grid.
              </div>
            </div>

            <!-- 2. CHARGER SOURCE PRIORITY -->
            <div class="form-group">
              <label class="form-label">
                <span>2. Prioritas Pengisian Baterai (Charger Source Priority)</span>
                <b id="lblCurChargerPrio" style="color:#fbbf24;">--</b>
              </label>
              <select class="form-control-select" id="inpChargerPriority">
                <option value="SOLAR_FIRST">Solar First (CSO) — Hanya Solar, PLN mengisi jika surya tidak ada</option>
                <option value="ONLY_SOLAR">Only Solar (OSO) — Khusus Tenaga Surya Saja (PLN dilarang cas)</option>
                <option value="SOLAR_UTILITY">Solar + Utility (SNU) — Solar &amp; PLN Mengisi Bersamaan</option>
                <option value="UTILITY_FIRST">Utility First (CUB) — Listrik PLN Mengisi Utama</option>
              </select>
              <div class="form-hint">
                Gunakan <b>Solar First</b> agar baterai LiFePO4 dicas gratis oleh surya siang hari tanpa menyedot tagihan PLN.
              </div>
            </div>

            <!-- 3. CHARGE CURRENTS -->
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
              <div class="form-group">
                <label class="form-label">
                  <span>Maks. Arus Cas Total</span>
                  <b id="lblCurMaxChg" style="color:#34d399;">-- A</b>
                </label>
                <select class="form-control-select" id="inpMaxChargeCurrent">
                  <option value="20">20 A (~1000 W)</option>
                  <option value="30">30 A (~1500 W)</option>
                  <option value="40" selected>40 A (~2000 W • Rekomendasi 200Ah Bekas)</option>
                  <option value="50">50 A (~2500 W)</option>
                  <option value="60">60 A (~3000 W)</option>
                  <option value="80">80 A (~4000 W)</option>
                </select>
                <div class="form-hint">Arus solar + PLN gabungan. 40A setara 0.2C untuk 200Ah.</div>
              </div>

              <div class="form-group">
                <label class="form-label">
                  <span>Maks. Arus Cas PLN</span>
                  <b id="lblCurMaxAcChg" style="color:#a855f7;">-- A</b>
                </label>
                <select class="form-control-select" id="inpMaxAcChargeCurrent">
                  <option value="2">2 A (Trickle hemat)</option>
                  <option value="10" selected>10 A (~500 W • Rekomendasi PLN)</option>
                  <option value="20">20 A (~1000 W)</option>
                  <option value="30">30 A (~1500 W)</option>
                </select>
                <div class="form-hint">Mencegah MCB meteran PLN rumah anjlok saat malam.</div>
              </div>
            </div>

            <!-- 4. VOLTAGE CURVES FOR 15S LIFEPO4 -->
            <div style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 0.85rem; margin-bottom: 1rem;">
              <div style="font-size: 0.76rem; font-weight: 700; color: #f1f5f9; margin-bottom: 0.65rem;">
                Kurva Voltase Aman LiFePO4 15S (2 Pack Paralel):
              </div>

              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
                <div class="form-group">
                  <label class="form-label">
                    <span>Tegangan Bulk / CV (V)</span>
                    <b id="lblCurBulkV" style="color:#38bdf8;">-- V</b>
                  </label>
                  <input type="number" step="0.1" min="48.0" max="55.0" class="form-control-input" id="inpBulkVoltage" value="53.8">
                  <div class="form-hint">Rekomendasi <b>53.8 V</b> (~3.58V/sel). Hindari 54.8V agar sel bekas tidak trip OVP.</div>
                </div>

                <div class="form-group">
                  <label class="form-label">
                    <span>Tegangan Float (V)</span>
                    <b id="lblCurFloatV" style="color:#38bdf8;">-- V</b>
                  </label>
                  <input type="number" step="0.1" min="48.0" max="54.0" class="form-control-input" id="inpFloatVoltage" value="52.5">
                  <div class="form-hint">Rekomendasi <b>52.5 V</b> (~3.50V/sel). Baterai tetap kenyang tanpa panas.</div>
                </div>

                <div class="form-group">
                  <label class="form-label">
                    <span>Back-to-Grid / PLN (V)</span>
                    <b id="lblCurBackGridV" style="color:#fbbf24;">-- V</b>
                  </label>
                  <input type="number" step="0.1" min="44.0" max="50.0" class="form-control-input" id="inpBackGridVoltage" value="47.0">
                  <div class="form-hint">Pindah ke PLN saat baterai turun ke <b>47.0 V</b> (~25% SOC aman).</div>
                </div>

                <div class="form-group">
                  <label class="form-label">
                    <span>Back-to-Battery (V)</span>
                    <b id="lblCurBackBattV" style="color:#34d399;">-- V</b>
                  </label>
                  <input type="number" step="0.1" min="48.0" max="54.0" class="form-control-input" id="inpBackBattVoltage" value="52.0">
                  <div class="form-hint">Kembali ke baterai saat terisi ke <b>52.0 V</b> (~85% SOC).</div>
                </div>
              </div>

              <div class="form-group" style="margin-bottom: 0;">
                <label class="form-label">
                  <span>Low DC Cut-off Darurat (V)</span>
                  <b id="lblCurCutoffV" style="color:#f87171;">-- V</b>
                </label>
                <input type="number" step="0.1" min="40.0" max="46.0" class="form-control-input" id="inpCutoffVoltage" value="44.0">
                <div class="form-hint">Batas inverter mati total. Rekomendasi <b>44.0 V</b> (jangan 40.0V bawaan pabrik agar sel terlemah tidak anjlok &lt;2.5V).</div>
              </div>
            </div>

            <!-- ACTION BUTTONS -->
            <div style="display: flex; gap: 0.65rem; justify-content: flex-end; align-items: center; border-top: 1px solid #22222d; padding-top: 0.85rem;">
              <span id="setSaveFeedback" style="font-size: 0.75rem; color: #94a3b8; display: none;"></span>
              <button type="button" class="fs-btn" onclick="loadInverterSettings()">Batal / Reset</button>
              <button type="submit" id="btnSaveInverterSettings" style="background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.5rem 1.15rem; font-size: 0.82rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
                <span>💾</span> Simpan Setelan ke Inverter
              </button>
            </div>

          </form>
        </div>

        <!-- RIGHT: TECHNICAL BMS ZTE & SAFETY ADVISORY -->
        <div style="display: flex; flex-direction: column; gap: 1rem;">
          
          <!-- BMS PROTECTION & CONFIGURATION CARD -->
          <div class="panel" style="margin: 0; padding: 1.25rem;">
            <div class="panel-header" style="margin-bottom: 0.85rem; border-bottom: 1px solid #22222d; padding-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
              <div>
                <span style="font-weight: 700; color: #34d399; font-size: 0.95rem;">🔋 Form Konfigurasi BMS ZTE Birubatt</span>
                <div style="font-size: 0.7rem; color: #94a3b8; margin-top: 2px;">Proteksi Voltase Sel, Ambang Balancing, Batas Arus &amp; Proteksi Suhu</div>
              </div>
              <span class="hermes-badge badge-ok" id="bmsCfgStatusBadge" style="font-size: 0.65rem;">● 2 PACK ONLINE (30 SEL)</span>
            </div>

            <form id="bmsConfigForm" onsubmit="event.preventDefault(); saveBmsConfig();">
              
              <!-- SECTION A: PROTEKSI VOLTASE & BALANCING SEL -->
              <div style="background: rgba(16,185,129,0.04); border: 1px solid rgba(16,185,129,0.2); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.85rem;">
                <div style="font-size: 0.78rem; font-weight: 700; color: #34d399; margin-bottom: 0.65rem; display: flex; align-items: center; gap: 0.35rem;">
                  <span>⚡</span> A. Ambang Batas Sel LiFePO4 (15S Rangkaian Seri)
                </div>

                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">
                      <span>Cell Over-Voltage Protection (OVP)</span>
                      <b style="color:#fbbf24;">V</b>
                    </label>
                    <input type="number" step="0.01" min="3.40" max="3.70" class="form-control-input" id="bmsInpOvpV" value="3.65">
                    <div class="form-hint">Cut-off mutlak sel LiFePO4. Standar pabrik <b>3.65 V</b> (trip jika sel mencapai nilai ini).</div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">
                      <span>Cell OVP Alarm / Peringatan</span>
                      <b style="color:#fbbf24;">V</b>
                    </label>
                    <input type="number" step="0.01" min="3.40" max="3.65" class="form-control-input" id="bmsInpOvAlarmV" value="3.55">
                    <div class="form-hint">Ambang peringatan sistem sebelum sel menyentuh OVP (rekomendasi <b>3.55 V</b>).</div>
                  </div>
                </div>

                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">
                      <span>Tegangan Awal Cell Balancing</span>
                      <b style="color:#38bdf8;">V</b>
                    </label>
                    <input type="number" step="0.01" min="3.30" max="3.55" class="form-control-input" id="bmsInpBalanceStartV" value="3.40">
                    <div class="form-hint">Tegangan sel mulai diseimbangkan. LiFePO4 hanya efektif di-balance saat &gt; <b>3.40 V</b>.</div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">
                      <span>Toleransi Deviasi Antar-Sel (&Delta; mV)</span>
                      <b style="color:#38bdf8;">mV</b>
                    </label>
                    <input type="number" step="1" min="10" max="150" class="form-control-input" id="bmsInpDeltaMv" value="30">
                    <div class="form-hint">Batas selisih sel tertinggi &amp; terendah. Rekomendasi <b>30 mV</b> (waspada &gt;80 mV).</div>
                  </div>
                </div>

                <div class="form-row" style="margin-bottom:0;">
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Cell Under-Voltage Alarm</span>
                      <b style="color:#f87171;">V</b>
                    </label>
                    <input type="number" step="0.01" min="2.50" max="3.00" class="form-control-input" id="bmsInpUvAlarmV" value="2.80">
                    <div class="form-hint">Peringatan sel mulai menipis sebelum cut-off (rekomendasi <b>2.80 V</b>).</div>
                  </div>
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Cell Under-Voltage Protection (UVP)</span>
                      <b style="color:#f87171;">V</b>
                    </label>
                    <input type="number" step="0.01" min="2.00" max="2.80" class="form-control-input" id="bmsInpUvpV" value="2.50">
                    <div class="form-hint">Batas mutlak sel LiFePO4 mati total (standar pabrik <b>2.50 V</b>).</div>
                  </div>
                </div>
              </div>

              <!-- SECTION B: BANK BATERAI, ARUS & CUT-OFF -->
              <div style="background: rgba(56,189,248,0.04); border: 1px solid rgba(56,189,248,0.2); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.85rem;">
                <div style="font-size: 0.78rem; font-weight: 700; color: #38bdf8; margin-bottom: 0.65rem; display: flex; align-items: center; gap: 0.35rem;">
                  <span>🔋</span> B. Kapasitas Bank, Arus &amp; Batas Total
                </div>

                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">
                      <span>Total Kapasitas Nominal Bank</span>
                      <b style="color:#38bdf8;">Ah</b>
                    </label>
                    <input type="number" step="10" min="50" max="1000" class="form-control-input" id="bmsInpNominalAh" value="200">
                    <div class="form-hint">Kapasitas total bank (<b>200 Ah</b> untuk 2 pack paralel ZTE FB100B3 100Ah).</div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">
                      <span>Maksimum Arus Cas Baterai</span>
                      <b style="color:#34d399;">A</b>
                    </label>
                    <input type="number" step="5" min="10" max="150" class="form-control-input" id="bmsInpMaxChargeA" value="40">
                    <div class="form-hint">Arus cas ramah baterai (<b>40 A</b> = 0.2C). Menjaga sel bekas tetap dingin &amp; awet.</div>
                  </div>
                </div>

                <div class="form-row" style="margin-bottom:0;">
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Maksimum Arus Lepas (Discharge)</span>
                      <b style="color:#fbbf24;">A</b>
                    </label>
                    <input type="number" step="10" min="20" max="250" class="form-control-input" id="bmsInpMaxDischargeA" value="100">
                    <div class="form-hint">Arus beban continuous maksimum (rekomendasi <b>100 A</b> untuk 2 pack).</div>
                  </div>
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Batas Cut-off Pack Baterai</span>
                      <b style="color:#f87171;">V</b>
                    </label>
                    <input type="number" step="0.1" min="40.0" max="46.0" class="form-control-input" id="bmsInpPackUvpV" value="44.0">
                    <div class="form-hint">Tegangan pack cut-off aman (rekomendasi <b>44.0 V</b> &approx; 2.93V/sel).</div>
                  </div>
                </div>
              </div>

              <!-- SECTION C: PROTEKSI SUHU & KOMUNIKASI RS485 -->
              <div style="background: rgba(245,158,11,0.04); border: 1px solid rgba(245,158,11,0.2); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.85rem;">
                <div style="font-size: 0.78rem; font-weight: 700; color: #fbbf24; margin-bottom: 0.65rem; display: flex; align-items: center; gap: 0.35rem;">
                  <span>🌡️</span> C. Proteksi Suhu &amp; Parameter RS485 Modbus
                </div>

                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">
                      <span>Alarm Suhu Tinggi Cas</span>
                      <b style="color:#fbbf24;">&deg;C</b>
                    </label>
                    <input type="number" step="1" min="35" max="65" class="form-control-input" id="bmsInpHighTempAlarm" value="48">
                    <div class="form-hint">Alarm sistem jika suhu sel menyentuh <b>48 &deg;C</b>.</div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">
                      <span>Cut-off Suhu Tinggi Cas</span>
                      <b style="color:#f87171;">&deg;C</b>
                    </label>
                    <input type="number" step="1" min="45" max="75" class="form-control-input" id="bmsInpHighTempCutoff" value="55">
                    <div class="form-hint">Batas cut-off proteksi hardware pabrik ZTE (<b>55 &deg;C</b>).</div>
                  </div>
                </div>

                <div class="form-row" style="margin-bottom:0;">
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Modbus Slave IDs Terpasang</span>
                      <b style="color:#94a3b8;">ID</b>
                    </label>
                    <input type="text" class="form-control-input" id="bmsInpSlaveIds" value="2, 5">
                    <div class="form-hint">Daftar slave address RS485 (contoh: <code>2, 5</code>).</div>
                  </div>
                  <div class="form-group" style="margin-bottom:0;">
                    <label class="form-label">
                      <span>Interval Polling Telemetri</span>
                      <b style="color:#94a3b8;">detik</b>
                    </label>
                    <input type="number" step="1" min="1" max="60" class="form-control-input" id="bmsInpPollingSec" value="3">
                    <div class="form-hint">Periode pembacaan telemetri Modbus (rekomendasi <b>3 detik</b>).</div>
                  </div>
                </div>
              </div>

              <!-- ACTION BUTTONS -->
              <div style="display: flex; gap: 0.65rem; justify-content: flex-end; align-items: center; border-top: 1px solid #22222d; padding-top: 0.85rem; flex-wrap: wrap;">
                <span id="bmsSaveFeedback" style="font-size: 0.75rem; color: #94a3b8; display: none;"></span>
                <button type="button" class="fs-btn" onclick="syncBmsFromConnectedPacks()">🔍 Sync Pack Terhubung</button>
                <button type="button" class="fs-btn" onclick="applyBmsPresetSafe()">🛡️ Preset Safe 15S</button>
                <button type="button" class="fs-btn" onclick="loadBmsConfig()">🔄 Muat Ulang</button>
                <button type="button" id="btnSaveBmsConfig" onclick="saveBmsConfig(event)" style="background: #059669; color: #fff; border: none; border-radius: 6px; padding: 0.5rem 1.15rem; font-size: 0.82rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
                  <span>💾</span> Simpan Konfigurasi BMS
                </button>
              </div>

            </form>
          </div>

          <!-- SOLAR MATCHING INFO -->
          <div class="panel" style="margin: 0; padding: 1.15rem;">
            <div class="panel-header" style="margin-bottom: 0.65rem;">
              <span style="font-weight: 700; color: #fbbf24;">Kesesuaian Panel Surya Longi 2x 550Wp</span>
              <span style="font-size: 0.7rem; color: #94a3b8;">1100 Wp ARRAY</span>
            </div>

            <div style="font-size: 0.74rem; color: #cbd5e1; line-height: 1.55;">
              &bull; <b>Tegangan Kerja MPPT</b>: 2x 41.8 V &approx; 83.6 V seri (atau 41.8 V paralel). Inverter PowMr 6.2kW memiliki rentang MPPT 60V - 450V (Ideal).<br>
              &bull; <b>Estimasi Panen Harian</b>: 1.1 kWp &times; 3.5 Jam Terik &approx; 3.8 kWh - 4.5 kWh per hari cerah.<br>
              &bull; <b>Arus Pengisian Surya</b>: Maksimum ~18 A - 20 A ke bank baterai 48V. Sangat aman dipasangkan dengan profil pengisian 40A di Inverter.
            </div>
          </div>

        </div>

      </div>
    </div>

    <!-- ==================== HALAMAN 3: WORKSPACE QA ==================== -->
    <div id="page-hermes" class="view-section">
      <!-- BRIEFING HERMES (HTML dari LLM, disanitasi server; otomatis 02:00 WIB hari kerja atau manual) -->
      <div id="hermesBriefingPanel" class="hb-panel">
        <div class="hb-head">
          <div style="display:flex; align-items:center; gap:0.7rem; min-width:0;">
            <div class="hb-avatar">H</div>
            <div style="min-width:0;">
              <div style="font-weight:700; color:#f4f4f5; font-size:1rem; line-height:1.2;">Briefing Hermes</div>
              <div id="hbMeta" style="font-size:0.68rem; color:#71717a; margin-top:2px;">Memuat...</div>
            </div>
          </div>
          <div style="display:flex; align-items:center; gap:0.45rem;">
            <button id="btnRefreshBriefing" onclick="refreshHermesBriefing()" class="hb-btn hb-btn-primary">↻ Perbarui Briefing</button>
            <button id="btnToggleBriefing" onclick="toggleHermesBriefing()" class="hb-btn" title="Ringkas / perluas">▾</button>
          </div>
        </div>
        <div id="hbChips" class="hb-chips"></div>
        <div id="hermesBriefingBody" class="hb-body">Memuat briefing...</div>
        <div class="hb-foot">Disusun Hermes otomatis pukul 02:00 WIB setiap hari kerja dari data akun Anda (task, agenda, cuaca). Tombol Perbarui menyusun ulang sekarang.</div>
      </div>

      <div class="page-header" style="margin-bottom: 1.25rem; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <h1 style="font-size: 1.15rem; font-weight: 700; color: #fff; margin: 0;">
            Workspace QA — <span id="workspaceProjectSprintTitle">Sprint Berjalan</span>
          </h1>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
            Sprint berjalan, agenda kalender harian, dan catatan pengujian Anda di satu tempat.
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 0.65rem;">
          <span id="spectrumLastSyncBadge" style="font-size: 0.72rem; color: #71717a;">Memuat...</span>
          <button onclick="navigateToPage('spectrum-config')" style="background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 500; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; transition: all 0.15s;" title="Pengaturan Akun & Kredensial SPEctrum">
            ⚙️ Akun SPEctrum
          </button>
          <a id="btnOpenGoogleSheet" href="https://docs.google.com/spreadsheets/d/1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs/edit" target="_blank" style="background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.3); border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 500; text-decoration: none; display: flex; align-items: center; gap: 0.4rem; transition: all 0.15s;" title="Buka Spreadsheet Test Case di Google Sheets">
            📊 Google Sheet
          </a>
          <button id="btnSyncSpectrum" onclick="syncSpectrumLive()" style="background: #202026; color: #a1a1aa; border: 1px solid #2d2d35; border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 500; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; transition: all 0.15s;">
            <span id="syncSpectrumSpinner" style="display: none; animation: blink 1s infinite;">●</span> Perbarui Data
          </button>
        </div>
      </div>

      <!-- NOTICE BANNER JIKA AKUN BELUM DIKONFIGURASI -->
      <div id="spectrumNeedsConfigBanner" style="display: none; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); border-radius: 8px; padding: 0.85rem 1rem; margin-bottom: 1rem; align-items: center; justify-content: space-between; gap: 1rem;">
        <div style="display: flex; align-items: center; gap: 0.75rem;">
          <div style="font-size: 1.25rem;">⚠️</div>
          <div>
            <div style="font-size: 0.85rem; font-weight: 600; color: #fbbf24;">Akun SPEctrum Belum Dihubungkan</div>
            <div style="font-size: 0.75rem; color: #d4d4d8; margin-top: 0.15rem;">Task sprint dan jadwal meeting harian mengikuti akun SPEctrum masing-masing. Silakan atur email dan password SPEctrum Anda.</div>
          </div>
        </div>
        <button onclick="switchTab('spectrum-config')" style="background: #f59e0b; color: #000; border: none; font-weight: 600; font-size: 0.75rem; padding: 0.4rem 0.85rem; border-radius: 6px; cursor: pointer; white-space: nowrap; transition: opacity 0.2s;">
          ⚙️ Buka Config SPEctrum
        </button>
      </div>

      <div class="grafana-grid">
        <!-- ROW 1: ACTIVE SPRINT OVERVIEW (col-7) -->
        <div class="panel col-7">
          <div class="panel-header">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="spectrumSprintTitle" style="font-weight: 600; color: #fff;">Memuat Sprint...</span>
              <span id="spectrumSprintStatusBadge" class="hermes-badge badge-ok" style="font-size: 0.65rem;">Berjalan</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.65rem;">
              <span id="spectrumDaysRemaining" style="font-size: 0.72rem; font-weight: 600; color: #f59e0b; background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.2); border-radius: 4px; padding: 0.1rem 0.45rem;">-- hari tersisa</span>
              <span id="spectrumSprintPeriod" style="font-size: 0.72rem; color: #a1a1aa;">-</span>
            </div>
          </div>

          <div id="spectrumSprintGoal" style="font-size: 0.76rem; color: #94a3b8; line-height: 1.4; margin: 0.4rem 0 0.7rem; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
            Memuat sasaran sprint...
          </div>

          <!-- SPRINT METRICS (KPI) -->
          <div class="kpi-grid">
            <div class="kpi-card" style="--kpi:#38bdf8;"><div class="kpi-label">Story Points</div><div id="metricTotalPoints" class="kpi-value">0</div></div>
            <div class="kpi-card" style="--kpi:#e4e4e7;"><div class="kpi-label">Total Task</div><div id="metricTotalTasks" class="kpi-value">0</div></div>
            <div class="kpi-card" style="--kpi:#10b981;"><div class="kpi-label">Ready QA</div><div id="metricDevDone" class="kpi-value">0</div></div>
            <div class="kpi-card" style="--kpi:#f59e0b;"><div class="kpi-label">Task Saya</div><div id="metricMyTasks" class="kpi-value">0</div></div>
          </div>

          <!-- PROGRESS BAR -->
          <div style="margin-top: 0.35rem;">
            <div style="display: flex; justify-content: space-between; font-size: 0.72rem; color: #71717a; margin-bottom: 0.35rem;">
              <span style="display:inline-flex; align-items:center; gap:0.7rem;">Progres Sprint
                <span style="display:inline-flex; align-items:center; gap:0.25rem; font-size:0.66rem;"><i style="width:8px;height:8px;border-radius:50%;background:#10b981;display:inline-block;"></i>Selesai</span>
                <span style="display:inline-flex; align-items:center; gap:0.25rem; font-size:0.66rem;"><i style="width:8px;height:8px;border-radius:50%;background:#38bdf8;display:inline-block;"></i>Jalan</span>
              </span>
              <span id="spectrumProgressText">Memuat...</span>
            </div>
            <div style="width: 100%; height: 9px; background: #27272a; border-radius: 6px; overflow: hidden; display: flex;">
              <div id="spectrumDevDoneBar" style="height: 100%; width: 0%; background: #10b981; transition: width 0.5s ease;"></div>
              <div id="spectrumInProgressBar" style="height: 100%; width: 0%; background: #38bdf8; transition: width 0.5s ease;"></div>
            </div>
          </div>
        </div>

        <!-- ROW 1: CALENDAR & DAILY SCRUM (col-5) -->
        <div class="panel col-5">
          <div class="panel-header">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span>Agenda Daily Scrum</span>
              <span class="hermes-badge badge-blue" style="font-size: 0.65rem;">Microsoft Sync</span>
            </div>
            <button onclick="prepareDailyUpdatePrompt()" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 4px; padding: 0.2rem 0.55rem; font-size: 0.7rem; cursor: pointer; font-weight: 500;">
              Siapkan Draf Daily
            </button>
          </div>
          <div id="spectrumCalendarList" style="display: flex; flex-direction: column; gap: 0.45rem; margin-top: 0.5rem; max-height: 150px; overflow-y: auto;">
            <div style="color: var(--gf-text-muted); font-size: 0.8rem;">Memuat agenda kalender...</div>
          </div>
        </div>

        <!-- ROW 2: TASK LIST & QA ACTION (col-12) -->
        <div class="panel col-12">
          <div class="panel-header" style="flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="spectrumTaskListTitle">Daftar Task Sprint (SPEctrum)</span>
              <span id="spectrumTaskCountBadge" class="hermes-badge badge-blue" style="font-size: 0.65rem;">0 Task</span>
            </div>
            <!-- FILTER TABS -->
            <div style="display: flex; gap: 0.35rem; flex-wrap: wrap; align-items: center;">
              <button class="spectrum-tab active" id="tabTaskAris" onclick="filterSpectrumTasks('my')"><span id="labelTabMyTask">Task Saya</span> <span id="countTabAris">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskDevDone" onclick="filterSpectrumTasks('devdone')">Ready QA / Dev Done <span id="countTabDevDone">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskProgress" onclick="filterSpectrumTasks('progress')">On Progress <span id="countTabProgress">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskAll" onclick="filterSpectrumTasks('all')">Semua Task <span id="countTabAll">(0)</span></button>
              <input id="spectrumTaskSearch" class="ws-search" type="search" placeholder="Cari kode / nama / assignee..." oninput="window._spectrumSearch = this.value; renderSpectrumTasks();">
              <select id="spectrumAssigneeSelect" onchange="filterSpectrumByAssignee(this.value)" style="display:none; background:#18181b; color:#38bdf8; border:1px solid #334155; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Filter berdasarkan anggota tim">
                <option value="">Semua Anggota Tim</option>
              </select>
            </div>
          </div>

          <div style="overflow-x: auto; max-height: 440px; margin-top: 0.5rem;">
            <table class="raw-table">
              <thead>
                <tr>
                  <th style="width: 75px;">Kode</th>
                  <th>Nama Task</th>
                  <th style="width: 150px;">Assignee</th>
                  <th style="width: 125px;">Status</th>
                  <th style="width: 60px; text-align: center;">Bobot</th>
                  <th style="width: 220px; text-align: right;">Aksi</th>
                </tr>
              </thead>
              <tbody id="spectrumTaskTableBody">
                <tr><td colspan="6" style="text-align: center; color: var(--gf-text-muted);">Memuat data task SPEctrum...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- ROW 3: CHAT HERMES (col-8) -->
        <div class="panel col-8" style="min-height: 400px; display: flex; flex-direction: column;">
          <div class="panel-header" style="margin-bottom: 0.65rem;">
            <div style="display: flex; align-items: center; gap: 0.65rem;">
              <div class="hb-avatar" style="width:32px; height:32px; font-size:0.9rem; border-radius:10px;">H</div>
              <div>
                <div style="color:#f4f4f5; font-weight:700; font-size:0.9rem; line-height:1.2; display:flex; align-items:center; gap:0.4rem;">Hermes <span style="width:7px; height:7px; background:#10b981; border-radius:50%; display:inline-block;" title="Online"></span></div>
                <div style="font-size:0.66rem; color:#71717a; text-transform:none; letter-spacing:0; font-weight:400;">Asisten QA yang paham sprint &amp; agenda Anda</div>
              </div>
            </div>
            <button onclick="clearAiChatHistory()" class="hb-btn" title="Bersihkan chat">Bersihkan</button>
          </div>

          <!-- CHAT BUBBLES CONTAINER -->
          <div class="chat-bubble-container" id="aiChatBubbles" style="flex: 1;">
            <div class="chat-bubble chat-bubble-ai">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.35rem;">
                <div style="display:flex;align-items:center;gap:0.4rem;font-weight:600;color:#38bdf8;font-size:0.78rem;">
                  <span>Hermes</span>
                </div>
              </div>
              <p style="margin:0 0 0.35rem 0; line-height: 1.45;">Halo __USER_NAME__! Konteks sprint dan agenda harian Anda sudah siap.</p>
              <p style="margin:0; font-size:0.75rem; color:#94a3b8; line-height: 1.45;">Klik salah satu task di atas untuk langsung susun skenario <b>Test Case</b>, klik <b>Siapkan Draf Daily</b> untuk bahan standup, atau tanya langsung apa pun ke saya.</p>
            </div>
          </div>

          <!-- OTP SUGGESTION STRIP (muncul kalau ada OTP baru) -->
          <div id="otpSuggestionStrip" style="display:none; background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.2); border-radius:6px; padding:0.45rem 0.75rem; margin-top:0.5rem; align-items:center; justify-content:space-between; gap:0.5rem;">
            <div style="font-size:0.75rem; color:#f59e0b;">OTP baru masuk — <span id="otpSuggestionCode" style="font-family:monospace; font-weight:700; font-size:0.85rem; letter-spacing:0.08em;"></span></div>
            <div style="display:flex; align-items:center; gap:0.4rem;">
              <button onclick="useOtpSuggestion()" style="background:rgba(245,158,11,0.15); color:#f59e0b; border:1px solid rgba(245,158,11,0.3); border-radius:4px; padding:0.2rem 0.55rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Pakai OTP ini</button>
              <button onclick="document.getElementById('otpSuggestionStrip').style.display='none'" style="background:transparent; border:none; color:#52525b; cursor:pointer; font-size:0.9rem; padding:0.1rem 0.3rem;">✕</button>
            </div>
          </div>

          <!-- QUICK PROMPT CHIPS (task chip mengikuti task milik user) -->
          <div style="display: flex; gap: 0.35rem; flex-wrap: wrap; margin-top: 0.65rem; margin-bottom: 0.35rem;">
            <button class="quick-chip" onclick="prepareDailyUpdatePrompt()">Draf Daily Standup</button>
            <span id="quickTaskChips" style="display: contents;"></span>
            <button class="quick-chip" onclick="applyQuickPrompt('Buatkan template Bug Report SPEctrum standar: severity, steps to reproduce, actual vs expected result, screenshot placeholder.')">Template Bug</button>
            <button class="quick-chip" id="chipOtp" onclick="applyQuickPrompt('Ada OTP atau email verifikasi baru yang masuk?')">Cek OTP Masuk</button>
            <button class="quick-chip" onclick="prepareEndOfDayPrompt()" style="background: rgba(16,185,129,0.1); border-color: rgba(16,185,129,0.3); color: #34d399;">Tutup Hari (Rekap)</button>
          </div>

          <!-- CHAT INPUT -->
          <label id="aiAgentToggleWrap" style="display:none; align-items:center; gap:0.4rem; font-size:0.72rem; color:#94a3b8; margin-top:0.35rem; cursor:pointer;" title="Hermes dapat memakai alat: adb, cek IP publik, Appium, status server. Aksi yang mengubah koneksi/layanan selalu minta persetujuan Anda.">
            <input type="checkbox" id="chkAgentMode" style="accent-color:#10b981;" onchange="document.getElementById('aiInputPrompt').placeholder = this.checked ? 'Perintah ke Hermes, mis. scan adb, cek IP publik, status Appium...' : 'Tanya Hermes...'">
            <span style="font-weight:600; color:#34d399;">Mode Agen</span> <span>(Hermes boleh menjalankan alat terminal yang diizinkan)</span>
          </label>
          <div class="chat-box" style="margin-top: 0.25rem;">
            <input type="text" id="aiInputPrompt" class="chat-input" placeholder="Tanya Hermes..." onkeypress="if(event.key==='Enter') sendAiPrompt();">
            <button class="chat-btn" id="btnSendAiPrompt" onclick="sendAiPrompt()">Kirim</button>
          </div>
        </div>

        <!-- ROW 3: PENGATURAN HERMES (col-4) -->
        <div class="panel col-4" style="align-self: start;">
          <div class="panel-header">
            <span>Pengaturan Hermes</span>
            <span class="hermes-badge badge-ok" id="hermesOnlineBadge">Layanan Aktif</span>
          </div>
          <div style="margin-top: 0.6rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem;">
              <span style="font-size:0.7rem; color:#a1a1aa; font-weight:600; text-transform:uppercase; letter-spacing:0.04em;">Model AI</span>
              <span id="modelChangeFeedback" style="color:#10b981; font-size:0.7rem; display:none; font-weight:600;">Tersimpan!</span>
            </div>
            <select id="hermesModelSelect" class="chat-input" style="width: 100%; box-sizing: border-box; padding: 0.4rem 0.5rem; font-size: 0.78rem; background: #14161b; color: #38bdf8; border: 1px solid #334155; border-radius: 4px; cursor: pointer;" onchange="onModelSelectChanged(this.value)">
                <option value="deepseek-v4-flash-0731:netra">DeepSeek V4 Flash (Netra 90% Off) - $0.05/1M</option>
              </select>
            <div style="font-size:0.68rem; color:#52525b; margin-top:0.4rem;">Dipakai untuk chat, briefing, dan notulen teks. Pilih model murah untuk pemakaian harian.</div>
          </div>
          <details class="mt-fold" id="hermesSolarCfg" style="margin-top: 0.9rem;">
            <summary>☀️ Pemantauan solar (otomasi baterai)</summary>
            <div style="display:flex; flex-direction:column; gap:0.45rem; padding: 0.6rem 0.1rem 0.3rem; font-size:0.74rem;">
              <div class="cfg-row"><span>Sumber data</span><b style="color:#10b981;">localhost:3005</b></div>
              <div class="cfg-row"><span>Baterai rendah</span><b style="color:#f87171;" id="hermesLowThresh">20% SOC</b></div>
              <div class="cfg-row"><span>Baterai surplus</span><b style="color:#34d399;" id="hermesHighThresh">95% SOC</b></div>
              <div class="cfg-row"><span>Interval polling</span><b id="hermesPollSec">60 detik</b></div>
              <div class="cfg-row"><span>Status</span><b id="hermesConnStatus" style="color:#34d399;">TERHUBUNG</b></div>
            </div>
          </details>
        </div>

        <!-- ROW 4: EVENT LOG TIMELINE (col-12) -->
        <details class="panel col-12 mt-fold-panel" id="hermesLogFold">
          <summary><span>Log Aktivitas Hermes</span></summary>
          <div class="panel-header" style="margin-top:0.5rem;">
            <span style="font-size:0.7rem; color:#71717a;">Riwayat aksi otomatis & perintah agen</span>
            <button onclick="fetchHermesStatus()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Refresh</button>
          </div>
          <div class="event-feed" id="hermesEventsFeed">
            <div style="color: var(--gf-text-muted); font-size: 0.85rem;">Memuat feed aktivitas...</div>
          </div>
        </details>
      </div>
    </div>

    <!-- ==================== HALAMAN 6: ANDROID & APPIUM ==================== -->
    <div id="page-android" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.15rem; font-weight: 700; color: #fff;">
          Android Remote & Kontrol Layar
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Kontrol perangkat Android langsung dari browser: sentuhan layar, tombol navigasi, input teks, dan terminal ADB shell.
        </div>
      </div>
      <!-- PERANGKAT ANDA (sewa per user) -->
      <div id="androidLeaseBar" class="lease-bar">
        <div style="min-width:0;">
          <div id="androidLeaseTitle" style="font-weight:700; color:#f4f4f5; font-size:0.92rem;">Memeriksa perangkat...</div>
          <div id="androidLeaseSub" style="font-size:0.72rem; color:#71717a; margin-top:2px;"></div>
        </div>
        <div style="display:flex; gap:0.45rem; flex-wrap:wrap; align-items:center;">
          <button id="btnClaimDevice" onclick="claimAndroidDevice(false)" class="hb-btn hb-btn-primary">📱 Ambil perangkat kosong</button>
          <button id="btnReleaseDevice" onclick="releaseAndroidDevice()" class="hb-btn">Lepas</button>
          <button id="btnAppiumCaps" onclick="showAppiumCaps()" class="hb-btn" title="Template capabilities Appium dengan port unik untuk perangkat Anda">⚙ Appium caps</button>
        </div>
      </div>
      <pre id="appiumCapsBox" style="display:none; background:#0b0c0e; border:1px solid #27272a; border-radius:8px; padding:0.7rem 0.9rem; font-size:0.72rem; color:#a3e635; margin-bottom:1rem; overflow:auto; max-height:260px;"></pre>

      <div class="grafana-grid">
        <!-- STATUS SERVER APPIUM -->
        <div class="panel col-4 owner-only">
          <div class="panel-header">
            <span>Status Server ADB & Appium</span>
            <span class="hermes-badge badge-ok" id="appiumBadge">ONLINE (v3.8.0)</span>
          </div>
          <div style="display:flex; flex-direction:column; gap:0.65rem; margin-top:0.5rem;">
            <div class="stat-sub" style="border:none; padding:0; flex-direction:column; align-items:flex-start; gap:0.2rem;">
              <span style="font-size:0.72rem; color:#71717a;">Subdomain (Internet / Automation):</span>
              <div style="display:flex; align-items:center; gap:0.4rem; width:100%; justify-content:space-between;">
                <a href="https://appium.westpro.my.id/status" target="_blank" style="color:#38bdf8; font-family:monospace; font-weight:600; font-size:0.78rem; text-decoration:none;">https://appium.westpro.my.id</a>
                <span class="hermes-badge badge-ok" style="font-size:0.6rem;">SSL Aktif</span>
              </div>
            </div>
            <div class="stat-sub" style="border:none; padding:0;">
              <span>Tailscale / LAN:</span><b style="font-family:monospace; color:#a1a1aa; font-size:0.75rem;">100.110.85.21:4723</b>
            </div>
            <div class="stat-sub" style="border:none; padding:0;">
              <span>ADB Path:</span><b style="font-family:monospace; color:#a3e635;">/home/ares/bin/adb</b>
            </div>
            <div class="stat-sub" style="border:none; padding:0;">
              <span>Mode Koneksi:</span><b style="color:#34d399;">USB & Wireless ADB (TCP 5555)</b>
            </div>
            <div class="stat-sub" style="border:none; padding:0;">
              <span>Status Service:</span><b style="color:#38bdf8;">appium.service (Active)</b>
            </div>
            <div class="stat-sub" style="border:none; padding:0;">
              <span>Tailscale VPN:</span><b id="tailscaleStatusText" style="color:#fbbf24;">Memuat...</b>
            </div>
            <div id="tailscaleAuthBox" style="display:none; margin-top:0.25rem; background:#1c1917; border:1px solid #78350f; border-radius:4px; padding:0.4rem 0.6rem; font-size:0.72rem;">
              <span style="color:#fde047; font-weight:600;">Autentikasi Tailscale Diperlukan:</span><br>
              <a id="tailscaleAuthLink" href="https://login.tailscale.com/a/1eb3282e0101d9" target="_blank" style="color:#38bdf8; font-weight:600; text-decoration:underline; display:inline-block; margin-top:0.2rem;">Buka Tautan Login Tailscale</a>
            </div>
          </div>
          <button onclick="fetchAppiumDevices()" style="margin-top:1rem; width:100%; background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.5rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Pindai Perangkat</button>
        </div>

        <!-- DEVICE LIST -->
        <div class="panel col-8" id="androidDevicesPanel">
          <div class="panel-header">
            <span>Perangkat Android Terdeteksi</span>
            <span style="font-size:0.7rem; color:var(--gf-text-muted);">Pilih perangkat untuk mulai kontrol layar</span>
          </div>
          <div id="appiumDevicesList" style="display:flex; flex-direction:column; gap:0.65rem;">
            <div style="color:var(--gf-text-muted); font-size:0.8rem; padding:1rem; text-align:center;">Memuat daftar perangkat...</div>
          </div>
        </div>

        <!-- KONEKSI ADB OVER WI-FI & TAILSCALE -->
        <div class="panel col-12 owner-only" style="background:#131317; border:1px solid #222228; padding:1rem;">
          <div class="panel-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; margin-bottom:0.75rem;">
            <div style="display:flex; align-items:center; gap:0.5rem;">
              <span style="font-weight:700; color:#f4f4f5; font-size:0.9rem;">Koneksi ADB Wi-Fi (Satu Jaringan) &amp; Tailscale</span>
              <span class="hermes-badge badge-ok">Auto-Discovery Port 5555</span>
            </div>
            <span style="font-size:0.72rem; color:#94a3b8;">Subnet Server: <b style="color:#38bdf8;">172.16.5.0/24 (wlan0)</b></span>
          </div>
          <div style="display:flex; gap:0.6rem; flex-wrap:wrap; align-items:center;">
            <button onclick="autoScanAdbWifi()" id="btnAutoScanAdb" style="background:linear-gradient(135deg, #10b981, #059669); color:#fff; border:none; padding:0.5rem 1.15rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; display:flex; align-items:center; gap:0.4rem; box-shadow:0 3px 10px rgba(16,185,129,0.3); transition:all 0.2s;">
              🔍 Auto-Scan &amp; Sambungkan ADB Wi-Fi
            </button>
            <input type="text" id="adbNetworkTargetInput" placeholder="Atau ketik manual IP HP: contoh 172.16.5.120:5555" style="flex:1; min-width:220px; background:#0b0c0e; color:#a3e635; border:1px solid #334155; padding:0.5rem 0.75rem; font-family:'JetBrains Mono',monospace; font-size:0.78rem; border-radius:6px;" />
            <button onclick="connectAdbNetwork()" id="btnAdbConnect" style="background:#2563eb; color:#fff; border:none; padding:0.5rem 0.95rem; border-radius:6px; font-size:0.75rem; font-weight:600; cursor:pointer;">Sambungkan</button>
            <button onclick="disconnectAdbNetwork()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; padding:0.5rem 0.85rem; border-radius:6px; font-size:0.75rem; cursor:pointer;">Putuskan</button>
            <button onclick="enableActiveDeviceTcpip()" style="background:#202026; color:#fbbf24; border:1px solid #d97706; padding:0.5rem 0.85rem; border-radius:6px; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Jalankan adb tcpip 5555 saat HP tersambung USB">Aktifkan Mode TCP/IP (Port 5555)</button>
          </div>
          <div id="adbNetworkStatusMsg" style="margin-top:0.6rem; font-size:0.75rem; color:#94a3b8; display:none; padding:0.6rem 0.85rem; background:#08080a; border-radius:6px; border:1px solid #22222a; line-height:1.4;"></div>
          <div style="font-size:0.72rem; color:#71717a; margin-top:0.5rem; line-height:1.4;">
            💡 <b>Tips Praktis:</b> Pastikan HP terhubung ke Wi-Fi yang sama dengan server. Cukup klik tombol hijau <b>Auto-Scan &amp; Sambungkan ADB Wi-Fi</b> di atas, sistem akan memindai seluruh subnet dan menyambungkan ADB secara otomatis tanpa perlu cari IP atau ketik perintah manual!
          </div>
        </div>

        <!-- INTERACTIVE LIVE SCREEN & ONLINE CONTROLLER -->
        <div class="panel col-12" id="panel-remote-control" style="background:#131317; border:1px solid #222228;">
          <div class="panel-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; padding-bottom:0.75rem; border-bottom:1px solid #222228;">
            <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
              <span style="font-weight:700; color:#fff;" id="ctrlActiveDeviceTitle">Remote Control: Memuat...</span>
              <select id="ctrlDeviceSwitcher" onchange="selectAndroidDevice(this.value)" style="display:none; background:#1e1e24; color:#38bdf8; border:1px solid #3b82f6; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Pilih perangkat yang ingin dikontrol"></select>
              <span class="hermes-badge badge-blue" id="ctrlActiveDeviceRes" style="font-size:0.7rem;">-- x --</span>
            </div>
            <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
              <select id="selScreenQuality" onchange="changeScreenQuality(this.value)" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.28rem 0.5rem; font-size:0.72rem; font-weight:600; cursor:pointer;" title="Pilih Kualitas Layar">
                <option value="low">⚡ Cepat (Low)</option>
                <option value="medium" selected>✨ Jernih (Medium)</option>
                <option value="high">💎 HD (High)</option>
                <option value="original">Original (Raw)</option>
              </select>
              <label style="display:flex; align-items:center; gap:0.35rem; font-size:0.75rem; color:#94a3b8; cursor:pointer;">
                <input type="checkbox" id="chkAutoStream" onchange="toggleScreenAutoStream()" style="cursor:pointer;" />
                <span>Auto-Stream</span>
              </label>
              <span id="streamFpsBadge" style="font-size:0.68rem; color:#34d399; font-weight:700; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.3); padding:0.15rem 0.45rem; border-radius:4px; display:none;">⚡ 0ms</span>
              <button onclick="refreshDeviceScreen()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Refresh</button>
              <button onclick="toggleAndroidFullscreen()" id="btnFullscreen" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Layar Penuh (Fullscreen Interactive Remote)">Layar Penuh</button>
            </div>
          </div>

          <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:1.25rem; margin-top:1rem; align-items:start;">
            
            <!-- PHONE FRAME & INTERACTIVE CANVAS -->
            <div style="display:flex; flex-direction:column; align-items:center;">
              <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:0.5rem; text-align:center; display:flex; align-items:center; gap:0.35rem;">
                <span>👆 Klik &amp; swap layar</span> &bull; <span>⌨️ Ketik langsung di HP</span>
              </div>
              <div id="phoneBezel" style="position:relative; width:300px; max-width:100%; background:#101014; border:2px solid #27272a; border-radius:28px; padding:10px 8px 12px 8px; box-shadow:0 12px 32px -4px rgba(0,0,0,0.7); user-select:none; transition:border-color 0.2s, box-shadow 0.2s;">
                <!-- Punch-Hole Camera -->
                <div style="width:7px; height:7px; background:#000; border:1.5px solid #27272a; border-radius:50%; margin:0 auto 6px auto;"></div>
                
                <!-- Screen Canvas / Image Container -->
                <div id="screenContainer" tabindex="0" style="position:relative; width:100%; background:#050507; border-radius:18px; overflow:hidden; aspect-ratio:9/19.5; display:flex; align-items:center; justify-content:center; cursor:crosshair; outline:none; transition:box-shadow 0.2s;">
                  <img id="androidScreenImg" src="" alt="Layar Android" style="width:100%; height:100%; object-fit:contain; display:none; image-rendering:-webkit-optimize-contrast;" draggable="false" />
                  <div id="screenLoadingPlaceholder" style="color:#64748b; font-size:0.75rem; text-align:center; padding:1rem;">
                    Pilih perangkat untuk menampilkan layar...
                  </div>
                  <!-- Keyboard Active Floating Badge -->
                  <div id="keyboardActivePill" style="position:absolute; top:8px; left:50%; transform:translateX(-50%); background:rgba(2,132,199,0.9); color:#fff; font-size:0.65rem; font-weight:700; padding:0.18rem 0.6rem; border-radius:12px; pointer-events:none; display:none; z-index:10; backdrop-filter:blur(6px); box-shadow:0 2px 10px rgba(0,0,0,0.5); letter-spacing:0.3px; white-space:nowrap;">⌨️ Keyboard Aktif</div>
                  <!-- Drag Vector / Swipe Trail Canvas / Overlay -->
                  <svg id="swipeTrailSvg" style="position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; display:none; z-index:12;">
                    <line id="swipeTrailLine" x1="0" y1="0" x2="0" y2="0" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" stroke-dasharray="4 2" />
                    <circle id="swipeTrailStart" cx="0" cy="0" r="4" fill="#0284c7" />
                    <circle id="swipeTrailEnd" cx="0" cy="0" r="6" fill="#38bdf8" stroke="#fff" stroke-width="2" />
                  </svg>
                  <!-- Tap Ripple Element -->
                  <div id="tapRipple" style="position:absolute; width:28px; height:28px; border-radius:50%; background:rgba(56,189,248,0.65); border:2px solid #fff; pointer-events:none; transform:translate(-50%, -50%) scale(0); opacity:0; transition:transform 0.22s ease-out, opacity 0.22s ease-out; z-index:11;"></div>
                </div>

                <!-- Bottom Hardware Navigation Bar -->
                <div style="display:flex; justify-content:space-around; align-items:center; margin-top:8px; padding:0 0.25rem;">
                  <button onclick="triggerActiveDeviceAction('back')" style="background:#18181f; color:#f1f5f9; border:1px solid #2e2e38; border-radius:6px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Kembali (Back / Esc)">◀ Back</button>
                  <button onclick="triggerActiveDeviceAction('home')" style="background:#18181f; color:#f59e0b; border:1px solid #2e2e38; border-radius:6px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Home (Home Screen)">● Home</button>
                  <button onclick="triggerActiveDeviceAction('recent')" style="background:#18181f; color:#38bdf8; border:1px solid #2e2e38; border-radius:6px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Recent Apps (Overview)">■ Recent</button>
                  <button onclick="triggerActiveDeviceAction('wake')" style="background:#18181f; color:#ef4444; border:1px solid #2e2e38; border-radius:6px; padding:0.35rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Power / Lock">⏻ Power</button>
                </div>
              </div>

              <!-- Extra Volume & Quick Swipe Keys -->
              <div style="display:flex; flex-direction:column; gap:0.4rem; margin-top:0.75rem; width:100%; max-width:280px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                  <span style="font-size:0.68rem; font-weight:700; color:#71717a; text-transform:uppercase;">Gestur Layar:</span>
                  <div style="display:flex; gap:0.3rem;">
                    <button onclick="triggerActiveDeviceAction('swipe_left')" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Kiri</button>
                    <button onclick="triggerActiveDeviceAction('swipe_right')" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Kanan</button>
                    <button onclick="triggerActiveDeviceAction('swipe_up')" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Atas</button>
                    <button onclick="triggerActiveDeviceAction('swipe_down')" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Bawah</button>
                  </div>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                  <span style="font-size:0.68rem; font-weight:700; color:#71717a; text-transform:uppercase;">Audio:</span>
                  <div style="display:flex; gap:0.35rem;">
                    <button onclick="triggerActiveDeviceAction('volup')" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.65rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Vol +</button>
                    <button onclick="triggerActiveDeviceAction('voldown')" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.65rem; font-size:0.72rem; cursor:pointer; font-weight:600;">Vol -</button>
                  </div>
                </div>
              </div>
            </div>

            <!-- INTERACTIVE CONTROL PANELS -->
            <div style="display:flex; flex-direction:column; gap:1rem;">
              
              <!-- KETIK TEKS ONLINE -->
              <div style="background:#131317; border:1px solid #222228; border-radius:6px; padding:0.85rem;">
                <div style="font-weight:600; color:#38bdf8; font-size:0.8rem; margin-bottom:0.4rem;">
                  Input Teks
                </div>
                <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:0.5rem;">
                  Kirim teks langsung ke kolom input aktif di perangkat Android.
                </div>
                <div style="display:flex; gap:0.5rem;">
                  <input type="text" id="androidTextInput" placeholder="Ketik pesan atau teks..." style="flex:1; background:#0b0c0e; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.6rem; color:#fff; font-size:0.78rem;" onkeydown="if(event.key==='Enter') sendAndroidText();" />
                  <button onclick="sendAndroidText()" style="background:#2563eb; color:#fff; border:none; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.78rem; font-weight:600; cursor:pointer;">Kirim Teks</button>
                </div>
              </div>

              <!-- BUKA URL WEB DI HP -->
              <div style="background:#131317; border:1px solid #222228; border-radius:6px; padding:0.85rem;">
                <div style="font-weight:600; color:#34d399; font-size:0.8rem; margin-bottom:0.4rem;">
                  Buka URL di Browser Android
                </div>
                <div style="display:flex; gap:0.5rem;">
                  <input type="text" id="androidUrlInput" placeholder="https://..." value="https://google.com" style="flex:1; background:#0b0c0e; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.6rem; color:#fff; font-size:0.78rem;" onkeydown="if(event.key==='Enter') openAndroidUrl();" />
                  <button onclick="openAndroidUrl()" style="background:#059669; color:#fff; border:none; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.78rem; font-weight:600; cursor:pointer;">Buka URL</button>
                </div>
              </div>

              <!-- ADB SHELL TERMINAL RUNNER ONLINE -->
              <div style="background:#131317; border:1px solid #222228; border-radius:6px; padding:0.85rem;">
                <div style="font-weight:600; color:#fbbf24; font-size:0.8rem; margin-bottom:0.4rem;">
                  Terminal ADB Shell
                </div>
                <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:0.5rem;">
                  Jalankan perintah ADB shell di perangkat aktif (contoh: <code>dumpsys battery</code>, <code>getprop ro.product.model</code>).
                </div>
                <div style="display:flex; gap:0.5rem; margin-bottom:0.5rem;">
                  <input type="text" id="androidShellCmdInput" placeholder="pm list packages -3" style="flex:1; background:#0b0c0e; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.6rem; color:#a3e635; font-family:'JetBrains Mono', monospace; font-size:0.75rem;" onkeydown="if(event.key==='Enter') runAndroidShellCmd();" />
                  <button onclick="runAndroidShellCmd()" style="background:#d97706; color:#fff; border:none; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.78rem; font-weight:600; cursor:pointer;">Jalankan</button>
                </div>
                <pre id="androidShellOutput" style="background:#090a0f; border:1px solid #1e293b; border-radius:4px; padding:0.5rem 0.65rem; color:#a3e635; font-family:'JetBrains Mono', monospace; font-size:0.7rem; max-height:140px; overflow-y:auto; margin:0; white-space:pre-wrap;">Output perintah shell akan tampil di sini...</pre>
              </div>

            </div>
          </div>
        </div>

        <!-- PANDUAN LENGKAP REMOTE ADB -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Panduan Akses Remote ADB</span>
          </div>
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px,1fr)); gap:1rem; margin-top:0.5rem;">
            
            <div style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem;">
              <div style="color:#ef4444; font-weight:600; font-size:0.825rem; margin-bottom:0.4rem;">Format Akses ADB Socket</div>
              <div style="font-size:0.75rem; color:var(--gf-text-muted); line-height:1.4;">
                Protokol <b>ADB bawaan Google</b> bekerja di <b>Layer 4 TCP Socket</b> murni (hanya menerima format <code>host:port</code>). ADB CLI bukan browser HTTP, sehingga tidak mengenali format URL path seperti <code>westpro.my.id/android/nama_device</code>.
              </div>
            </div>

            <div style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem;">
              <div style="color:#34d399; font-weight:600; font-size:0.825rem; margin-bottom:0.4rem;">Metode 1: Kontrol Web Terintegrasi</div>
              <div style="font-size:0.75rem; color:var(--gf-text-muted); line-height:1.4;">
                Gunakan dashboard di atas. Dari peramban web mana pun lewat <b>https://westpro.my.id/android</b>, Anda dapat berinteraksi langsung dengan layar sentuh, gestur navigasi, input teks, dan perintah shell.
              </div>
            </div>

            <div style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem;">
              <div style="color:#38bdf8; font-weight:600; font-size:0.825rem; margin-bottom:0.4rem;">Metode 2: Wireless ADB di Jaringan Lokal</div>
              <div style="font-size:0.75rem; color:var(--gf-text-muted); line-height:1.4;">
                Aktifkan mode TCP/IP port 5555 melalui kartu di atas, lalu sambungkan terminal laptop pada subnet yang sama dengan perintah:
                <br><code style="color:#a3e635;">adb connect &lt;IP_HP&gt;:5555</code>
              </div>
            </div>

            <div style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem;">
              <div style="color:#fbbf24; font-weight:600; font-size:0.825rem; margin-bottom:0.4rem;">Metode 3: Remote CLI via Cloudflare Tunnel</div>
              <div style="font-size:0.75rem; color:var(--gf-text-muted); line-height:1.4;">
                Jika memerlukan akses terminal di luar jaringan lokal, gunakan client Cloudflare Tunnel:
                <br><code style="color:#a3e635;">cloudflared access tcp --hostname adb.westpro.my.id --url 127.0.0.1:5555</code>
                <br>Lalu: <code style="color:#a3e635;">adb connect 127.0.0.1:5555</code>
              </div>
            </div>

          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 7: KOTAK MASUK OTP ==================== -->
    <div id="page-otp" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.15rem; font-weight: 700; color: #fff;">
          Kotak Masuk OTP & Email IMAP
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Penerimaan email IMAP multi-akun, ekstraksi otomatis kode OTP, serta endpoint API publik.
        </div>
      </div>
      <div class="grafana-grid">

        <!-- KONFIGURASI IMAP (MULTI-AKUN) -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Akun Gmail & IMAP OTP</span>
            <span class="hermes-badge badge-blue" id="mailPollerBadge">Multi-Akun SSL</span>
          </div>

          <!-- DAFTAR AKUN AKTIF -->
          <div style="margin-top:0.5rem;">
            <div style="font-size:0.75rem; font-weight:600; color:#94a3b8; margin-bottom:0.35rem; display:flex; justify-content:space-between; align-items:center;">
              <span>Akun Terdaftar:</span>
              <span id="mailAccountsCount" style="color:#38bdf8; font-size:0.7rem; font-weight:700;">0 / 20 Akun</span>
            </div>
            <div id="mailAccountsList" style="display:flex; flex-direction:column; gap:0.4rem; max-height:220px; overflow-y:auto;">
              <div style="font-size:0.75rem; color:var(--gf-text-muted); text-align:center; padding:0.5rem; background:#141720; border-radius:6px;">Memuat akun...</div>
            </div>
          </div>

          <!-- FORM TAMBAH / UPDATE AKUN -->
          <div style="margin-top:0.85rem; padding-top:0.75rem; border-top:1px solid rgba(255,255,255,0.08); display:flex; flex-direction:column; gap:0.6rem;">
            <div style="font-size:0.75rem; font-weight:700; color:#38bdf8;">Tambah / Perbarui Akun:</div>
            <div>
              <label style="font-size:0.75rem; color:var(--gf-text-muted); display:block; margin-bottom:0.25rem;">Alamat Email Gmail:</label>
              <input type="email" id="mailInputEmail" class="chat-input" style="width:100%; box-sizing:border-box;" placeholder="contoh@gmail.com">
            </div>
            <div>
              <label style="font-size:0.75rem; color:var(--gf-text-muted); display:block; margin-bottom:0.25rem;">Google App Password (16 Karakter):</label>
              <div style="display:flex; gap:0.35rem;">
                <input type="password" id="mailInputPassword" class="chat-input" style="flex:1;" placeholder="xxxx xxxx xxxx xxxx">
                <button type="button" onclick="toggleMailPasswordVisibility()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0 0.6rem; cursor:pointer; font-size:0.75rem;" id="mailTogglePassBtn">Lihat</button>
              </div>
              <div style="font-size:0.7rem; color:var(--gf-text-dim); margin-top:0.3rem;">
                Dibuat di: <i>Google Account → Keamanan → Sandi Aplikasi</i>
              </div>
            </div>
            <div style="display:flex; gap:0.5rem; margin-top:0.25rem;">
              <button onclick="testMailConnection()" class="range-btn" style="background:#202026; color:#38bdf8; border:1px solid #353540; flex:1; padding:0.5rem; border-radius:4px; cursor:pointer; font-weight:600;">Tes Koneksi</button>
              <button onclick="saveMailConfig()" class="range-btn" style="background:#2563eb; color:#fff; border:none; flex:1; padding:0.5rem; border-radius:4px; cursor:pointer; font-weight:600;">Simpan Akun</button>
            </div>
            <div id="mailTestResult" style="display:none; font-size:0.75rem; padding:0.5rem; border-radius:4px; margin-top:0.2rem;"></div>
          </div>
          <!-- ATURAN FILTER & KATA KUNCI OTP -->
          <div style="margin-top:1rem; background:#131317; border:1px solid #222228; border-radius:6px; padding:0.75rem;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <span style="font-size:0.78rem; font-weight:700; color:#38bdf8;">Aturan Filter & Kata Kunci OTP</span>
              <button onclick="toggleOtpRulesPanel()" style="background:none; border:none; color:#38bdf8; font-size:0.75rem; cursor:pointer; font-weight:600;" id="toggleOtpRulesBtn">▼ Buka</button>
            </div>
            <div id="otpRulesContent" style="display:none; flex-direction:column; gap:0.6rem; margin-top:0.6rem;">
              <div>
                <label style="font-size:0.72rem; color:#38bdf8; display:block; margin-bottom:0.2rem; font-weight:600;">Pengirim Diizinkan (Whitelist):</label>
                <input type="text" id="ruleIncludeSenders" class="chat-input" style="width:100%; box-sizing:border-box; font-size:0.72rem;" placeholder="google.com, spesolution.com, bni (kosongkan jika semua)">
                <div style="font-size:0.65rem; color:var(--gf-text-dim); margin-top:0.2rem;">Pisahkan koma. Kosongkan agar menerima dari semua pengirim.</div>
              </div>
              <div>
                <label style="font-size:0.72rem; color:#38bdf8; display:block; margin-bottom:0.2rem; font-weight:600;">Kata Kunci Wajib (Subject / Body):</label>
                <textarea id="ruleIncludeKeywords" class="chat-input" style="width:100%; height:55px; box-sizing:border-box; font-size:0.72rem; resize:vertical;" placeholder="otp, verifikasi, verification, kode, code, token, pin, sandi, bni yap"></textarea>
                <div style="font-size:0.65rem; color:var(--gf-text-dim); margin-top:0.2rem;">Email harus memuat salah satu kata ini (atau pola angka OTP).</div>
              </div>
              <div>
                <label style="font-size:0.72rem; color:#f87171; display:block; margin-bottom:0.2rem; font-weight:600;">Abaikan Pengirim (Blacklist):</label>
                <input type="text" id="ruleExcludeSenders" class="chat-input" style="width:100%; box-sizing:border-box; font-size:0.72rem;" placeholder="newsletter, promo, marketing, spam">
              </div>
              <div>
                <label style="font-size:0.72rem; color:#f87171; display:block; margin-bottom:0.2rem; font-weight:600;">Abaikan Kata Kunci (Blacklist):</label>
                <input type="text" id="ruleExcludeKeywords" class="chat-input" style="width:100%; box-sizing:border-box; font-size:0.72rem;" placeholder="promo, diskon, cashback, webinar, unsubscribe">
              </div>
              <div style="display:flex; gap:0.4rem; margin-top:0.25rem;">
                <button onclick="saveOtpRules()" class="range-btn" style="background:#2563eb; color:#fff; border:none; flex:1; padding:0.45rem; font-size:0.72rem; border-radius:4px; cursor:pointer; font-weight:600;">Simpan Aturan</button>
                <button onclick="resetOtpRulesDefault()" class="range-btn" style="background:#202026; color:#94a3b8; border:1px solid #353540; padding:0.45rem 0.65rem; font-size:0.72rem; border-radius:4px; cursor:pointer;" title="Reset ke Default">Default</button>
              </div>
              <div id="otpRulesSaveResult" style="display:none; font-size:0.7rem; padding:0.4rem; border-radius:4px;"></div>
            </div>
          </div>

          <!-- API Publik Info -->
          <div style="margin-top:1rem; background:#131317; border:1px solid #222228; border-radius:6px; padding:0.75rem;">
            <div style="font-size:0.75rem; font-weight:600; color:#f4f4f5; margin-bottom:0.35rem;">Akses API Publik</div>
            <div style="font-size:0.7rem; color:var(--gf-text-muted); margin-bottom:0.35rem;">Endpoint publik tanpa autentikasi untuk automasi bot atau pipeline pengujian:</div>
            <div style="font-family:monospace; font-size:0.72rem; color:#34d399; word-break:break-all;">/api/otp/public?otp_only=true&amp;limit=10</div>
            <div style="font-size:0.68rem; color:var(--gf-text-dim); margin-top:0.25rem;">Parameter: otp_only, sender, keyword, account, limit (maks 50)</div>
            <button onclick="copyOtpPublicUrl()" style="margin-top:0.5rem; width:100%; background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.3rem; font-size:0.72rem; cursor:pointer;">Salin URL API</button>
          </div>
        </div>

        <!-- INBOX TABLE -->
        <div class="panel col-8">
          <div class="panel-header">
            <div style="display:flex; align-items:center; gap:0.5rem;">
              <span>Kotak Masuk Email</span>
              <span class="hermes-badge badge-ok" style="font-size:0.65rem; padding:0.12rem 0.4rem;">AUTO-SYNC (4s)</span>
            </div>
            <div style="display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap;">
              <span id="otpInboxCount" style="font-size:0.75rem; color:#94a3b8;">0 email</span>
              <button onclick="syncImapInboxNow()" id="btnSyncImap" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.65rem; cursor:pointer; font-size:0.75rem; font-weight:600;" title="Pindai dan tarik email terbaru dari Gmail">Sinkronisasi</button>
              <button onclick="loadOtpInbox()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Refresh</button>
              <button onclick="clearOtpInbox()" style="background:rgba(239,68,68,0.12); color:#f87171; border:1px solid rgba(239,68,68,0.25); border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;" title="Hapus semua email di kotak masuk">Bersihkan</button>
            </div>
          </div>

          <!-- FILTER BAR -->
          <div style="display:flex; flex-wrap:wrap; gap:0.5rem; align-items:center; margin-bottom:0.75rem; background:#131317; border:1px solid #222228; border-radius:6px; padding:0.6rem 0.75rem;">
            <div style="display:flex; align-items:center; gap:0.35rem; flex:1; min-width:160px;">
              <span style="font-size:0.72rem; color:var(--gf-text-muted); white-space:nowrap;">Pencarian:</span>
              <input type="text" id="otpFilterKeyword" class="chat-input" style="flex:1; padding:0.3rem 0.5rem; font-size:0.75rem;" placeholder="cari subjek, pengirim..." oninput="loadOtpInbox()">
            </div>
            <div style="display:flex; align-items:center; gap:0.35rem;">
              <span style="font-size:0.72rem; color:var(--gf-text-muted);">Tampilkan:</span>
              <select id="otpFilterMode" onchange="loadOtpInbox()" style="background:#14161b; color:#e2e8f0; border:1px solid #334155; border-radius:4px; padding:0.3rem 0.5rem; font-size:0.75rem; cursor:pointer;">
                <option value="all">Semua Email</option>
                <option value="otp" selected>Hanya OTP</option>
              </select>
            </div>
            <div style="display:flex; align-items:center; gap:0.35rem;">
              <span style="font-size:0.72rem; color:var(--gf-text-muted);">Maks:</span>
              <select id="otpFilterLimit" onchange="loadOtpInbox()" style="background:#14161b; color:#e2e8f0; border:1px solid #334155; border-radius:4px; padding:0.3rem 0.5rem; font-size:0.75rem; cursor:pointer;">
                <option value="20">20</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
            </div>
            <div style="font-size:0.72rem; color:var(--gf-text-muted);" id="otpInboxCount">-- email</div>
          </div>

          <!-- INBOX LIST -->
          <div id="otpInboxList" style="display:flex; flex-direction:column; gap:0.4rem; max-height:480px; overflow-y:auto;">
            <div style="color:var(--gf-text-muted); font-size:0.85rem; padding:2rem; text-align:center;">Memuat kotak masuk...</div>
          </div>
        </div>

        <!-- MULTI-PILIH & SELECTED OTP PANEL -->
        <div class="panel col-12" id="otpSelectedPanel" style="display:none;">
          <div class="panel-header">
            <span>OTP Terpilih</span>
            <button onclick="clearOtpSelection()" style="background:#202026; color:#f87171; border:1px solid #353540; border-radius:4px; padding:0.2rem 0.5rem; cursor:pointer; font-size:0.75rem;">Bersihkan Pilihan</button>
          </div>
          <div id="otpSelectedList" style="display:flex; flex-wrap:wrap; gap:0.5rem; padding:0.5rem 0;"></div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 4: NOTULEN MEETING AI ==================== -->
    <div id="page-meeting" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.15rem; font-weight: 700; color: #fff;">
          Notulen Rapat & Transkripsi Audio
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Perekaman suara mikrofon lokal, transkripsi real-time, dan rangkuman notulen rapat via model LLM.
        </div>
      </div>
      <div class="grafana-grid">
        <!-- MEETING CONTROLS (disederhanakan: judul + rekam/unggah + analisa; sisanya dilipat) -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Rekam atau Unggah Rapat</span>
            <span class="hermes-badge badge-blue" id="meetingStatusBadge">SIAP</span>
          </div>

          <!-- BARIS UTAMA: judul, jadwal, timer, tombol rekam -->
          <div style="display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; margin-top: 0.5rem;">
            <input type="text" id="meetingTitleInput" class="chat-input" style="flex: 1; min-width: 220px;" placeholder="Judul Meeting" value="Meeting Koordinasi" oninput="renderMeetingContext()">
            <select id="meetingScheduleSelect" class="chat-input" style="max-width: 260px; color: #38bdf8; font-weight: 500;" onchange="onSelectScheduledMeeting(this.value)" title="Isi judul otomatis dari jadwal SPEctrum">
              <option value="">Jadwal SPEctrum...</option>
            </select>
            <div style="font-family: 'JetBrains Mono', monospace; font-size: 1.05rem; font-weight: 700; color: #38bdf8; min-width: 82px; text-align: center;" id="meetingTimerDisplay">00:00:00</div>
            <button id="btnStartMeeting" class="chat-btn" style="background: #2563eb;" onclick="startMeetingRecording()">● Mulai Merekam</button>
            <button id="btnStopMeeting" class="chat-btn" style="background: #ef4444; display: none;" onclick="stopMeetingRecording()">■ Selesai & Rangkum</button>
            <button class="chat-btn" style="background: #202026; color: #f4f4f5; border: 1px solid #353540;" onclick="clearMeetingData()">Reset</button>
          </div>

          <!-- BARIS UNGGAH & ANALISA -->
          <div style="margin-top: 0.6rem; padding: 0.5rem 0.75rem; background: #14161b; border: 1px dashed #353540; border-radius: 8px; display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
            <input type="file" id="meetingAudioFile" accept="audio/*,video/mp4,video/webm,.wav,.mp3,.m4a,.aac,.ogg,.opus,.flac,.webm,.mp4" style="display:none;" onchange="uploadMeetingAudio(this)">
            <button id="btnUploadMeetingAudio" onclick="document.getElementById('meetingAudioFile').click()" style="background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); border-radius: 6px; padding: 0.35rem 0.8rem; font-size: 0.75rem; font-weight: 600; cursor: pointer;">📁 Unggah Rekaman</button>
            <button id="btnAnalyzeMeeting" onclick="analyzeMeeting()" style="background: rgba(168,85,247,0.15); color: #c084fc; border: 1px solid rgba(168,85,247,0.4); border-radius: 6px; padding: 0.35rem 0.8rem; font-size: 0.75rem; font-weight: 600; cursor: pointer;">✨ Analisa &amp; Susun Notulen</button>
            <span style="font-size: 0.7rem; color: #64748b;">WAV/MP3/M4A/WEBM/OGG, maks 120 MB</span>
            <span id="uploadTranscribeStatus" style="font-size: 0.72rem; color: #38bdf8; margin-left: auto;"></span>
          </div>

          <!-- PENGATURAN (dilipat) -->
          <details class="mt-fold">
            <summary>⚙️ Pengaturan: sumber audio, bahasa, model AI, dasar analisa</summary>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 0.6rem; padding: 0.6rem 0.2rem 0.2rem;">
              <label class="mt-field">Sumber audio
                <select id="meetingAudioSourceSelect" class="chat-input" style="color: #38bdf8;" title="Mikrofon, suara meeting laptop, atau keduanya">
                  <option value="both" selected>🎙️+💻 Mic + Audio Laptop (Meet/Zoom)</option>
                  <option value="mic">🎤 Mikrofon Saja (Tatap Muka)</option>
                  <option value="system">💻 Audio Laptop/Tab Saja</option>
                </select>
              </label>
              <label class="mt-field">Bahasa
                <select id="meetingLangSelect" class="chat-input" title="Pilih campuran bila rapat memakai banyak istilah teknis bahasa Inggris">
                  <option value="mix" selected>Indonesia + English (istilah teknis)</option>
                  <option value="id-ID">Indonesia saja</option>
                  <option value="en-US">English saja</option>
                </select>
              </label>
              <label class="mt-field">Model AI
                <select id="meetingModelSelect" class="chat-input" style="color: #38bdf8;">
                  <option value="">Default (Sesuai Hermes)</option>
                </select>
              </label>
              <label class="mt-field">Dasar analisa
                <select id="meetingAnalysisBasis" class="chat-input" style="color: #c084fc;" title="Transkrip: audio diubah ke teks dulu. Audio langsung: rekaman dikirim utuh ke AI multimodal (menangkap nada/penekanan).">
                  <option value="audio" selected>Rekaman audio langsung</option>
                  <option value="transcript">Transkrip (teks)</option>
                </select>
              </label>
            </div>
          </details>

          <!-- KONTEKS MEETING: sprint & task milik user yang terkait meeting ini (dilipat) -->
          <details class="mt-fold" id="meetingContextFold">
            <summary>📎 Konteks meeting: sprint &amp; task terkait <span id="meetingContextCount" class="hermes-badge badge-blue" style="font-size: 0.62rem;">0 task</span></summary>
            <div id="sprintContextBar" style="padding: 0.5rem 0.1rem 0.2rem; display: flex; flex-direction: column; gap: 0.5rem;">
              <div style="display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
                <span style="font-size: 0.68rem; color: #71717a; text-transform: uppercase; font-weight: 600;">Sprint</span>
                <span id="meetingSprintBadge" class="hermes-badge badge-blue" style="font-weight: 600;">Sprint Berjalan</span>
                <span id="meetingSprintGoal" style="font-size: 0.75rem; color: #94a3b8;">Fokus sprint aktif</span>
              </div>
              <div style="font-size: 0.68rem; color: #71717a;">Task di bawah ini ikut dikirim ke AI sebagai rujukan agar notulen menyebut kode task yang relevan (task Anda ditandai kuning). Mengikuti judul/jadwal meeting yang dipilih.</div>
              <div id="meetingContextTasks" style="display: flex; flex-wrap: wrap; gap: 0.35rem;"></div>
              <div id="meetingOwnerTcTools" style="display: none; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                <button onclick="openSprintTestCasesModal()" class="chat-btn" style="padding: 0.2rem 0.55rem; font-size: 0.72rem; background: #1e293b; color: #38bdf8; border: 1px solid #38bdf8;">📋 Lihat Test Case di Sheet</button>
                <a href="https://docs.google.com/spreadsheets/d/1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs/edit" target="_blank" style="font-size: 0.72rem; color: #34d399; text-decoration: none; background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25); padding: 0.2rem 0.5rem; border-radius: 4px;">📊 Google Sheet</a>
              </div>
            </div>
          </details>

          <div id="speechEngineInfo" style="margin-top: 0.5rem; padding: 0.35rem 0.65rem; background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; font-size: 0.75rem; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="speechEngineBadge" class="hermes-badge badge-blue">Mendeteksi Fitur Audio Browser...</span>
              <span id="speechEngineDetail" style="color: var(--gf-text-muted);">Memeriksa dukungan Web Speech API & perekaman mikrofon...</span>
            </div>
            <div id="audioWaveIndicator" style="display: none; align-items: center; gap: 0.35rem; color: #ef4444; font-weight: 600;">
              <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444;"></span>
              <span id="audioWaveText">Audio Mikrofon Aktif</span>
            </div>
          </div>
        </div>

        <!-- TRANSCRIPT LIVE BOX -->
        <div class="panel col-6">
          <div class="panel-header">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span>Transkrip Suara Real-time</span>
              <span id="transcriptEngineBadge" class="hermes-badge badge-blue" style="font-size: 0.65rem;">STT</span>
            </div>
            <div style="display: flex; gap: 0.35rem; align-items: center; flex-wrap: wrap;">
              <button id="btnPolishTranscript" onclick="polishMeetingTranscript()" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); border-radius: 4px; padding: 0.2rem 0.6rem; cursor: pointer; font-size: 0.72rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.35rem;" title="Koreksi kata salah dengar fonetik (misal 'haid'->'hide', 'penari'->'PNRI'), tambah tanda baca, dan rapikan paragraf via AI Hermes">
                ✨ Koreksi & Rapikan (AI)
              </button>
              <button onclick="clearMeetingTranscript()" style="background: #202026; color: #94a3b8; border: 1px solid #353540; border-radius: 4px; padding: 0.2rem 0.5rem; cursor: pointer; font-size: 0.72rem;" title="Bersihkan kotak transkrip">
                Bersihkan
              </button>
            </div>
          </div>
          <textarea id="meetingTranscriptText" class="chat-input" style="width: 100%; height: 420px; resize: vertical; line-height: 1.6; font-size: 0.85rem;" placeholder="Ketika tombol 'Mulai Merekam' ditekan, suara pembicaraan dari mikrofon akan otomatis diubah menjadi teks di sini. Anda juga dapat mengedit teks ini secara manual atau menekan tombol 'Koreksi & Rapikan (AI)'."></textarea>
          
          <!-- LIVE INTERIM SPEECH INDICATOR -->
          <div id="liveSpeechInterimBox" style="display: none; margin-top: 0.4rem; padding: 0.35rem 0.65rem; background: rgba(56, 189, 248, 0.08); border: 1px dashed rgba(56, 189, 248, 0.35); border-radius: 6px; font-size: 0.78rem; color: #38bdf8;">
            <span style="font-style: italic;">🎙️ Sedang berbicara: </span><span id="liveSpeechInterimText" style="font-weight: 500;">...</span>
          </div>

          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; font-size: 0.75rem;">
            <label style="color: #94a3b8; display: inline-flex; align-items: center; gap: 0.4rem; cursor: pointer;" title="Otomatis jalankan AI pembersih fonetik saat Anda menekan tombol Selesai & Rangkum">
              <input type="checkbox" id="chkAutoPolishTranscript" checked style="accent-color: #a855f7;">
              <span style="font-weight: 500; color: #e2e8f0;">Auto-Koreksi AI saat Selesai Merekam</span>
            </label>
            <span id="transcriptStatusText" style="color: #64748b; font-size: 0.7rem;">Mendukung mikrofon laptop, HP, atau USB.</span>
          </div>
        </div>

        <!-- SUMMARY RESULT BOX -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Notulen & Ringkasan Rapat</span>
            <div style="display: flex; gap: 0.4rem; flex-wrap: wrap;">
              <button onclick="copyMeetingSummary()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Salin</button>
              <button onclick="downloadMeetingMarkdown()" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Unduh MD</button>
              <button onclick="downloadMeetingText()" style="background:#202026; color:#34d399; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Unduh TXT</button>
            </div>
          </div>
          <div id="meetingSummaryOutput" style="background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; padding: 1.25rem; height: 420px; overflow-y: auto; line-height: 1.6; font-size: 0.875rem;">
            <div style="color: var(--gf-text-muted); text-align: center; margin-top: 5rem;">
              Tekan <b>"Selesai & Rangkum"</b> setelah rapat untuk menghasilkan notulen rapi yang merangkum poin diskusi, keputusan, dan tindak lanjut.
            </div>
          </div>
        </div>

        <!-- PANEL: EKSTRAKSI & APPROVAL TEST CASE KE SPREADSHEET (col-12) -->
        <details class="panel col-12 mt-fold-panel" id="meetingTcPanel" style="margin-top: 0.5rem;">
          <summary>
            <span>📋 Rekomendasi Test Case dari Rapat (Pilih &amp; Setujui)</span>
            <span id="meetingTcCountBadge" class="hermes-badge badge-blue" style="font-size: 0.65rem;">0 Skenario</span>
          </summary>
          <div class="panel-header" style="flex-wrap: wrap; gap: 0.5rem; margin-top: 0.6rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;"></div>
            <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 0.4rem; background: #131418; border: 1px solid #2d3340; border-radius: 6px; padding: 0.2rem 0.5rem; font-size: 0.75rem;" title="Nama Tab Spreadsheet (Otomatis dipisah per Project &amp; Sprint)">
                <span style="color: #94a3b8; font-size: 0.7rem;">Tab Sheet:</span>
                <input type="text" id="targetSheetTabName" style="background: transparent; border: none; color: #38bdf8; font-size: 0.75rem; font-weight: 600; width: 175px; outline: none;" value="" placeholder="Project - Sprint">
              </div>
              <button id="btnExtractTcMeeting" onclick="extractTcFromCurrentMeeting()" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 500; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
                <span id="extractTcSpinner" style="display: none; animation: blink 1s infinite;">●</span> 🔍 Ekstrak TC dari Rapat
              </button>
              <button id="btnApproveAndExportTc" onclick="approveAndExportSelectedTc()" style="background: rgba(16, 185, 129, 0.18); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; display: none;" title="Kirim skenario yang dicentang ke Google Sheet">
                ✓ Setujui &amp; Kirim ke Google Sheet (<span id="selectedTcCount">0</span>)
              </button>
              <a href="https://docs.google.com/spreadsheets/d/1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs/edit" target="_blank" style="background: #202026; color: #a1a1aa; border: 1px solid #2d2d35; border-radius: 6px; padding: 0.35rem 0.65rem; font-size: 0.75rem; text-decoration: none; display: flex; align-items: center; gap: 0.3rem;">
                📊 Buka Sheet
              </a>
            </div>
          </div>

          <div style="font-size: 0.78rem; color: var(--gf-text-muted); margin-top: 0.25rem; margin-bottom: 0.75rem;">
            Pilih (centang) skenario test case hasil analisis rapat yang disetujui. Tab sheet di spreadsheet otomatis dipisah dan dibuatkan sesuai Project &amp; Sprint.
          </div>

          <!-- TABEL CHECKLIST TEST CASE -->
          <div style="overflow-x: auto; max-height: 420px;">
            <table class="raw-table">
              <thead>
                <tr>
                  <th style="width: 40px; text-align: center;">
                    <input type="checkbox" id="checkAllTc" onchange="toggleSelectAllTc(this.checked)" checked title="Pilih Semua">
                  </th>
                  <th style="width: 100px; text-align: center;">Aksi</th>
                  <th style="width: 85px;">ID</th>
                  <th>Test Case (Skenario)</th>
                  <th style="width: 25%;">Test Steps (Langkah)</th>
                  <th style="width: 25%;">Expected Result</th>
                  <th>Catatan Rapat</th>
                  <th style="width: 75px; text-align: center;">Status</th>
                </tr>
              </thead>
              <tbody id="extractedTcTableBody">
                <tr>
                  <td colspan="8" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">
                    Belum ada ekstraksi test case. Klik tombol <b>"🔍 Ekstrak TC dari Rapat"</b> atau pilih <b>"Ekstrak TC"</b> di tabel arsip meeting di bawah.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>

        <!-- PAST MEETINGS ARCHIVE -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Arsip Riwayat Notulen (Database SQLite)</span>
            <button onclick="loadPastMeetings()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Refresh</button>
          </div>
          <div style="overflow-x: auto; max-height: 300px;">
            <table class="raw-table">
              <thead>
                <tr>
                  <th>Tanggal & Waktu</th>
                  <th>Judul Meeting</th>
                  <th>Durasi</th>
                  <th>Panjang Transkrip</th>
                  <th>Aksi</th>
                </tr>
              </thead>
              <tbody id="pastMeetingsTableBody">
                <tr><td colspan="5" style="text-align: center; color: var(--gf-text-muted);">Memuat arsip meeting...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- MODAL VIEWER TEST CASE BERJALAN -->
      <div id="modalSprintTestCases" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 9999; backdrop-filter: blur(4px); align-items: center; justify-content: center; padding: 1.5rem;">
        <div style="background: #18181b; border: 1px solid #3f3f46; border-radius: 8px; width: 100%; max-width: 1100px; max-height: 85vh; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.5);">
          <div style="padding: 0.85rem 1.25rem; border-bottom: 1px solid #27272a; display: flex; justify-content: space-between; align-items: center; background: #141417;">
            <div style="display: flex; align-items: center; gap: 0.75rem;">
              <span id="modalSprintTcHeaderTitle" style="font-weight: 700; font-size: 0.95rem; color: #f4f4f5;">Daftar Skenario Test Case Berjalan</span>
              <span id="modalTcCountBadge" class="hermes-badge badge-blue">Memuat...</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.6rem;">
              <input type="text" id="modalTcSearchInput" class="chat-input" style="max-width: 260px; font-size: 0.75rem; padding: 0.25rem 0.6rem;" placeholder="Cari ID / Modul / Judul..." oninput="filterSprintTestCases(this.value)">
              <button class="chat-btn" style="padding: 0.25rem 0.6rem; font-size: 0.75rem; background: #27272a; color: #e4e4e7;" onclick="closeSprintTestCasesModal()">Tutup</button>
            </div>
          </div>
          <div style="overflow-y: auto; padding: 0.75rem; flex: 1;">
            <table class="raw-table" style="width: 100%; font-size: 0.75rem;">
              <thead>
                <tr>
                  <th style="width: 75px;">ID</th>
                  <th style="width: 110px;">Modul</th>
                  <th style="width: 140px;">Section</th>
                  <th>Judul Skenario Test Case</th>
                  <th style="width: 70px;">Tipe</th>
                  <th>Hasil yang Diharapkan (Expected)</th>
                  <th style="width: 80px; text-align: center;">Status DEV</th>
                </tr>
              </thead>
              <tbody id="modalSprintTcTableBody">
                <tr><td colspan="7" style="text-align: center; color: #71717a; padding: 2rem;">Memuat daftar test case...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 5: SISTEM 24/7 & RESOURCE SERVER MONITOR ==================== -->
    <div id="page-system" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <h1 style="font-size: 1.2rem; font-weight: 700; color: #fff; margin: 0;">
              Status Sistem &amp; Resource Server
            </h1>
            <span class="hermes-badge badge-ok" id="sysServerLiveBadge">24/7 ONLINE</span>
            <span class="hermes-badge badge-blue">FANLESS M90n</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">
            Monitoring real-time CPU, RAM, NVMe SSD, suhu perangkat keras, status service systemd, dan pipeline auto-deploy GitHub.
          </div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center;">
          <button onclick="fetchSystemResources(true); fetchDeployStatus();" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; border-radius: 4px; padding: 0.4rem 0.75rem; font-size: 0.75rem; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;">
            🔄 Refresh Resource
          </button>
        </div>
      </div>

      <!-- LAYANAN EMAIL (SMTP keluar) -->
      <div class="panel" id="emailServiceCard" style="margin-bottom: 1.25rem; background: #14151a; border: 1px solid #272832; border-radius: 10px; padding: 1rem 1.15rem;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:0.6rem; flex-wrap:wrap;">
          <div style="display:flex; align-items:center; gap:0.55rem;">
            <span style="font-weight:700; color:#f4f4f5; font-size:0.95rem;">✉️ Layanan Email (SMTP keluar)</span>
            <span id="emailSvcBadge" class="hermes-badge badge-warn" style="font-size:0.65rem;">Memuat...</span>
          </div>
          <div style="display:flex; gap:0.45rem; align-items:center; flex-wrap:wrap;">
            <input id="emailTestTo" class="ws-search" type="email" placeholder="Kirim uji ke (kosong = email Anda)" style="width:240px;">
            <button id="btnEmailTest" onclick="sendTestEmail()" class="hb-btn hb-btn-primary">Kirim email uji</button>
          </div>
        </div>
        <div id="emailSvcInfo" style="margin-top:0.6rem; font-size:0.75rem; color:#94a3b8; line-height:1.6;"></div>
        <div id="emailTestResult" style="display:none; margin-top:0.5rem; font-size:0.75rem; padding:0.5rem 0.7rem; border-radius:8px;"></div>
        <details class="mt-fold" style="margin-top:0.6rem;">
          <summary>Riwayat pengiriman terakhir</summary>
          <div id="emailSvcLogs" style="padding:0.5rem 0.1rem 0.2rem; font-size:0.72rem; color:#a1a1aa; font-family:'JetBrains Mono', monospace;"></div>
        </details>
      </div>

      <!-- 4 REAL-TIME METRIC CARDS -->
      <div class="grafana-grid" style="margin-bottom: 1.25rem;">
        <!-- METRIC 1: CPU LOAD & TEMP -->
        <div class="panel col-3" style="background: #14151a; border: 1px solid #272832; border-radius: 8px; padding: 1rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
            <span style="font-size: 0.75rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">Beban CPU</span>
            <span id="sysCpuTempBadge" class="hermes-badge badge-blue" style="font-size: 0.7rem; padding: 0.1rem 0.4rem;">-- °C</span>
          </div>
          <div style="display: flex; align-items: baseline; gap: 0.4rem;">
            <span id="sysCpuPct" style="font-size: 1.85rem; font-weight: 800; color: #38bdf8; font-family: 'JetBrains Mono', monospace;">--</span>
            <span style="font-size: 1rem; color: #64748b; font-weight: 700;">%</span>
          </div>
          <div style="width: 100%; height: 6px; background: #22232e; border-radius: 3px; overflow: hidden; margin: 0.6rem 0 0.5rem 0;">
            <div id="sysCpuBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #38bdf8, #0284c7); transition: width 0.4s ease;"></div>
          </div>
          <div style="font-size: 0.7rem; color: #94a3b8; display: flex; justify-content: space-between;">
            <span>Freq: <b id="sysCpuFreq" style="color: #cbd5e1;">--</b> MHz</span>
            <span>Load: <b id="sysLoadAvg" style="color: #cbd5e1;">--</b></span>
          </div>
        </div>

        <!-- METRIC 2: RAM USAGE -->
        <div class="panel col-3" style="background: #14151a; border: 1px solid #272832; border-radius: 8px; padding: 1rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
            <span style="font-size: 0.75rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">Memori RAM</span>
            <span id="sysMemFreeText" style="font-size: 0.7rem; color: #10b981; font-weight: 600;">-- MB Bebas</span>
          </div>
          <div style="display: flex; align-items: baseline; gap: 0.4rem;">
            <span id="sysMemPct" style="font-size: 1.85rem; font-weight: 800; color: #10b981; font-family: 'JetBrains Mono', monospace;">--</span>
            <span style="font-size: 1rem; color: #64748b; font-weight: 700;">%</span>
          </div>
          <div style="width: 100%; height: 6px; background: #22232e; border-radius: 3px; overflow: hidden; margin: 0.6rem 0 0.5rem 0;">
            <div id="sysMemBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #10b981, #059669); transition: width 0.4s ease;"></div>
          </div>
          <div style="font-size: 0.7rem; color: #94a3b8; display: flex; justify-content: space-between;">
            <span>Terpakai: <b id="sysMemUsed" style="color: #cbd5e1;">--</b> MB</span>
            <span>Total: <b id="sysMemTotal" style="color: #cbd5e1;">--</b> MB</span>
          </div>
        </div>

        <!-- METRIC 3: STORAGE NVME -->
        <div class="panel col-3" style="background: #14151a; border: 1px solid #272832; border-radius: 8px; padding: 1rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
            <span style="font-size: 0.75rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">NVMe SSD Disk</span>
            <span id="sysNvmeTempBadge" class="hermes-badge badge-blue" style="font-size: 0.7rem; padding: 0.1rem 0.4rem;">-- °C</span>
          </div>
          <div style="display: flex; align-items: baseline; gap: 0.4rem;">
            <span id="sysDiskPct" style="font-size: 1.85rem; font-weight: 800; color: #a855f7; font-family: 'JetBrains Mono', monospace;">--</span>
            <span style="font-size: 1rem; color: #64748b; font-weight: 700;">%</span>
          </div>
          <div style="width: 100%; height: 6px; background: #22232e; border-radius: 3px; overflow: hidden; margin: 0.6rem 0 0.5rem 0;">
            <div id="sysDiskBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #a855f7, #7c3aed); transition: width 0.4s ease;"></div>
          </div>
          <div style="font-size: 0.7rem; color: #94a3b8; display: flex; justify-content: space-between;">
            <span>Terpakai: <b id="sysDiskUsed" style="color: #cbd5e1;">--</b> GB</span>
            <span>Bebas: <b id="sysDiskFree" style="color: #cbd5e1;">--</b> GB</span>
          </div>
        </div>

        <!-- METRIC 4: UPTIME & JARINGAN -->
        <div class="panel col-3" style="background: #14151a; border: 1px solid #272832; border-radius: 8px; padding: 1rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
            <span style="font-size: 0.75rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">Uptime Server</span>
            <span class="hermes-badge badge-ok" style="font-size: 0.7rem; padding: 0.1rem 0.4rem;">24/7 ONLINE</span>
          </div>
          <div style="display: flex; align-items: baseline; gap: 0.4rem;">
            <span id="sysUptime" style="font-size: 1.25rem; font-weight: 800; color: #f59e0b; font-family: 'JetBrains Mono', monospace; line-height: 1.85rem;">--</span>
          </div>
          <div style="font-size: 0.72rem; color: #94a3b8; margin-top: 0.85rem; display: flex; flex-direction: column; gap: 0.2rem;">
            <div>IP Lokal: <b id="sysNetworkIp" style="color: #38bdf8;">--</b></div>
            <div>Domain: <b style="color: #34d399;">westpro.my.id (HTTPS)</b></div>
          </div>
        </div>
      </div>

      <!-- MAIN PANELS: SPESIFIKASI HARDWARE & STATUS SERVICE SYSTEMD -->
      <div class="grafana-grid">
        <!-- PANEL KIRI: SPESIFIKASI MESIN -->
        <div class="panel col-6" style="background: #131418; border: 1px solid #27272e; border-radius: 8px; padding: 1.25rem;">
          <div class="panel-header" style="border-bottom: 1px solid #22222a; padding-bottom: 0.75rem; margin-bottom: 1rem;">
            <span style="font-weight: 700; color: #fff;">Spesifikasi Mesin ThinkCentre M90n</span>
            <span style="color: #38bdf8; font-size: 0.72rem; font-weight: 600;">FANLESS INDUSTRIAL PC</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem;">
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Processor (CPU):</span>
              <b style="color: #fff;">Intel Celeron 4205U @ 1.80GHz (2 Cores, 2T)</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Memori Fisik (RAM):</span>
              <b style="color: #fff;">3.9 GB Low-Power DDR4</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Penyimpanan (SSD):</span>
              <b style="color: #fff;">128 GB M.2 NVMe PCIe SSD</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Sistem Operasi:</span>
              <b style="color: #fff;">Kali GNU/Linux Rolling x86_64</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Waktu Boot Terakhir:</span>
              <b id="sysBootTime" style="color: #cbd5e1;">--</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between;">
              <span style="color: #94a3b8;">Mode Server 24/7:</span>
              <b style="color: #34d399;">Linger=yes, Sleep/Suspend Masked</b>
            </div>
          </div>
        </div>

        <!-- PANEL KANAN: STATUS SYSTEMD LIVE MONITOR -->
        <div class="panel col-6" style="background: #131418; border: 1px solid #27272e; border-radius: 8px; padding: 1.25rem;">
          <div class="panel-header" style="border-bottom: 1px solid #22222a; padding-bottom: 0.75rem; margin-bottom: 1rem;">
            <span style="font-weight: 700; color: #fff;">Status Service Systemd (Live)</span>
            <span id="sysServicesSummaryBadge" class="hermes-badge badge-ok">ALL RUNNING</span>
          </div>
          <div id="sysServicesList" style="display: flex; flex-direction: column; gap: 0.65rem;">
            <!-- Populated dynamically via JS -->
            <div style="color: #71717a; font-size: 0.8rem;">Memeriksa status service...</div>
          </div>
        </div>

        <!-- AUTO-DEPLOY CI/CD PANEL -->
        <div class="panel col-12" style="margin-top: 1rem; background: #131418; border: 1px solid #27272e; border-radius: 8px; padding: 1.25rem;">
          <div class="panel-header" style="border-bottom: 1px solid #22222a; padding-bottom: 0.75rem; margin-bottom: 1rem;">
            <span style="font-weight: 700; color: #fff;">Otomasi CI/CD &amp; Auto-Deploy (GitHub origin/main)</span>
            <span class="hermes-badge badge-ok" id="deployBadgeHeader">AKTIF (Dual Mode)</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem;">
            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid #222228; border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">GitHub Webhook (Instan &lt; 3 Detik)</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-bottom: 0.5rem;">
                Setiap kali ada push / merge PR ke branch <code>main</code>, GitHub Action otomatis memanggil endpoint webhook server:
              </div>
              <div style="display: flex; gap: 0.5rem; align-items: center;">
                <input type="text" readonly value="https://westpro.my.id/api/webhook/github" style="width: 100%; background: #14161b; color: #34d399; border: 1px solid #334155; padding: 0.35rem 0.5rem; font-family: monospace; font-size: 0.75rem; border-radius: 4px;" id="webhookUrlInput">
                <button onclick="copyWebhookUrl()" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; padding: 0.35rem 0.65rem; border-radius: 4px; cursor: pointer; font-size: 0.75rem; white-space: nowrap;">Salin URL</button>
              </div>
            </div>

            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid #222228; border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">Background Watcher (Failsafe)</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-bottom: 0.5rem;">
                Service <code>deploy-watcher.timer</code> otomatis memeriksa commit baru di <code>origin/main</code> setiap <b>2 menit</b>. Tanpa webhook pun, server tetap auto-deploy.
              </div>
              <div style="font-size: 0.75rem; color: #34d399;">
                Status: <b>deploy-watcher.timer Running (Active)</b>
              </div>
            </div>

            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid #222228; border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">Status Deploy Terakhir</div>
              <div style="font-size: 0.75rem; margin-bottom: 0.25rem;">Commit: <b id="deployCommitText" style="color: #fff;">--</b></div>
              <div style="font-size: 0.75rem; margin-bottom: 0.25rem;">Waktu: <b id="deployTimeText" style="color: var(--gf-text-muted);">--</b></div>
              <div style="font-size: 0.75rem; margin-bottom: 0.5rem;">Pesan: <i id="deployMsgText" style="color: #94a3b8;">--</i></div>
              <button id="btnManualDeploy" onclick="triggerManualDeploy()" style="background: #2563eb; color: #fff; font-weight: 600; border: none; border-radius: 4px; padding: 0.4rem 0.75rem; cursor: pointer; font-size: 0.75rem;">
                Deploy Sekarang (Origin/Main)
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 8: WEBHOOK & API MONITOR (BEECEPTOR STYLE) ==================== -->
    <div id="page-webhook" class="view-section">
      <!-- HEADER / INTRO -->
      <div class="page-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem; margin-bottom:1.25rem;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <h2 style="font-size:1.15rem; font-weight:700; color:#fff; margin:0;">Webhook & API Monitor</h2>
            <span class="hermes-badge badge-ok" id="whLiveIndicator">LIVE</span>
          </div>
          <div style="font-size:0.8rem; color:var(--gf-text-muted); margin-top:0.25rem;">
            Inspeksi permintaan webhook masuk, payload JSON, header, query parameter, dan simulasi respons HTTP.
          </div>
        </div>
        <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
          <button onclick="openMockRulesModal()" style="background:#202026; color:#fbbf24; border:1px solid #d97706; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.75rem; font-weight:600; cursor:pointer;">
            Aturan Mock (<span id="whRulesCount">0</span>)
          </button>
          <button onclick="toggleWebhookStreamPause()" id="btnWhPauseStream" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.4rem 0.75rem; font-size:0.75rem; cursor:pointer;">
            Jeda
          </button>
          <button onclick="loadWebhookRequests(true)" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.4rem 0.75rem; font-size:0.75rem; cursor:pointer;">
            Refresh
          </button>
          <button onclick="clearAllWebhookRequests()" style="background:rgba(239,68,68,0.12); color:#f87171; border:1px solid rgba(239,68,68,0.25); border-radius:4px; padding:0.4rem 0.75rem; font-size:0.75rem; cursor:pointer;">
            Bersihkan
          </button>
        </div>
      </div>

      <!-- ENDPOINT INFO CARD -->
      <div class="panel col-12" style="background:#131317; border:1px solid #222228; padding:0.85rem 1rem; margin-bottom:1.25rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
          <div style="flex:1; min-width:280px;">
            <div style="font-size:0.72rem; font-weight:700; color:#38bdf8; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.35rem;">
              URL Webhook Publik (GET, POST, PUT, DELETE, PATCH):
            </div>
            <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
              <input type="text" id="whActiveEndpointInput" value="https://westpro.my.id/catch/demo" readonly style="flex:1; min-width:240px; background:#0b0c0e; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.65rem; font-family:'JetBrains Mono', monospace; font-size:0.78rem; color:#34d399;" />
              <button onclick="copyWebhookActiveUrl()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Salin URL</button>
              <button onclick="sendQuickTestWebhook()" style="background:#2563eb; color:#fff; border:none; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Kirim Test Webhook</button>
            </div>
          </div>
          <div style="font-size:0.72rem; color:#94a3b8; max-width:320px; line-height:1.4;">
            Path kustom otomatis direkam (contoh: <code style="color:#a3e635;">/catch/midtrans</code>, <code style="color:#a3e635;">/catch/xendit</code>, <code style="color:#a3e635;">/catch/github</code>).
          </div>
        </div>
      </div>

      <!-- FILTER & SEARCH BAR -->
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem; margin-bottom:1rem;">
        <div style="display:flex; align-items:center; gap:0.35rem; flex-wrap:wrap;">
          <span style="font-size:0.75rem; color:#94a3b8; margin-right:0.3rem;">Filter Method:</span>
          <button class="wh-filter-btn active" data-method="ALL" onclick="setWhMethodFilter('ALL', this)">SEMUA</button>
          <button class="wh-filter-btn" data-method="POST" onclick="setWhMethodFilter('POST', this)">POST</button>
          <button class="wh-filter-btn" data-method="GET" onclick="setWhMethodFilter('GET', this)">GET</button>
          <button class="wh-filter-btn" data-method="PUT" onclick="setWhMethodFilter('PUT', this)">PUT</button>
          <button class="wh-filter-btn" data-method="DELETE" onclick="setWhMethodFilter('DELETE', this)">DELETE</button>
          <button class="wh-filter-btn" data-method="PATCH" onclick="setWhMethodFilter('PATCH', this)">PATCH</button>
        </div>
        <div style="display:flex; align-items:center; gap:0.5rem; flex:1; max-width:380px;">
          <input type="text" id="whSearchInput" placeholder="Cari path, body, atau IP..." style="width:100%; background:#14161b; border:1px solid #334155; border-radius:6px; padding:0.35rem 0.65rem; color:#fff; font-size:0.78rem;" oninput="debounceWhSearch()" />
          <button onclick="exportWebhookJson()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.35rem 0.65rem; font-size:0.75rem; cursor:pointer; white-space:nowrap;" title="Ekspor daftar request ke file JSON">Ekspor</button>
        </div>
      </div>

      <!-- 2-COLUMN SPLIT PANE (FEED LIST & DEEP INSPECTOR) -->
      <div style="display:grid; grid-template-columns: 340px 1fr; gap:1.25rem; min-height:550px; align-items:start;" id="whSplitPane">
        
        <!-- LEFT: REQUEST LIST FEED -->
        <div class="panel" style="background:#131317; border:1px solid #222228; padding:0.75rem; display:flex; flex-direction:column; height:600px; max-height:80vh;">
          <div style="display:flex; justify-content:space-between; align-items:center; padding-bottom:0.5rem; border-bottom:1px solid #222228; margin-bottom:0.5rem;">
            <span style="font-size:0.75rem; font-weight:700; color:#94a3b8;">DAFTAR REQUEST (<span id="whReqTotalCount">0</span>)</span>
            <span style="font-size:0.7rem; color:#64748b;">Pilih untuk inspeksi</span>
          </div>
          <div id="whRequestsListContainer" style="flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:0.4rem; padding-right:0.25rem;">
            <div style="color:#64748b; font-size:0.78rem; text-align:center; padding:2rem;">
              Memuat request webhook...
            </div>
          </div>
        </div>

        <!-- RIGHT: DEEP REQUEST INSPECTOR -->
        <div class="panel" id="whInspectorPanel" style="background:#131317; border:1px solid #222228; padding:1rem; min-height:600px; display:flex; flex-direction:column;">
          <!-- Empty State -->
          <div id="whInspectorEmpty" style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; color:#64748b; text-align:center; padding:3rem;">
            <div style="font-size:0.9rem; font-weight:600; color:#94a3b8;">Pilih salah satu request di sebelah kiri</div>
            <div style="font-size:0.75rem; margin-top:0.25rem; color:#64748b;">Atau kirim request baru ke URL webhook untuk melihat Headers, Body, Params, dan Response.</div>
          </div>

          <!-- Active Inspector Content -->
          <div id="whInspectorActive" style="display:none; flex-direction:column; height:100%;">
            <!-- Header Info & Actions -->
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; padding-bottom:0.75rem; border-bottom:1px solid #222228; margin-bottom:0.75rem;">
              <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
                <span id="inspMethodBadge" class="wh-badge-method badge-post">POST</span>
                <span id="inspPath" style="font-family:'JetBrains Mono',monospace; font-size:0.85rem; font-weight:700; color:#f1f5f9; word-break:break-all;">/catch/demo</span>
                <span id="inspStatusBadge" style="background:rgba(16,185,129,0.15); color:#10b981; font-size:0.72rem; font-weight:700; padding:0.15rem 0.45rem; border-radius:4px;">200 OK</span>
                <span id="inspMockedBadge" style="display:none; background:rgba(245,158,11,0.15); color:#fbbf24; font-size:0.68rem; font-weight:700; padding:0.15rem 0.45rem; border-radius:4px;">MOCKED</span>
              </div>
              <div style="display:flex; gap:0.4rem; align-items:center;">
                <button onclick="openReplayModal()" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Kirim ulang request ini (Replay)">Replay</button>
                <button onclick="deleteCurrentInspectedRequest()" style="background:rgba(239,68,68,0.12); color:#f87171; border:1px solid rgba(239,68,68,0.25); border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; cursor:pointer;">Hapus</button>
              </div>
            </div>

            <!-- Meta Quick Bar -->
            <div style="display:flex; gap:1.25rem; font-size:0.72rem; color:#94a3b8; margin-bottom:0.75rem; flex-wrap:wrap; background:#141720; padding:0.45rem 0.75rem; border-radius:6px;">
              <div>Waktu: <span id="inspTimestamp" style="color:#fff;">--</span></div>
              <div>Durasi: <span id="inspDuration" style="color:#fff;">-- ms</span></div>
              <div>IP Pengirim: <span id="inspClientIp" style="font-family:monospace; color:#38bdf8;">--</span></div>
              <div>Ukuran: <span id="inspBodySize" style="color:#fff;">--</span></div>
            </div>

            <!-- Inspector Sub-Tabs -->
            <div style="display:flex; gap:0.35rem; border-bottom:1px solid #272a31; margin-bottom:0.75rem;">
              <button class="insp-tab-btn active" data-tab="tabBody" onclick="switchInspTab('tabBody', this)">Body Payload</button>
              <button class="insp-tab-btn" data-tab="tabHeaders" onclick="switchInspTab('tabHeaders', this)">Headers (<span id="inspHeadersCount">0</span>)</button>
              <button class="insp-tab-btn" data-tab="tabParams" onclick="switchInspTab('tabParams', this)">Query Params (<span id="inspParamsCount">0</span>)</button>
              <button class="insp-tab-btn" data-tab="tabResponse" onclick="switchInspTab('tabResponse', this)">Server Response</button>
              <button class="insp-tab-btn" data-tab="tabCurl" onclick="switchInspTab('tabCurl', this)">Perintah cURL</button>
            </div>

            <!-- Tab 1: Body Payload -->
            <div id="inspTabBody" class="insp-tab-content active" style="flex:1; display:flex; flex-direction:column;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;">
                <div style="font-size:0.72rem; color:#94a3b8;">Format: <span id="inspContentTypeBadge" style="color:#a3e635;">JSON</span></div>
                <button onclick="copyInspBody()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.2rem 0.5rem; font-size:0.7rem; cursor:pointer;">Salin Body</button>
              </div>
              <pre id="inspBodyPre" style="flex:1; background:#08090b; border:1px solid #1e293b; border-radius:6px; padding:0.75rem; color:#f1f5f9; font-family:'JetBrains Mono',monospace; font-size:0.75rem; overflow:auto; max-height:420px; white-space:pre-wrap; margin:0;"></pre>
            </div>

            <!-- Tab 2: Headers -->
            <div id="inspTabHeaders" class="insp-tab-content" style="flex:1; display:none; flex-direction:column;">
              <div style="display:flex; justify-content:flex-end; margin-bottom:0.4rem;">
                <button onclick="copyInspHeaders()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.2rem 0.5rem; font-size:0.7rem; cursor:pointer;">Salin Semua Header</button>
              </div>
              <div id="inspHeadersTable" style="flex:1; max-height:420px; overflow-y:auto; border:1px solid #1e293b; border-radius:6px;"></div>
            </div>

            <!-- Tab 3: Query Params -->
            <div id="inspTabParams" class="insp-tab-content" style="flex:1; display:none; flex-direction:column;">
              <div id="inspParamsTable" style="flex:1; max-height:420px; overflow-y:auto; border:1px solid #1e293b; border-radius:6px;"></div>
            </div>

            <!-- Tab 4: Server Response -->
            <div id="inspTabResponse" class="insp-tab-content" style="flex:1; display:none; flex-direction:column;">
              <div style="font-size:0.75rem; color:#94a3b8; margin-bottom:0.4rem;">
                Respons dikembalikan (Status: <b id="inspRespStatus" style="color:#10b981;">200</b>):
              </div>
              <pre id="inspRespBodyPre" style="flex:1; background:#08090b; border:1px solid #1e293b; border-radius:6px; padding:0.75rem; color:#a3e635; font-family:'JetBrains Mono',monospace; font-size:0.75rem; overflow:auto; max-height:420px; white-space:pre-wrap; margin:0;"></pre>
            </div>

            <!-- Tab 5: cURL Generator -->
            <div id="inspTabCurl" class="insp-tab-content" style="flex:1; display:none; flex-direction:column;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;">
                <div style="font-size:0.72rem; color:#94a3b8;">Jalankan kembali request ini di terminal:</div>
                <button onclick="copyInspCurl()" style="background:#2563eb; color:#fff; border:none; border-radius:4px; padding:0.2rem 0.6rem; font-size:0.7rem; font-weight:600; cursor:pointer;">Salin cURL</button>
              </div>
              <pre id="inspCurlPre" style="flex:1; background:#08090b; border:1px solid #1e293b; border-radius:6px; padding:0.75rem; color:#38bdf8; font-family:'JetBrains Mono',monospace; font-size:0.75rem; overflow:auto; max-height:420px; white-space:pre-wrap; margin:0;"></pre>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- ==================== HALAMAN 9: PROFIL PENGGUNA & CONFIG SPECTRUM ==================== -->
    <div id="page-profile" class="view-section">
      <!-- HEADER -->
      <div class="page-header" style="margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; margin: 0;">
              Profil Pengguna &amp; Konfigurasi SPEctrum
            </h1>
            <span id="specUserStatusBadge" class="hermes-badge badge-blue">Memuat...</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">
            Kelola profil akun, ubah kata sandi, dan hubungkan kredensial Lifeinspectrum untuk isolasi task &amp; notulen rapat.
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <button onclick="navigateToPage('hermes')" style="background: #202026; color: #cbd5e1; border: 1px solid #353540; border-radius: 6px; padding: 0.4rem 0.85rem; font-size: 0.75rem; font-weight: 500; cursor: pointer;">
            ← Kembali ke Workspace QA
          </button>
        </div>
      </div>

      <!-- ALERT MESSAGE BOX -->
      <div id="profileAlertBox" style="display: none; padding: 0.75rem 1rem; border-radius: 8px; font-size: 0.8rem; margin-bottom: 1.25rem;"></div>

      <div class="grafana-grid">
        <!-- BARIS ATAS: KIRI (PROFIL & NAMA SAPAAN) & KANAN (UBAH PASSWORD) -->
        <!-- CARD 1: PROFIL & NAMA SAPAAN (COL-6) -->
        <div class="panel col-6" style="background: #131418; border: 1px solid #27272e; border-radius: 10px; padding: 1.25rem;">
          <div style="display: flex; align-items: center; gap: 0.6rem; border-bottom: 1px solid #22222a; padding-bottom: 0.85rem; margin-bottom: 1.25rem;">
            <div style="width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #10b981, #059669); display: flex; align-items: center; justify-content: center; font-size: 1.15rem; color: #fff;">
              👤
            </div>
            <div>
              <div style="font-weight: 700; font-size: 0.95rem; color: #fff;">Profil Akun Server</div>
              <div style="font-size: 0.725rem; color: var(--gf-text-muted);">Informasi akun dan nama sapaan Anda di seluruh dashboard</div>
            </div>
          </div>

          <form id="profileNameForm" onsubmit="event.preventDefault(); saveUserProfileName();" style="display: flex; flex-direction: column; gap: 1rem;">
            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                Alamat Email Akun
              </label>
              <input type="text" id="cfgUserEmailInput" readonly class="form-input" style="width: 100%; background: #08090b; border: 1px solid #22222a; border-radius: 6px; padding: 0.55rem 0.85rem; color: #94a3b8; font-size: 0.825rem; cursor: not-allowed;" />
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
              <div>
                <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                  Peran / Hak Akses (Role)
                </label>
                <div style="display: flex; align-items: center; gap: 0.5rem; height: 38px;">
                  <span id="cfgUserRoleBadge" class="hermes-badge badge-warn" style="font-size: 0.75rem; padding: 0.25rem 0.6rem;">OWNER</span>
                  <span style="font-size: 0.72rem; color: #71717a;">User ID: <b id="cfgUserIdDisplay" style="color: #cbd5e1;">-</b></span>
                </div>
              </div>
              <div>
                <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                  Terakhir Masuk (Last Login)
                </label>
                <div id="cfgUserLastLoginDisplay" style="height: 38px; display: flex; align-items: center; font-size: 0.75rem; color: #94a3b8;">
                  -
                </div>
              </div>
            </div>

            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                Nama Lengkap / Nama Sapaan <span style="color: #f87171;">*</span>
              </label>
              <input type="text" id="cfgUserNameInput" class="form-input" placeholder="Masukkan nama panggilan Anda (contoh: Ares / Hanafi / Budi)" required style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
              <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Nama ini digunakan untuk sapaan briefing harian, filter task sprint, dan author test case.</div>
            </div>

            <div style="margin-top: 0.25rem;">
              <button type="submit" id="btnSaveProfileName" style="background: #10b981; color: #022c22; border: none; border-radius: 6px; padding: 0.55rem 1.25rem; font-size: 0.8rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; transition: all 0.2s;">
                💾 Simpan Nama Profil
              </button>
            </div>
          </form>
        </div>

        <!-- CARD 2: UBAH KATA SANDI (COL-6) -->
        <div class="panel col-6" style="background: #131418; border: 1px solid #27272e; border-radius: 10px; padding: 1.25rem;">
          <div style="display: flex; align-items: center; gap: 0.6rem; border-bottom: 1px solid #22222a; padding-bottom: 0.85rem; margin-bottom: 1.25rem;">
            <div style="width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #f59e0b, #d97706); display: flex; align-items: center; justify-content: center; font-size: 1.15rem; color: #fff;">
              🔒
            </div>
            <div>
              <div style="font-weight: 700; font-size: 0.95rem; color: #fff;">Ubah Kata Sandi (Password)</div>
              <div style="font-size: 0.725rem; color: var(--gf-text-muted);">Keamanan akun server terenkripsi PBKDF2-HMAC-SHA256</div>
            </div>
          </div>

          <form id="changePasswordForm" onsubmit="event.preventDefault(); saveUserPasswordChange();" style="display: flex; flex-direction: column; gap: 0.9rem;">
            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                Kata Sandi Saat Ini
              </label>
              <div style="position: relative; display: flex; align-items: center;">
                <input type="password" id="cfgOldPassword" class="form-input" placeholder="Masukkan kata sandi lama (opsional jika belum pernah diset)" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 2.5rem 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
                <button type="button" onclick="togglePasswordVisibility('cfgOldPassword', 'eyeIconOldPass')" style="position: absolute; right: 0.65rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.2rem; font-size: 0.95rem;">
                  <span id="eyeIconOldPass">👁️</span>
                </button>
              </div>
              <div id="cfgOldPassNote" style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Kosongkan jika sebelumnya belum pernah menyetel password.</div>
            </div>

            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                Kata Sandi Baru <span style="color: #f87171;">*</span> (Min. 6 Karakter)
              </label>
              <div style="position: relative; display: flex; align-items: center;">
                <input type="password" id="cfgNewPassword" required minlength="6" class="form-input" placeholder="Buat kata sandi baru" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 2.5rem 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
                <button type="button" onclick="togglePasswordVisibility('cfgNewPassword', 'eyeIconNewPass')" style="position: absolute; right: 0.65rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.2rem; font-size: 0.95rem;">
                  <span id="eyeIconNewPass">👁️</span>
                </button>
              </div>
            </div>

            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.35rem;">
                Konfirmasi Kata Sandi Baru <span style="color: #f87171;">*</span>
              </label>
              <div style="position: relative; display: flex; align-items: center;">
                <input type="password" id="cfgConfirmNewPassword" required minlength="6" class="form-input" placeholder="Ulangi kata sandi baru" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 2.5rem 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
                <button type="button" onclick="togglePasswordVisibility('cfgConfirmNewPassword', 'eyeIconConfPass')" style="position: absolute; right: 0.65rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.2rem; font-size: 0.95rem;">
                  <span id="eyeIconConfPass">👁️</span>
                </button>
              </div>
            </div>

            <div style="margin-top: 0.25rem;">
              <button type="submit" id="btnSaveNewPassword" style="background: #f59e0b; color: #0b0c0e; border: none; border-radius: 6px; padding: 0.55rem 1.25rem; font-size: 0.8rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; transition: all 0.2s;">
                🔑 Perbarui Kata Sandi
              </button>
            </div>
          </form>
        </div>

        <!-- NOTIFIKASI EMAIL (COL-12) -->
        <div class="panel col-12" id="emailPrefsCard" style="background: #131418; border: 1px solid #27272e; border-radius: 10px; padding: 1rem 1.25rem;">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:0.75rem; flex-wrap:wrap;">
            <div>
              <div style="font-weight:700; font-size:0.95rem; color:#fff;">✉️ Notifikasi Email</div>
              <div id="emailPrefsHint" style="font-size:0.74rem; color:var(--gf-text-muted); margin-top:0.2rem;">Memuat...</div>
            </div>
            <label style="display:inline-flex; align-items:center; gap:0.5rem; font-size:0.82rem; color:#e4e4e7; cursor:pointer;">
              <input type="checkbox" id="chkEmailBriefing" onchange="saveEmailPrefs(this.checked)" style="accent-color:#38bdf8; width:16px; height:16px;">
              Kirim Briefing Hermes harian ke email saya (hari kerja, ±02:00)
            </label>
          </div>
        </div>

        <!-- BARIS BAWAH: CARD 3 (KREDENSIAL SPECTRUM COL-7) & CARD 4 (STATUS SINKRONISASI COL-5) -->
        <!-- CARD 3: FORM PENGATURAN KREDENSIAL SPECTRUM (COL-7) -->
        <div class="panel col-7" style="background: #131418; border: 1px solid #27272e; border-radius: 10px; padding: 1.25rem;">
          <div style="display: flex; align-items: center; gap: 0.6rem; border-bottom: 1px solid #22222a; padding-bottom: 0.85rem; margin-bottom: 1.25rem;">
            <div style="width: 34px; height: 34px; border-radius: 8px; background: linear-gradient(135deg, #0284c7, #38bdf8); display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">
              🔑
            </div>
            <div>
              <div style="font-weight: 700; font-size: 0.95rem; color: #fff;">Kredensial Login SPEctrum</div>
              <div style="font-size: 0.725rem; color: var(--gf-text-muted);">Digunakan untuk otentikasi API Lifeinspectrum (https://api.lifeinspectrum.com)</div>
            </div>
          </div>

          <form id="spectrumUserConfigForm" onsubmit="event.preventDefault(); saveSpectrumUserConfig();" style="display: flex; flex-direction: column; gap: 1rem;">
            <div>
              <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.4rem;">
                Email / Username SPEctrum <span style="color: #f87171;">*</span>
              </label>
              <input type="email" id="cfgSpecEmail" class="form-input" placeholder="contoh: aris.aris@spesolution.com" required style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
              <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Alamat email yang terdaftar pada portal Lifeinspectrum SPE Solution.</div>
            </div>

            <div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.4rem;">
                <label style="font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin: 0;">
                  Kata Sandi SPEctrum <span style="color: #f87171;">*</span>
                </label>
                <span id="cfgPassStatusNote" style="font-size: 0.7rem; color: #94a3b8;"></span>
              </div>
              <div style="position: relative; display: flex; align-items: center;">
                <input type="password" id="cfgSpecPassword" class="form-input" placeholder="Masukkan password SPEctrum Anda" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 2.5rem 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
                <button type="button" onclick="togglePasswordVisibility('cfgSpecPassword', 'eyeIconSpecPass')" style="position: absolute; right: 0.65rem; background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0.2rem; font-size: 0.95rem;" title="Lihat/Sembunyikan">
                  <span id="eyeIconSpecPass">👁️</span>
                </button>
              </div>
              <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Password disimpan aman secara lokal dan hanya dipakai saat request ke API Lifeinspectrum.</div>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
              <div>
                <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.4rem;">
                  Project ID Lifeinspectrum
                </label>
                <input type="text" id="cfgSpecProjectId" class="form-input" value="" placeholder="Kosongkan = otomatis dari proyek di akun SPEctrum Anda" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 0.85rem; color: #fff; font-size: 0.825rem; font-family: 'JetBrains Mono', monospace;" />
                <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Kosongkan agar proyek &amp; task dibaca otomatis dari akun SPEctrum Anda. Jika punya lebih dari satu proyek dan ingin membatasi, isi beberapa ID dipisah koma.</div>
              </div>

              <div>
                <label style="display: block; font-size: 0.8rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.4rem;">
                  Nama Anggota di Task ('Task Saya')
                </label>
                <input type="text" id="cfgSpecMemberName" class="form-input" placeholder="contoh: Aris" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 0.85rem; color: #fff; font-size: 0.825rem;" />
                <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">Nama untuk filter tab 'Task Saya' di Workspace QA.</div>
              </div>
            </div>

            <div style="margin-top: 0.25rem;">
              <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; color: #e2e8f0; cursor: pointer;">
                <input type="checkbox" id="cfgSpecAutoSync" checked style="width: 16px; height: 16px; accent-color: #0284c7; cursor: pointer;" />
                <span>Aktifkan sinkronisasi otomatis saat masuk ke Workspace QA</span>
              </label>
            </div>

            <div style="display: flex; align-items: center; gap: 0.65rem; margin-top: 0.75rem; border-top: 1px solid #22222a; padding-top: 1rem;">
              <button type="submit" id="btnSaveSpecConfig" style="background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.55rem 1.25rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
                💾 Simpan Konfigurasi
              </button>
              <button type="button" id="btnTestSpecConfig" onclick="testSpectrumUserConnection()" style="background: #1f2937; color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); border-radius: 6px; padding: 0.55rem 1rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
                🧪 Uji Koneksi SPEctrum
              </button>
            </div>
          </form>
        </div>

        <!-- CARD 4: STATUS SINKRONISASI SPECTRUM (COL-5) -->
        <div class="panel col-5" style="display: flex; flex-direction: column; gap: 1rem;">
          <div style="background: #131418; border: 1px solid #27272e; border-radius: 10px; padding: 1.25rem; flex: 1;">
            <div style="font-size: 0.72rem; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.75rem;">
              Status Sinkronisasi SPEctrum
            </div>
            <div style="background: #0d0e12; border: 1px solid #22222a; border-radius: 8px; padding: 0.85rem; margin-bottom: 1rem;">
              <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.35rem;">
                <span>Terakhir Sinkron:</span>
                <span id="cfgLastSyncTime" style="color: #f1f5f9; font-weight: 600;">Belum pernah</span>
              </div>
              <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.35rem;">
                <span>Status Terakhir:</span>
                <span id="cfgLastStatusText" style="color: #38bdf8; font-weight: 600;">Siap</span>
              </div>
              <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: #94a3b8;">
                <span>Endpoint API:</span>
                <span style="color: #10b981; font-family: 'JetBrains Mono', monospace; font-size: 0.7rem;">api.lifeinspectrum.com</span>
              </div>
            </div>

            <button onclick="syncSpectrumUserData()" id="btnSyncSpectrumNow" style="width: 100%; background: linear-gradient(135deg, #0284c7, #0369a1); color: #fff; border: none; border-radius: 6px; padding: 0.65rem; font-size: 0.825rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.5rem; box-shadow: 0 4px 12px rgba(2,132,199,0.25);">
              <span id="cfgSyncSpinner" style="display: none; animation: blink 1s infinite;">●</span> 🔄 Sinkronkan Data SPEctrum Sekarang
            </button>
            <div style="font-size: 0.7rem; color: #64748b; text-align: center; margin-top: 0.5rem; line-height: 1.4;">
              Mengambil task aktif, rincian sprint, backlog, dan kalender standup langsung dari akun Anda.
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== WESTPRO MAIL (inbox domain sendiri) ==================== -->
    <div id="page-webmail" class="view-section">
      <div class="page-header" style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <h1 style="font-size: 1.2rem; font-weight: 700; color: #fff; margin: 0;">Westpro Mail</h1>
            <span class="hermes-badge badge-blue" id="wmDomainBadge">@domain</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">Kotak masuk untuk banyak alamat uji. Buat alamat sendiri agar hanya Anda yang melihatnya; email ke alamat lain di domain ini hanya terlihat oleh pemilik server.</div>
        </div>
        <div style="display:flex; gap:0.5rem;">
          <button class="hb-btn hb-btn-primary" onclick="wmOpenCompose()">✎ Tulis email</button>
          <button class="hb-btn" onclick="wmToggleApi()">🔑 API otomasi tes</button>
        </div>
      </div>
      <div id="wmSetupBanner" style="display:none; background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 0.7rem 1rem; margin-bottom: 0.9rem; font-size: 0.78rem; color: #fcd34d; line-height: 1.55;"></div>
      <div id="wmApiBox" style="display:none; background:#131418; border:1px solid #27272e; border-radius:12px; padding:0.9rem 1.1rem; margin-bottom:0.9rem; font-size:0.78rem; color:#cbd5e1;"></div>
      <div class="wm-wrap">
        <aside class="wm-side">
          <button id="wmFolderInbox" class="wm-folder active" onclick="wmSetFolder('inbox')"><span>📥 Kotak Masuk</span><span id="wmInboxUnread"></span></button>
          <button id="wmFolderSent" class="wm-folder" onclick="wmSetFolder('sent')" style="margin-top:0.25rem;"><span>📤 Terkirim</span></button>
          <div class="wm-side-title">Alamat uji</div>
          <div id="wmAddresses"></div>
          <div style="display:flex; gap:0.3rem; align-items:center; margin-top:0.6rem; background:#0b0c0e; border:1px solid #2d2d35; border-radius:8px; padding:0.2rem 0.4rem;">
            <input id="wmNewLocal" placeholder="nama (kosong = acak)" style="flex:1; min-width:0; background:transparent; border:none; color:#e4e4e7; font-size:0.74rem; outline:none; font-family:'JetBrains Mono', monospace;" onkeydown="if(event.key==='Enter') wmCreateAlias()">
            <button class="hb-btn hb-btn-primary" style="padding:0.2rem 0.5rem;" onclick="wmCreateAlias()">+ Buat</button>
          </div>
          <div style="font-size:0.64rem; color:#52525b; margin-top:0.45rem; line-height:1.45;">Tip: <b>qa+apa-saja@</b> tetap masuk ke <b>qa@</b> (plus-addressing), jadi satu alamat bisa dipakai untuk banyak akun uji.</div>
        </aside>
        <section class="wm-list-pane">
          <div class="wm-toolbar">
            <input type="search" id="wmSearch" placeholder="Cari pengirim, subjek, isi..." oninput="wmDebouncedLoad()">
            <label style="font-size:0.7rem; color:#a1a1aa; display:inline-flex; gap:0.25rem; align-items:center; white-space:nowrap;"><input type="checkbox" id="wmUnreadOnly" onchange="wmLoadMessages()"> Belum dibaca</label>
            <button class="hb-btn" onclick="wmLoadMessages()" title="Muat ulang">↻</button>
          </div>
          <div id="wmList"><div class="wm-empty">Memuat...</div></div>
        </section>
        <section class="wm-read-pane" id="wmReader"><div class="wm-empty">Pilih email untuk dibaca</div></section>
      </div>

      <!-- Tulis email -->
      <div id="wmCompose" class="wm-modal">
        <div class="wm-modal-card">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.7rem;"><b style="color:#f4f4f5;">Tulis email</b><button class="wm-mini" onclick="wmCloseCompose()" style="font-size:1.1rem;">✕</button></div>
          <select id="wmFrom"></select>
          <input id="wmTo" type="email" placeholder="Kepada (email)">
          <input id="wmSubject" type="text" placeholder="Subjek" maxlength="200">
          <textarea id="wmBody" rows="8" placeholder="Pesan (teks)"></textarea>
          <div style="display:flex; justify-content:space-between; align-items:center; gap:0.6rem;">
            <span id="wmComposeMsg" style="font-size:0.74rem; color:#94a3b8;">Balasan akan masuk ke alamat pengirim di atas (Reply-To).</span>
            <button id="wmSendBtn" class="hb-btn hb-btn-primary" onclick="wmSend()">Kirim</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN TEAMS: MICROSOFT TEAMS & AI DIGEST ==================== -->
    <div id="page-teams" class="view-section">
      <div class="page-header" style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
            <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; margin: 0;">Microsoft Teams &amp; AI Daily Digest</h1>
            <span id="teamsStatusBadge" class="hermes-badge badge-warn">MEMERIKSA SESI...</span>
            <span id="teamsModeBadge" class="hermes-badge badge-ok">👻 HEADLESS (HEMAT RESOURCE)</span>
            <span id="teamsAiModelBadge" class="hermes-badge badge-blue">🤖 MODEL: ROTASI OTOMATIS</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">
            Pemantauan obrolan Teams dari server 24/7. Sesi browser tersimpan permanen di server &bull; Analisis insiden, tugas &amp; agenda harian secara otomatis.
          </div>
        </div>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button id="btnTeamsAnalyze" class="hb-btn hb-btn-primary" onclick="teamsRunAnalysis()" style="background:linear-gradient(135deg, #7c3aed, #4f46e5); border:none; box-shadow:0 2px 10px rgba(124,58,237,0.3);">
            ⚡ Jalankan Analisa AI Sekarang
          </button>
          <button id="btnTeamsToggleBrowser" class="hb-btn" onclick="teamsToggleBrowser()">
            🖥️ Buka Remote Browser (Login/Layar)
          </button>
          <button id="btnTeamsSwitchMode" class="hb-btn" style="background:rgba(100,116,139,0.2); color:#cbd5e1; border:1px solid rgba(100,116,139,0.35);" onclick="teamsToggleMode()" title="Ganti mode Chromium antara Headless (background hemat CPU/RAM) atau Headed (layar desktop server)">
            👻 Mode: Headless
          </button>
          <button class="hb-btn" style="background:rgba(249,115,22,0.2); color:#fb923c; border:1px solid rgba(249,115,22,0.35);" onclick="teamsSyncFromFirefox()" title="Ambil otomatis sesi login Teams dari Firefox server">
            🦊 Ambil Sesi dari Firefox
          </button>
          <button class="hb-btn" onclick="teamsReloadBrowser()" title="Muat ulang halaman Teams di server">
            ↻ Muat Ulang Teams
          </button>
        </div>
      </div>

      <!-- Remote Browser Section (Collapsible) -->
      <div id="teamsBrowserContainer" style="display:none; background:#121318; border:1px solid #27272a; border-radius:12px; padding:1rem; margin-bottom:1.5rem; box-shadow:0 8px 30px rgba(0,0,0,0.5);">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem; flex-wrap:wrap; gap:0.5rem;">
          <div>
            <div style="font-weight:700; color:#f4f4f5; font-size:0.92rem; display:flex; align-items:center; gap:0.4rem;">
              <span>🌐 Layar Remote Browser Teams (Interaktif)</span>
            </div>
            <div style="font-size:0.75rem; color:#94a3b8; margin-top:2px;">
              Klik layar untuk klik tombol/form, atau gunakan kotak input di bawah. Setujui 2-digit Microsoft Authenticator di HP Anda. Sesi akan tersimpan permanen.
            </div>
          </div>
          <div style="display:flex; gap:0.4rem; align-items:center;">
            <span id="teamsPageInfo" style="font-size:0.72rem; color:#a1a1aa; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>
            <button class="hb-btn" style="padding:0.25rem 0.6rem; font-size:0.75rem;" onclick="teamsRefreshScreen(true)">↻ Refresh Layar</button>
            <button class="hb-btn" style="padding:0.25rem 0.6rem; font-size:0.75rem;" onclick="teamsToggleBrowser()">✕ Tutup Layar</button>
          </div>
        </div>

        <!-- Virtual Screen Viewport -->
        <div style="display:flex; flex-direction:column; align-items:center;">
          <div id="teamsScreenWrapper" style="position:relative; width:100%; max-width:1080px; aspect-ratio:16/10; background:#000; border:2px solid #3f3f46; border-radius:10px; overflow:hidden; box-shadow:0 10px 25px -5px rgba(0,0,0,0.7); cursor:crosshair; user-select:none;">
            <img id="teamsScreenImg" src="" alt="Layar Microsoft Teams" style="width:100%; height:100%; object-fit:contain; display:none;" draggable="false" onclick="teamsClickScreen(event)" />
            <div id="teamsScreenLoading" style="position:absolute; top:0; left:0; width:100%; height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; color:#94a3b8; font-size:0.85rem; background:rgba(0,0,0,0.85);">
              <div style="font-size:1.8rem; margin-bottom:0.5rem; animation:pulse 1.5s infinite;">⏳</div>
              <div>Menghubungkan ke peramban server Teams...</div>
            </div>
            <div id="teamsTapRipple" style="position:absolute; width:24px; height:24px; border-radius:50%; background:rgba(124,58,237,0.7); border:2px solid #fff; pointer-events:none; transform:translate(-50%, -50%) scale(0); opacity:0; transition:transform 0.25s ease-out, opacity 0.25s ease-out;"></div>
          </div>

          <!-- Virtual Keyboard & Typing Controller -->
          <div style="width:100%; max-width:1080px; margin-top:0.75rem; background:#18181b; border:1px solid #27272a; border-radius:8px; padding:0.6rem 0.8rem; display:flex; flex-wrap:wrap; gap:0.5rem; align-items:center; justify-content:space-between;">
            <div style="display:flex; gap:0.4rem; flex:1; min-width:260px;">
              <input type="text" id="teamsTextInput" placeholder="Ketik teks (email, sandi, dsb) lalu Enter atau klik Kirim..." style="flex:1; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.65rem; color:#fff; font-size:0.8rem;" onkeydown="if(event.key==='Enter') teamsSendText();" />
              <button class="hb-btn hb-btn-primary" style="padding:0.4rem 0.75rem; font-size:0.78rem;" onclick="teamsSendText()">Kirim Teks</button>
            </div>
            <div style="display:flex; gap:0.35rem; align-items:center; flex-wrap:wrap;">
              <button class="hb-btn" style="padding:0.35rem 0.6rem; font-size:0.75rem;" onclick="teamsSendKey('Tab')">Tab ⇥</button>
              <button class="hb-btn" style="padding:0.35rem 0.6rem; font-size:0.75rem;" onclick="teamsSendKey('Enter')">Enter ↵</button>
              <button class="hb-btn" style="padding:0.35rem 0.6rem; font-size:0.75rem;" onclick="teamsSendKey('Backspace')">⌫ Backspace</button>
              <button class="hb-btn" style="padding:0.35rem 0.6rem; font-size:0.75rem; background:rgba(245,158,11,0.2); color:#fbbf24; border:1px solid rgba(245,158,11,0.35);" onclick="teamsSendKey('Clear')" title="Kosongkan seluruh teks input aktif">🧹 Bersihkan</button>
              <button class="hb-btn" style="padding:0.35rem 0.6rem; font-size:0.75rem;" onclick="teamsSendKey('Escape')">Esc</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(59,130,246,0.25); color:#60a5fa; border:1px solid rgba(59,130,246,0.4);" onclick="teamsClickNext()">Klik Next / Submit</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(239,68,68,0.2); color:#f87171; border:1px solid rgba(239,68,68,0.3);" onclick="teamsResetToLogin()" title="Kembali ke halaman awal login Teams jika nyasar ke lupa sandi">⬅️ Reset ke Login</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(239,68,68,0.25); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);" onclick="teamsCleanProfile()" title="Bersihkan cookies/storage korup yang menyebabkan refresh berulang">🧹 Bersihkan Cache / Stop Loop</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(168,85,247,0.25); color:#c084fc; border:1px solid rgba(168,85,247,0.4);" onclick="teamsSwitchMode(true)" title="Buka jendela Chromium di layar server (Headed) agar bisa login fisik di desktop Kali Linux">🖥️ Buka di Layar Server (Headed)</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(100,116,139,0.25); color:#cbd5e1; border:1px solid rgba(100,116,139,0.4);" onclick="teamsSwitchMode(false)" title="Jalankan Chromium di background tanpa jendela (Headless)">👻 Mode Headless</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(249,115,22,0.25); color:#fb923c; border:1px solid rgba(249,115,22,0.4);" onclick="teamsSyncFromFirefox()" title="Ambil otomatis sesi login Teams dari browser Firefox yang berjalan di server Kali Linux">🦊 Ambil Sesi dari Firefox Server</button>
              <button class="hb-btn" style="padding:0.35rem 0.65rem; font-size:0.75rem; background:rgba(16,185,129,0.2); color:#34d399; border:1px solid rgba(16,185,129,0.35);" onclick="teamsOpenImportModal()" title="Impor sesi login Microsoft Teams langsung dari tab browser laptop Anda">📥 Impor Sesi dari Laptop</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Modal Impor Sesi Teams -->
      <div id="teamsImportModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.8); z-index:9999; align-items:center; justify-content:center; padding:1rem;">
        <div style="background:#18181b; border:1px solid #3f3f46; border-radius:12px; max-width:680px; width:100%; padding:1.5rem; box-shadow:0 20px 25px -5px rgba(0,0,0,0.5);">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; border-bottom:1px solid #27272a; padding-bottom:0.75rem;">
            <div style="font-weight:700; color:#f4f4f5; font-size:1.05rem; display:flex; align-items:center; gap:0.5rem;">
              <span>📥</span> Impor Sesi Teams Langsung dari Laptop
            </div>
            <button onclick="teamsCloseImportModal()" style="background:transparent; border:none; color:#a1a1aa; font-size:1.2rem; cursor:pointer;">✕</button>
          </div>

          <div style="font-size:0.82rem; color:#cbd5e1; line-height:1.6; margin-bottom:1rem;">
            Karena akun kerja Anda menggunakan Passkey/Windows Hello yang tersimpan di laptop, Anda bisa langsung mentransfer sesi aktif dari tab Microsoft Teams yang sedang terbuka di laptop Anda ke server dalam 1 klik!
          </div>

          <!-- Cara 1: 1 Baris di Console -->
          <div style="background:#090a0f; border:1px solid #27272a; border-radius:8px; padding:0.85rem; margin-bottom:1rem;">
            <div style="font-size:0.8rem; font-weight:700; color:#38bdf8; margin-bottom:0.4rem;">
              ⚡ Cara Tercepat (Jalankan di Tab Teams Anda):
            </div>
            <div style="font-size:0.75rem; color:#94a3b8; margin-bottom:0.5rem;">
              1. Buka tab <b>Microsoft Teams</b> di Chrome/Edge Anda (tempat Anda sudah login).<br>
              2. Tekan <b>F12</b> (DevTools) lalu pilih tab <b>Console</b>.<br>
              3. Salin &amp; paste kode 1-baris di bawah ini, lalu tekan <b>Enter</b>:
            </div>
            <div style="position:relative;">
              <textarea id="teamsConsoleSnippet" readonly style="width:100%; box-sizing:border-box; height:65px; background:#121318; border:1px solid #334155; border-radius:6px; color:#a3e635; font-family:monospace; font-size:0.72rem; padding:0.5rem; resize:none;">fetch('https://westpro.my.id/api/teams/session', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:window.location.href, storage:{...localStorage}, cookie:document.cookie})}).then(r=>r.json()).then(d=>alert(d.success?'✅ Sesi Teams Berhasil Disinkronkan ke Westpro!':'Gagal: '+d.error));</textarea>
              <button class="hb-btn" style="position:absolute; top:6px; right:6px; font-size:0.7rem; padding:0.2rem 0.5rem; background:#2563eb; color:#fff;" onclick="teamsCopySnippet()">📋 Salin Kode</button>
            </div>
          </div>

          <!-- Cara 2: Paste manual -->
          <div>
            <div style="font-size:0.78rem; font-weight:600; color:#e2e8f0; margin-bottom:0.35rem;">
              Atau paste data sesi JSON secara manual di sini:
            </div>
            <textarea id="teamsImportJsonInput" placeholder="Paste data sesi JSON di sini jika copy manual..." style="width:100%; box-sizing:border-box; height:90px; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-family:monospace; font-size:0.75rem; padding:0.5rem; resize:vertical;"></textarea>
          </div>

          <div style="display:flex; justify-content:flex-end; gap:0.5rem; margin-top:1rem;">
            <button class="hb-btn" onclick="teamsCloseImportModal()">Tutup</button>
            <button class="hb-btn hb-btn-primary" onclick="teamsSubmitImportSession()">🚀 Terapkan Sesi Manual</button>
          </div>
        </div>
      </div>

      <!-- Sprint & Target Chat Configuration Card -->
      <div class="panel" style="background:#131418; border:1px solid #27272a; border-radius:12px; padding:1.25rem; margin-bottom:1.25rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem; border-bottom:1px solid #27272a; padding-bottom:0.75rem;">
          <div style="display:flex; align-items:center; gap:0.6rem;">
            <div style="width:32px; height:32px; border-radius:8px; background:rgba(59,130,246,0.15); border:1px solid rgba(59,130,246,0.3); display:flex; align-items:center; justify-content:center; color:#60a5fa; font-size:1.1rem;">
              ⚙️
            </div>
            <div>
              <div style="font-weight:700; color:#f4f4f5; font-size:0.95rem;">Target Obrolan Sprint &amp; Strategi AI Multi-Model</div>
              <div style="font-size:0.72rem; color:#71717a; margin-top:2px;">Tentukan obrolan target serta model AI yang digunakan dengan sistem auto-failover otomatis jika model sibuk.</div>
            </div>
          </div>
          <div style="display:flex; gap:0.4rem;">
            <button class="hb-btn" style="font-size:0.75rem; padding:0.3rem 0.65rem;" onclick="teamsSyncSprintName()">⚡ Ambil Sprint Aktif</button>
            <button class="hb-btn hb-btn-primary" style="font-size:0.75rem; padding:0.3rem 0.75rem;" onclick="teamsSaveConfig()">💾 Simpan Target</button>
          </div>
        </div>

        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:1rem;">
          <!-- Sprint Name -->
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:#e2e8f0; margin-bottom:0.35rem;">
              🎯 Nama Sprint Berjalan:
            </label>
            <input type="text" id="cfgTeamsSprintName" placeholder="Contoh: Sprint 6 PNRI" style="width:100%; box-sizing:border-box; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-size:0.8rem; padding:0.45rem 0.65rem;" />
            <div style="font-size:0.7rem; color:#71717a; margin-top:0.25rem;">Nama sprint yang sedang aktif dikerjakan.</div>
          </div>

          <!-- Target Group Chats -->
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:#e2e8f0; margin-bottom:0.35rem;">
              👥 Target Group Chats / Channel:
            </label>
            <input type="text" id="cfgTeamsChannels" placeholder="Contoh: Sprint PNRI, Tech Team, Dev QA, Group B" style="width:100%; box-sizing:border-box; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-size:0.8rem; padding:0.45rem 0.65rem;" />
            <div style="font-size:0.7rem; color:#71717a; margin-top:0.25rem;">Pisahkan dengan tanda koma (nama channel atau grup Teams).</div>
          </div>

          <!-- AI Model Selection & Failover Strategy -->
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:#e2e8f0; margin-bottom:0.35rem;">
              🤖 Model AI &amp; Strategi Failover:
            </label>
            <select id="cfgTeamsAiModel" onchange="teamsUpdateModelBadge(this.value)" style="width:100%; box-sizing:border-box; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-size:0.8rem; padding:0.45rem 0.65rem;">
              <option value="auto_rotate">🔄 Rotasi Otomatis (Bergiliran Setiap Analisis)</option>
              <option value="groq/llama-3.3-70b-versatile">⚡ Groq Llama 3.3 70B (9Router - Super Cepat &amp; Ringan)</option>
              <option value="deepseek-v4-flash-0731:netra">🐋 DeepSeek V4 Flash (Sumopod / Netra - Cerdas)</option>
              <option value="gpt-4o">🤖 GPT-4o (GitHub Copilot via 9Router)</option>
              <option value="claude-3-5-sonnet">🎭 Claude 3.5 Sonnet (Claude Code via 9Router)</option>
              <option value="gemini/gemini-2.5-flash">⚡ Gemini 2.5 Flash (9Router - Cepat &amp; Cerdas)</option>
              <option value="gemini/gemini-1.5-flash">⚡ Gemini 1.5 Flash (9Router - Stabil &amp; Hemat)</option>
              <option value="gemini/gemini-3.8-flash">🚀 Gemini 3.8 Flash (9Router - Generasi Terbaru)</option>
              <option value="gemini/gemini-1.5-pro">🧠 Gemini 1.5 Pro (9Router - Analisis Mendalam)</option>
              <option value="claude-sonnet-5">🎭 Claude 3.5 Sonnet (Sumopod)</option>
            </select>
            <div style="font-size:0.7rem; color:#38bdf8; margin-top:0.25rem;">🛡️ Auto-Failover: otomatis beralih jika model utama limit/gagal.</div>
          </div>

          <!-- Target Personal Chats (Pemegang Task Sprint) -->
          <div style="grid-column: 1 / -1;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem; flex-wrap:wrap; gap:0.4rem;">
              <label style="display:block; font-size:0.78rem; font-weight:600; color:#e2e8f0; margin:0;">
                💬 Rekan Pemegang Task Sprint (Target Analisis PC / 1-on-1):
              </label>
              <button type="button" class="fs-btn" onclick="teamsSyncSprintContext()" style="padding:0.2rem 0.65rem; font-size:0.72rem; background:rgba(56,189,248,0.12); color:#38bdf8; border:1px solid rgba(56,189,248,0.3); border-radius:4px; cursor:pointer;">
                🔄 Auto-Detect dari SPEctrum Task
              </button>
            </div>
            <input type="text" id="cfgTeamsChats" placeholder="Contoh: Dimas, Bayu, Reza, Iqbal, Kevin (rekan dengan task sprint)" style="width:100%; box-sizing:border-box; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-size:0.8rem; padding:0.45rem 0.65rem;" />
            <div style="font-size:0.7rem; color:#71717a; margin-top:0.25rem;">AI akan memfokuskan analisis obrolan personal (PC) hanya dengan orang-orang yang memiliki task di sprint berjalan ini.</div>
          </div>
        </div>
      </div>

      <!-- Main Daily Digest & Intelligence Section -->
      <div style="display:grid; grid-template-columns: 1fr; gap:1.25rem;">
        <!-- Latest Briefing Card -->
        <div class="panel" style="background:#131418; border:1px solid #27272a; border-radius:12px; padding:1.25rem;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem; border-bottom:1px solid #27272a; padding-bottom:0.75rem;">
            <div style="display:flex; align-items:center; gap:0.6rem;">
              <div style="width:34px; height:34px; border-radius:8px; background:linear-gradient(135deg, #7c3aed, #4f46e5); display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:1.1rem;">
                T
              </div>
              <div>
                <div style="font-weight:700; color:#f4f4f5; font-size:0.95rem;">Rangkuman &amp; Analisis Harian Teams</div>
                <div id="teamsDigestMeta" style="font-size:0.72rem; color:#71717a; margin-top:2px;">Memuat riwayat analisis...</div>
              </div>
            </div>
            <div style="display:flex; gap:0.5rem; align-items:center;">
              <select id="teamsHistorySelect" onchange="teamsSelectHistoryDigest(this.value)" style="background:#090a0f; border:1px solid #334155; border-radius:6px; color:#cbd5e1; font-size:0.78rem; padding:0.35rem 0.6rem;">
                <option value="latest">Analisis Terkini</option>
              </select>
            </div>
          </div>

          <!-- Digest Content Container -->
          <div id="teamsDigestContainer" style="min-height:160px;">
            <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; padding:2.5rem 1rem; color:#71717a; text-align:center;">
              <div style="font-size:2rem; margin-bottom:0.75rem;">📋</div>
              <div style="font-size:0.9rem; font-weight:600; color:#cbd5e1; margin-bottom:0.25rem;">Belum ada hasil analisis Teams tersimpan</div>
              <div style="font-size:0.78rem; max-width:450px; line-height:1.5;">
                Pastikan Anda sudah login akun Microsoft di Remote Browser, lalu klik tombol <b>⚡ Jalankan Analisa AI Sekarang</b> di atas untuk menghasilkan rangkuman harian.
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Raw Chat Viewer (Penampil Obrolan Mentah Grup & PC) -->
      <div class="panel" style="background:#131418; border:1px solid #27272a; border-radius:12px; padding:1.25rem; margin-top:1.25rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; flex-wrap:wrap; gap:0.5rem; border-bottom:1px solid #27272a; padding-bottom:0.75rem;">
          <div style="display:flex; align-items:center; gap:0.6rem;">
            <div style="width:34px; height:34px; border-radius:8px; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.3); display:flex; align-items:center; justify-content:center; color:#34d399; font-weight:800; font-size:1.1rem;">
              📋
            </div>
            <div>
              <div style="font-weight:700; color:#f4f4f5; font-size:0.95rem;">Penampil Data Mentah Obrolan Teams (Grup &amp; PC)</div>
              <div id="teamsRawMeta" style="font-size:0.72rem; color:#71717a; margin-top:2px;">Memuat obrolan tersimpan...</div>
            </div>
          </div>
          <div style="display:flex; gap:0.4rem; flex-wrap:wrap;">
            <button class="hb-btn" style="font-size:0.75rem; padding:0.35rem 0.65rem;" onclick="teamsToggleRawJsonModal()">
              { } Lihat JSON Mentah
            </button>
            <button id="btnHarvestAll" class="hb-btn hb-btn-primary" style="font-size:0.75rem; padding:0.35rem 0.75rem; background:#10b981; border-color:#059669; color:#fff;" onclick="teamsTriggerHarvestAll()">
              🔄 Tarik &amp; Baca Semua Obrolan Sekarang
            </button>
          </div>
        </div>

        <!-- Filter & Search Toolbar -->
        <div style="display:flex; justify-content:space-between; align-items:center; gap:0.75rem; margin-bottom:1rem; flex-wrap:wrap;">
          <div style="display:flex; gap:0.35rem; flex-wrap:wrap;" id="teamsRawTabs">
            <button class="hb-btn raw-filter-active" id="filterTabAll" style="padding:0.3rem 0.65rem; font-size:0.75rem;" onclick="teamsFilterChats('all')">Semua (<span id="countAll">0</span>)</button>
            <button class="hb-btn" id="filterTabUnread" style="padding:0.3rem 0.65rem; font-size:0.75rem;" onclick="teamsFilterChats('unread')">🔔 Baru / Belum Dibaca (<span id="countUnread">0</span>)</button>
            <button class="hb-btn" id="filterTabGroup" style="padding:0.3rem 0.65rem; font-size:0.75rem;" onclick="teamsFilterChats('group')">👥 Grup (<span id="countGroup">0</span>)</button>
            <button class="hb-btn" id="filterTabPersonal" style="padding:0.3rem 0.65rem; font-size:0.75rem;" onclick="teamsFilterChats('personal')">👤 Personal Chat (<span id="countPersonal">0</span>)</button>
          </div>
          <input type="text" id="teamsRawSearchInput" oninput="teamsSearchChats(this.value)" placeholder="🔍 Cari nama obrolan, pengirim, kata..." style="flex:1; max-width:320px; background:#090a0f; border:1px solid #334155; border-radius:6px; color:#fff; font-size:0.78rem; padding:0.35rem 0.6rem;" />
        </div>

        <!-- Split Pane Layout: Chat List on Left, Active Conversation on Right -->
        <div style="display:grid; grid-template-columns: 280px 1fr; gap:1rem; min-height:420px; border:1px solid #27272a; border-radius:8px; overflow:hidden; background:#0c0d12;">
          <!-- Left Column: Chat List -->
          <div id="teamsRawChatList" style="border-right:1px solid #27272a; overflow-y:auto; max-height:550px; padding:0.5rem; display:flex; flex-direction:column; gap:0.35rem;">
            <div style="color:#71717a; font-size:0.78rem; text-align:center; padding:2rem 1rem;">
              Belum ada obrolan ditarik. Klik <b>"🔄 Tarik &amp; Baca Semua Obrolan Sekarang"</b> untuk memulai.
            </div>
          </div>

          <!-- Right Column: Messages Feed -->
          <div style="display:flex; flex-direction:column; max-height:550px;">
            <div id="teamsActiveChatHeader" style="background:#131418; border-bottom:1px solid #27272a; padding:0.6rem 0.85rem; display:flex; justify-content:space-between; align-items:center;">
              <div style="font-weight:700; color:#f4f4f5; font-size:0.85rem;" id="teamsActiveChatTitle">
                Pilih salah satu obrolan di sebelah kiri
              </div>
              <div id="teamsActiveChatAction" style="display:none;">
                <button class="hb-btn" style="font-size:0.7rem; padding:0.2rem 0.5rem;" onclick="teamsOpenActiveInRemote()">▶️ Buka di Remote Browser</button>
              </div>
            </div>
            <div id="teamsActiveChatMessages" style="flex:1; overflow-y:auto; padding:0.85rem; display:flex; flex-direction:column; gap:0.6rem; background:#090a0f;">
              <div style="color:#71717a; font-size:0.8rem; text-align:center; padding:3rem 1rem;">
                Pilih obrolan dari daftar sebelah kiri untuk membaca isi pesan mentahnya di sini.
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Modal JSON Mentah -->
      <div id="teamsRawJsonModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.8); z-index:9999; align-items:center; justify-content:center; padding:1rem;">
        <div style="background:#18181b; border:1px solid #3f3f46; border-radius:12px; max-width:800px; width:100%; padding:1.5rem; box-shadow:0 20px 25px -5px rgba(0,0,0,0.5);">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem; border-bottom:1px solid #27272a; padding-bottom:0.5rem;">
            <div style="font-weight:700; color:#f4f4f5; font-size:1rem;">{ } Data Mentah JSON Obrolan Teams</div>
            <button onclick="teamsToggleRawJsonModal()" style="background:transparent; border:none; color:#a1a1aa; font-size:1.2rem; cursor:pointer;">✕</button>
          </div>
          <pre id="teamsRawJsonContent" style="background:#090a0f; border:1px solid #334155; border-radius:6px; color:#a3e635; font-family:monospace; font-size:0.72rem; padding:0.75rem; height:380px; overflow:auto; margin:0;"></pre>
          <div style="display:flex; justify-content:flex-end; gap:0.5rem; margin-top:1rem;">
            <button class="hb-btn" onclick="teamsCopyRawJson()">📋 Salin JSON</button>
            <button class="hb-btn hb-btn-primary" onclick="teamsToggleRawJsonModal()">Tutup</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 10: 9ROUTER AI GATEWAY DASHBOARD ==================== -->
__SAHAM_PAGE__
    <div id="page-9router" class="view-section">
      <div class="page-header" style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <h1 style="font-size: 1.2rem; font-weight: 700; color: #fff; margin: 0;">
              9Router AI Gateway Dashboard
            </h1>
            <span class="hermes-badge badge-ok" id="nineRouterLiveBadge">PORT 20128</span>
            <span class="hermes-badge badge-blue">SPEECH &amp; LLM ROUTER</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">
            Gateway multi-provider AI &amp; speech-to-text lokal untuk Westpro. Hubungkan provider (Gemini, dll) dan buat API Key di sini.
          </div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center;">
          <a id="btnOpen9RouterTab" href="" target="_blank" style="background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.45rem 0.85rem; font-size: 0.775rem; font-weight: 600; cursor: pointer; text-decoration: none; display: flex; align-items: center; gap: 0.35rem;">
            ↗ Buka di Tab Baru
          </a>
          <button onclick="reload9RouterIframe()" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; border-radius: 6px; padding: 0.45rem 0.75rem; font-size: 0.775rem; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;">
            ↻ Muat Ulang Frame
          </button>
        </div>
      </div>

      <div style="background: rgba(37,99,235,0.1); border: 1px solid rgba(59,130,246,0.3); border-radius: 8px; padding: 0.65rem 1rem; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
        <div style="font-size: 0.8rem; color: #93c5fd;">
          🔑 <b>Info Login Dashboard:</b> Kata sandi awal (dari <code>~/.9router/env</code>): <code style="background:rgba(0,0,0,0.4); padding:0.15rem 0.4rem; border-radius:4px; color:#fde047; font-weight:700;">12345678</code>
        </div>
        <div style="font-size: 0.75rem; color: #94a3b8;">
          Endpoint API Lokal: <code style="color:#38bdf8;">http://127.0.0.1:20128/v1</code> &bull; LAN: <code style="color:#38bdf8;">http://172.16.5.107:20128</code> &bull; Publik (Cloudflare): <a href="https://ai.westpro.my.id" target="_blank" style="color:#60a5fa; text-decoration:underline;">ai.westpro.my.id</a>
        </div>
      </div>

      <div style="width: 100%; height: calc(100vh - 210px); min-height: 700px; background: #0c0d11; border: 1px solid #272832; border-radius: 10px; overflow: hidden; position: relative;">
        <iframe id="nineRouterFrame" src="" style="width: 100%; height: 100%; border: none; display: block;" allow="clipboard-read; clipboard-write"></iframe>
      </div>
    </div>

  </div>

      <!-- ==================== HALAMAN 11: SPECTRUM MYTASK AUTOMATION & AUDIT ==================== -->
    <div id="page-spectrum" class="view-section">
      <div class="page-header" style="margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <h1 style="font-size: 1.2rem; font-weight: 700; color: #fff; margin: 0;">
              SPEctrum MyTask Automation &amp; Reminder
            </h1>
            <span class="hermes-badge badge-blue" id="specUserBadge">aris.aris@spesolution.com</span>
            <span class="hermes-badge badge-ok" id="specStatusBadge">Memuat...</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.25rem;">
            Otomasi jam kerja harian berbasis AI (target &ge; 8 jam) dengan pembagian proporsional task backlog dan peringatan riwayat 3 bulan.
          </div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
          <button onclick="refreshSpectrumMyTask()" style="background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.45rem 0.85rem; font-size: 0.775rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;">
            ↻ Muat Ulang Hari Ini
          </button>
          <button onclick="runSpectrumAudit3Months(true)" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; border-radius: 6px; padding: 0.45rem 0.75rem; font-size: 0.775rem; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;">
            🔍 Audit 3 Bulan Lalu
          </button>
        </div>
      </div>

      <!-- Quick Metrics Grid -->
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem;">
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 8px; padding: 0.85rem 1rem;">
          <div style="font-size: 0.75rem; color: #94a3b8; font-weight: 600;">TOTAL JAM HARI INI</div>
          <div id="metricTodayHours" style="font-size: 1.4rem; font-weight: 700; color: #38bdf8; margin-top: 0.25rem;">--</div>
          <div id="metricTodayNote" style="font-size: 0.7rem; color: #64748b; margin-top: 0.2rem;">Target minimal 8 jam</div>
        </div>
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 8px; padding: 0.85rem 1rem;">
          <div style="font-size: 0.75rem; color: #94a3b8; font-weight: 600;">MEETING TEAMS (READ-ONLY)</div>
          <div id="metricMeetingDuration" style="font-size: 1.4rem; font-weight: 700; color: #a78bfa; margin-top: 0.25rem;">--</div>
          <div id="metricMeetingCount" style="font-size: 0.7rem; color: #64748b; margin-top: 0.2rem;">0 Jadwal Meeting</div>
        </div>
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 8px; padding: 0.85rem 1rem;">
          <div style="font-size: 0.75rem; color: #94a3b8; font-weight: 600;">TASK BACKLOG AKTIF</div>
          <div id="metricBacklogCount" style="font-size: 1.4rem; font-weight: 700; color: #34d399; margin-top: 0.25rem;">--</div>
          <div id="metricBacklogNote" style="font-size: 0.7rem; color: #64748b; margin-top: 0.2rem;">Bisa diedit &amp; auto-fill</div>
        </div>
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 8px; padding: 0.85rem 1rem;">
          <div style="font-size: 0.75rem; color: #94a3b8; font-weight: 600;">AUDIT 3 BULAN (HARI KERJA)</div>
          <div id="metricAuditSummary" style="font-size: 1.4rem; font-weight: 700; color: #fbbf24; margin-top: 0.25rem;">--</div>
          <div id="metricAuditNote" style="font-size: 0.7rem; color: #64748b; margin-top: 0.2rem;">Memeriksa riwayat...</div>
        </div>
      </div>

      <!-- Main Two Column Content -->
      <div style="display: grid; grid-template-columns: 1.3fr 1fr; gap: 1rem; align-items: start;">
        
        <!-- Left Column: Daily Hours Form & AI Auto-Fill -->
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 10px; padding: 1.25rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <h2 style="font-size: 1rem; font-weight: 700; color: #fff; margin: 0;">
                Pengisian Jam Kerja Harian
              </h2>
              <span id="activeDateBadge" style="background: rgba(56,189,248,0.15); color: #38bdf8; font-weight: 600; font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px;">--</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.35rem;">
              <button onclick="changeSpectrumDate(-1)" title="Hari Sebelumnya" style="padding: 0.3rem 0.6rem; font-size: 0.75rem; border-radius: 4px; border: 1px solid #353540; background: #202026; color: #fff; cursor: pointer;">&lt;</button>
              <input type="date" id="inputSpectrumDate" onchange="loadSpectrumDateData(this.value)" style="background: #202026; color: #f4f4f5; border: 1px solid #353540; border-radius: 4px; padding: 0.25rem 0.5rem; font-size: 0.75rem;">
              <button onclick="changeSpectrumDate(1)" title="Hari Berikutnya" style="padding: 0.3rem 0.6rem; font-size: 0.75rem; border-radius: 4px; border: 1px solid #353540; background: #202026; color: #fff; cursor: pointer;">&gt;</button>
              <button onclick="loadSpectrumDateDataToday()" style="padding: 0.3rem 0.6rem; font-size: 0.75rem; border-radius: 4px; border: 1px solid #353540; background: #202026; color: #38bdf8; cursor: pointer; font-weight: 600;">Hari Ini</button>
            </div>
          </div>

          <!-- Section: Non Project / Teams Meetings -->
          <div style="margin-bottom: 1.25rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; font-weight: 700; color: #cbd5e1; text-transform: uppercase; letter-spacing: 0.05em;">
                📅 Jadwal Meet dari Teams (Read-Only)
              </span>
              <span id="meetingSubtotalText" style="font-size: 0.75rem; color: #a78bfa; font-weight: 600;">Total: 0m</span>
            </div>
            <div id="teamsMeetingList" style="display: flex; flex-direction: column; gap: 0.4rem;">
              <div style="color: #64748b; font-size: 0.75rem; font-style: italic;">Tidak ada jadwal meeting hari ini.</div>
            </div>
          </div>

          <!-- Section: Project Backlogs (Editable & AI Auto-fill) -->
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; flex-wrap: wrap; gap: 0.5rem;">
              <div>
                <span style="font-size: 0.8rem; font-weight: 700; color: #cbd5e1; text-transform: uppercase; letter-spacing: 0.05em;">
                  ⚡ Task Backlog Proyek (Dapat Diedit)
                </span>
                <div style="font-size: 0.7rem; color: #64748b;">Klik angka durasi untuk menyesuaikan jam. AI membagi proporsi berdasarkan bobot task.</div>
              </div>
              <button onclick="applyAiRecommendationsToInputs()" style="background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.4); color: #34d399; border-radius: 6px; padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;">
                ⚡ Auto-Fill Proporsional AI (&ge; 8 Jam)
              </button>
            </div>
            <div id="backlogTasksList" style="display: flex; flex-direction: column; gap: 0.6rem;">
              <!-- Task rows populated via JS -->
            </div>
          </div>

          <!-- Bottom Action & Live Recalculation Bar -->
          <div style="margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid #242632; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
            <div style="display: flex; align-items: center; gap: 0.75rem;">
              <span style="font-size: 0.8rem; color: #94a3b8;">Total Baru Terhitung:</span>
              <span id="liveCalculatedTotal" style="font-size: 1.15rem; font-weight: 700; color: #38bdf8;">--</span>
              <span id="liveTargetBadge" class="hermes-badge badge-blue">Cek</span>
            </div>
            <button id="btnSubmitSpectrumHours" onclick="submitSpectrumHoursToApi()" style="background: #10b981; color: #fff; border: none; border-radius: 6px; padding: 0.55rem 1.25rem; font-size: 0.825rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 0.4rem;">
              💾 Simpan &amp; Terapkan ke SPEctrum
            </button>
          </div>
        </div>

        <!-- Right Column: 3-Month Audit & Warning System -->
        <div style="background: #13141a; border: 1px solid #242632; border-radius: 10px; padding: 1.25rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <div>
              <h2 style="font-size: 1rem; font-weight: 700; color: #fff; margin: 0;">
                ⚠️ Peringatan Masa Lalu (3 Bulan)
              </h2>
              <div style="font-size: 0.7rem; color: #64748b; margin-top: 0.15rem;">
                Daftar hari kerja (Senin - Jumat) yang jam kerjanya masih kosong atau &lt; 8 jam.
              </div>
            </div>
            <span id="auditBadgeCount" class="hermes-badge badge-yellow">Memeriksa...</span>
          </div>

          <div style="display: flex; gap: 0.4rem; margin-bottom: 0.85rem; flex-wrap: wrap;">
            <button onclick="filterAuditList('all')" id="fltAll" style="background:#202026; color:#fff; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.55rem; font-size:0.7rem; cursor:pointer;">Semua Perlu Perhatian</button>
            <button onclick="filterAuditList('deficient')" id="fltDeficient" style="background:transparent; color:#94a3b8; border:1px solid #2d2e3b; border-radius:4px; padding:0.25rem 0.55rem; font-size:0.7rem; cursor:pointer;">Kurang (&lt; 8 Jam)</button>
            <button onclick="filterAuditList('empty')" id="fltEmpty" style="background:transparent; color:#94a3b8; border:1px solid #2d2e3b; border-radius:4px; padding:0.25rem 0.55rem; font-size:0.7rem; cursor:pointer;">Kosong (0 Jam)</button>
          </div>

          <div id="auditListContainer" style="max-height: 540px; overflow-y: auto; display: flex; flex-direction: column; gap: 0.45rem; padding-right: 0.25rem;">
            <div style="color: #64748b; font-size: 0.75rem; text-align: center; padding: 1.5rem;">
              Sedang memuat data audit 3 bulan...
            </div>
          </div>
        </div>

      </div>
    </div>


  <!-- ==================== ANDROID FULLSCREEN REMOTE MODAL ==================== -->
  <div id="androidFullscreenModal" style="display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:#090a0f; z-index:999999; flex-direction:column; align-items:center; justify-content:space-between; user-select:none;">
    <!-- Top Bar -->
    <div style="height:44px; width:100%; background:#131317; border-bottom:1px solid #222228; display:flex; align-items:center; justify-content:space-between; padding:0 1.25rem; font-size:0.8rem;">
      <div style="display:flex; align-items:center; gap:0.6rem; font-weight:700; color:#fff;">
        <span>Remote Fullscreen:</span>
        <span id="fsDeviceTitle" style="color:#38bdf8;">Samsung SM_T865</span>
        <span class="hermes-badge badge-blue" id="fsDeviceRes" style="font-size:0.65rem;">1600x2560</span>
      </div>
      <div style="display:flex; align-items:center; gap:0.75rem;">
        <select id="fsScreenQuality" onchange="changeScreenQuality(this.value)" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.2rem 0.5rem; font-size:0.72rem; cursor:pointer;">
          <option value="low">Low (Cepat)</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="original">Original HD</option>
        </select>
        <button onclick="exitAndroidFullscreen()" style="background:#ef4444; color:#fff; border:none; border-radius:4px; padding:0.25rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Keluar (Esc)</button>
      </div>
    </div>
    <!-- Interactive Canvas -->
    <div id="fsScreenContainer" style="position:relative; flex:1; width:100%; max-height:calc(100vh - 105px); display:flex; align-items:center; justify-content:center; cursor:crosshair; padding:8px 0;">
      <img id="fsAndroidScreenImg" src="" alt="Layar Android Fullscreen" style="max-height:100%; max-width:100%; object-fit:contain; border-radius:8px; box-shadow:0 10px 30px rgba(0,0,0,0.8);" draggable="false" />
      <div id="fsTapRipple" style="position:absolute; width:28px; height:28px; border-radius:50%; background:rgba(56,189,248,0.7); border:2px solid #fff; pointer-events:none; transform:translate(-50%, -50%) scale(0); opacity:0; transition:transform 0.25s ease-out, opacity 0.25s ease-out;"></div>
    </div>
    <!-- Bottom Floating Navigation Bar -->
    <div style="height:54px; width:100%; background:#131317; border-top:1px solid #222228; display:flex; align-items:center; justify-content:center; gap:0.5rem; padding:0 1rem; flex-wrap:wrap;">
      <button onclick="triggerActiveDeviceAction('back')" class="fs-btn">Back</button>
      <button onclick="triggerActiveDeviceAction('home')" class="fs-btn" style="color:#f59e0b;">Home</button>
      <button onclick="triggerActiveDeviceAction('recent')" class="fs-btn" style="color:#38bdf8;">Recent</button>
      <div style="width:1px; height:22px; background:#353540; margin:0 0.2rem;"></div>
      <button onclick="triggerActiveDeviceAction('swipe_left')" class="fs-btn">Kiri</button>
      <button onclick="triggerActiveDeviceAction('swipe_right')" class="fs-btn">Kanan</button>
      <button onclick="triggerActiveDeviceAction('swipe_up')" class="fs-btn">Atas</button>
      <button onclick="triggerActiveDeviceAction('swipe_down')" class="fs-btn">Bawah</button>
      <div style="width:1px; height:22px; background:#353540; margin:0 0.2rem;"></div>
      <button onclick="triggerActiveDeviceAction('wake')" class="fs-btn" style="color:#ef4444;">Power</button>
    </div>
  </div>

  <!-- ==================== WEBHOOK MOCK RULES MODAL ==================== -->
  <div id="mockRulesModal" style="display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.75); backdrop-filter:blur(4px); z-index:99999; align-items:center; justify-content:center; padding:1rem;">
    <div style="background:#131317; border:1px solid #222228; border-radius:8px; width:680px; max-width:100%; max-height:90vh; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 20px 50px rgba(0,0,0,0.8);">
      <div style="display:flex; justify-content:space-between; align-items:center; padding:0.85rem 1.25rem; border-bottom:1px solid #222228; background:#18181f;">
        <div style="font-weight:700; color:#f1f5f9; font-size:0.9rem;">Aturan Mocking & Respons Webhook</div>
        <button onclick="closeMockRulesModal()" style="background:none; border:none; color:#94a3b8; font-size:1.1rem; cursor:pointer;">✕</button>
      </div>
      <div style="padding:1.25rem; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:1.25rem;">
        <!-- Form Add Rule -->
        <div style="background:#18181f; border:1px solid #262630; border-radius:6px; padding:1rem;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem;">
            <div style="font-weight:600; font-size:0.8rem; color:#38bdf8;" id="mockRuleFormTitle">Buat Aturan Mock Baru</div>
            <button id="btnCancelEditRule" onclick="cancelEditMockRule()" style="display:none; background:none; border:1px solid #3f3f46; color:#94a3b8; font-size:0.7rem; padding:0.2rem 0.5rem; border-radius:4px; cursor:pointer;">Batal Edit</button>
          </div>
          <div style="display:grid; grid-template-columns: 1fr 1fr; gap:0.65rem; margin-bottom:0.65rem;">
            <div>
              <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Nama Aturan:</label>
              <input type="text" id="newRuleName" placeholder="Misal: Mock Xendit Success" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.35rem 0.5rem; color:#fff; font-size:0.75rem;" />
            </div>
            <div>
              <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Path Pattern (Mendukung Wildcard *):</label>
              <input type="text" id="newRulePath" placeholder="/catch/xendit*" value="/catch/*" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.35rem 0.5rem; color:#a3e635; font-family:monospace; font-size:0.75rem;" />
            </div>
          </div>
          <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:0.65rem; margin-bottom:0.65rem;">
            <div>
              <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Method:</label>
              <select id="newRuleMethod" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.35rem 0.5rem; color:#fff; font-size:0.75rem;">
                <option value="ALL">SEMUA (ALL)</option>
                <option value="POST" selected>POST</option>
                <option value="GET">GET</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
                <option value="PATCH">PATCH</option>
              </select>
            </div>
            <div>
              <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">HTTP Status Code:</label>
              <select id="newRuleStatus" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.35rem 0.5rem; color:#fff; font-size:0.75rem;">
                <option value="200" selected>200 OK</option>
                <option value="201">201 Created</option>
                <option value="400">400 Bad Request</option>
                <option value="401">401 Unauthorized</option>
                <option value="404">404 Not Found</option>
                <option value="500">500 Internal Error</option>
              </select>
            </div>
            <div>
              <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Simulasi Latency / Delay:</label>
              <select id="newRuleDelay" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.35rem 0.5rem; color:#fff; font-size:0.75rem;">
                <option value="0" selected>0 ms (Instan)</option>
                <option value="250">250 ms</option>
                <option value="500">500 ms</option>
                <option value="1000">1.000 ms (1 detik)</option>
                <option value="2000">2.000 ms (2 detik)</option>
              </select>
            </div>
          </div>
          <div style="margin-bottom:0.65rem;">
            <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Mock Response Body (JSON / Teks):</label>
            <textarea id="newRuleBody" style="width:100%; height:90px; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.6rem; color:#f1f5f9; font-family:'JetBrains Mono',monospace; font-size:0.72rem; resize:vertical;">{\n  "status": "success",\n  "message": "Custom Mock Response"\n}</textarea>
          </div>
          <button id="btnSubmitMockRule" onclick="saveNewMockRule()" style="background:#2563eb; color:#fff; border:none; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Simpan Aturan</button>
        </div>

        <!-- Rules List -->
        <div>
          <div style="font-weight:600; font-size:0.8rem; color:#cbd5e1; margin-bottom:0.5rem;">Daftar Aturan Aktif:</div>
          <div id="mockRulesListContainer" style="display:flex; flex-direction:column; gap:0.5rem;">
            <div style="color:#64748b; font-size:0.75rem; text-align:center; padding:1rem;">Memuat aturan...</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ==================== REPLAY REQUEST MODAL ==================== -->
  <div id="replayModal" style="display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.75); backdrop-filter:blur(4px); z-index:99999; align-items:center; justify-content:center; padding:1rem;">
    <div style="background:#131317; border:1px solid #222228; border-radius:8px; width:520px; max-width:100%; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 20px 50px rgba(0,0,0,0.8);">
      <div style="display:flex; justify-content:space-between; align-items:center; padding:0.85rem 1.25rem; border-bottom:1px solid #222228; background:#18181f;">
        <div style="font-weight:700; color:#f1f5f9; font-size:0.9rem;">Kirim Ulang Request (Replay)</div>
        <button onclick="closeReplayModal()" style="background:none; border:none; color:#94a3b8; font-size:1.1rem; cursor:pointer;">✕</button>
      </div>
      <div style="padding:1.25rem; display:flex; flex-direction:column; gap:0.75rem;">
        <div style="font-size:0.75rem; color:#94a3b8;">
          Request ini akan dikirim ulang secara identik (Method, Headers, dan Body) ke target URL:
        </div>
        <div>
          <label style="display:block; font-size:0.7rem; color:#94a3b8; margin-bottom:0.25rem;">Target URL Pengiriman Ulang:</label>
          <input type="text" id="replayTargetUrlInput" style="width:100%; background:#090a0f; border:1px solid #334155; border-radius:4px; padding:0.4rem 0.6rem; color:#38bdf8; font-family:'JetBrains Mono',monospace; font-size:0.75rem;" />
        </div>
        <div id="replayStatusBox" style="display:none; font-size:0.75rem; padding:0.6rem; border-radius:4px; font-family:monospace; white-space:pre-wrap;"></div>
        <div style="display:flex; justify-content:flex-end; gap:0.5rem; margin-top:0.5rem;">
          <button onclick="closeReplayModal()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.4rem 0.85rem; font-size:0.75rem; cursor:pointer;">Batal</button>
          <button onclick="submitReplayRequest()" id="btnSubmitReplay" style="background:#2563eb; color:#fff; border:none; border-radius:4px; padding:0.4rem 1rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Kirim Ulang</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ==================== WEATHER & SOLAR CLIMATE OUTLOOK MODAL ==================== -->
  <div id="weatherForecastModal" style="display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.8); backdrop-filter:blur(6px); z-index:99999; align-items:center; justify-content:center; padding:1rem; box-sizing:border-box;">
    <div style="background:#111217; border:1px solid #22222d; border-radius:12px; width:920px; max-width:98vw; max-height:90vh; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 25px 60px rgba(0,0,0,0.85);">
      
      <!-- Modal Header -->
      <div style="display:flex; justify-content:space-between; align-items:center; padding:1rem 1.25rem; border-bottom:1px solid #22222d; background:#16161e;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <span style="font-size:1.15rem;">🌤️</span>
            <span style="font-weight:700; color:#f1f5f9; font-size:1.05rem;">Prakiraan Cuaca 14 Hari & Prospek Energi PLTS</span>
            <span class="hermes-badge badge-blue" style="font-size:0.65rem;">BMKG + Open-Meteo Satelit</span>
          </div>
          <div style="font-size:0.72rem; color:#94a3b8; margin-top:0.2rem;">
            Stasiun Resmi BMKG Desa Kaliagung Sentolo (34.01.06.2006) • D.I. Yogyakarta • Koordinat (-7.834°, 110.219°)
          </div>
        </div>
        <button onclick="closeWeatherForecastModal()" style="background:none; border:none; color:#94a3b8; font-size:1.35rem; cursor:pointer; padding:0.25rem 0.5rem; line-height:1;" title="Tutup">&times;</button>
      </div>

      <!-- Modal Body (Scrollable) -->
      <div style="padding:1.25rem; overflow-y:auto; display:flex; flex-direction:column; gap:1.25rem;">
        
        <!-- Monthly Climate Outlook Highlights -->
        <div style="background:rgba(2, 132, 199, 0.08); border:1px solid rgba(56, 189, 248, 0.25); border-radius:10px; padding:1rem;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem; flex-wrap:wrap; gap:0.5rem;">
            <div style="font-weight:700; color:#38bdf8; font-size:0.88rem; display:flex; align-items:center; gap:0.4rem;">
              <span>🗓️</span> <span id="modalOutlookMonthTitle">Prospek Iklim & Panen Surya Bulan Ini</span>
            </div>
            <span id="modalOutlookSeasonBadge" class="hermes-badge badge-purple" style="font-size:0.68rem; padding:0.2rem 0.5rem;">Pancaroba (Transisi Kemarau ke Penghujan)</span>
          </div>
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:0.75rem; margin-bottom:0.75rem;">
            <div style="background:rgba(0,0,0,0.3); padding:0.65rem 0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.05);">
              <div style="font-size:0.68rem; color:#94a3b8;">☀️ Jam Puncak Terik / Hari</div>
              <div id="modalOutlookPeakSun" style="font-size:1.15rem; font-weight:700; color:#fbbf24; margin-top:2px;">~5.2 Jam</div>
              <div style="font-size:0.62rem; color:#64748b;">Rata-rata jam surya efektif</div>
            </div>
            <div style="background:rgba(0,0,0,0.3); padding:0.65rem 0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.05);">
              <div style="font-size:0.68rem; color:#94a3b8;">⚡ Estimasi Potensi Panen PLTS</div>
              <div id="modalOutlookSolarPot" style="font-size:1.15rem; font-weight:700; color:#34d399; margin-top:2px;">~79.1 kWh</div>
              <div style="font-size:0.62rem; color:#64748b;">Potensi total energi bulanan</div>
            </div>
            <div style="background:rgba(0,0,0,0.3); padding:0.65rem 0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.05);">
              <div style="font-size:0.68rem; color:#94a3b8;">🌡️ Rentang Rata-rata Suhu</div>
              <div id="modalOutlookTempRange" style="font-size:1.15rem; font-weight:700; color:#f8fafc; margin-top:2px;">32.5° / 23.2°C</div>
              <div style="font-size:0.62rem; color:#64748b;">Siang terik / malam sejuk</div>
            </div>
            <div style="background:rgba(0,0,0,0.3); padding:0.65rem 0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.05);">
              <div style="font-size:0.68rem; color:#94a3b8;">🌧️ Frekuensi Hari Hujan</div>
              <div id="modalOutlookRainDays" style="font-size:1.15rem; font-weight:700; color:#60a5fa; margin-top:2px;">10 - 12 Hari</div>
              <div style="font-size:0.62rem; color:#64748b;">Dominan sore & malam hari</div>
            </div>
          </div>
          <div id="modalOutlookAdvice" style="font-size:0.75rem; color:#cbd5e1; line-height:1.4; background:rgba(0,0,0,0.25); padding:0.5rem 0.75rem; border-radius:6px; border-left:3px solid #38bdf8;">
            <!-- Advice text -->
          </div>
        </div>

        <!-- Saran Tanam & Iklim Bulanan -->
        <div style="background:rgba(16, 185, 129, 0.07); border:1px solid rgba(52, 211, 153, 0.25); border-radius:10px; padding:1rem;">
          <div style="font-weight:700; color:#34d399; font-size:0.88rem; margin-bottom:0.5rem; display:flex; align-items:center; gap:0.4rem;">
            <span>🌱</span> <span>Perkiraan Tanam (indikatif)</span>
          </div>
          <div id="modalPlantingAdvice" style="font-size:0.75rem; color:#cbd5e1; line-height:1.5; margin-bottom:0.75rem;"></div>
          <div style="overflow-x:auto; border:1px solid #22222d; border-radius:8px; background:#0d0e12;">
            <table style="width:100%; border-collapse:collapse; font-size:0.75rem; text-align:left;">
              <thead>
                <tr style="background:#16161e; color:#94a3b8; border-bottom:1px solid #22222d;">
                  <th style="padding:0.6rem 0.75rem; font-weight:600;">Bulan</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Suhu (Maks/Min)</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Curah Hujan</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Hari Hujan</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Jam Puncak Surya</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600;">Musim &amp; Saran</th>
                </tr>
              </thead>
              <tbody id="modalClimateTableBody"></tbody>
            </table>
          </div>
          <div style="font-size:0.64rem; color:#64748b; margin-top:0.4rem;">Rata-rata 5 tahun terakhir dari arsip Open-Meteo untuk lokasi ini. Saran bersifat umum; sesuaikan dengan jenis tanaman, jenis tanah, dan kalender tanam resmi setempat.</div>
        </div>

        <!-- 14-Day Forecast Table Section -->
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.65rem; flex-wrap:wrap; gap:0.5rem;">
            <div style="font-weight:700; color:#f1f5f9; font-size:0.88rem; display:flex; align-items:center; gap:0.4rem;">
              <span>📅</span> <span>Rincian Prakiraan 14 Hari (Minggu Ini & Minggu Depan)</span>
            </div>
            <div style="font-size:0.68rem; color:#94a3b8;">
              <span style="display:inline-block; width:8px; height:8px; background:#0284c7; border-radius:2px; margin-right:4px;"></span>Minggu Ini
              <span style="display:inline-block; width:8px; height:8px; background:#a855f7; border-radius:2px; margin-left:8px; margin-right:4px;"></span>Minggu Depan
            </div>
          </div>

          <div style="overflow-x:auto; border:1px solid #22222d; border-radius:8px; background:#0d0e12;">
            <table style="width:100%; border-collapse:collapse; font-size:0.75rem; text-align:left;">
              <thead>
                <tr style="background:#16161e; color:#94a3b8; border-bottom:1px solid #22222d;">
                  <th style="padding:0.6rem 0.75rem; font-weight:600;">Hari & Tanggal</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600;">Cuaca</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Suhu (Maks/Min)</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Peluang Hujan</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Curah Hujan</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600; text-align:center;">Indeks UV</th>
                  <th style="padding:0.6rem 0.75rem; font-weight:600;">Potensi Beban PLTS</th>
                </tr>
              </thead>
              <tbody id="modalForecastTableBody">
                <!-- Dynamically populated rows -->
              </tbody>
            </table>
          </div>
        </div>

      </div>

      <!-- Modal Footer -->
      <div style="padding:0.75rem 1.25rem; border-top:1px solid #22222d; background:#16161e; display:flex; justify-content:space-between; align-items:center; font-size:0.7rem; color:#64748b;">
        <div>Pembaruan berkala otomatis setiap 15 menit melalui API BMKG Indonesia & model resolusi tinggi Open-Meteo.</div>
        <button onclick="closeWeatherForecastModal()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.4rem 1rem; font-size:0.75rem; cursor:pointer;">Tutup</button>
      </div>

    </div>
  </div>

  <!-- ==================== ZTE BMS CELL BALANCING & DETAILED TELEMETRY MODAL ==================== -->
  <!-- ==================== ZTE BMS CELL BALANCING & DETAILED TELEMETRY MODAL ==================== -->
  <div id="zteBmsModal" style="display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.85); backdrop-filter:blur(6px); z-index:99999; align-items:center; justify-content:center; padding:1rem; box-sizing:border-box;">
    <div style="background:#111217; border:1px solid #22222d; border-radius:12px; width:920px; max-width:98vw; max-height:92vh; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 25px 60px rgba(0,0,0,0.85);">
      
      <!-- Modal Header -->
      <div style="display:flex; justify-content:space-between; align-items:center; padding:1rem 1.25rem; border-bottom:1px solid #22222d; background:#16161e;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
            <span style="font-size:1.15rem;">🔋</span>
            <span style="font-weight:700; color:#f1f5f9; font-size:1.05rem;" id="modalBmsTitle">Telemetri BMS ZTE Birubatt FB100B3</span>
            <span class="hermes-badge badge-ok" id="modalBmsStatusBadge" style="font-size:0.65rem;">ONLINE (RS485)</span>
          </div>
          <div style="font-size:0.72rem; color:#94a3b8; margin-top:0.2rem;" id="modalBmsSubtitle">
            LiFePO4 15S • Port /dev/ttyUSB0 • 9600 bps Even
          </div>
        </div>
        <button onclick="closeZteBmsModal()" style="background:none; border:none; color:#94a3b8; font-size:1.35rem; cursor:pointer; padding:0.25rem 0.5rem; line-height:1;" title="Tutup">&times;</button>
      </div>

      <!-- Modal Body -->
      <div style="padding:1.25rem; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:1rem;">
        <!-- Top 4 Summary Metrics -->
        <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:0.75rem;">
          <div class="panel" style="margin:0; padding:0.75rem; background:rgba(255,255,255,0.03);">
            <div style="font-size:0.68rem; color:#94a3b8;">Tegangan Bank & Bus</div>
            <div style="font-size:1.35rem; font-weight:700; color:#34d399; margin:0.2rem 0;" id="mBmsPackV">-- V</div>
            <div style="font-size:0.68rem; color:#64748b;">Busbar: <b id="mBmsBusV" style="color:#cbd5e1;">-- V</b></div>
          </div>
          <div class="panel" style="margin:0; padding:0.75rem; background:rgba(255,255,255,0.03);">
            <div style="font-size:0.68rem; color:#94a3b8;">Total Arus & Daya</div>
            <div style="font-size:1.35rem; font-weight:700; color:#38bdf8; margin:0.2rem 0;" id="mBmsCurr">-- A</div>
            <div style="font-size:0.68rem; color:#64748b;">Daya Net: <b id="mBmsWatt" style="color:#cbd5e1;">-- W</b></div>
          </div>
          <div class="panel" style="margin:0; padding:0.75rem; background:rgba(255,255,255,0.03);">
            <div style="font-size:0.68rem; color:#94a3b8;">SOC Aktual Bank</div>
            <div style="font-size:1.35rem; font-weight:700; color:#fbbf24; margin:0.2rem 0;" id="mBmsSoc">--%</div>
            <div style="font-size:0.68rem; color:#64748b;">Kapasitas: <b id="mBmsCapacity" style="color:#cbd5e1;">-- Ah</b></div>
          </div>
          <div class="panel" style="margin:0; padding:0.75rem; background:rgba(255,255,255,0.03);">
            <div style="font-size:0.68rem; color:#94a3b8;">Suhu Internal Sel</div>
            <div style="font-size:1.35rem; font-weight:700; color:#f87171; margin:0.2rem 0;" id="mBmsTemp">-- °C</div>
            <div style="font-size:0.68rem; color:#64748b;"><span id="mBmsSensorCount">Sensor Sel Aktif</span></div>
          </div>
        </div>

        <!-- Cell Balance Analysis Banner -->
        <div style="background:rgba(56,189,248,0.08); border:1px solid rgba(56,189,248,0.25); border-radius:8px; padding:0.75rem 1rem; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
          <div>
            <div style="font-size:0.8rem; font-weight:700; color:#38bdf8; display:flex; align-items:center; gap:0.4rem;">
              <span>⚖️ Status Keseimbangan:</span>
              <span id="mBmsBalanceLabel" class="hermes-badge badge-warn" style="font-size:0.7rem;">--</span>
            </div>
            <div style="font-size:0.72rem; color:#cbd5e1; margin-top:0.2rem;">
              Sel Tertinggi: <b id="mBmsMaxCell" style="color:#34d399;">-- V</b> • Sel Terendah: <b id="mBmsMinCell" style="color:#f87171;">-- V</b> • Deviasi Terbesar: <b id="mBmsDeltaMv" style="color:#fbbf24;">-- mV</b>
            </div>
          </div>
          <div style="font-size:0.68rem; color:#94a3b8; text-align:right;">
            Pembaruan Terakhir: <span id="mBmsLastUpdate" style="color:#fff;">--:--:--</span>
          </div>
        </div>

        <!-- PACK SELECTOR TABS -->
        <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #22222d; padding-bottom:0.5rem; margin-top:0.25rem; flex-wrap:wrap; gap:0.5rem;">
          <div style="display:flex; gap:0.4rem;" id="mBmsTabButtons">
            <!-- Dynamically populated or switchable -->
          </div>
          <div style="font-size:0.7rem; color:#64748b;">
            Standar LiFePO4: 2.80V (0%) ~ 3.45V (100%)
          </div>
        </div>

        <!-- PACKS CONTAINER (Dynamic Cards & 15-cell Grids) -->
        <div id="mBmsPacksContainer" style="display:flex; flex-direction:column; gap:1.25rem;">
          <!-- Rendered dynamically -->
        </div>

      </div>

      <!-- Modal Footer -->
      <div style="padding:0.75rem 1.25rem; border-top:1px solid #22222d; background:#16161e; display:flex; justify-content:space-between; align-items:center;">
        <span style="font-size:0.72rem; color:#64748b;">RS485 Port: <b style="color:#cbd5e1;">/dev/ttyUSB0</b> • Modbus RTU Even Parity • Dual Pack</span>
        <button onclick="closeZteBmsModal()" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:4px; padding:0.4rem 1rem; font-size:0.75rem; cursor:pointer;">Tutup</button>
      </div>

    </div>
  </div>

  <!-- ==================== POPUP MODAL IZIN-IZIN ==================== -->
  <div id="permissionModal" style="display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(6px); z-index: 100000; align-items: center; justify-content: center; padding: 1rem;">
    <div style="background: #131317; border: 1px solid #262630; border-radius: 12px; width: 100%; max-width: 480px; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.7); overflow: hidden;">
      <div style="padding: 1.25rem 1.5rem; border-bottom: 1px solid #222228; display: flex; align-items: center; justify-content: space-between;">
        <div>
          <h3 style="font-size: 1.05rem; font-weight: 700; color: #fff;">Izin Perangkat & Notifikasi</h3>
          <p style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.15rem;">Aktifkan mikrofon, audio speaker laptop, dan notifikasi browser untuk rapat serta telemetri.</p>
        </div>
        <button onclick="closePermissionModal(true)" style="background: none; border: none; color: var(--gf-text-muted); font-size: 1.25rem; cursor: pointer; padding: 0.25rem;" title="Tutup">&times;</button>
      </div>

      <div style="padding: 1.25rem 1.5rem; display: flex; flex-direction: column; gap: 0.85rem;">
        <!-- NON-SECURE CONTEXT WARNING -->
        <div id="insecureContextAlert" style="display: none; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 0.75rem; font-size: 0.75rem; color: #f87171;">
          Browser membatasi izin mikrofon dan audio sistem pada HTTP tanpa enkripsi. Buka melalui <a href="https://westpro.my.id" style="color: #38bdf8; text-decoration: underline;">https://westpro.my.id</a> untuk mengaktifkan.
        </div>

        <!-- PERMISSION 1: MIKROFON -->
        <div style="background: #18181f; border: 1px solid #262630; border-radius: 8px; padding: 0.85rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">Mikrofon</div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
              Perekaman audio suara Anda via mikrofon laptop / headset.
            </div>
            <div style="margin-top: 0.35rem;">
              <span id="badgeMicStatus" class="hermes-badge badge-blue">Belum Diizinkan</span>
            </div>
          </div>
          <div>
            <button id="btnGrantMic" onclick="requestMicrophonePermission()" style="background: #2563eb; color: white; border: none; border-radius: 4px; padding: 0.4rem 0.85rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap;">
              Izinkan
            </button>
          </div>
        </div>

        <!-- PERMISSION 2: AUDIO SPEAKER / SISTEM LAPTOP -->
        <div style="background: #18181f; border: 1px solid #262630; border-radius: 8px; padding: 0.85rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff; display: flex; align-items: center; gap: 0.4rem;">
              <span>Audio Speaker / Laptop</span>
              <span style="font-size: 0.65rem; background: rgba(56, 189, 248, 0.15); color: #38bdf8; padding: 0.1rem 0.35rem; border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.3);">Google Meet/Zoom</span>
            </div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
              Menangkap suara rekan rapat langsung dari speaker laptop / tab browser.
            </div>
            <div style="margin-top: 0.35rem;">
              <span id="badgeSysAudioStatus" class="hermes-badge badge-blue">Didukung (Perlu Izin)</span>
            </div>
          </div>
          <div>
            <button id="btnGrantSysAudio" onclick="requestSystemAudioPermission()" style="background: #2563eb; color: white; border: none; border-radius: 4px; padding: 0.4rem 0.85rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap;">
              Izinkan
            </button>
          </div>
        </div>

        <!-- PERMISSION 3: NOTIFIKASI -->
        <div style="background: #18181f; border: 1px solid #262630; border-radius: 8px; padding: 0.85rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">Notifikasi Browser</div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
              Pemberitahuan peringatan baterai dan kedatangan kode OTP.
            </div>
            <div style="margin-top: 0.35rem;">
              <span id="badgeNotifStatus" class="hermes-badge badge-blue">Belum Diizinkan</span>
            </div>
          </div>
          <div>
            <button id="btnGrantNotif" onclick="requestNotificationPermission()" style="background: #2563eb; color: white; border: none; border-radius: 4px; padding: 0.4rem 0.85rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap;">
              Izinkan
            </button>
          </div>
        </div>

        <!-- HELPER TEXT IF BLOCKED -->
        <div id="blockedHelperText" style="display: none; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 6px; padding: 0.75rem; font-size: 0.75rem; color: #fbbf24;">
          Jika izin diblokir, klik ikon setelan situs pada bilah alamat URL peramban untuk mengaktifkan izin, lalu muat ulang halaman.
        </div>
      </div>

      <div style="padding: 1rem 1.5rem; background: #0f0f13; border-top: 1px solid #222228; display: flex; align-items: center; justify-content: flex-end; gap: 0.75rem;">
        <button onclick="closePermissionModal(true)" style="background: transparent; color: var(--gf-text-muted); border: 1px solid #353540; border-radius: 4px; padding: 0.45rem 0.85rem; font-size: 0.775rem; cursor: pointer;">
          Nanti Saja
        </button>
        <button id="btnGrantAll" onclick="requestAllPermissions()" style="background: #2563eb; color: white; border: none; border-radius: 4px; padding: 0.45rem 1.15rem; font-size: 0.775rem; font-weight: 600; cursor: pointer;">
          Izinkan Semua
        </button>
      </div>
    </div>
  </div>

  <div id="toastContainer" style="position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 100005; display: flex; flex-direction: column; gap: 0.5rem; pointer-events: none;"></div>

  <!-- JAVASCRIPT LOGIC -->
  <script>
    let CURRENT_USER_EMAIL = "__USER_EMAIL__";
    let CURRENT_USER_NAME = "__USER_NAME__";
    let CURRENT_USER_ROLE = "__USER_ROLE__";
    const IS_ARES_OWNER = (CURRENT_USER_EMAIL.trim().toLowerCase() === "areskretindo@gmail.com");

    let currentRange = '15m';
    let liveChart = null;
    let histPowerChart = null;
    let histBattChart = null;
    let histVoltChart = null;
    let histTempChart = null;

    const liveDataPoints = {
      labels: [],
      pvWatts: [],
      loadWatts: []
    };

    const PAGE_TITLES = {
      solar: 'Solar & Inverter Live - Westpro Home Server',
      history: 'Riwayat & Analitik Grafik - Westpro Home Server',
      'inverter-settings': 'Setelan Inverter & BMS - Westpro Home Server',
      hermes: 'Hermes AI Agent Orchestrator - Westpro Home Server',
      meeting: 'Notulen Meeting AI - Westpro Home Server',
      android: 'Android Remote & Appium - Westpro Home Server',
      otp: 'Kotak Masuk OTP - Westpro Home Server',
      webhook: 'Webhook & API Monitor - Westpro Home Server',
      profile: 'Profil Pengguna & SPEctrum - Westpro Home Server',
      'spectrum-config': 'Profil Pengguna & SPEctrum - Westpro Home Server',
      system: 'Sistem 24/7 & Resource Server - Westpro Home Server',
      '9router': '9Router AI Gateway - Westpro Home Server',
      webmail: 'Westpro Mail - Westpro Home Server',
      teams: 'Microsoft Teams & AI Digest - Westpro Home Server',
      saham: 'Analisis Saham IHSG - Westpro Home Server',
      'spectrum': 'SPEctrum MyTask & Audit - Westpro Home Server'
    };

    const OWNER_ONLY_PAGES = ['solar', 'history', 'inverter-settings', 'otp', 'webhook', 'system', '9router', 'teams', 'saham'];

    function reload9RouterIframe() {
      const iframe = document.getElementById('nineRouterFrame');
      const host = window.location.hostname || '172.16.5.107';
      let targetUrl = '';
      if (host.includes('westpro.my.id')) {
        targetUrl = 'https://ai.westpro.my.id';
      } else {
        targetUrl = 'http://' + host + ':20128';
      }
      const btn = document.getElementById('btnOpen9RouterTab');
      if (btn) btn.href = targetUrl;
      if (iframe) {
        if (!iframe.src || iframe.src === 'about:blank' || (!iframe.src.includes(':20128') && !iframe.src.includes('ai.westpro.my.id'))) {
          iframe.src = targetUrl;
        }
      }
    }

    function navigateToPage(pageId, pushState = true) {
      if (!IS_ARES_OWNER && (OWNER_ONLY_PAGES.includes(pageId) || pageId === 'overview')) {
        pageId = 'hermes';
      }
      if (pageId === 'overview') pageId = IS_ARES_OWNER ? 'solar' : 'hermes';
      if (pageId === 'spectrum-config') pageId = 'profile';
      if (!PAGE_TITLES[pageId]) pageId = IS_ARES_OWNER ? 'solar' : 'hermes';

      if (pageId !== 'webhook' && typeof stopWebhookAutoStream === 'function') {
        stopWebhookAutoStream();
      }
      if (pageId !== 'webmail' && typeof wmStopPolling === 'function') wmStopPolling();
      if (pageId !== 'teams' && typeof teamsStopPolling === 'function') teamsStopPolling();
      if (pageId !== 'saham' && typeof shStopPolling === 'function') shStopPolling();

      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-section').forEach(sec => sec.classList.remove('active'));
      
      const navBtn = document.getElementById('nav-' + pageId) || (pageId === 'profile' ? document.getElementById('nav-spectrum-config') : null);
      if (navBtn) navBtn.classList.add('active');

      const targetSec = document.getElementById('page-' + pageId) || document.getElementById('view-' + pageId) || (pageId === 'profile' ? document.getElementById('page-spectrum-config') : null);
      if (targetSec) targetSec.classList.add('active');

      document.title = PAGE_TITLES[pageId] || 'Westpro Home Server';

      if (pushState) {
        try {
          window.history.pushState({ page: pageId }, '', '/' + pageId);
        } catch(e) {}
      }

      if (pageId === 'inverter-settings') {
        loadInverterSettings();
        loadBmsConfig();
      } else if (pageId === 'spectrum') {
        loadSpectrumMyTaskPage();
      } else if (pageId === '9router') {
        reload9RouterIframe();
      } else if (pageId === 'history') {
        loadPLTSAnalysisDashboard();
      } else if (pageId === 'hermes') {
        fetchHermesStatus();
        loadSpectrumWorkspaceData();
        loadHermesBriefing();
      } else if (pageId === 'android') {
        fetchAppiumDevices();
        setTimeout(initScreenInteractiveListeners, 200);
      } else if (pageId === 'otp') {
        loadMailConfig();
        loadOtpInbox();
      } else if (pageId === 'webhook') {
        loadWebhookRequests(true);
        loadWebhookMockRules();
        startWebhookAutoStream();
      } else if (pageId === 'webmail') {
        wmOnShow();
      } else if (pageId === 'teams') {
        teamsOnShow();
      } else if (pageId === 'saham') {
        shOnShow();
      } else if (pageId === 'profile' || pageId === 'spectrum-config') {
        loadUserSpectrumConfig();
        loadEmailPrefs();
      } else if (pageId === 'system') {
        fetchDeployStatus();
        fetchSystemResources(true);
        loadEmailStatus();
      } else if (pageId === 'meeting') {
        renderMeetingContext();
        checkSpeechEngine();
        loadPastMeetings();
        loadMeetingSchedulesAndSprint();
        checkAllPermissions().then(({ micGranted }) => {
          if (!micGranted) {
            setTimeout(openPermissionModal, 400);
          }
        });
      } else if (pageId === 'system') {
        fetchDeployStatus();
      }
    }

    function switchTab(tabId) {
      navigateToPage(tabId, true);
    }

    window.addEventListener('popstate', (e) => {
      const page = (e.state && e.state.page) || (window.location.pathname.startsWith('/') ? window.location.pathname.slice(1) : window.location.pathname) || 'solar';
      navigateToPage(page, false);
    });

    async function handleLogout() {
      if (!confirm('Apakah Anda yakin ingin keluar dari akun?')) return;
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } catch(e) {}
      window.location.href = '/login';
    }

    // Toast Notification System
    function showToast(message, type = 'info') {
      const container = document.getElementById('toastContainer');
      if (!container) return;
      const toast = document.createElement('div');
      const bg = type === 'success' 
        ? 'linear-gradient(135deg, #059669, #10b981)' 
        : (type === 'error' ? 'linear-gradient(135deg, #dc2626, #ef4444)' : 'linear-gradient(135deg, #0284c7, #38bdf8)');
      toast.style.cssText = `background: ${bg}; color: white; padding: 0.75rem 1.25rem; border-radius: 8px; font-size: 0.825rem; font-weight: 600; box-shadow: 0 10px 25px rgba(0,0,0,0.5); pointer-events: auto; animation: slideToast 0.3s ease-out; display: flex; align-items: center; gap: 0.5rem; max-width: 380px;`;
      toast.innerHTML = message;
      container.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.4s ease';
        setTimeout(() => toast.remove(), 400);
      }, 4000);
    }

    // Permission Modal Logic
    async function checkAllPermissions() {
      const isSec = window.isSecureContext || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
      const secAlert = document.getElementById('insecureContextAlert');
      if (secAlert) secAlert.style.display = isSec ? 'none' : 'block';

      let micGranted = false;
      let notifGranted = false;
      let sysAudioGranted = false;

      // 1. Notification
      const notifBadge = document.getElementById('badgeNotifStatus');
      const notifBtn = document.getElementById('btnGrantNotif');
      if (window.Notification) {
        if (Notification.permission === 'granted') {
          notifGranted = true;
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-ok'; notifBadge.textContent = 'Diizinkan'; }
          if (notifBtn) { notifBtn.style.display = 'none'; }
        } else if (Notification.permission === 'denied') {
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-warn'; notifBadge.textContent = 'Diblokir'; }
        } else {
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-blue'; notifBadge.textContent = 'Belum Diizinkan'; }
        }
      }

      // 2. Microphone
      const micBadge = document.getElementById('badgeMicStatus');
      const micBtn = document.getElementById('btnGrantMic');
      if (navigator.permissions && navigator.permissions.query) {
        try {
          const micStatus = await navigator.permissions.query({ name: 'microphone' });
          if (micStatus.state === 'granted') {
            micGranted = true;
            if (micBadge) { micBadge.className = 'hermes-badge badge-ok'; micBadge.textContent = 'Diizinkan'; }
            if (micBtn) { micBtn.style.display = 'none'; }
          } else if (micStatus.state === 'denied') {
            if (micBadge) { micBadge.className = 'hermes-badge badge-warn'; micBadge.textContent = 'Diblokir'; }
          } else {
            if (micBadge) { micBadge.className = 'hermes-badge badge-blue'; micBadge.textContent = 'Belum Diizinkan'; }
          }
          micStatus.onchange = () => checkAllPermissions();
        } catch (e) {}
      }

      // 3. Audio Speaker / Sistem Laptop
      const sysAudioBadge = document.getElementById('badgeSysAudioStatus');
      const sysAudioBtn = document.getElementById('btnGrantSysAudio');
      const hasDisplayMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
      const sysAudioTested = localStorage.getItem('westpro_sys_audio_granted') === '1';

      if (!hasDisplayMedia) {
        if (sysAudioBadge) {
          sysAudioBadge.className = 'hermes-badge badge-warn';
          sysAudioBadge.textContent = 'Tidak Didukung';
        }
        if (sysAudioBtn) {
          sysAudioBtn.style.display = 'none';
        }
      } else if (sysAudioTested) {
        sysAudioGranted = true;
        if (sysAudioBadge) {
          sysAudioBadge.className = 'hermes-badge badge-ok';
          sysAudioBadge.textContent = 'Siap / Diuji';
        }
        if (sysAudioBtn) {
          sysAudioBtn.textContent = 'Uji Ulang';
          sysAudioBtn.style.display = 'inline-block';
        }
      } else {
        if (sysAudioBadge) {
          sysAudioBadge.className = 'hermes-badge badge-blue';
          sysAudioBadge.textContent = 'Didukung (Perlu Izin)';
        }
        if (sysAudioBtn) {
          sysAudioBtn.textContent = 'Izinkan';
          sysAudioBtn.style.display = 'inline-block';
        }
      }

      // 4. Update Navbar Summary
      const navSummary = document.getElementById('navPermissionSummary');
      if (navSummary) {
        if (micGranted && notifGranted && sysAudioGranted) {
          navSummary.innerHTML = '<span style="color:#10b981;">Izin Lengkap</span>';
        } else if (micGranted || notifGranted || sysAudioGranted) {
          navSummary.innerHTML = '<span style="color:#38bdf8;">Izin Sebagian</span>';
        } else {
          navSummary.innerHTML = '<span style="color:#f59e0b;">Belum Diizinkan</span>';
        }
      }

      // 5. Update Sticky Banner Visibility
      const banner = document.getElementById('stickyPermissionBanner');
      if (banner) {
        if (micGranted && notifGranted && sysAudioGranted) {
          banner.style.display = 'none';
        } else if (sessionStorage.getItem('westpro_perm_dismissed') === '1') {
          banner.style.display = 'none';
        } else {
          banner.style.display = 'flex';
        }
      }

      // 6. Show Blocked Helper if Denied
      const blockedHelper = document.getElementById('blockedHelperText');
      if (blockedHelper) {
        const isBlocked = (window.Notification && Notification.permission === 'denied');
        blockedHelper.style.display = isBlocked ? 'block' : 'none';
      }

      return { micGranted, notifGranted, sysAudioGranted };
    }

    function dismissPermissionBanner() {
      const banner = document.getElementById('stickyPermissionBanner');
      if (banner) banner.style.display = 'none';
      sessionStorage.setItem('westpro_perm_dismissed', '1');
    }

    function openPermissionModal() {
      sessionStorage.removeItem('westpro_perm_dismissed');
      const modal = document.getElementById('permissionModal');
      if (modal) {
        modal.style.display = 'flex';
        checkAllPermissions();
      }
    }

    function closePermissionModal(userDismissed = false) {
      const modal = document.getElementById('permissionModal');
      if (modal) modal.style.display = 'none';
      if (userDismissed) {
        sessionStorage.setItem('westpro_perm_dismissed', '1');
      }
    }

    async function requestMicrophonePermission() {
      const btn = document.getElementById('btnGrantMic');
      if (btn) { btn.disabled = true; btn.textContent = 'Meminta...'; }
      try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          throw new Error('Browser membatasi mikrofon di HTTP. Silakan buka https://westpro.my.id');
        }
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(t => t.stop());
        showToast('Mikrofon berhasil diizinkan', 'success');
        await checkAllPermissions();
        if (window.checkSpeechEngine) checkSpeechEngine();
        return true;
      } catch (err) {
        console.warn('Izin mic gagal:', err);
        showToast('Mikrofon belum diizinkan: ' + (err.message || 'Ditolak'), 'error');
        await checkAllPermissions();
        const blockedHelper = document.getElementById('blockedHelperText');
        if (blockedHelper) blockedHelper.style.display = 'block';
        return false;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Izinkan'; }
      }
    }

    async function requestNotificationPermission() {
      if (!window.Notification) {
        showToast('Browser ini tidak mendukung Notifikasi Desktop', 'error');
        return false;
      }
      const btn = document.getElementById('btnGrantNotif');
      if (btn) { btn.disabled = true; btn.textContent = 'Meminta...'; }
      try {
        const perm = await Notification.requestPermission();
        if (perm === 'granted') {
          showToast('Notifikasi berhasil diizinkan', 'success');
          try {
            new Notification('Westpro Home Server', {
              body: 'Notifikasi aktif untuk telemetri dan kode OTP.',
              icon: '/favicon.ico'
            });
          } catch(e) {}
        } else {
          showToast('Notifikasi belum diizinkan (' + perm + ')', 'error');
        }
        await checkAllPermissions();
        return perm === 'granted';
      } catch (err) {
        console.warn('Izin notif error:', err);
        showToast('Gagal meminta izin notifikasi', 'error');
        return false;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Izinkan'; }
      }
    }

    async function requestSystemAudioPermission() {
      const btn = document.getElementById('btnGrantSysAudio');
      if (btn) { btn.disabled = true; btn.textContent = 'Meminta...'; }
      try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
          throw new Error('Browser tidak mendukung tangkapan audio sistem/tab. Silakan gunakan Google Chrome atau Microsoft Edge.');
        }
        showToast('Pilih tab atau layar dan pastikan mencentang opsi "Bagikan audio / Share audio"', 'info');
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video: { displaySurface: 'browser' },
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
        });
        // Nonaktifkan video agar hemat memori & CPU
        stream.getVideoTracks().forEach(t => { t.enabled = false; });
        const audioTracks = stream.getAudioTracks();
        stream.getTracks().forEach(t => t.stop());

        if (audioTracks.length > 0) {
          localStorage.setItem('westpro_sys_audio_granted', '1');
          showToast('Audio speaker/sistem laptop berhasil diizinkan dan terdeteksi!', 'success');
          await checkAllPermissions();
          return true;
        } else {
          showToast('Tab/Layar dipilih, tetapi opsi "Share audio" tidak dicentang.', 'error');
          await checkAllPermissions();
          return false;
        }
      } catch (err) {
        console.warn('Izin audio sistem gagal/batal:', err);
        if (err.name !== 'AbortError' && err.name !== 'NotAllowedError') {
          showToast('Izin audio speaker laptop belum diberikan: ' + (err.message || 'Dibatalkan'), 'error');
        } else {
          showToast('Pemilihan audio laptop dibatalkan', 'info');
        }
        await checkAllPermissions();
        return false;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Izinkan'; }
      }
    }

    async function requestAllPermissions() {
      const btn = document.getElementById('btnGrantAll');
      if (btn) { btn.disabled = true; btn.textContent = 'Meminta Izin Browser...'; }

      // 1. Trigger Microphone prompt
      try { await requestMicrophonePermission(); } catch (e) {}

      // 2. Trigger Notification prompt
      try { await requestNotificationPermission(); } catch (e) {}

      // 3. Trigger Laptop / Speaker Audio prompt
      try { await requestSystemAudioPermission(); } catch (e) {}

      if (btn) { btn.disabled = false; btn.textContent = 'Izinkan Semua'; }

      try {
        const { micGranted, notifGranted, sysAudioGranted } = await checkAllPermissions();
        if (micGranted && notifGranted && sysAudioGranted) {
          showToast('Semua izin berhasil diaktifkan', 'success');
        } else {
          showToast('Status izin telah diperbarui', 'info');
        }
      } catch (e) {}
      setTimeout(() => closePermissionModal(true), 600);
    }

    function setRange(range, btn) {
      document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentRange = range;
      fetchHistoryData();
    }

    // Init Live Chart
    function initLiveChart() {
      const canvas = document.getElementById('livePowerChart');
      if (!canvas || typeof Chart === 'undefined') return;
      const ctx = canvas.getContext('2d');
      liveChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: liveDataPoints.labels,
          datasets: [
            {
              label: 'Solar PV (Watt)',
              data: liveDataPoints.pvWatts,
              borderColor: '#f59e0b',
              backgroundColor: 'rgba(245, 158, 11, 0.1)',
              borderWidth: 2,
              fill: true,
              tension: 0.3
            },
            {
              label: 'Beban AC Output (Watt)',
              data: liveDataPoints.loadWatts,
              borderColor: '#38bdf8',
              backgroundColor: 'rgba(56, 189, 248, 0.1)',
              borderWidth: 2,
              fill: true,
              tension: 0.3
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#8b949e', font: { family: 'JetBrains Mono' } } }
          },
          scales: {
            x: {
              grid: { color: 'rgba(255, 255, 255, 0.05)' },
              ticks: { color: '#8b949e', maxTicksLimit: 8, font: { family: 'JetBrains Mono' } }
            },
            y: {
              grid: { color: 'rgba(255, 255, 255, 0.05)' },
              ticks: { color: '#8b949e', font: { family: 'JetBrains Mono' } }
            }
          }
        }
      });
    }

    // Update Live Overview
    // Update Live Overview
    async function updateLiveData() {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error('API down');
        const data = await res.json();

        const elPvW = document.getElementById('ovPvWatt');
        if (elPvW) elPvW.textContent = data.pv_power_watt + ' W';
        const elPvV = document.getElementById('ovPvVolt');
        if (elPvV) elPvV.textContent = data.pv_voltage.toFixed(1) + ' V';
        const elPvA = document.getElementById('ovPvCurr');
        if (elPvA) elPvA.textContent = data.pv_current.toFixed(1) + ' A';

        const elBatV = document.getElementById('ovBattVolt');
        if (elBatV) elBatV.textContent = data.battery_voltage.toFixed(2) + ' V';
        const elSoc = document.getElementById('ovSocBadge');
        if (elSoc) elSoc.textContent = 'SOC ' + data.soc + '%';
        const elBatChg = document.getElementById('ovBattChg');
        if (elBatChg) elBatChg.textContent = data.battery_charging_current + ' A';
        const elBatDis = document.getElementById('ovBattDis');
        if (elBatDis) elBatDis.textContent = data.battery_discharge_current + ' A';

        // Update 4-cell visual battery stack
        const cellSegments = document.querySelectorAll('#battCellGrid .batt-cell-segment');
        if (cellSegments && cellSegments.length === 4) {
          const thresholds = [20, 45, 70, 85];
          cellSegments.forEach((seg, idx) => {
            seg.className = 'batt-cell-segment';
            if (data.soc >= thresholds[idx]) {
              seg.classList.add(data.soc <= 25 ? 'danger' : (data.soc <= 50 ? 'warn' : 'active'));
            }
          });
        }

        const elLoadW = document.getElementById('ovLoadWatt');
        if (elLoadW) elLoadW.textContent = data.ac_output_watt + ' W';
        const elLoadPct = document.getElementById('ovLoadPctBadge');
        if (elLoadPct) elLoadPct.textContent = 'Load ' + data.load_percent + '%';
        const elAcV = document.getElementById('ovAcOutVolt');
        if (elAcV) elAcV.textContent = data.ac_output_voltage.toFixed(1) + ' V';
        const elAcVa = document.getElementById('ovAcOutVa');
        if (elAcVa) elAcVa.textContent = data.ac_output_va + ' VA';

        const elGridV = document.getElementById('ovGridVolt');
        if (elGridV) elGridV.textContent = data.grid_voltage.toFixed(1) + ' V';
        const elGridHz = document.getElementById('ovGridHz');
        if (elGridHz) elGridHz.textContent = data.grid_frequency.toFixed(1) + ' Hz';
        const elBusV = document.getElementById('ovBusVolt');
        if (elBusV) elBusV.textContent = data.bus_voltage + ' V';

        const elInvMode = document.getElementById('ovInverterMode');
        if (elInvMode) elInvMode.textContent = data.mode;
        const elTemp = document.getElementById('ovTemp');
        if (elTemp) elTemp.textContent = data.temperature + ' °C';
        const elAcHz = document.getElementById('ovAcOutHz');
        if (elAcHz) elAcHz.textContent = data.ac_output_frequency.toFixed(1) + ' Hz';
        const elBatNet = document.getElementById('ovBattNetWatt');
        if (elBatNet) elBatNet.textContent = data.battery_power_watt + ' W';

        // =====================================================================
        // REALTIME TOPOLOGY HYBRID POWER FLOW HUB UPDATE
        // =====================================================================
        // 1. Solar PV Node & Flow Bridge (PV -> Inverter)
        const pvW = data.pv_power_watt || 0;
        const hubPvW = document.getElementById('hubPvWatt');
        if (hubPvW) hubPvW.textContent = pvW + ' W';
        const hubPvR = document.getElementById('hubPvRatio');
        if (hubPvR) hubPvR.textContent = ((pvW / 1100) * 100).toFixed(0) + '% Wp';
        const hubPvV = document.getElementById('hubPvVolt');
        const pvVoltVal = (data.pv_voltage != null) ? data.pv_voltage : data.pv_input_voltage;
        if (hubPvV) hubPvV.textContent = (pvVoltVal != null ? pvVoltVal.toFixed(1) : '--') + ' V';
        const hubPvC = document.getElementById('hubPvCurr');
        if (hubPvC) {
          const pvCurrVal = (data.pv_current != null) ? data.pv_current : data.pv_input_current;
          const pvA = (pvCurrVal != null) ? pvCurrVal.toFixed(1) : ((pvVoltVal && pvVoltVal > 0) ? (pvW / pvVoltVal).toFixed(1) : '0.0');
          hubPvC.textContent = pvA + ' A';
        }
        const flowBadgePvW = document.getElementById('flowBadgePvWatt');
        if (flowBadgePvW) flowBadgePvW.textContent = '+' + pvW + ' W ➔';
        const partPv = document.getElementById('particlesPv');
        if (partPv) {
          if (pvW > 10) partPv.className = 'energy-particles flow-pv-active';
          else partPv.className = 'energy-particles';
        }

        // 2. LiFePO4 15S Battery Bank Node & Bidirectional Bridge
        const hubBattSoc = document.getElementById('hubBattSoc');
        if (hubBattSoc) hubBattSoc.textContent = data.soc + '%';
        const hubBattSocBar = document.getElementById('hubBattSocBar');
        if (hubBattSocBar) {
          hubBattSocBar.style.width = Math.min(100, Math.max(0, data.soc)) + '%';
          hubBattSocBar.style.background = data.soc > 50 ? '#10b981' : (data.soc > 25 ? '#f59e0b' : '#ef4444');
        }
        const hubBattV = document.getElementById('hubBattVolt');
        if (hubBattV) hubBattV.textContent = (data.battery_voltage != null ? data.battery_voltage.toFixed(1) : '--') + ' V';
        const hubBattC = document.getElementById('hubBattCurr');
        if (hubBattC) {
          if (data.battery_charging_current > 0) hubBattC.textContent = '+' + data.battery_charging_current + ' A';
          else if (data.battery_discharge_current > 0) hubBattC.textContent = '-' + data.battery_discharge_current + ' A';
          else hubBattC.textContent = '0.0 A';
        }
        const hubBattDelta = document.getElementById('hubBattDelta');
        if (hubBattDelta) {
          if (data.bms && data.bms.cell_delta_mv != null) {
            hubBattDelta.textContent = data.bms.cell_delta_mv.toFixed(0) + ' mV';
            hubBattDelta.style.color = data.bms.cell_delta_mv < 50 ? '#34d399' : (data.bms.cell_delta_mv < 120 ? '#fbbf24' : '#f87171');
          } else {
            hubBattDelta.textContent = '-- mV';
          }
        }
        const hubBattDir = document.getElementById('hubBattFlowDirBadge');
        const pillBattDir = document.getElementById('pillBattDirText');
        const flowBadgeBattW = document.getElementById('flowBadgeBattWatt');
        const partBatt = document.getElementById('particlesBatt');

        if (data.battery_charging_current > 0) {
          if (hubBattDir) {
            hubBattDir.className = 'hermes-badge badge-ok';
            hubBattDir.textContent = '+' + (data.battery_power_watt || 0) + ' W (Charging)';
          }
          if (pillBattDir) pillBattDir.textContent = 'MPPT ➔ Charge';
          if (flowBadgeBattW) flowBadgeBattW.textContent = '+' + (data.battery_power_watt || 0) + ' W 🠘';
          if (partBatt) partBatt.className = 'energy-particles flow-batt-charge';
        } else if (data.battery_discharge_current > 0) {
          const disW = Math.abs(data.battery_power_watt || 0);
          if (hubBattDir) {
            hubBattDir.className = 'hermes-badge badge-warn';
            hubBattDir.textContent = '-' + disW + ' W (Discharge)';
          }
          if (pillBattDir) pillBattDir.textContent = 'Baterai ➔ Beban';
          if (flowBadgeBattW) flowBadgeBattW.textContent = '-' + disW + ' W ➔';
          if (partBatt) partBatt.className = 'energy-particles flow-batt-discharge';
        } else {
          if (hubBattDir) {
            hubBattDir.className = 'hermes-badge';
            hubBattDir.style.background = 'rgba(255,255,255,0.08)';
            hubBattDir.style.color = '#94a3b8';
            hubBattDir.textContent = '0 W (Standby)';
          }
          if (pillBattDir) pillBattDir.textContent = 'Dua Arah';
          if (flowBadgeBattW) flowBadgeBattW.textContent = '0 W Standby';
          if (partBatt) partBatt.className = 'energy-particles';
        }

        // 3. PowMr 6.2kW Inverter Hub
        const hubInvM = document.getElementById('hubInverterMode');
        if (hubInvM) hubInvM.textContent = data.mode || 'SBU Mode';
        const hubInvMF = document.getElementById('hubInvModeFull');
        if (hubInvMF) hubInvMF.textContent = (data.mode || 'SBU') + ' Hybrid Mode (Priority)';
        // Dynamic Inverter Self-Loss & Total Gross Load calculation
        let invSelfLoss = 45;
        if (data.pv_power_watt > 0 && data.battery_power_watt > 0) {
          const calcLoss = data.pv_power_watt - data.battery_power_watt - data.ac_output_watt;
          if (calcLoss >= 25 && calcLoss <= 120) invSelfLoss = Math.round(calcLoss);
        } else if (data.battery_power_watt < 0) {
          const calcLoss = Math.abs(data.battery_power_watt) - data.ac_output_watt;
          if (calcLoss >= 25 && calcLoss <= 120) invSelfLoss = Math.round(calcLoss);
        }
        const grossLoadW = data.ac_output_watt + invSelfLoss;

        const elInvSelfLoss = document.getElementById('ovInvSelfLoss');
        if (elInvSelfLoss) elInvSelfLoss.textContent = '~' + invSelfLoss + ' W';
        const elGrossLoad = document.getElementById('ovGrossLoadWatt');
        if (elGrossLoad) elGrossLoad.textContent = grossLoadW + ' W';

        const hubInvLW = document.getElementById('hubInvLoadWatt');
        if (hubInvLW) hubInvLW.textContent = data.ac_output_watt + ' W';
        const hubInvSelf = document.getElementById('hubInvSelfLossWatt');
        if (hubInvSelf) hubInvSelf.textContent = '~' + invSelfLoss + ' W';
        const hubInvGross = document.getElementById('hubInvGrossWatt');
        if (hubInvGross) hubInvGross.textContent = grossLoadW + ' W';
        const hubInvLVa = document.getElementById('hubInvLoadVa');
        if (hubInvLVa) hubInvLVa.textContent = (data.ac_output_va || data.ac_output_watt) + ' VA';
        const hubInvLPct = document.getElementById('hubInvLoadPercent');
        if (hubInvLPct) hubInvLPct.textContent = data.load_percent + '%';
        const hubInvBus = document.getElementById('hubInvBusVolt');
        if (hubInvBus) hubInvBus.textContent = data.bus_voltage + ' V';
        const hubInvSink = document.getElementById('hubInvHeatsink');
        if (hubInvSink) hubInvSink.textContent = data.temperature + '°C';

        // 4. Grid PLN Node & Bridge (PLN -> Inverter)
        const hubGridV = document.getElementById('hubGridVolt');
        if (hubGridV) hubGridV.textContent = data.grid_voltage.toFixed(1) + ' V';
        const hubGridHz = document.getElementById('hubGridHz');
        if (hubGridHz) hubGridHz.textContent = data.grid_frequency.toFixed(1) + ' Hz';
        const hubGridSt = document.getElementById('hubGridStatusBadge');
        const badgeGridW = document.getElementById('flowBadgeGridWatt');
        const partGrid = document.getElementById('particlesGrid');
        const isGridActive = data.grid_voltage > 100;
        const isGridSupplying = (data.mode && (data.mode.includes('Line') || data.mode.includes('Bypass')));

        if (hubGridSt) {
          if (isGridSupplying) {
            hubGridSt.className = 'hermes-badge badge-warn';
            hubGridSt.textContent = 'BYPASS / AKTIF';
          } else if (isGridActive) {
            hubGridSt.className = 'hermes-badge badge-ok';
            hubGridSt.textContent = 'STANDBY SIAP';
          } else {
            hubGridSt.className = 'hermes-badge badge-danger';
            hubGridSt.textContent = 'PLN PADAM';
          }
        }
        if (badgeGridW) {
          if (isGridSupplying) {
            badgeGridW.textContent = '🠘 ' + data.ac_output_watt + ' W Suplai';
            if (partGrid) partGrid.className = 'energy-particles flow-grid-active';
          } else if (isGridActive) {
            badgeGridW.textContent = '🠘 220V Standby';
            if (partGrid) partGrid.className = 'energy-particles';
          } else {
            badgeGridW.textContent = 'Padam (0V)';
            if (partGrid) partGrid.className = 'energy-particles';
          }
        }

        // 5. AC Home Load Node & Bridge (Inverter -> Rumah)
        const loadW = data.ac_output_watt || 0;
        const hubLoadW = document.getElementById('hubLoadWatt');
        if (hubLoadW) hubLoadW.textContent = loadW + ' W';
        const hubLoadV = document.getElementById('hubLoadVolt');
        if (hubLoadV) hubLoadV.textContent = data.ac_output_voltage.toFixed(1) + ' V';
        const hubLoadHz = document.getElementById('hubLoadHz');
        if (hubLoadHz) hubLoadHz.textContent = data.ac_output_frequency.toFixed(1) + ' Hz';
        const hubLoadC = document.getElementById('hubLoadCurr');
        if (hubLoadC) {
          const loadA = data.ac_output_voltage > 0 ? (loadW / data.ac_output_voltage).toFixed(1) : '0.0';
          hubLoadC.textContent = loadA + ' A';
        }
        const hubLoadBadge = document.getElementById('hubLoadSupplyBadge');
        if (hubLoadBadge) {
          if (isGridSupplying) {
            hubLoadBadge.className = 'hermes-badge badge-warn';
            hubLoadBadge.textContent = '100% BYPASS PLN';
          } else if (pvW >= loadW && loadW > 0) {
            hubLoadBadge.className = 'hermes-badge badge-ok';
            hubLoadBadge.textContent = '100% SOLAR PV DIRECT';
          } else if (data.battery_discharge_current > 0) {
            hubLoadBadge.className = 'hermes-badge badge-ok';
            hubLoadBadge.textContent = 'SOLAR + BATERAI';
          } else {
            hubLoadBadge.className = 'hermes-badge badge-ok';
            hubLoadBadge.textContent = 'HYBRID MANDIRI';
          }
        }
        const flowBadgeLoadW = document.getElementById('flowBadgeLoadWatt');
        if (flowBadgeLoadW) flowBadgeLoadW.textContent = loadW + ' W ➔';
        const partLoad = document.getElementById('particlesLoad');
        if (partLoad) {
          if (loadW > 10) partLoad.className = 'energy-particles flow-load-active';
          else partLoad.className = 'energy-particles';
        }

        const ovLoadBarMini = document.getElementById('ovLoadBarMini');
        if (ovLoadBarMini) {
          ovLoadBarMini.style.width = Math.min(100, Math.max(0, data.load_percent)) + '%';
        }

        // Update Battery Energy & Support Estimation Cards (Deterministik dari Data Kemarin)
        if (data.battery_stats) {
          const bs = data.battery_stats;
          const elChgKwh = document.getElementById('ovBattChargedKwh');
          if (elChgKwh) elChgKwh.textContent = (bs.today_charged_kwh != null ? bs.today_charged_kwh.toFixed(2) : '--') + ' kWh';
          const elChgYest = document.getElementById('ovBattChargedYest');
          if (elChgYest) elChgYest.textContent = (bs.yesterday_charged_kwh != null ? bs.yesterday_charged_kwh.toFixed(2) : '--') + ' kWh';
          const elChgWh = document.getElementById('ovBattChargedWh');
          if (elChgWh) elChgWh.textContent = Math.round(bs.today_charged_wh || 0) + ' Wh';

          const elChgBadge = document.getElementById('ovBattChgBadge');
          if (elChgBadge) {
            if (data.battery_charging_current > 0) {
              elChgBadge.className = 'hermes-badge badge-ok';
              elChgBadge.textContent = '+' + data.battery_power_watt + ' W (' + data.battery_charging_current + 'A)';
            } else {
              elChgBadge.className = 'hermes-badge';
              elChgBadge.style.background = 'rgba(255,255,255,0.08)';
              elChgBadge.style.color = '#94a3b8';
              elChgBadge.textContent = '0 W (Standby)';
            }
          }

          const elDisKwh = document.getElementById('ovBattDischargedKwh');
          if (elDisKwh) elDisKwh.textContent = (bs.today_discharged_kwh != null ? bs.today_discharged_kwh.toFixed(2) : '--') + ' kWh';
          const elDisYest = document.getElementById('ovBattDischargedYest');
          if (elDisYest) elDisYest.textContent = (bs.yesterday_discharged_kwh != null ? bs.yesterday_discharged_kwh.toFixed(2) : '--') + ' kWh';
          const elDodYest = document.getElementById('ovBattDodYest');
          if (elDodYest) elDodYest.textContent = (bs.yesterday_discharged_dod_pct || 0) + '%';

          const elDisBadge = document.getElementById('ovBattDisBadge');
          if (elDisBadge) {
            if (data.battery_discharge_current > 0) {
              elDisBadge.className = 'hermes-badge badge-warn';
              elDisBadge.textContent = '-' + Math.abs(data.battery_power_watt) + ' W (' + data.battery_discharge_current + 'A)';
            } else {
              elDisBadge.className = 'hermes-badge';
              elDisBadge.style.background = 'rgba(255,255,255,0.08)';
              elDisBadge.style.color = '#94a3b8';
              elDisBadge.textContent = '0 W (Solar Direct)';
            }
          }

          const elTarget = document.getElementById('ovBattSupportTarget');
          if (elTarget) elTarget.textContent = bs.target_time_display || '--:-- WIB';
          const elHours = document.getElementById('ovBattSupportHours');
          if (elHours) elHours.textContent = '~' + (bs.hours_remaining != null ? bs.hours_remaining.toFixed(1) : '--') + ' Jam';
          const elUsable = document.getElementById('ovBattSupportUsable');
          if (elUsable) elUsable.textContent = (bs.usable_kwh != null ? bs.usable_kwh.toFixed(2) : '--') + ' kWh';
          const elRefLoad = document.getElementById('ovBattSupportRefLoad');
          if (elRefLoad) elRefLoad.textContent = '~' + Math.round(bs.yesterday_avg_night_w || 180) + ' W';

          const elSuppBadge = document.getElementById('ovBattSupportBadge');
          if (elSuppBadge) {
            elSuppBadge.className = 'hermes-badge ' + (bs.status_badge_class || 'badge-ok');
            elSuppBadge.textContent = bs.status_label || '🟢 Aman Subuh';
          }

          const elRuntime = document.getElementById('ovBattRuntime');
          if (elRuntime && bs.hours_remaining != null) {
            elRuntime.textContent = '~' + bs.hours_remaining.toFixed(1) + ' Jam';
          }
        }

        // Update ZTE BMS live telemetry if available
        if (data.bms && data.bms.connected) {
          const elBmsDet = document.getElementById('ovBmsDetails');
          if (elBmsDet) {
            elBmsDet.style.display = 'block';
            const packCount = data.bms.pack_count || (data.bms.packs ? Object.keys(data.bms.packs).length : 1);
            const elTitle = document.getElementById('ovBmsTitle');
            if (elTitle) elTitle.textContent = packCount > 1 ? `ZTE BMS (${packCount} Pack)` : 'ZTE BMS';
            const elLink = document.getElementById('ovBmsLink');
            if (elLink) elLink.textContent = packCount > 1 ? `Detail ${packCount} Pack (${data.bms.cell_count} Sel) ↗` : '15 Sel Detail ↗';
            const elDelta = document.getElementById('ovBmsDelta');
            if (elDelta) elDelta.textContent = `Δ ${data.bms.cell_delta_mv} mV (${data.bms.cell_count} Sel)`;
            const elTemp = document.getElementById('ovBmsTemp');
            if (elTemp) elTemp.textContent = `${data.bms.avg_cell_temp_c}°C`;
          }
          if (data.bms.soc != null) {
            const elSoc = document.getElementById('ovSocBadge');
            if (elSoc) elSoc.textContent = `SOC ${data.bms.soc}% (BMS)`;
          }
          latestBmsData = data.bms;
          const bmsModal = document.getElementById('zteBmsModal');
          if (bmsModal && bmsModal.style.display !== 'none') {
            renderZteBmsModal();
          }
        }

        const now = new Date();
        const timeStr = now.toTimeString().split(' ')[0];
        const elLastUpdate = document.getElementById('ovLastUpdate');
        if (elLastUpdate) elLastUpdate.textContent = timeStr;
        const elLastTop = document.getElementById('ovLastUpdateTop');
        if (elLastTop) elLastTop.textContent = `Pembaruan: ${timeStr} WIB`;
        const elNavMode = document.getElementById('navModeText');
        if (elNavMode) elNavMode.textContent = data.mode;

        // Add to Live Chart
        if (liveChart) {
          liveDataPoints.labels.push(timeStr);
          liveDataPoints.pvWatts.push(data.pv_power_watt);
          liveDataPoints.loadWatts.push(data.ac_output_watt);

          if (liveDataPoints.labels.length > 30) {
            liveDataPoints.labels.shift();
            liveDataPoints.pvWatts.shift();
            liveDataPoints.loadWatts.shift();
          }
          liveChart.update('none');
        }
      } catch (err) {
        const elNavMode = document.getElementById('navModeText');
        if (elNavMode) elNavMode.textContent = 'OFFLINE / RECONNECTING';
      }
    }

    // ==================== LIVE BMKG / GOOGLE WEATHER & SMART ADVISORY ====================
    function updateLiveClock() {
      const el = document.getElementById('heroLiveClock');
      if (el) {
        const d = new Date();
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const ss = String(d.getSeconds()).padStart(2, '0');
        el.textContent = `${hh}:${mm}:${ss} WIB`;
      }
    }
    setInterval(updateLiveClock, 1000);

    async function updateSolarWeatherAndAdvisory(force = false) {
      try {
        const url = force ? '/api/weather/solar?force=1' : '/api/weather/solar';
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        // 1. Current Weather
        if (data.current) {
          const cur = data.current;
          const locEl = document.getElementById('heroWeatherLocation');
          if (locEl) locEl.textContent = data.location || 'Jetak, Kaliagung, Sentolo';

          const provBadge = document.getElementById('heroWeatherProviderBadge');
          if (provBadge) {
            provBadge.textContent = (data.provider_badge || 'BMKG RESMI') + (force ? ' ✓' : '');
            provBadge.title = (data.provider || 'BMKG Indonesia (Desa Kaliagung, Sentolo)') + ' • Klik untuk refresh instan.';
          }

          const syncStatus = document.getElementById('heroWeatherSyncStatus');
          if (syncStatus) {
            syncStatus.textContent = '● Sinkron Otomatis (' + (data.updated_at || 'Aktif') + ')';
          }

          if (data.current_date) {
            const dtEl = document.getElementById('heroWeatherDateText');
            if (dtEl) dtEl.textContent = data.current_date.full_date || 'Selasa, 13 Oktober 2026';
            const wkEl = document.getElementById('heroWeatherWeekText');
            if (wkEl) wkEl.textContent = data.current_date.week_str || 'Minggu ke-42';
          }

          const tempEl = document.getElementById('heroWeatherTemp');
          if (tempEl) tempEl.textContent = (typeof cur.temp === 'number') ? cur.temp.toFixed(1) : '22.0';

          const iconEl = document.getElementById('heroWeatherIcon');
          if (iconEl) iconEl.textContent = cur.icon || '⛅';

          const condBadge = document.getElementById('heroWeatherCondBadge');
          if (condBadge) condBadge.textContent = cur.condition || 'Cerah Berawan';

          const cloudEl = document.getElementById('heroWeatherCloud');
          if (cloudEl) cloudEl.textContent = `☁️ Awan: ${cur.cloud_cover}%`;

          const humEl = document.getElementById('heroWeatherHumidity');
          if (humEl) humEl.textContent = `💧 Lembab: ${cur.humidity}%`;

          const sunTimesEl = document.getElementById('heroSunTimes');
          if (sunTimesEl) sunTimesEl.textContent = `🌅 ${data.sunrise || '05:17'} • 🌇 ${data.sunset || '17:33'}`;

          // Kotak-Kotak Metrik Cuaca (Weather Metrics Boxes)
          const elEstKwh = document.getElementById('wmEstKwh');
          if (elEstKwh) {
            const kwh = (data.advisory && data.advisory.est_solar_kwh != null) ? data.advisory.est_solar_kwh : (data.est_kwh != null ? data.est_kwh : '--');
            elEstKwh.textContent = (typeof kwh === 'number') ? `${kwh.toFixed(1)} kWh` : `${kwh}`;
          }
          const elPeakWin = document.getElementById('wmPeakWindow');
          if (elPeakWin) {
            elPeakWin.textContent = (data.advisory && data.advisory.range_highlight) ? data.advisory.range_highlight : 'Puncak: 10:00 - 14:00';
          }

          const elUv = document.getElementById('wmUvIndex');
          if (elUv) elUv.textContent = (cur.uv_index != null) ? `UV ${cur.uv_index}` : 'UV --';
          const elUvLvl = document.getElementById('wmUvLevel');
          if (elUvLvl) elUvLvl.textContent = cur.uv_level ? `${cur.uv_level} Radiasi` : 'Tingkat Radiasi';

          const elPop = document.getElementById('wmPrecipProb');
          if (elPop) elPop.textContent = `${cur.precipitation_prob != null ? cur.precipitation_prob : 0}%`;
          const elPrecipDesc = document.getElementById('wmPrecipDesc');
          if (elPrecipDesc) {
            const p = cur.precipitation_prob || 0;
            elPrecipDesc.textContent = p > 60 ? 'Potensi Hujan Lebat' : (p > 30 ? 'Peluang Gerimis' : 'Presipitasi Rendah');
          }

          const elWs = document.getElementById('wmWindSpeed');
          if (elWs) elWs.textContent = (typeof cur.wind_speed === 'number') ? `${cur.wind_speed.toFixed(1)} km/h` : `${cur.wind_speed || 0} km/h`;
          
          const COMPASS_MAP = {
            'N': 'Utara', 'NNE': 'U-TL', 'NE': 'Timur Laut', 'ENE': 'T-TL',
            'E': 'Timur', 'ESE': 'T-TG', 'SE': 'Tenggara', 'SSE': 'S-TG',
            'S': 'Selatan', 'SSW': 'S-BD', 'SW': 'Barat Daya', 'WSW': 'B-BD',
            'W': 'Barat', 'WNW': 'B-BL', 'NW': 'Barat Laut', 'NNW': 'U-BL'
          };
          const wdText = COMPASS_MAP[cur.wind_direction] || cur.wind_direction || 'Selatan';
          const elWd = document.getElementById('wmWindDir');
          if (elWd) elWd.textContent = `Arah: ${wdText}`;

          const elFl = document.getElementById('wmFeelsLike');
          if (elFl) elFl.textContent = `${(typeof cur.feels_like === 'number') ? cur.feels_like.toFixed(1) : (cur.feels_like != null ? cur.feels_like : cur.temp)}°C`;
          const elDp = document.getElementById('wmDewPoint');
          if (elDp) elDp.textContent = `Titik Embun: ${(typeof cur.dew_point === 'number') ? cur.dew_point.toFixed(1) : (cur.dew_point != null ? cur.dew_point : '--')}°C`;

          const elVis = document.getElementById('wmVisibility');
          if (elVis) elVis.textContent = cur.visibility || '> 10 km';
          const elPress = document.getElementById('wmPressure');
          if (elPress) {
            const pStr = cur.pressure != null ? String(cur.pressure) : '1012 hPa';
            elPress.textContent = pStr.includes('hPa') ? pStr : `Barometer: ${pStr} hPa`;
          }
        }

        // 2. Prakiraan (3 hari / 7 hari / minggu depan / bulan) mengikuti tab aktif
        window._weatherData = data;
        renderWeatherRange();
        renderWeatherModalIfOpen();

        // 2b. Render Prakiraan Jam-jaman BMKG (Hourly Weather Strip)
        const hourlyEl = document.getElementById('weatherHourlyStrip');
        if (hourlyEl && Array.isArray(data.hourly_forecast) && data.hourly_forecast.length > 0) {
          hourlyEl.innerHTML = data.hourly_forecast.map(function(h) {
            const activeCls = h.is_now ? ' active' : '';
            const nowBadge = h.is_now ? '<span style="color: #38bdf8; font-size: 0.52rem; font-weight: 700; display: block; margin-top:-2px;">KINI</span>' : '';
            const popVal = (h.pop != null ? h.pop : (h.precipitation_prob != null ? h.precipitation_prob : null));
            const popTxt = (popVal != null) ? `<span style="font-size: 0.58rem; color: #60a5fa;">💧${popVal}%</span>` : '';
            return '<div class="hourly-box' + activeCls + '">' +
              '<div class="time">' + escapeHtml(h.time || '--:--') + '</div>' +
              nowBadge +
              '<div class="icon">' + (h.icon || '⛅') + '</div>' +
              '<div class="temp">' + (h.temp != null ? h.temp + '°' : '--') + '</div>' +
              popTxt +
            '</div>';
          }).join('');
        }

        // 3. Smart Solar Advisory & ESG Metrics
        if (data.advisory) {
          const adv = data.advisory;
          const titleEl = document.getElementById('advTitle');
          if (titleEl) titleEl.textContent = adv.advice_title;

          const tagEl = document.getElementById('advStatusTag');
          if (tagEl) tagEl.textContent = adv.status_tag;

          const peakEl = document.getElementById('advPeakWindow');
          if (peakEl) peakEl.textContent = `⏱️ ${adv.range_highlight}`;

          const descEl = document.getElementById('advDescription');
          if (descEl) descEl.textContent = adv.advice_desc;

          const runtimeEl = document.getElementById('ovBattRuntime');
          if (runtimeEl) runtimeEl.textContent = `~${adv.runtime_hours} Jam`;

          const co2El = document.getElementById('ovCo2Saved');
          if (co2El) co2El.textContent = `${adv.co2_kg} kg`;

          const treesEl = document.getElementById('ovTreesSaved');
          if (treesEl) treesEl.textContent = `${adv.trees} 🌲`;

          const rpEl = document.getElementById('ovSavingsRp');
          if (rpEl) rpEl.textContent = `Rp ${Number(adv.savings_rp || 10500).toLocaleString('id-ID')}`;
        }
      } catch (e) {
        console.warn('Gagal memuat cuaca & anjuran surya:', e);
      }
    }

    let weatherRange = '3d';
    const WEATHER_RANGE_TITLES = {
      '3d': 'Prakiraan Cuaca 3 Hari',
      '7d': 'Prakiraan Cuaca 7 Hari',
      '14d': 'Prakiraan Minggu Depan (Hari ke-8 s/d 14)',
      'month': 'Iklim Bulanan & Saran Tanam (rata-rata 5 tahun)'
    };

    function switchWeatherForecastRange(range) {
      weatherRange = range;
      ['3d', '7d', '14d', 'Month'].forEach(function (k) {
        const btn = document.getElementById('btnWeather' + k);
        if (btn) btn.classList.toggle('active', k.toLowerCase() === range);
      });
      const t = document.getElementById('weatherRangeTitle');
      if (t) t.textContent = WEATHER_RANGE_TITLES[range] || '';
      renderWeatherRange();
    }

    function weatherDayPill(f) {
      const rain = (f.precipitation_mm != null) ? ('💧 ' + f.precipitation_mm + ' mm') : '';
      return '<div class="forecast-pill" style="padding: 0.4rem 0.35rem; text-align: center;">' +
        '<div style="font-size: 0.68rem; font-weight: 700; color: #f4f4f5;">' + escapeHtml(f.label || f.day_name || '') + '</div>' +
        '<div style="font-size: 0.62rem; color: #38bdf8; font-weight: 600; margin: 1px 0;">' + escapeHtml(f.date_short || '') + '</div>' +
        '<div style="font-size: 1.15rem; margin: 2px 0;">' + (f.icon || '⛅') + '</div>' +
        '<div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5;">' + f.temp_max + '° / ' + f.temp_min + '°</div>' +
        '<div style="font-size: 0.58rem; color: #60a5fa;">' + rain + (f.precipitation_prob != null ? ' • ' + f.precipitation_prob + '%' : '') + '</div>' +
        '<div style="font-size: 0.58rem; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="' + escapeHtml(f.condition || '') + '">' + escapeHtml(f.condition || '') + '</div>' +
        '</div>';
    }

    function weatherMonthPill(m) {
      return '<div class="forecast-pill" style="padding: 0.4rem 0.35rem; text-align: center;' + (m.is_current ? ' border-color: #38bdf8;' : '') + '">' +
        '<div style="font-size: 0.68rem; font-weight: 700; color: #f4f4f5;">' + escapeHtml(m.month_name) + '</div>' +
        '<div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5; margin: 2px 0;">' + m.avg_temp_max + '° / ' + m.avg_temp_min + '°</div>' +
        '<div style="font-size: 0.62rem; color: #60a5fa;">💧 ' + m.rain_mm + ' mm • ' + m.rainy_days + ' hari hujan</div>' +
        '<div style="font-size: 0.6rem; color: #fbbf24;">☀️ ' + m.peak_sun_hours + ' jam puncak</div>' +
        '<div style="font-size: 0.58rem; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="' + escapeHtml(m.planting_hint || '') + '">' + escapeHtml(m.season) + '</div>' +
        '</div>';
    }

    function renderWeatherRange() {
      const gridEl = document.getElementById('heroForecastGrid');
      const data = window._weatherData;
      if (!gridEl || !data) return;
      let items = [];
      let html = '';
      if (weatherRange === '3d') items = data.forecast_3d || data.forecast || [];
      else if (weatherRange === '7d') items = data.forecast_7d || [];
      else if (weatherRange === '14d') items = (data.forecast_14d || []).slice(7);
      if (weatherRange === 'month') {
        const months = data.climate_outlook || [];
        html = months.map(weatherMonthPill).join('');
      } else {
        html = items.map(weatherDayPill).join('');
      }
      if (!html) {
        const src = data.sources_ok || {};
        html = '<div style="grid-column: 1 / -1; font-size: 0.72rem; color: #f59e0b; padding: 0.5rem;">Data belum tersedia' +
          (weatherRange === 'month' ? (src.climate === false ? ' (arsip iklim Open-Meteo tidak terjangkau).' : '.') :
           (src.open_meteo === false ? ' (Open-Meteo tidak terjangkau; mencoba lagi otomatis).' : '.')) + '</div>';
      }
      gridEl.style.gridTemplateColumns = (weatherRange === '7d' || weatherRange === 'month' || weatherRange === '14d') ? 'repeat(auto-fit, minmax(92px, 1fr))' : '';
      gridEl.innerHTML = html;
    }

    function weatherPvPotential(f) {
      const mm = f.precipitation_mm || 0, pr = f.precipitation_prob || 0;
      if (mm < 2 && pr < 30) return '<span style="color:#34d399;">Optimal ☀️</span>';
      if (mm < 8 && pr < 70) return '<span style="color:#fbbf24;">Sedang ⛅</span>';
      return '<span style="color:#f87171;">Rendah 🌧️ (andalkan baterai)</span>';
    }

    function renderWeatherModalIfOpen() {
      const modal = document.getElementById('weatherForecastModal');
      if (modal && modal.style.display !== 'none') renderWeatherModal();
    }

    function renderWeatherModal() {
      const data = window._weatherData;
      if (!data) return;
      const mo = data.monthly_outlook || {};
      const setText = function (id, v) { const el = document.getElementById(id); if (el) el.textContent = v; };
      setText('modalOutlookMonthTitle', 'Prospek Iklim & Panen Surya ' + (mo.month_name || 'Bulan Ini'));
      setText('modalOutlookSeasonBadge', mo.season_phase || '-');
      setText('modalOutlookPeakSun', mo.avg_peak_sun_hours || '-');
      setText('modalOutlookSolarPot', mo.monthly_solar_potential_kwh || '-');
      setText('modalOutlookTempRange', (mo.avg_temp_day || '-') + ' / ' + (mo.avg_temp_night || '-'));
      setText('modalOutlookRainDays', mo.rainy_days_prob || '-');
      setText('modalOutlookAdvice', mo.recommendation || '');

      const tbody = document.getElementById('modalForecastTableBody');
      if (tbody) {
        const rows = (data.forecast_14d || []).map(function (f) {
          const nextWeek = f.is_next_week;
          return '<tr style="border-bottom:1px solid #1b1b24; border-left:3px solid ' + (nextWeek ? '#a855f7' : '#0284c7') + ';">' +
            '<td style="padding:0.5rem 0.75rem;"><div style="font-weight:600; color:#f1f5f9;">' + escapeHtml(f.label) + '</div><div style="font-size:0.66rem; color:#64748b;">' + escapeHtml(f.full_date || f.date) + '</div></td>' +
            '<td style="padding:0.5rem 0.75rem;">' + (f.icon || '') + ' ' + escapeHtml(f.condition || '') + '</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center; font-weight:700;">' + f.temp_max + '° / ' + f.temp_min + '°</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center; color:#60a5fa;">' + f.precipitation_prob + '%</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center; color:#60a5fa;">' + f.precipitation_mm + ' mm</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center;">' + f.uv + '</td>' +
            '<td style="padding:0.5rem 0.75rem;">' + weatherPvPotential(f) + '</td></tr>';
        });
        tbody.innerHTML = rows.join('') || '<tr><td colspan="7" style="padding:1rem; color:#f59e0b;">Prakiraan 14 hari belum tersedia (Open-Meteo tidak terjangkau). Coba lagi sebentar.</td></tr>';
      }

      const pa = data.planting_advice;
      const paEl = document.getElementById('modalPlantingAdvice');
      if (paEl) {
        paEl.innerHTML = pa
          ? '<b style="color:#34d399;">14 hari ke depan:</b> total hujan ' + pa.rain_14d_mm + ' mm, ' + pa.wet_days_14d + ' hari basah, suhu rata-rata ' + pa.avg_temp_max_14d + '° / ' + pa.avg_temp_min_14d + '°.<br>' + escapeHtml(pa.verdict)
          : 'Ringkasan 14 hari belum tersedia.';
      }
      const cBody = document.getElementById('modalClimateTableBody');
      if (cBody) {
        cBody.innerHTML = (data.climate_outlook || []).map(function (m) {
          return '<tr style="border-bottom:1px solid #1b1b24;' + (m.is_current ? ' background:rgba(56,189,248,0.07);' : '') + '">' +
            '<td style="padding:0.5rem 0.75rem; font-weight:600; color:#f1f5f9;">' + escapeHtml(m.month_name) + '</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center;">' + m.avg_temp_max + '° / ' + m.avg_temp_min + '°</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center; color:#60a5fa;">' + m.rain_mm + ' mm</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center;">' + m.rainy_days + ' hari</td>' +
            '<td style="padding:0.5rem 0.75rem; text-align:center; color:#fbbf24;">' + m.peak_sun_hours + ' jam</td>' +
            '<td style="padding:0.5rem 0.75rem;"><div style="font-weight:600;">' + escapeHtml(m.season) + '</div><div style="font-size:0.66rem; color:#94a3b8;">' + escapeHtml(m.planting_hint) + '</div></td></tr>';
        }).join('') || '<tr><td colspan="6" style="padding:1rem; color:#f59e0b;">Arsip iklim belum tersedia.</td></tr>';
      }
    }

    async function openWeatherForecastModal() {
      const modal = document.getElementById('weatherForecastModal');
      if (!modal) return;
      modal.style.display = 'flex';
      if (!window._weatherData) await updateSolarWeatherAndAdvisory(false);
      renderWeatherModal();
    }

    function closeWeatherForecastModal() {
      const modal = document.getElementById('weatherForecastModal');
      if (modal) modal.style.display = 'none';
    }

    let latestBmsData = null;
    let currentBmsTab = 'all';

    function setZteBmsTab(tab) {
      currentBmsTab = tab;
      renderZteBmsModal();
    }

    function openZteBmsModal() {
      const modal = document.getElementById('zteBmsModal');
      if (modal) {
        modal.style.display = 'flex';
        renderZteBmsModal();
      }
    }

    function closeZteBmsModal() {
      const modal = document.getElementById('zteBmsModal');
      if (modal) modal.style.display = 'none';
    }

    function renderZteBmsModal() {
      if (!latestBmsData) return;
      const b = latestBmsData;
      const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

      const packCount = b.pack_count || (b.packs ? Object.keys(b.packs).length : 1);
      setText('modalBmsTitle', `Telemetri BMS ZTE Birubatt (${packCount} Pack • ${b.nominal_capacity_ah || (packCount*100)}Ah)`);
      const slaveList = b.packs ? Object.keys(b.packs).map(k => 'Slave ' + k).join(' & ') : 'Slave 2';
      setText('modalBmsSubtitle', `${packCount}x 15S LiFePO4 (${b.cell_count || (packCount*15)} Sel Total) • Port /dev/ttyUSB0 • ${slaveList} • 9600 bps Even`);
      
      const badge = document.getElementById('modalBmsStatusBadge');
      if (badge) badge.textContent = `${packCount} PACK ONLINE (RS485)`;

      setText('mBmsPackV', b.pack_voltage != null ? b.pack_voltage.toFixed(2) + ' V' : '-- V');
      setText('mBmsBusV', b.busbar_voltage != null ? b.busbar_voltage.toFixed(2) + ' V' : '-- V');
      setText('mBmsCurr', (b.pack_current > 0 ? '+' : '') + (b.pack_current != null ? b.pack_current.toFixed(2) : '--') + ' A');
      setText('mBmsWatt', (b.pack_power_watt > 0 ? '+' : '') + (b.pack_power_watt != null ? b.pack_power_watt : '--') + ' W');
      setText('mBmsSoc', (b.soc != null ? b.soc : '--') + '%');
      setText('mBmsCapacity', `${b.nominal_capacity_ah || (packCount * 100)} Ah (${b.nominal_energy_kwh || (packCount * 4.8)} kWh)`);
      setText('mBmsTemp', (b.avg_cell_temp_c != null ? b.avg_cell_temp_c.toFixed(1) : '--') + ' °C');
      setText('mBmsSensorCount', `${b.temperatures ? b.temperatures.length : (packCount * 3)} Sensor Suhu Aktif`);
      setText('mBmsBalanceLabel', b.balance_status || '🟢 Seimbang');
      setText('mBmsMaxCell', b.cell_max_v != null ? b.cell_max_v.toFixed(3) + ' V' : '-- V');
      setText('mBmsMinCell', b.cell_min_v != null ? b.cell_min_v.toFixed(3) + ' V' : '-- V');
      setText('mBmsDeltaMv', (b.cell_delta_mv != null ? b.cell_delta_mv.toFixed(1) : '--') + ' mV');
      setText('mBmsLastUpdate', b.timestamp ? b.timestamp.substr(11, 8) + ' WIB' : '--');

      // 1. Render Tab Buttons
      const tabBtnsEl = document.getElementById('mBmsTabButtons');
      if (tabBtnsEl && b.packs) {
        const packKeys = Object.keys(b.packs);
        let tabHtml = `
          <button onclick="setZteBmsTab('all')" style="background:${currentBmsTab === 'all' ? '#38bdf8' : '#1e293b'}; color:${currentBmsTab === 'all' ? '#0f172a' : '#94a3b8'}; border:1px solid ${currentBmsTab === 'all' ? '#38bdf8' : '#334155'}; border-radius:4px; padding:0.35rem 0.85rem; font-size:0.74rem; font-weight:700; cursor:pointer;">
            🔋 Semua Baterai (${b.cell_count} Sel)
          </button>
        `;
        packKeys.forEach(k => {
          const p = b.packs[k];
          const isAct = currentBmsTab === k;
          tabHtml += `
            <button onclick="setZteBmsTab('${k}')" style="background:${isAct ? '#38bdf8' : '#1e293b'}; color:${isAct ? '#0f172a' : '#94a3b8'}; border:1px solid ${isAct ? '#38bdf8' : '#334155'}; border-radius:4px; padding:0.35rem 0.85rem; font-size:0.74rem; font-weight:600; cursor:pointer;">
              🔋 ${p.name || ('Pack Slave ' + k)}
            </button>
          `;
        });
        tabBtnsEl.innerHTML = tabHtml;
      }

      // 2. Render Packs Container
      const containerEl = document.getElementById('mBmsPacksContainer');
      if (!containerEl) return;

      const packsToShow = [];
      if (b.packs) {
        if (currentBmsTab === 'all') {
          Object.keys(b.packs).forEach(k => packsToShow.push(b.packs[k]));
        } else if (b.packs[currentBmsTab]) {
          packsToShow.push(b.packs[currentBmsTab]);
        }
      } else {
        packsToShow.push({
          slave_id: 2,
          name: "Baterai 1 (Slave 2)",
          pack_voltage: b.pack_voltage,
          pack_current: b.pack_current,
          pack_power_watt: b.pack_power_watt,
          soc: b.soc,
          cycle_count: b.cycle_count,
          avg_cell_temp_c: b.avg_cell_temp_c,
          cell_delta_mv: b.cell_delta_mv,
          cell_max_v: b.cell_max_v,
          cell_min_v: b.cell_min_v,
          cell_max_idx: b.cell_max_idx,
          cell_min_idx: b.cell_min_idx,
          balance_status: b.balance_status,
          cell_voltages: b.cell_voltages || []
        });
      }

      containerEl.innerHTML = packsToShow.map(p => {
        const cells = p.cell_voltages || [];
        const avgV = cells.length ? (cells.reduce((a, c) => a + c, 0) / cells.length) : 3.28;
        const maxCell = p.cell_max_v || (cells.length ? Math.max(...cells) : 0);
        const minCell = p.cell_min_v || (cells.length ? Math.min(...cells) : 0);

        const cellsHtml = cells.map((v, i) => {
          const idx = i + 1;
          const diff = Math.round((v - avgV) * 1000);
          const isMax = idx === p.cell_max_idx || (Math.abs(v - maxCell) < 0.0005);
          const isMin = idx === p.cell_min_idx || (Math.abs(v - minCell) < 0.0005);

          let borderColor = 'rgba(255,255,255,0.08)';
          let bgGrad = 'background: rgba(255,255,255,0.02);';
          let badgeColor = '#94a3b8';

          if (isMax) {
            borderColor = '#34d399';
            bgGrad = 'background: rgba(16,185,129,0.08);';
            badgeColor = '#34d399';
          } else if (isMin) {
            borderColor = '#f87171';
            bgGrad = 'background: rgba(239,68,68,0.08);';
            badgeColor = '#f87171';
          }

          const pct = Math.min(100, Math.max(0, ((v - 2.8) / (3.45 - 2.8)) * 100));

          return `
            <div style="border: 1px solid ${borderColor}; border-radius: 6px; padding: 0.5rem; ${bgGrad}">
              <div style="display:flex; justify-content:space-between; align-items:center; font-size:0.68rem; margin-bottom:0.25rem;">
                <span style="font-weight:700; color:#fff;">Sel ${idx.toString().padStart(2, '0')}</span>
                <span style="color:${badgeColor}; font-size:0.62rem; font-weight:600;">${isMax ? 'MAX' : (isMin ? 'MIN' : (diff >= 0 ? '+' : '') + diff + 'mV')}</span>
              </div>
              <div style="font-size:1.1rem; font-weight:700; color:#fff; font-family:monospace; margin-bottom:0.25rem;">
                ${v.toFixed(3)} <span style="font-size:0.65rem; color:#94a3b8;">V</span>
              </div>
              <div style="width:100%; height:4px; background:rgba(255,255,255,0.08); border-radius:2px; overflow:hidden;">
                <div style="width:${pct}%; height:100%; background:${isMin ? '#f87171' : (isMax ? '#34d399' : '#38bdf8')};"></div>
              </div>
            </div>
          `;
        }).join('');

        return `
          <div style="background:#16161e; border:1px solid #22222d; border-radius:8px; padding:1rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; margin-bottom:0.75rem; border-bottom:1px solid rgba(255,255,255,0.05); padding-bottom:0.6rem;">
              <div style="display:flex; align-items:center; gap:0.5rem;">
                <span style="font-size:1rem;">🔋</span>
                <span style="font-weight:700; color:#f1f5f9; font-size:0.92rem;">${p.name || ('Pack Slave ' + p.slave_id)}</span>
                <span class="hermes-badge badge-ok" style="font-size:0.62rem;">ONLINE</span>
                <span style="font-size:0.75rem; color:#94a3b8;">${p.balance_status || ''}</span>
              </div>
              <div style="display:flex; gap:0.85rem; font-size:0.75rem; color:#cbd5e1; flex-wrap:wrap;">
                <span>Volt: <b style="color:#34d399;">${p.pack_voltage != null ? p.pack_voltage.toFixed(2) : '--'} V</b></span>
                <span>Arus: <b style="color:#38bdf8;">${(p.pack_current > 0 ? '+' : '') + (p.pack_current != null ? p.pack_current.toFixed(2) : '--')} A</b> (<b style="color:#fff;">${(p.pack_power_watt > 0 ? '+' : '') + (p.pack_power_watt != null ? p.pack_power_watt : 0)} W</b>)</span>
                <span>SOC: <b style="color:#fbbf24;">${p.soc}%</b></span>
                <span>SOH: <b style="color:#34d399;">${p.soh || 98}%</b></span>
                <span>Siklus: <b style="color:#cbd5e1;">${p.cycle_count || 0}x</b></span>
                <span>Suhu: <b style="color:#f87171;">${p.avg_cell_temp_c != null ? p.avg_cell_temp_c.toFixed(1) : '--'}°C</b></span>
                <span>Δ Sel: <b style="color:#fbbf24;">${p.cell_delta_mv != null ? p.cell_delta_mv.toFixed(1) : '--'} mV</b></span>
              </div>
            </div>
            <div style="display:grid; grid-template-columns:repeat(5, 1fr); gap:0.6rem;">
              ${cellsHtml}
            </div>
          </div>
        `;
      }).join('');
    }

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        closeWeatherForecastModal();
        closeZteBmsModal();
      }
    });

    // =========================================================================
    // INVERTER SETTINGS & BMS MANAGEMENT CLIENT CONTROLLER
    // =========================================================================
    async function loadInverterSettings() {
      const fb = document.getElementById('setSaveFeedback');
      if (fb) {
        fb.style.display = 'inline-block';
        fb.style.color = '#38bdf8';
        fb.textContent = 'Memuat parameter dari inverter...';
      }
      try {
        const res = await fetch('/api/inverter/settings');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (!data.success && !data.settings) throw new Error(data.error || 'Gagal membaca settings');
        const s = data.settings || {};
        
        // Populate inputs & badges flexibly
        const outPrio = s.output_priority_code || (s.output_priority != null ? (s.output_priority === 2 ? 'SBU' : (s.output_priority === 1 ? 'SUB' : 'USB')) : null) || (s.output_source_priority != null ? (s.output_source_priority === 2 ? 'SBU' : (s.output_source_priority === 1 ? 'SUB' : 'USB')) : null);
        if (outPrio) {
          const el = document.getElementById('inpOutputPriority');
          if (el) el.value = outPrio;
          const lbl = document.getElementById('lblCurOutputPrio');
          if (lbl) lbl.textContent = outPrio + ' (Aktif)';
        }

        const chgCodeMap = { 0: 'UTILITY_FIRST', 1: 'SOLAR_FIRST', 2: 'SOLAR_UTILITY', 3: 'ONLY_SOLAR' };
        const chgVal = s.charger_priority != null ? chgCodeMap[s.charger_priority] : (s.charger_source_priority != null ? chgCodeMap[s.charger_source_priority] : null);
        if (chgVal) {
          const el = document.getElementById('inpChargerPriority');
          if (el) el.value = chgVal;
          const lbl = document.getElementById('lblCurChargerPrio');
          if (lbl) lbl.textContent = (s.charger_priority_name || chgVal) + ' (Aktif)';
        }

        const maxChg = s.max_charge_current != null ? s.max_charge_current : s.max_charging_current;
        if (maxChg != null) {
          const el = document.getElementById('inpMaxChargeCurrent');
          if (el) el.value = String(maxChg);
          const lbl = document.getElementById('lblCurMaxChg');
          if (lbl) lbl.textContent = maxChg + ' A';
        }

        const maxAcChg = s.max_ac_charge_current != null ? s.max_ac_charge_current : s.max_ac_charging_current;
        if (maxAcChg != null) {
          const el = document.getElementById('inpMaxAcChargeCurrent');
          if (el) el.value = String(maxAcChg);
          const lbl = document.getElementById('lblCurMaxAcChg');
          if (lbl) lbl.textContent = maxAcChg + ' A';
        }

        const bulkV = s.bulk_voltage != null ? s.bulk_voltage : s.battery_bulk_voltage;
        if (bulkV != null) {
          const el = document.getElementById('inpBulkVoltage');
          if (el) el.value = Number(bulkV).toFixed(1);
          const lbl = document.getElementById('lblCurBulkV');
          if (lbl) lbl.textContent = Number(bulkV).toFixed(1) + ' V';
        }

        const floatV = s.float_voltage != null ? s.float_voltage : s.battery_float_voltage;
        if (floatV != null) {
          const el = document.getElementById('inpFloatVoltage');
          if (el) el.value = Number(floatV).toFixed(1);
          const lbl = document.getElementById('lblCurFloatV');
          if (lbl) lbl.textContent = Number(floatV).toFixed(1) + ' V';
        }

        const cutoffV = s.cutoff_voltage != null ? s.cutoff_voltage : s.battery_cutoff_voltage;
        if (cutoffV != null) {
          const el = document.getElementById('inpCutoffVoltage');
          if (el) el.value = Number(cutoffV).toFixed(1);
          const lbl = document.getElementById('lblCurCutoffV');
          if (lbl) lbl.textContent = Number(cutoffV).toFixed(1) + ' V';
        }

        const backGridV = s.back_to_grid_voltage != null ? s.back_to_grid_voltage : null;
        if (backGridV != null) {
          const el = document.getElementById('inpBackGridVoltage');
          if (el) el.value = Number(backGridV).toFixed(1);
          const lbl = document.getElementById('lblCurBackGridV');
          if (lbl) lbl.textContent = Number(backGridV).toFixed(1) + ' V';
        }

        const backBattV = s.back_to_battery_voltage != null ? s.back_to_battery_voltage : null;
        if (backBattV != null) {
          const el = document.getElementById('inpBackBattVoltage');
          if (el) el.value = Number(backBattV).toFixed(1);
          const lbl = document.getElementById('lblCurBackBattV');
          if (lbl) lbl.textContent = Number(backBattV).toFixed(1) + ' V';
        }

        const b = document.getElementById('setInverterStatusBadge');
        if (b) {
          b.className = 'hermes-badge badge-ok';
          b.textContent = '● INVERTER RS232 AKTIF & SINKRON';
        }
        if (fb) {
          fb.style.color = '#34d399';
          fb.textContent = '✓ Parameter berhasil disinkronkan (' + new Date().toLocaleTimeString('id-ID') + ')';
          setTimeout(() => { fb.style.display = 'none'; }, 4000);
        }
      } catch (e) {
        if (fb) {
          fb.style.color = '#f87171';
          fb.textContent = 'Gagal membaca: ' + e.message;
        }
      }
    }

    async function applyInverterPreset(presetMode) {
      if (!confirm('Terapkan profil optimasi ' + presetMode + ' ke Inverter PowMr sekarang?')) return;
      const fb = document.getElementById('setSaveFeedback');
      if (fb) {
        fb.style.display = 'inline-block';
        fb.style.color = '#38bdf8';
        fb.textContent = 'Menerapkan preset ' + presetMode + '...';
      }
      try {
        const res = await fetch('/api/inverter/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'apply_preset', preset: presetMode === 'SUB' ? 'sub_standby' : 'safe_15s' })
        });
        const d = await res.json();
        if (d.success) {
          if (fb) {
            fb.style.color = '#34d399';
            fb.textContent = '✓ Profil berhasil diterapkan ke Inverter!';
          }
          await loadInverterSettings();
        } else {
          throw new Error(d.error || 'Gagal menerapkan preset');
        }
      } catch (e) {
        if (fb) {
          fb.style.color = '#f87171';
          fb.textContent = 'Error: ' + e.message;
        }
      }
    }

    async function saveInverterSettings() {
      const fb = document.getElementById('setSaveFeedback');
      const btn = document.getElementById('btnSaveInverterSettings');
      if (btn) btn.disabled = true;
      if (fb) {
        fb.style.display = 'inline-block';
        fb.style.color = '#38bdf8';
        fb.textContent = 'Mengirim parameter ke inverter...';
      }

      const settings = {
        output_priority: document.getElementById('inpOutputPriority').value,
        charger_priority: document.getElementById('inpChargerPriority').value,
        max_charge_current: parseInt(document.getElementById('inpMaxChargeCurrent').value, 10),
        max_ac_charge_current: parseInt(document.getElementById('inpMaxAcChargeCurrent').value, 10),
        bulk_voltage: parseFloat(document.getElementById('inpBulkVoltage').value),
        float_voltage: parseFloat(document.getElementById('inpFloatVoltage').value),
        cutoff_voltage: parseFloat(document.getElementById('inpCutoffVoltage').value),
        back_to_grid_voltage: parseFloat(document.getElementById('inpBackGridVoltage').value),
        back_to_battery_voltage: parseFloat(document.getElementById('inpBackBattVoltage').value)
      };

      try {
        const res = await fetch('/api/inverter/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'update_batch', settings: settings })
        });
        const d = await res.json();
        if (d.success) {
          if (fb) {
            fb.style.color = '#34d399';
            fb.textContent = '✓ Semua parameter berhasil disimpan ke Inverter!';
          }
          await loadInverterSettings();
        } else {
          let errs = [];
          if (d.results) {
            for (let k in d.results) {
              if (!d.results[k].success) errs.push(k + ': ' + d.results[k].message);
            }
          }
          throw new Error(errs.length ? errs.join(', ') : (d.error || 'Beberapa parameter gagal diubah'));
        }
      } catch (e) {
        if (fb) {
          fb.style.color = '#f87171';
          fb.textContent = 'Gagal menyimpan: ' + e.message;
        }
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function loadBmsConfig() {
      const fb = document.getElementById('bmsSaveFeedback');
      try {
        const res = await fetch('/api/bms/config');
        if (!res.ok) return;
        const data = await res.json();
        if (!data.success || !data.config) return;
        const c = data.config;

        const connIds = c.connected_slave_ids || c.system?.slave_ids || "2, 5";
        const pCount = c.system?.pack_count || (c.connected_slaves?.length) || 2;
        const badge = document.getElementById('bmsCfgStatusBadge');
        if (badge) {
          badge.textContent = `● ${pCount} PACK ONLINE (Slave ${connIds} • ${pCount * 15} Sel)`;
        }

        if (c.cell_protection) {
          const cp = c.cell_protection;
          if (cp.ovp_v != null) document.getElementById('bmsInpOvpV').value = cp.ovp_v;
          if (cp.ov_alarm_v != null) document.getElementById('bmsInpOvAlarmV').value = cp.ov_alarm_v;
          if (cp.balance_start_v != null) document.getElementById('bmsInpBalanceStartV').value = cp.balance_start_v;
          if (cp.balance_delta_mv != null) document.getElementById('bmsInpDeltaMv').value = cp.balance_delta_mv;
          if (cp.uv_alarm_v != null) document.getElementById('bmsInpUvAlarmV').value = cp.uv_alarm_v;
          if (cp.uvp_v != null) document.getElementById('bmsInpUvpV').value = cp.uvp_v;
        }

        if (c.system) {
          if (c.system.nominal_capacity_ah != null) document.getElementById('bmsInpNominalAh').value = c.system.nominal_capacity_ah;
          if (c.system.slave_ids != null) document.getElementById('bmsInpSlaveIds').value = c.system.slave_ids;
          if (c.system.polling_interval_sec != null) document.getElementById('bmsInpPollingSec').value = c.system.polling_interval_sec;
        }

        if (c.current_limits) {
          if (c.current_limits.max_charge_a != null) document.getElementById('bmsInpMaxChargeA').value = c.current_limits.max_charge_a;
          if (c.current_limits.max_discharge_a != null) document.getElementById('bmsInpMaxDischargeA').value = c.current_limits.max_discharge_a;
        }

        if (c.pack_protection && c.pack_protection.pack_uvp_v != null) {
          document.getElementById('bmsInpPackUvpV').value = c.pack_protection.pack_uvp_v;
        }

        if (c.thermal_protection) {
          const tp = c.thermal_protection;
          if (tp.high_temp_charge_alarm_c != null) document.getElementById('bmsInpHighTempAlarm').value = tp.high_temp_charge_alarm_c;
          if (tp.high_temp_charge_cutoff_c != null) document.getElementById('bmsInpHighTempCutoff').value = tp.high_temp_charge_cutoff_c;
          if (tp.low_temp_charge_cutoff_c != null) document.getElementById('bmsInpLowTempCutoff').value = tp.low_temp_charge_cutoff_c;
        }
      } catch (err) {
        console.error('Gagal memuat konfigurasi BMS:', err);
      }
    }

    function syncBmsFromConnectedPacks() {
      fetch('/api/bms/config')
        .then(r => r.json())
        .then(d => {
          if (!d.success || !d.config) return;
          const c = d.config;
          const connIds = c.connected_slave_ids || c.system?.slave_ids || "2, 5";
          const pCount = c.system?.pack_count || (c.connected_slaves?.length) || 2;
          document.getElementById('bmsInpSlaveIds').value = connIds;
          document.getElementById('bmsInpNominalAh').value = pCount * 100;
          document.getElementById('bmsInpMaxChargeA').value = pCount * 20;
          const badge = document.getElementById('bmsCfgStatusBadge');
          if (badge) {
            badge.textContent = `● ${pCount} PACK ONLINE (Slave ${connIds} • ${pCount * 15} Sel)`;
          }
          const fb = document.getElementById('bmsSaveFeedback');
          if (fb) {
            fb.style.display = 'inline-block';
            fb.style.color = '#34d399';
            fb.textContent = `✓ Disinkronkan dari pack aktif: Slave ${connIds} (${pCount} pack • ${pCount * 100}Ah)`;
          }
        })
        .catch(err => {
          console.error(err);
        });
    }

    async function saveBmsConfig(event) {
      if (event && event.preventDefault) event.preventDefault();
      const fb = document.getElementById('bmsSaveFeedback');
      const btn = document.getElementById('btnSaveBmsConfig');
      const origBtnHtml = btn ? btn.innerHTML : '<span>💾</span> Simpan Konfigurasi BMS';
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> Menyimpan...';
      }
      if (fb) {
        fb.style.display = 'inline-block';
        fb.style.color = '#38bdf8';
        fb.textContent = 'Menyimpan konfigurasi BMS...';
      }

      const payload = {
        system: {
          nominal_capacity_ah: parseFloat(document.getElementById('bmsInpNominalAh')?.value) || 200,
          slave_ids: document.getElementById('bmsInpSlaveIds')?.value || "2, 5",
          polling_interval_sec: parseInt(document.getElementById('bmsInpPollingSec')?.value, 10) || 3
        },
        cell_protection: {
          ovp_v: parseFloat(document.getElementById('bmsInpOvpV')?.value) || 3.65,
          ov_alarm_v: parseFloat(document.getElementById('bmsInpOvAlarmV')?.value) || 3.55,
          balance_start_v: parseFloat(document.getElementById('bmsInpBalanceStartV')?.value) || 3.40,
          balance_delta_mv: parseInt(document.getElementById('bmsInpDeltaMv')?.value, 10) || 30,
          uv_alarm_v: parseFloat(document.getElementById('bmsInpUvAlarmV')?.value) || 2.80,
          uvp_v: parseFloat(document.getElementById('bmsInpUvpV')?.value) || 2.50
        },
        pack_protection: {
          pack_uvp_v: parseFloat(document.getElementById('bmsInpPackUvpV')?.value) || 44.0
        },
        current_limits: {
          max_charge_a: parseInt(document.getElementById('bmsInpMaxChargeA')?.value, 10) || 40,
          max_discharge_a: parseInt(document.getElementById('bmsInpMaxDischargeA')?.value, 10) || 100
        },
        thermal_protection: {
          high_temp_charge_alarm_c: parseInt(document.getElementById('bmsInpHighTempAlarm')?.value, 10) || 48,
          high_temp_charge_cutoff_c: parseInt(document.getElementById('bmsInpHighTempCutoff')?.value, 10) || 55,
          low_temp_charge_cutoff_c: parseInt(document.getElementById('bmsInpLowTempCutoff')?.value, 10) || 0
        }
      };

      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);

      try {
        const res = await fetch('/api/bms/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: controller.signal
        });
        clearTimeout(timeoutId);
        const d = await res.json();
        if (d.success) {
          if (fb) {
            fb.style.color = '#34d399';
            fb.textContent = '✓ Konfigurasi BMS berhasil disimpan & diterapkan!';
          }
          if (btn) {
            btn.innerHTML = '<span>✓</span> Tersimpan!';
          }
        } else {
          throw new Error(d.error || 'Gagal menyimpan konfigurasi');
        }
      } catch (err) {
        clearTimeout(timeoutId);
        if (fb) {
          fb.style.color = '#f87171';
          fb.textContent = 'Error: ' + (err.name === 'AbortError' ? 'Koneksi timeout (8s)' : err.message);
        }
        if (btn) {
          btn.innerHTML = '<span>⚠️</span> Gagal Simpan';
        }
      } finally {
        setTimeout(() => {
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = origBtnHtml;
          }
        }, 2000);
      }
    }

    function applyBmsPresetSafe() {
      document.getElementById('bmsInpOvpV').value = 3.65;
      document.getElementById('bmsInpOvAlarmV').value = 3.55;
      document.getElementById('bmsInpBalanceStartV').value = 3.40;
      document.getElementById('bmsInpDeltaMv').value = 30;
      document.getElementById('bmsInpUvAlarmV').value = 2.80;
      document.getElementById('bmsInpUvpV').value = 2.50;
      document.getElementById('bmsInpNominalAh').value = 200;
      document.getElementById('bmsInpMaxChargeA').value = 40;
      document.getElementById('bmsInpMaxDischargeA').value = 100;
      document.getElementById('bmsInpPackUvpV').value = 44.0;
      document.getElementById('bmsInpHighTempAlarm').value = 48;
      document.getElementById('bmsInpHighTempCutoff').value = 55;
      document.getElementById('bmsInpLowTempCutoff').value = 0;
      document.getElementById('bmsInpSlaveIds').value = "2, 5";
      document.getElementById('bmsInpPollingSec').value = 3;

      const fb = document.getElementById('bmsSaveFeedback');
      if (fb) {
        fb.style.display = 'inline-block';
        fb.style.color = '#fbbf24';
        fb.textContent = 'Preset Safe 15S dimuat. Klik Simpan untuk menerapkan.';
      }
    }

    let energyDailyChart = null;

    // Fetch and render Energy Analytics (Daily & Monthly History & Projections)
    async function fetchEnergyAnalytics() {
      try {
        const res = await fetch('/api/energy/analytics');
        if (!res.ok) return;
        const data = await res.json();

        // 1. Timestamp
        const timeEl = document.getElementById('energyAnalyticsUpdateText');
        if (timeEl && data.updated_at) {
          timeEl.textContent = 'Diperbarui: ' + data.updated_at.substr(11, 8) + ' WIB';
        }

        // 2. Today metrics & projections
        if (data.today) {
          const t = data.today;
          const elTodayActLoad = document.getElementById('kwhTodayActualLoad');
          if (elTodayActLoad) elTodayActLoad.textContent = (t.actual_load_kwh != null ? t.actual_load_kwh.toFixed(2) : '--') + ' kWh';

          const elTodayProjLoad = document.getElementById('kwhTodayProjectedLoad');
          if (elTodayProjLoad) elTodayProjLoad.textContent = '~' + (t.projected_load_kwh != null ? t.projected_load_kwh.toFixed(2) : '1.85') + ' kWh';

          const elTodayPeakLoad = document.getElementById('kwhTodayPeakLoad');
          if (elTodayPeakLoad) elTodayPeakLoad.textContent = (t.peak_load_w || 0) + ' W';

          const elTodayActPv = document.getElementById('kwhTodayActualPv');
          if (elTodayActPv) elTodayActPv.textContent = (t.actual_pv_kwh != null ? t.actual_pv_kwh.toFixed(2) : '--') + ' kWh';

          const elTodayProjPv = document.getElementById('kwhTodayProjectedPv');
          if (elTodayProjPv) elTodayProjPv.textContent = '~' + (t.projected_pv_kwh != null ? t.projected_pv_kwh.toFixed(2) : '4.80') + ' kWh';

          const elTodayProjSav = document.getElementById('kwhTodayProjSavings');
          if (elTodayProjSav) elTodayProjSav.textContent = 'Rp ' + Number(t.projected_savings_rp || 0).toLocaleString('id-ID');
        }

        // 3. Monthly metrics & projections
        if (data.monthly) {
          const m = data.monthly;
          const elMonthTitle = document.getElementById('monthLoadTitle');
          if (elMonthTitle) elMonthTitle.textContent = 'Konsumsi ' + (m.month_name || 'Bulan Ini');

          const elMonthActLoad = document.getElementById('kwhMonthActualLoad');
          if (elMonthActLoad) elMonthActLoad.textContent = (m.actual_load_kwh != null ? m.actual_load_kwh.toFixed(2) : '--') + ' kWh';

          const elMonthProjLoad = document.getElementById('kwhMonthProjectedLoad');
          if (elMonthProjLoad) elMonthProjLoad.textContent = '~' + (m.projected_load_kwh != null ? m.projected_load_kwh.toFixed(1) : '55.0') + ' kWh';

          const elSelfSuff = document.getElementById('kwhSelfSufficiency');
          if (elSelfSuff) elSelfSuff.textContent = (m.self_sufficiency_pct != null ? m.self_sufficiency_pct : 0) + '% Mandiri Surya';

          const elMonthProjSav = document.getElementById('kwhMonthProjSavings');
          if (elMonthProjSav) elMonthProjSav.textContent = 'Rp ' + Number(m.projected_savings_rp || 0).toLocaleString('id-ID');

          const elMonthProjPv = document.getElementById('kwhMonthProjPv');
          if (elMonthProjPv) elMonthProjPv.textContent = '~' + (m.projected_pv_kwh != null ? m.projected_pv_kwh.toFixed(1) : '125.0') + ' kWh';

          const elMonthProjCo2 = document.getElementById('kwhMonthProjCo2');
          if (elMonthProjCo2) elMonthProjCo2.textContent = (m.projected_co2_kg != null ? m.projected_co2_kg : '0') + ' kg (' + (m.projected_trees || '0') + ' 🌲)';
        }

        // 3b. Battery Stats & Support Estimation
        if (data.battery_stats) {
          const bs = data.battery_stats;
          const elChgKwh = document.getElementById('ovBattChargedKwh');
          if (elChgKwh && elChgKwh.textContent.includes('--')) elChgKwh.textContent = bs.today_charged_kwh.toFixed(2) + ' kWh';
          const elChgYest = document.getElementById('ovBattChargedYest');
          if (elChgYest) elChgYest.textContent = bs.yesterday_charged_kwh.toFixed(2) + ' kWh';
          const elChgWh = document.getElementById('ovBattChargedWh');
          if (elChgWh && elChgWh.textContent.includes('--')) elChgWh.textContent = Math.round(bs.today_charged_wh || 0) + ' Wh';

          const elDisKwh = document.getElementById('ovBattDischargedKwh');
          if (elDisKwh && elDisKwh.textContent.includes('--')) elDisKwh.textContent = bs.today_discharged_kwh.toFixed(2) + ' kWh';
          const elDisYest = document.getElementById('ovBattDischargedYest');
          if (elDisYest) elDisYest.textContent = bs.yesterday_discharged_kwh.toFixed(2) + ' kWh';
          const elDodYest = document.getElementById('ovBattDodYest');
          if (elDodYest) elDodYest.textContent = bs.yesterday_discharged_dod_pct + '%';

          const elTarget = document.getElementById('ovBattSupportTarget');
          if (elTarget && elTarget.textContent.includes('--')) elTarget.textContent = bs.target_time_display;
          const elHours = document.getElementById('ovBattSupportHours');
          if (elHours && elHours.textContent.includes('--')) elHours.textContent = '~' + bs.hours_remaining.toFixed(1) + ' Jam';
          const elUsable = document.getElementById('ovBattSupportUsable');
          if (elUsable && elUsable.textContent.includes('--')) elUsable.textContent = bs.usable_kwh.toFixed(2) + ' kWh';
          const elRefLoad = document.getElementById('ovBattSupportRefLoad');
          if (elRefLoad) elRefLoad.textContent = '~' + Math.round(bs.yesterday_avg_night_w || 180) + ' W';

          const elSuppBadge = document.getElementById('ovBattSupportBadge');
          if (elSuppBadge) {
            elSuppBadge.className = 'hermes-badge ' + (bs.status_badge_class || 'badge-ok');
            elSuppBadge.textContent = bs.status_label;
          }
        }

        // 4. Bar Chart 7 Hari
        if (data.seven_days_chart && typeof Chart !== 'undefined') {
          const canvas = document.getElementById('energyDailyBarChart');
          if (canvas) {
            const ctx = canvas.getContext('2d');
            const cData = data.seven_days_chart;
            if (energyDailyChart) {
              energyDailyChart.data.labels = cData.labels;
              energyDailyChart.data.datasets[0].data = cData.pv_kwh;
              energyDailyChart.data.datasets[1].data = cData.load_kwh;
              if (energyDailyChart.data.datasets[2]) energyDailyChart.data.datasets[2].data = cData.batt_charge_kwh || [];
              if (energyDailyChart.data.datasets[3]) energyDailyChart.data.datasets[3].data = cData.batt_discharge_kwh || [];
              energyDailyChart.update();
            } else {
              energyDailyChart = new Chart(ctx, {
                type: 'bar',
                data: {
                  labels: cData.labels,
                  datasets: [
                    {
                      label: '☀️ Produksi Surya (kWh)',
                      data: cData.pv_kwh,
                      backgroundColor: 'rgba(245, 158, 11, 0.85)',
                      borderColor: '#f59e0b',
                      borderWidth: 1,
                      borderRadius: 4,
                    },
                    {
                      label: '🔋 Baterai Masuk (Charge kWh)',
                      data: cData.batt_charge_kwh || [],
                      backgroundColor: 'rgba(16, 185, 129, 0.85)',
                      borderColor: '#10b981',
                      borderWidth: 1,
                      borderRadius: 4,
                    },
                    {
                      label: '⚡ Baterai Keluar (Discharge kWh)',
                      data: cData.batt_discharge_kwh || [],
                      backgroundColor: 'rgba(168, 85, 247, 0.85)',
                      borderColor: '#a855f7',
                      borderWidth: 1,
                      borderRadius: 4,
                    },
                    {
                      label: '🏠 Beban Konsumsi (kWh)',
                      data: cData.load_kwh,
                      backgroundColor: 'rgba(56, 189, 248, 0.85)',
                      borderColor: '#38bdf8',
                      borderWidth: 1,
                      borderRadius: 4,
                    }
                  ]
                },
                options: {
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: {
                    legend: { labels: { color: '#cbd5e1', font: { family: 'Outfit, sans-serif' } } },
                    tooltip: {
                      callbacks: {
                        label: function(ctx) {
                          return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y + ' kWh';
                        }
                      }
                    }
                  },
                  scales: {
                    x: {
                      grid: { color: 'rgba(255, 255, 255, 0.05)' },
                      ticks: { color: '#94a3b8', font: { family: 'Outfit, sans-serif' } }
                    },
                    y: {
                      grid: { color: 'rgba(255, 255, 255, 0.05)' },
                      ticks: {
                        color: '#94a3b8',
                        font: { family: 'Outfit, sans-serif' },
                        callback: function(val) { return val + ' kWh'; }
                      }
                    }
                  }
                }
              });
            }
          }
        }

        // 5. Tabel Riwayat Harian
        if (Array.isArray(data.daily_history)) {
          const tbody = document.getElementById('energyDailyTableBody');
          if (tbody) {
            tbody.innerHTML = data.daily_history.map(row => {
              const isSurplus = row.net_balance_kwh >= 0;
              const badgeClass = isSurplus ? 'badge-ok' : 'badge-warn';
              const badgePrefix = isSurplus ? '+ ' : '';
              return `
                <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                  <td style="padding: 0.45rem 0.5rem;">
                    <div style="font-weight: 600; color: #fff;">${escapeHtml(row.day_name)}</div>
                    <div style="font-size: 0.68rem; color: #64748b;">${escapeHtml(row.date)}</div>
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: right; color: #fbbf24; font-weight: 700; font-family: monospace;">
                    ${row.pv_kwh.toFixed(2)}
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: right; color: #34d399; font-weight: 600; font-family: monospace;">
                    +${(row.batt_charged_kwh != null ? row.batt_charged_kwh : 0).toFixed(2)}
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: right; color: #f59e0b; font-weight: 600; font-family: monospace;">
                    -${(row.batt_discharged_kwh != null ? row.batt_discharged_kwh : 0).toFixed(2)}
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: right; color: #38bdf8; font-weight: 700; font-family: monospace;">
                    ${row.load_kwh.toFixed(2)}
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: right; color: #34d399; font-weight: 600;">
                    Rp ${Number(row.savings_rp).toLocaleString('id-ID')}
                  </td>
                  <td style="padding: 0.45rem 0.5rem; text-align: center;">
                    <span class="hermes-badge ${badgeClass}" style="font-size: 0.62rem; padding: 0.1rem 0.35rem;">
                      ${badgePrefix}${row.net_balance_kwh.toFixed(2)} kWh
                    </span>
                  </td>
                </tr>
              `;
            }).join('');
          }
        }

      } catch (err) {
        console.warn('Gagal memuat analitik energi:', err);
      }
    }

    let currentHistoryRows = [];
    let activeAnalysisCycleMode = 'harian';
    let cachedAnalyticsData = null;
    let anMainCycleChart = null;

    // Load PLTS Analysis Dashboard (Multi-cycle & weekly load capacity)
    async function loadPLTSAnalysisDashboard() {
      try {
        const res = await fetch('/api/energy/analytics');
        if (res.ok) {
          cachedAnalyticsData = await res.json();
          if (cachedAnalyticsData.weekly_capacity_analysis) {
            renderWeeklyCapacityCard(cachedAnalyticsData.weekly_capacity_analysis);
          }
          if (cachedAnalyticsData.cycles) {
            renderCycleMode(activeAnalysisCycleMode);
          }
        }
      } catch (e) {
        console.warn('Gagal memuat analitik siklus PLTS:', e);
      }
      fetchHistoryData();
    }

    async function setAnalysisCycleMode(mode) {
      activeAnalysisCycleMode = mode;
      document.querySelectorAll('.cycle-mode-btn').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById('btnMode' + mode.charAt(0).toUpperCase() + mode.slice(1));
      if (activeBtn) activeBtn.classList.add('active');

      if (!cachedAnalyticsData) {
        await loadPLTSAnalysisDashboard();
      } else if (cachedAnalyticsData.cycles) {
        renderCycleMode(mode);
      }
    }

    function renderWeeklyCapacityCard(cap) {
      if (!cap) return;
      const elPeriod = document.getElementById('wkAnalysisPeriodText');
      if (elPeriod) {
        elPeriod.textContent = `Dihitung otomatis dari ${Number(cap.samples_count || 22800).toLocaleString('id-ID')} sampel telemetri SQLite 7 hari terakhir: mengukur batas aman LiFePO4 48V 100Ah dan inverter PowMr 6.2kW.`;
      }
      const elAvgLoad = document.getElementById('wkAvgLoad');
      if (elAvgLoad) elAvgLoad.textContent = `${cap.avg_load_w || 94.3} W`;

      const elAvgPct = document.getElementById('wkAvgPct');
      if (elAvgPct) elAvgPct.textContent = `${cap.avg_load_pct || 1.8}%`;

      const elPeakLoad = document.getElementById('wkPeakLoad');
      if (elPeakLoad) elPeakLoad.textContent = `${cap.peak_load_w || 332} W`;

      const elSafe24h = document.getElementById('wkSafe24h');
      if (elSafe24h) elSafe24h.textContent = `${cap.safe_24h_continuous_w || 320} W`;

      const elRem = document.getElementById('wkRemaining24h');
      if (elRem) elRem.textContent = `+${cap.remaining_24h_continuous_w || 225.7} W`;

      const elDaytime = document.getElementById('wkSafeDaytime');
      if (elDaytime) elDaytime.textContent = `${Number(cap.safe_daytime_continuous_w || 2500).toLocaleString('id-ID')} W`;

      const elHeadroomText = document.getElementById('wkHeadroomPctText');
      if (elHeadroomText) elHeadroomText.textContent = `${cap.headroom_used_pct || 29.5}%`;

      const elFreeText = document.getElementById('wkFreePctText');
      if (elFreeText) elFreeText.textContent = `${(100 - (cap.headroom_used_pct || 29.5)).toFixed(1)}%`;

      const elBar = document.getElementById('wkHeadroomProgressBar');
      if (elBar) elBar.style.width = `${Math.min(100, cap.headroom_used_pct || 29.5)}%`;

      const elRec = document.getElementById('wkRecommendationDesc');
      if (elRec) {
        elRec.innerHTML = `💡 <b>Rekomendasi Cerdas Mingguan:</b> Penggunaan listrik rumah Anda saat ini sangat hemat (rata-rata <b>${cap.avg_load_w}W</b> atau <b>${cap.avg_load_pct}%</b> dari kapasitas inverter). Anda masih memiliki ruang aman untuk menyalakan beban kontinyu 24 jam sebesar <b>~${cap.remaining_24h_continuous_w} Watt</b> (contoh: Kulkas Inverter ~80W + Homelab/Server Tambahan ~70W) tanpa risiko kekurangan baterai di malam hari. Di siang hari terik (09:30 - 14:00 WIB), beban dapat dinaikkan hingga <b>${Number(cap.safe_daytime_continuous_w).toLocaleString('id-ID')} Watt</b> (AC 1/2 PK ~350W, pompa air ~250W, mesin cuci) murni dari tenaga surya.`;
      }
    }

    function renderCycleMode(mode) {
      if (!cachedAnalyticsData || !cachedAnalyticsData.cycles || !cachedAnalyticsData.cycles[mode]) return;
      const cyc = cachedAnalyticsData.cycles[mode];

      // 1. Update 4 KPI Cards
      const k = cyc.kpis || {};
      const elPvT = document.getElementById('anKpiPvTitle');
      if (elPvT) elPvT.textContent = k.pv_title || 'Produksi Surya';
      const elPvV = document.getElementById('anKpiPvVal');
      if (elPvV) elPvV.textContent = k.pv_val || '-- kWh';
      const elPvS = document.getElementById('anKpiPvSub');
      if (elPvS) elPvS.textContent = k.pv_sub || '';
      const elPvB = document.getElementById('anKpiPvBadge');
      if (elPvB) elPvB.textContent = k.pv_badge || 'SOLAR';

      const elBatT = document.getElementById('anKpiBattTitle');
      if (elBatT) elBatT.textContent = k.batt_title || 'Siklus Baterai';
      const elBatV = document.getElementById('anKpiBattVal');
      if (elBatV) elBatV.textContent = k.batt_val || '--';
      const elBatS = document.getElementById('anKpiBattSub');
      if (elBatS) elBatS.textContent = k.batt_sub || '';
      const elBatB = document.getElementById('anKpiBattBadge');
      if (elBatB) elBatB.textContent = k.batt_badge || 'LiFePO4';

      const elLdT = document.getElementById('anKpiLoadTitle');
      if (elLdT) elLdT.textContent = k.load_title || 'Konsumsi Beban';
      const elLdV = document.getElementById('anKpiLoadVal');
      if (elLdV) elLdV.textContent = k.load_val || '-- kWh';
      const elLdS = document.getElementById('anKpiLoadSub');
      if (elLdS) elLdS.textContent = k.load_sub || '';
      const elLdB = document.getElementById('anKpiLoadBadge');
      if (elLdB) elLdB.textContent = k.load_badge || 'BEBAN';

      const elFnT = document.getElementById('anKpiFinTitle');
      if (elFnT) elFnT.textContent = k.fin_title || 'Nilai Penghematan';
      const elFnV = document.getElementById('anKpiFinVal');
      if (elFnV) elFnV.textContent = k.fin_val || 'Rp --';
      const elFnS = document.getElementById('anKpiFinSub');
      if (elFnS) elFnS.textContent = k.fin_sub || '';
      const elFnB = document.getElementById('anKpiFinBadge');
      if (elFnB) elFnB.textContent = k.fin_badge || 'PLN R1';

      // 2. Update Main Cycle Chart Title & Badge
      const elTitle = document.getElementById('anMainCycleChartTitle');
      if (elTitle) elTitle.textContent = cyc.title || 'Grafik Siklus Energi';
      const elBadge = document.getElementById('anMainCycleBadge');
      if (elBadge) {
        elBadge.textContent = mode === 'harian' ? 'SIKLUS 24 JAM' : (mode === 'mingguan' ? 'SIKLUS 7 HARI' : (mode === 'bulanan' ? 'SIKLUS 30 HARI' : 'SIKLUS 12 BULAN (2026)'));
      }

      // 3. Render anMainCycleChart
      const canvas = document.getElementById('anMainCycleChart');
      if (!canvas || typeof Chart === 'undefined') return;
      const ctx = canvas.getContext('2d');

      if (anMainCycleChart) anMainCycleChart.destroy();

      if (mode === 'harian') {
        anMainCycleChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels: cyc.labels,
            datasets: [
              {
                label: '☀️ Solar PV (Watt)',
                data: cyc.pv_w,
                borderColor: '#f59e0b',
                backgroundColor: 'rgba(245, 158, 11, 0.15)',
                borderWidth: 2,
                fill: true,
                tension: 0.35,
              },
              {
                label: '🏠 Beban AC Output (Watt)',
                data: cyc.load_w,
                borderColor: '#38bdf8',
                backgroundColor: 'rgba(56, 189, 248, 0.12)',
                borderWidth: 2,
                fill: true,
                tension: 0.35,
              },
              {
                label: '🔋 Daya Baterai (Net Watt)',
                data: cyc.batt_w,
                borderColor: '#10b981',
                borderWidth: 1.5,
                borderDash: [4, 4],
                fill: false,
                tension: 0.3,
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { labels: { color: '#cbd5e1', font: { family: 'Outfit, sans-serif' } } },
              tooltip: {
                callbacks: {
                  label: function(ctx) { return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y + ' W'; }
                }
              }
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#94a3b8' } },
              y: {
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#94a3b8', callback: function(val) { return val + ' W'; } }
              }
            }
          }
        });
      } else if (mode === 'mingguan') {
        anMainCycleChart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: cyc.labels,
            datasets: [
              {
                label: '☀️ Produksi Surya (kWh)',
                data: cyc.pv_kwh,
                backgroundColor: 'rgba(245, 158, 11, 0.85)',
                borderColor: '#f59e0b',
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                label: '🔋 Baterai Masuk (Charge kWh)',
                data: cyc.batt_charge_kwh || [],
                backgroundColor: 'rgba(16, 185, 129, 0.85)',
                borderColor: '#10b981',
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                label: '⚡ Baterai Keluar (Discharge kWh)',
                data: cyc.batt_discharge_kwh || [],
                backgroundColor: 'rgba(168, 85, 247, 0.85)',
                borderColor: '#a855f7',
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                label: '🏠 Konsumsi Beban (kWh)',
                data: cyc.load_kwh,
                backgroundColor: 'rgba(56, 189, 248, 0.85)',
                borderColor: '#38bdf8',
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                type: 'line',
                label: '🟢 Surplus Energi (kWh)',
                data: cyc.net_kwh,
                borderColor: '#34d399',
                borderWidth: 2,
                fill: false,
                tension: 0.2,
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { labels: { color: '#cbd5e1', font: { family: 'Outfit, sans-serif' } } },
              tooltip: {
                callbacks: {
                  label: function(ctx) { return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y + ' kWh'; }
                }
              }
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#94a3b8' } },
              y: {
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#94a3b8', callback: function(val) { return val + ' kWh'; } }
              }
            }
          }
        });
      } else if (mode === 'bulanan') {
        anMainCycleChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels: cyc.labels,
            datasets: [
              {
                label: '☀️ Kumulatif Surya (kWh)',
                data: cyc.cum_pv_kwh,
                borderColor: '#f59e0b',
                backgroundColor: 'rgba(245, 158, 11, 0.15)',
                borderWidth: 2.5,
                fill: true,
                tension: 0.25,
              },
              {
                label: '🏠 Kumulatif Beban (kWh)',
                data: cyc.cum_load_kwh,
                borderColor: '#38bdf8',
                backgroundColor: 'rgba(56, 189, 248, 0.1)',
                borderWidth: 2.5,
                fill: true,
                tension: 0.25,
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { labels: { color: '#cbd5e1', font: { family: 'Outfit, sans-serif' } } },
              tooltip: {
                callbacks: {
                  label: function(ctx) { return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y + ' kWh'; }
                }
              }
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#94a3b8' } },
              y: {
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#94a3b8', callback: function(val) { return val + ' kWh'; } }
              }
            }
          }
        });
      } else if (mode === 'tahunan') {
        anMainCycleChart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: cyc.labels,
            datasets: [
              {
                label: '☀️ Produksi Surya (kWh / bln)',
                data: cyc.pv_kwh,
                backgroundColor: 'rgba(245, 158, 11, 0.85)',
                borderColor: '#f59e0b',
                borderWidth: 1,
                borderRadius: 4,
              },
              {
                label: '🏠 Konsumsi Beban (kWh / bln)',
                data: cyc.load_kwh,
                backgroundColor: 'rgba(56, 189, 248, 0.85)',
                borderColor: '#38bdf8',
                borderWidth: 1,
                borderRadius: 4,
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { labels: { color: '#cbd5e1', font: { family: 'Outfit, sans-serif' } } },
              tooltip: {
                callbacks: {
                  label: function(ctx) { return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y + ' kWh'; }
                }
              }
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#94a3b8' } },
              y: {
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#94a3b8', callback: function(val) { return val + ' kWh'; } }
              }
            }
          }
        });
      }
    }

    // Fetch History Data from SQLite
    async function fetchHistoryData() {
      try {
        const res = await fetch('/api/history?range=' + currentRange);
        if (!res.ok) return;
        const rows = await res.json();
        currentHistoryRows = rows;

        const labels = rows.map(r => r.time.substr(11, 8));
        const pvW = rows.map(r => r.pv_w);
        const loadW = rows.map(r => r.load_w);
        const battV = rows.map(r => r.batt_v);
        const soc = rows.map(r => r.batt_soc);
        const gridV = rows.map(r => r.grid_v);
        const acOutV = rows.map(r => r.ac_out_v);
        const tempC = rows.map(r => r.temp_c);

        renderHistoryCharts(labels, pvW, loadW, battV, soc, gridV, acOutV, tempC);
        renderHistoryTable(rows);
      } catch (e) {
        console.error('Gagal mengambil history:', e);
      }
    }

    function downloadHistoryCSV() {
      if (!currentHistoryRows || !currentHistoryRows.length) {
        alert('Belum ada data riwayat yang dimuat untuk diunduh.');
        return;
      }
      const headers = ['Waktu', 'Mode', 'PV_Watt', 'PV_Volt', 'PV_Amp', 'Batt_Volt', 'Batt_SOC', 'Batt_Chg_A', 'Batt_Dischg_A', 'Batt_Watt', 'Load_Watt', 'Load_VA', 'Load_Pct', 'AC_Out_Volt', 'Grid_Volt', 'Temp_C'];
      const csvLines = [headers.join(',')];
      for (const r of currentHistoryRows) {
        csvLines.push([
          `"${r.time || ''}"`,
          `"${r.mode || ''}"`,
          r.pv_w ?? 0,
          r.pv_v ?? 0,
          r.pv_a ?? 0,
          r.batt_v ?? 0,
          r.batt_soc ?? 0,
          r.batt_charge_a ?? 0,
          r.batt_discharge_a ?? 0,
          r.batt_power_w ?? 0,
          r.load_w ?? 0,
          r.load_va ?? 0,
          r.load_pct ?? 0,
          r.ac_out_v ?? 0,
          r.grid_v ?? 0,
          r.temp_c ?? 0
        ].join(','));
      }
      const blob = new Blob([csvLines.join('\\n')], { type: 'text/csv;charset=utf-8;' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `westpro_telemetry_${currentRange}_${new Date().toISOString().slice(0,19).replace(/[:T]/g, '_')}.csv`;
      link.click();
      if (window.showToast) showToast('Berkas CSV riwayat berhasil diunduh.', 'success');
    }

    function renderHistoryCharts(labels, pvW, loadW, battV, soc, gridV, acOutV, tempC) {
      const commonScales = {
        x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', maxTicksLimit: 8, font: { family: 'JetBrains Mono' } } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { family: 'JetBrains Mono' } } }
      };

      // 2. Hist Batt Chart
      if (histBattChart) histBattChart.destroy();
      histBattChart = new Chart(document.getElementById('histBattChart').getContext('2d'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Baterai Volt (V)', data: battV, borderColor: '#10b981', yAxisID: 'y', borderWidth: 2 },
            { label: 'SOC (%)', data: soc, borderColor: '#a855f7', yAxisID: 'y1', borderWidth: 2 }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: commonScales.x,
            y: { position: 'left', grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#10b981' } },
            y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: '#a855f7' } }
          }
        }
      });

      // 3. Hist Volt Chart
      if (histVoltChart) histVoltChart.destroy();
      histVoltChart = new Chart(document.getElementById('histVoltChart').getContext('2d'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'AC Output (V)', data: acOutV, borderColor: '#38bdf8', borderWidth: 2 },
            { label: 'Grid Input (V)', data: gridV, borderColor: '#f43f5e', borderWidth: 2 }
          ]
        },
        options: { responsive: true, maintainAspectRatio: false, scales: commonScales }
      });

      // 4. Hist Temp Chart
      if (histTempChart) histTempChart.destroy();
      histTempChart = new Chart(document.getElementById('histTempChart').getContext('2d'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Suhu Inverter (°C)', data: tempC, borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,0.1)', fill: true, borderWidth: 2 }
          ]
        },
        options: { responsive: true, maintainAspectRatio: false, scales: commonScales }
      });
    }

    function renderHistoryTable(rows) {
      const tbody = document.getElementById('histTableBody');
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="11" style="text-align: center;">Belum ada riwayat data.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.slice(-50).reverse().map(r => `
        <tr>
          <td>${r.time}</td>
          <td><span style="color:#fbbf24;">${r.mode}</span></td>
          <td><b>${r.pv_w} W</b></td>
          <td>${r.pv_v} V</td>
          <td style="color:#10b981;"><b>${r.batt_v} V</b></td>
          <td>${r.batt_soc}%</td>
          <td>${r.batt_charge_a} A</td>
          <td style="color:#38bdf8;"><b>${r.load_w} W</b></td>
          <td>${r.ac_out_v} V</td>
          <td>${r.grid_v} V</td>
          <td style="color:#ef4444;">${r.temp_c} °C</td>
        </tr>
      `).join('');
    }

    // Fetch Models & Model Selection
    let availableModelsData = null;

    async function fetchModels() {
      try {
        const res = await fetch('/api/models');
        if (!res.ok) return;
        const data = await res.json();
        availableModelsData = data;

        const populateSelect = (selectEl, includeDefaultOption = false) => {
          if (!selectEl) return;
          let html = includeDefaultOption ? '<option value="">Default (Sesuai Hermes)</option>' : '';
          data.categories.forEach(cat => {
            html += `<optgroup label="${cat.category}">`;
            cat.models.forEach(m => {
              const badgeTxt = m.badge ? ` [${m.badge}]` : '';
              html += `<option value="${m.id}">${m.name}${badgeTxt}</option>`;
            });
            html += '</optgroup>';
          });
          selectEl.innerHTML = html;
        };

        const hermesSel = document.getElementById('hermesModelSelect');
        if (hermesSel) {
          populateSelect(hermesSel, false);
          if (data.current_model) hermesSel.value = data.current_model;
        }

        const meetingSel = document.getElementById('meetingModelSelect');
        if (meetingSel) {
          populateSelect(meetingSel, true);
        }
      } catch (e) {
        console.error('Gagal fetch models:', e);
      }
    }

    async function onModelSelectChanged(newModel) {
      if (!newModel) return;
      const fb = document.getElementById('modelChangeFeedback');
      if (fb) {
        fb.textContent = 'Menyimpan...';
        fb.style.display = 'inline';
        fb.style.color = '#38bdf8';
      }
      try {
        const res = await fetch('/api/model/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: newModel })
        });
        const data = await res.json();
        if (fb) {
          fb.textContent = 'Aktif: ' + newModel.split(':')[0];
          fb.style.color = '#10b981';
          setTimeout(() => { fb.style.display = 'none'; }, 3500);
        }
        fetchHermesStatus();
      } catch (e) {
        if (fb) {
          fb.textContent = 'Gagal menyimpan pilihan model';
          fb.style.color = '#f87171';
        }
      }
    }

    // Fetch Hermes Status & Events
    async function fetchHermesStatus() {
      try {
        const res = await fetch('/api/hermes/status');
        if (!res.ok) return;
        const data = await res.json();

        const modelSel = document.getElementById('hermesModelSelect');
        if (modelSel && data.llm_model && document.activeElement !== modelSel && modelSel.options.length > 1) {
          modelSel.value = data.llm_model;
        }
        document.getElementById('hermesLowThresh').textContent = data.soc_low_threshold + '% SOC';
        document.getElementById('hermesHighThresh').textContent = data.soc_high_threshold + '% SOC';
        document.getElementById('hermesPollSec').textContent = data.solar_poll_seconds + ' detik';
        document.getElementById('hermesConnStatus').textContent = data.state.connected_to_solar ? 'TERHUBUNG (SOC ' + data.state.last_soc + '%)' : 'TERPUTUS';

        const feed = document.getElementById('hermesEventsFeed');
        if (data.events && data.events.length) {
          feed.innerHTML = data.events.map(ev => `
            <div class="event-item ${ev.category}">
              <div class="event-time">${ev.timestamp.replace('T', ' ').substr(0, 19)} &bull; [${ev.category.toUpperCase()}]</div>
              <div style="font-weight: 600; color: #fff; margin: 0.15rem 0;">${ev.title}</div>
              <div style="font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: #cbd5e1;">${JSON.stringify(ev.details)}</div>
            </div>
          `).join('');
        } else {
          feed.innerHTML = '<div style="color: var(--gf-text-muted);">Belum ada catatan aktivitas Hermes.</div>';
        }
      } catch (e) {
        console.error('Gagal fetch status Hermes:', e);
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function applyQuickPrompt(text) {
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = text;
        input.focus();
      }
    }

    function clearAiChatHistory() {
      const bubbles = document.getElementById('aiChatBubbles');
      if (bubbles) {
        bubbles.innerHTML = '<div class="chat-bubble chat-bubble-ai"><div style="display:flex;align-items:center;gap:0.4rem;font-weight:600;color:#38bdf8;font-size:0.78rem;margin-bottom:0.35rem;"><span>Hermes</span></div><p style="margin:0; line-height:1.45;">Catatan percakapan dibersihkan. Mau mulai cek task apa sekarang, Ris?</p></div>';
      }
    }

    // ==================== SPECTRUM SPRINT & QA WORKSPACE ====================
    window._spectrumData = null;
    window._spectrumFilter = 'my';
    window._lastOtpCount = 0;
    window._myMemberName = '';
    window._myEmail = '';

    function isMyTask(t) {
      if (!t) return false;
      const myName = (window._myMemberName || (typeof CURRENT_USER_NAME !== 'undefined' ? CURRENT_USER_NAME : '')).toLowerCase().trim();
      const myEmail = (window._myEmail || (typeof CURRENT_USER_EMAIL !== 'undefined' ? CURRENT_USER_EMAIL : '')).toLowerCase().trim();
      const tAssignee = (t.assignee || '').toLowerCase();
      const tEmail = (t.assignee_email || '').toLowerCase();
      const tCreator = (t.created_by || '').toLowerCase();

      // 1. Direct match on email
      if (myEmail && tEmail && (tEmail === myEmail || tEmail.includes(myEmail) || myEmail.includes(tEmail))) return true;

      // 2. Direct match on full name
      if (myName && tAssignee && (tAssignee.includes(myName) || myName.includes(tAssignee))) return true;
      if (myName && tCreator && (tCreator.includes(myName) || myName.includes(tCreator))) return true;

      // 3. Match by name and email tokens (words with >= 3 characters)
      const nameTokens = myName.split(/\\s+/).filter(w => w.length >= 3);
      const emailTokens = myEmail.split('@')[0].split(/[._-]/).filter(w => w.length >= 3);
      const allTokens = [...new Set([...nameTokens, ...emailTokens])];

      // If user is Ares / Aris
      if (myEmail.includes('ares') || myEmail.includes('aris') || myName.includes('ares') || myName.includes('aris')) {
        allTokens.push('aris');
      }

      for (const tok of allTokens) {
        if (tAssignee.includes(tok) || tEmail.includes(tok) || tCreator.includes(tok)) {
          return true;
        }
      }

      return false;
    }

    async function loadSpectrumWorkspaceData() {
      try {
        const res = await fetch('/api/spectrum/data');
        if (!res.ok) return;
        const data = await res.json();
        window._spectrumData = data;
        try { renderBriefingChips(); } catch (e) {}
        try { renderMeetingContext(); } catch (e) {}
        try { renderQuickTaskChips(); } catch (e) {}

        const banner = document.getElementById('spectrumNeedsConfigBanner');
        if (data.needs_config) {
          if (banner) banner.style.display = 'flex';
        } else {
          if (banner) banner.style.display = 'none';
        }

        const userObj = data.user || {};
        window._myMemberName = (userObj.name || (typeof CURRENT_USER_NAME !== 'undefined' ? CURRENT_USER_NAME : '')).trim();
        window._myEmail = (userObj.email || (typeof CURRENT_USER_EMAIL !== 'undefined' ? CURRENT_USER_EMAIL : '')).trim().toLowerCase();

        const myTabLabel = document.getElementById('labelTabMyTask');
        if (myTabLabel) {
          myTabLabel.textContent = window._myMemberName ? `Task Saya (${window._myMemberName})` : 'Task Saya';
        }

        // 1. Last sync badge
        const syncBadge = document.getElementById('spectrumLastSyncBadge');
        if (syncBadge && data.last_synced) {
          const syncDate = new Date(data.last_synced);
          const timeStr = syncDate.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' });
          syncBadge.textContent = 'Diperbarui ' + timeStr;
        }

        // 2. Sprint Overview
        const sprint = data.active_sprint || {};
        const project = data.active_project || {};
        const projName = project.name || (data.projects && data.projects[0] && data.projects[0].name) || 'Proyek QA';
        const sprintName = sprint.name || 'Sprint Berjalan';

        // Update header & badges
        const wsTitle = document.getElementById('workspaceProjectSprintTitle');
        if (wsTitle) wsTitle.textContent = `${sprintName} — ${projName}`;

        const tlTitle = document.getElementById('spectrumTaskListTitle');
        if (tlTitle) tlTitle.textContent = `Daftar Task ${sprintName} (${projName})`;

        const mBadge = document.getElementById('meetingSprintBadge');
        if (mBadge) mBadge.textContent = `${sprintName} (${projName})`;

        const mGoal = document.getElementById('meetingSprintGoal');
        if (mGoal) mGoal.textContent = sprint.description || sprint.target || `Fokus pengerjaan dan pengujian ${projName}.`;

        const tabInput = document.getElementById('targetSheetTabName');
        if (tabInput && (!tabInput.value || tabInput.value.includes('BNI YAP!'))) {
          tabInput.value = `${projName} - ${sprintName}`;
        }

        const tcHeader = document.getElementById('modalSprintTcHeaderTitle');
        if (tcHeader) {
          tcHeader.textContent = `Daftar Skenario Test Case Berjalan — ${sprintName} (${projName})`;
        }

        if (document.getElementById('spectrumSprintTitle')) {
          document.getElementById('spectrumSprintTitle').textContent = `${sprintName} (${projName})`;
        }
        if (document.getElementById('spectrumSprintPeriod') && sprint.start_date && sprint.end_date) {
          document.getElementById('spectrumSprintPeriod').textContent = sprint.start_date + ' s/d ' + sprint.end_date;
        }
        if (document.getElementById('spectrumSprintGoal')) {
          document.getElementById('spectrumSprintGoal').textContent = sprint.description || sprint.target || `Fokus pengerjaan dan pengujian ${projName} pada ${sprintName}.`;
        }

        // Days remaining counter
        if (sprint.end_date) {
          const endDate = new Date(sprint.end_date);
          const today = new Date();
          today.setHours(0,0,0,0);
          const diffMs = endDate - today;
          const diffDays = Math.max(0, Math.ceil(diffMs / (1000*60*60*24)));
          const el = document.getElementById('spectrumDaysRemaining');
          if (el) {
            el.textContent = diffDays + ' hari tersisa';
            if (diffDays <= 3) {
              el.style.color = '#f87171';
              el.style.borderColor = 'rgba(248,113,113,0.3)';
              el.style.background = 'rgba(248,113,113,0.1)';
            } else if (diffDays <= 5) {
              el.style.color = '#fb923c';
              el.style.borderColor = 'rgba(251,146,60,0.3)';
              el.style.background = 'rgba(251,146,60,0.1)';
            }
          }
        }

        const tasks = data.tasks || [];
        const totalTasks = tasks.length || sprint.total_tasks || 0;
        const summary = sprint.summary || {};
        const totalPoints = summary.task_weight || 0;

        const devDoneCount = tasks.filter(t => t.status === 'Done-Development' || t.status === 'Task-Finished').length;
        const inProgressCount = tasks.filter(t => t.status === 'On-Progress').length;
        const myTasks = tasks.filter(t => isMyTask(t));

        if (document.getElementById('metricTotalPoints')) document.getElementById('metricTotalPoints').textContent = totalPoints;
        if (document.getElementById('metricTotalTasks')) document.getElementById('metricTotalTasks').textContent = totalTasks;
        if (document.getElementById('metricDevDone')) document.getElementById('metricDevDone').textContent = devDoneCount;
        if (document.getElementById('metricMyTasks')) document.getElementById('metricMyTasks').textContent = myTasks.length;

        // Progress bar
        const devPct = totalTasks > 0 ? Math.round((devDoneCount / totalTasks) * 100) : 0;
        const progPct = totalTasks > 0 ? Math.round((inProgressCount / totalTasks) * 100) : 0;
        const todoCount = Math.max(0, totalTasks - devDoneCount - inProgressCount);
        if (document.getElementById('spectrumDevDoneBar')) document.getElementById('spectrumDevDoneBar').style.width = devPct + '%';
        if (document.getElementById('spectrumInProgressBar')) document.getElementById('spectrumInProgressBar').style.width = progPct + '%';
        if (document.getElementById('spectrumProgressText')) {
          document.getElementById('spectrumProgressText').textContent = devDoneCount + ' selesai, ' + inProgressCount + ' jalan, ' + todoCount + ' belum';
        }

        // Tab count badges
        if (document.getElementById('countTabAris')) document.getElementById('countTabAris').textContent = '(' + myTasks.length + ')';
        if (document.getElementById('countTabDevDone')) document.getElementById('countTabDevDone').textContent = '(' + devDoneCount + ')';
        if (document.getElementById('countTabProgress')) document.getElementById('countTabProgress').textContent = '(' + inProgressCount + ')';
        if (document.getElementById('countTabAll')) document.getElementById('countTabAll').textContent = '(' + totalTasks + ')';
        if (document.getElementById('spectrumTaskCountBadge')) document.getElementById('spectrumTaskCountBadge').textContent = totalTasks + ' Task';

        // Populate Assignee Select
        const assigneeSelect = document.getElementById('spectrumAssigneeSelect');
        if (assigneeSelect) {
          const assigneeCounts = {};
          tasks.forEach(t => {
            const name = t.assignee || 'Unassigned';
            assigneeCounts[name] = (assigneeCounts[name] || 0) + 1;
          });
          const assignees = Object.keys(assigneeCounts).sort();
          if (assignees.length > 1) {
            assigneeSelect.style.display = 'inline-block';
            let selHtml = `<option value="">Semua Anggota Tim (${tasks.length})</option>`;
            assignees.forEach(name => {
              const isMe = (name.toLowerCase() === (window._myMemberName || '').toLowerCase());
              const label = `${name} (${assigneeCounts[name]} task)${isMe ? ' ⭐ (Anda)' : ''}`;
              selHtml += `<option value="${name}">${label}</option>`;
            });
            assigneeSelect.innerHTML = selHtml;
          } else {
            assigneeSelect.style.display = 'none';
          }
        }

        // Auto select tab: if user has tasks, select 'my', else select 'all'
        if (myTasks.length === 0 && tasks.length > 0) {
          window._spectrumFilter = 'all';
          document.querySelectorAll('.spectrum-tab').forEach(b => b.classList.remove('active'));
          document.getElementById('tabTaskAll')?.classList.add('active');
        } else {
          window._spectrumFilter = 'my';
          document.querySelectorAll('.spectrum-tab').forEach(b => b.classList.remove('active'));
          document.getElementById('tabTaskAris')?.classList.add('active');
        }

        // 3. Calendar Events
        renderSpectrumCalendar(data.calendar_events || []);

        // 4. Tasks Table
        renderSpectrumTasks();

        // 5. Daily Briefing — tampilkan sekali saat pertama kali buka

        // 6. Check OTP inbox untuk suggestion strip
        checkOtpForSuggestion();

      } catch (err) {
        console.error('Gagal memuat data workspace SPEctrum:', err);
      }
    }

    // ==================== WESTPRO MAIL ====================
    const wm = { folder: 'inbox', address: '', selected: null, aliases: [], domain: '', status: null, timer: null, debounce: null, current: null };

    async function wmGet(path) {
      const res = await fetch(path);
      return res.json();
    }
    async function wmPost(path, body) {
      const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      return res.json();
    }

    function wmCopy(text, label) {
      const done = function () { showToast((label || 'Teks') + ' disalin', 'success'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () { showToast('Gagal menyalin', 'error'); });
      } else {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (e) { showToast('Gagal menyalin', 'error'); }
        ta.remove();
      }
    }

    function wmAgo(ts) {
      const d = new Date(String(ts).replace(' ', 'T'));
      const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
      if (sec < 60) return 'baru saja';
      if (sec < 3600) return Math.floor(sec / 60) + ' mnt';
      if (sec < 86400) return Math.floor(sec / 3600) + ' jam';
      return d.toLocaleDateString('id-ID', { day: 'numeric', month: 'short' });
    }

    function wmOnShow() {
      wmLoadStatus();
      wmLoadAddresses();
      wmLoadMessages();
      wmStopPolling();
      wm.timer = setInterval(function () { wmLoadMessages(true); wmLoadAddresses(); }, 6000);
    }
    function wmStopPolling() { if (wm.timer) { clearInterval(wm.timer); wm.timer = null; } }
    function wmDebouncedLoad() { clearTimeout(wm.debounce); wm.debounce = setTimeout(function () { wmLoadMessages(); }, 300); }

    async function wmLoadStatus() {
      try {
        const d = await wmGet('/api/webmail/status');
        if (!d.success) return;
        wm.status = d; wm.domain = d.domain;
        const badge = document.getElementById('wmDomainBadge'); if (badge) badge.textContent = '@' + d.domain;
        const ban = document.getElementById('wmSetupBanner');
        if (ban) {
          if (d.owner && !d.inbound_configured) {
            ban.style.display = 'block';
            ban.innerHTML = '⚠️ <b>Penerimaan email belum aktif.</b> Isi <code>MAIL_INBOUND_SECRET</code> di <code>.env</code> lalu pasang Cloudflare Email Worker (panduan: <code>cloudflare/email-worker/README.md</code>). Webhook: <code>' + escapeHtml(d.webhook_url) + '</code>';
          } else { ban.style.display = 'none'; }
        }
      } catch (e) {}
    }

    async function wmLoadAddresses() {
      try {
        const d = await wmGet('/api/webmail/addresses');
        if (!d.success) return;
        wm.aliases = d.addresses || []; wm.domain = d.domain;
        const box = document.getElementById('wmAddresses'); if (!box) return;
        const totalUnread = wm.aliases.reduce(function (a, x) { return a + (x.unread || 0); }, 0);
        const iu = document.getElementById('wmInboxUnread'); if (iu) iu.innerHTML = totalUnread ? '<span class="wm-badge">' + totalUnread + '</span>' : '';
        let html = '<div class="wm-addr' + (wm.address === '' ? ' active' : '') + '" onclick="wmSetAddress(' + "''" + ')"><span class="wm-a-name" style="font-family:inherit;">' + (IS_ARES_OWNER ? 'Semua alamat (pemilik)' : 'Semua alamat saya') + '</span></div>';
        html += wm.aliases.map(function (a) {
          const act = wm.address === a.address ? ' active' : '';
          const own = (IS_ARES_OWNER && a.owner_name) ? '<span style="color:#71717a; font-size:0.62rem;">' + escapeHtml(a.owner_name) + '</span>' : '';
          return '<div class="wm-addr' + act + '" data-addr="' + escapeHtml(a.address) + '" onclick="wmSetAddress(this.dataset.addr)" title="' + escapeHtml(a.address) + '">' +
            '<span class="wm-a-name">' + escapeHtml(a.address.split('@')[0]) + '</span>' + own +
            (a.unread ? '<span class="wm-badge">' + a.unread + '</span>' : '') +
            '<button class="wm-mini" title="Salin alamat" data-addr="' + escapeHtml(a.address) + '" onclick="event.stopPropagation(); wmCopy(this.dataset.addr, ' + "'Alamat'" + ')">⧉</button>' +
            '<button class="wm-mini" title="Hapus alamat" data-addr="' + escapeHtml(a.address) + '" onclick="event.stopPropagation(); wmDeleteAlias(this.dataset.addr)">✕</button></div>';
        }).join('');
        if (!wm.aliases.length) html += '<div style="font-size:0.72rem; color:#71717a; padding:0.3rem 0.5rem;">Belum ada alamat. Klik <b>+ Buat</b> di bawah.</div>';
        box.innerHTML = html;
      } catch (e) {}
    }

    function wmSetFolder(f) {
      wm.folder = f; wm.selected = null; wm.address = '';
      document.getElementById('wmFolderInbox').classList.toggle('active', f === 'inbox');
      document.getElementById('wmFolderSent').classList.toggle('active', f === 'sent');
      document.getElementById('wmReader').innerHTML = '<div class="wm-empty">Pilih email untuk dibaca</div>';
      wmLoadAddresses(); wmLoadMessages();
    }
    function wmSetAddress(a) { wm.address = a || ''; wm.selected = null; wmLoadAddresses(); wmLoadMessages(); }

    async function wmCreateAlias() {
      const inp = document.getElementById('wmNewLocal');
      const d = await wmPost('/api/webmail/addresses', { local: inp.value.trim().toLowerCase() });
      if (d.success) { inp.value = ''; showToast('Alamat dibuat: ' + d.address, 'success'); wm.address = d.address; wmCopy(d.address, 'Alamat'); wmLoadAddresses(); wmLoadMessages(); }
      else showToast(d.error || 'Gagal membuat alamat', 'error');
    }
    async function wmDeleteAlias(addr) {
      if (!confirm('Hapus alamat ' + addr + '? Email lama tetap tersimpan sampai kedaluwarsa tetapi tidak bisa Anda lihat lagi.')) return;
      await wmPost('/api/webmail/addresses/delete', { address: addr });
      if (wm.address === addr) wm.address = '';
      wmLoadAddresses(); wmLoadMessages();
    }

    async function wmLoadMessages(silent) {
      const list = document.getElementById('wmList'); if (!list) return;
      const q = (document.getElementById('wmSearch')?.value || '').trim();
      const unread = document.getElementById('wmUnreadOnly')?.checked ? '1' : '0';
      const url = '/api/webmail/messages?folder=' + wm.folder + '&address=' + encodeURIComponent(wm.address) + '&q=' + encodeURIComponent(q) + '&unread=' + unread + '&limit=100';
      try {
        const d = await wmGet(url);
        if (!d.success) return;
        if (!d.messages.length) {
          list.innerHTML = '<div class="wm-empty">' + (wm.folder === 'sent' ? 'Belum ada email terkirim.' : (q || unread === '1' ? 'Tidak ada email yang cocok.' : 'Belum ada email masuk.<br><span style="font-size:0.72rem;">Kirim email ke alamat @' + escapeHtml(wm.domain) + ' lalu muncul di sini.</span>')) + '</div>';
          return;
        }
        list.innerHTML = d.messages.map(function (m) {
          const who = wm.folder === 'sent' ? ('Ke: ' + m.to) : String(m.from || '').replace(/<.*>/, '').trim() || m.from;
          const otp = (m.otp && m.otp.length) ? '<span class="wm-chip" style="margin:0 0 0 0.3rem; cursor:default;">' + escapeHtml(m.otp[0]) + '</span>' : '';
          return '<div class="wm-item' + (m.is_read ? '' : ' unread') + (wm.selected === m.id ? ' active' : '') + '" data-id="' + m.id + '" onclick="wmOpen(Number(this.dataset.id))">' +
            '<div class="wm-from"><span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + escapeHtml(who) + '</span><span style="color:#71717a; font-size:0.68rem; flex-shrink:0;">' + wmAgo(m.received_at) + '</span></div>' +
            '<div class="wm-subj">' + escapeHtml(m.subject) + otp + '</div>' +
            (wm.folder === 'inbox' ? '<div class="wm-prev">ke ' + escapeHtml(m.to) + ' — ' + escapeHtml(m.preview) + '</div>' : '<div class="wm-prev">' + escapeHtml(m.preview) + '</div>') + '</div>';
        }).join('');
      } catch (e) { if (!silent) list.innerHTML = '<div class="wm-empty">Gagal memuat: ' + escapeHtml(e.message) + '</div>'; }
    }

    async function wmOpen(id) {
      wm.selected = id;
      const reader = document.getElementById('wmReader');
      try {
        const d = await wmGet('/api/webmail/message?id=' + id);
        if (!d.success) { reader.innerHTML = '<div class="wm-empty">Email tidak ditemukan</div>'; return; }
        const m = d.message; wm.current = m;

        // Banner OTP menonjol bila ada kode terdeteksi
        let otpBanner = '';
        if (m.otp && m.otp.length) {
          otpBanner = '<div style="background:linear-gradient(135deg, rgba(16,185,129,0.18), rgba(56,189,248,0.1)); border:1px solid rgba(16,185,129,0.4); border-radius:10px; padding:0.85rem 1.1rem; margin-bottom:1rem; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.6rem;">' +
            '<div>' +
              '<div style="font-size:0.68rem; text-transform:uppercase; font-weight:800; color:#34d399; letter-spacing:0.06em;">🔑 KODE VERIFIKASI / OTP TERDETEKSI</div>' +
              '<div style="font-family:monospace; font-size:1.45rem; font-weight:800; color:#fff; letter-spacing:0.12em; margin-top:0.2rem;">' + escapeHtml(m.otp[0]) + '</div>' +
            '</div>' +
            '<button class="hb-btn hb-btn-primary" style="padding:0.4rem 0.9rem; font-size:0.8rem; font-weight:700;" data-otp="' + escapeHtml(m.otp[0]) + '" onclick="wmCopy(this.dataset.otp, ' + "'Kode OTP'" + ')">📋 Salin OTP</button>' +
          '</div>';
        }

        // Tautan dirapikan dalam collapsible accordion agar tidak memenuhi layar
        let linksBox = '';
        if (m.links && m.links.length) {
          const linksHtml = m.links.map(function (l) {
            return '<div class="wm-link' + (l.verify ? ' verify' : '') + '"><button class="wm-mini" data-u="' + escapeHtml(l.url) + '" onclick="wmCopy(this.dataset.u, ' + "'Tautan'" + ')" title="Salin">⧉</button><a href="' + escapeHtml(l.url) + '" target="_blank" rel="noopener noreferrer nofollow">' + escapeHtml(l.url.length > 95 ? l.url.substring(0, 95) + '…' : l.url) + '</a></div>';
          }).join('');
          linksBox = '<details style="background:#14151a; border:1px solid #23242c; border-radius:9px; padding:0.5rem 0.85rem; margin-bottom:1rem;">' +
            '<summary style="font-size:0.75rem; color:#94a3b8; font-weight:600; cursor:pointer; user-select:none;">🔗 ' + m.links.length + ' Tautan Terdeteksi di Email (klik untuk buka)</summary>' +
            '<div style="margin-top:0.6rem; max-height:170px; overflow-y:auto;">' + linksHtml + '</div>' +
          '</details>';
        }

        const att = (m.attachments || []).map(function (a) { return '<span style="font-size:0.72rem; background:#1c1d24; border:1px solid #2d2e38; border-radius:6px; padding:0.15rem 0.5rem; margin-right:0.35rem; display:inline-block; margin-bottom:0.35rem;">📎 ' + escapeHtml(a.filename || a.type) + ' (' + Math.round(a.size / 1024) + ' KB)</span>'; }).join('');

        // Extract clean sender name
        const senderMatch = String(m.from || '').match(/^([^<]+)/);
        const senderName = senderMatch ? senderMatch[1].trim().replace(/^["']|["']$/g, '') : m.from;

        reader.innerHTML =
          '<div class="wm-read-head">' +
            '<div style="font-size:1.1rem; font-weight:700; color:#f8fafc; margin-bottom:0.55rem; word-break:break-word; line-height:1.4;">' + escapeHtml(m.subject) + '</div>' +
            '<div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:0.6rem;">' +
              '<div style="font-size:0.76rem; color:#94a3b8; line-height:1.55;">' +
                'Dari: <b style="color:#f1f5f9; font-size:0.82rem;">' + escapeHtml(senderName) + '</b> <code style="color:#93c5fd; background:rgba(59,130,246,0.1); padding:0.1rem 0.35rem; border-radius:4px;">' + escapeHtml(m.from || '') + '</code><br>' +
                'Ke: <span style="color:#e2e8f0;">' + escapeHtml(m.to) + '</span> &bull; <span style="color:#64748b;">' + escapeHtml(m.received_at) + '</span>' +
              '</div>' +
              '<div style="display:flex; gap:0.4rem; flex-wrap:wrap; align-items:center;">' +
                (m.folder === 'inbox' ? '<button class="hb-btn hb-btn-primary" style="padding:0.35rem 0.85rem; font-weight:700;" onclick="wmReply()">↩ Balas</button>' : '') +
                '<button class="hb-btn" onclick="wmMarkUnread()">Tandai belum dibaca</button>' +
                '<button class="hb-btn" onclick="wmDeleteCurrent()" style="color:#f87171; border-color:rgba(239,68,68,0.3);">🗑 Hapus</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
          '<div class="wm-read-body">' +
            otpBanner +
            linksBox +
            (att ? '<div style="margin-bottom:0.8rem;">' + att + '</div>' : '') +
            '<div id="wmBodyHolder"></div>' +
          '</div>';

        const holder = document.getElementById('wmBodyHolder');
        if (m.html) {
          // Render HTML di sandbox iframe dengan CSP yang mengizinkan HTTPS images & style agar logo dan visual tampil 100% sempurna
          const fr = document.createElement('iframe');
          fr.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox allow-same-origin');
          fr.setAttribute('referrerpolicy', 'no-referrer');
          fr.style.cssText = 'width:100%; min-height:480px; border:1px solid #23242c; border-radius:10px; background:#ffffff; display:block; transition:height 0.2s;';
          fr.srcdoc = '<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; img-src data: https: http: cid: blob:; style-src &apos;unsafe-inline&apos; https: http:; font-src https: data:;"><base target="_blank"><style>body{margin:0;padding:16px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#1e293b;line-height:1.55;word-wrap:break-word}img{max-width:100%;height:auto}</style></head><body>' + m.html + '</body></html>';
          fr.onload = function () {
            try {
              const h = fr.contentWindow.document.body.scrollHeight;
              if (h > 150) fr.style.height = (h + 35) + 'px';
            } catch (e) {}
          };
          holder.appendChild(fr);
        } else {
          const pre = document.createElement('pre');
          pre.style.cssText = 'white-space:pre-wrap; word-break:break-word; font-family:inherit; margin:0; background:#14151a; border:1px solid #23242c; border-radius:10px; padding:1.2rem; color:#e2e8f0; font-size:0.86rem; line-height:1.65;';
          pre.textContent = m.text || '(kosong)';
          holder.appendChild(pre);
        }
        wmLoadMessages(true); wmLoadAddresses();
      } catch (e) { reader.innerHTML = '<div class="wm-empty">Gagal membuka: ' + escapeHtml(e.message) + '</div>'; }
    }

    async function wmDeleteCurrent() {
      if (!wm.current || !confirm('Hapus email ini?')) return;
      await wmPost('/api/webmail/delete', { ids: [wm.current.id] });
      wm.selected = null; wm.current = null;
      document.getElementById('wmReader').innerHTML = '<div class="wm-empty">Email dihapus</div>';
      wmLoadMessages(); wmLoadAddresses();
    }
    async function wmMarkUnread() {
      if (!wm.current) return;
      await wmPost('/api/webmail/read', { ids: [wm.current.id], read: false });
      showToast('Ditandai belum dibaca', 'success'); wmLoadMessages(); wmLoadAddresses();
    }

    function wmFillFrom(selected) {
      const sel = document.getElementById('wmFrom'); if (!sel) return;
      sel.innerHTML = (wm.aliases.length ? wm.aliases.map(function (a) { return '<option value="' + escapeHtml(a.address) + '">' + escapeHtml(a.address) + '</option>'; }).join('') : '<option value="">(buat alamat dulu)</option>');
      if (selected) sel.value = selected;
    }
    function wmOpenCompose(prefill) {
      wmFillFrom(prefill && prefill.from);
      document.getElementById('wmTo').value = (prefill && prefill.to) || '';
      document.getElementById('wmSubject').value = (prefill && prefill.subject) || '';
      document.getElementById('wmBody').value = (prefill && prefill.body) || '';
      const msg = document.getElementById('wmComposeMsg');
      msg.style.color = '#94a3b8';
      msg.textContent = 'Email dikirim via Resend SMTP resmi domain @' + (wm.domain || 'westpro.my.id');
      document.getElementById('wmCompose').style.display = 'flex';
      setTimeout(function () { (prefill && prefill.body ? document.getElementById('wmBody') : document.getElementById('wmTo')).focus(); }, 100);
    }
    function wmCloseCompose() { document.getElementById('wmCompose').style.display = 'none'; }

    function wmReply() {
      const m = wm.current; if (!m) return;
      // Ambil email pengirim asli secara akurat dari header From
      const fromMatch = String(m.from || '').match(/<([^>]+)>/) || String(m.from || '').match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/);
      const replyTo = fromMatch ? (fromMatch[1] || fromMatch[0]) : '';

      // Tentukan alias mana di akun ini yang menerima email tersebut
      const toMatch = String(m.to || '').match(/<([^>]+)>/) || String(m.to || '').match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/);
      const rawTo = (toMatch ? (toMatch[1] || toMatch[0]) : m.to).trim().toLowerCase();
      const baseTo = rawTo.split('+')[0].replace(/@.*/, '') + '@' + (wm.domain || 'westpro.my.id');

      let fromAlias = '';
      if (wm.aliases && wm.aliases.length) {
        const foundExact = wm.aliases.find(function (a) { return a.address.toLowerCase() === rawTo; });
        const foundBase = wm.aliases.find(function (a) { return a.address.toLowerCase() === baseTo; });
        fromAlias = (foundExact || foundBase || wm.aliases[0]).address;
      }

      const nl = String.fromCharCode(10);
      const quoted = m.text ? (nl + nl + '> ' + String(m.text).trim().split(nl).slice(0, 15).join(nl + '> ')) : '';
      wmOpenCompose({
        from: fromAlias,
        to: replyTo,
        subject: /^re:/i.test(m.subject) ? m.subject : 'Re: ' + m.subject,
        body: quoted
      });
    }

    async function wmSend() {
      const btn = document.getElementById('wmSendBtn');
      const msg = document.getElementById('wmComposeMsg');
      const fromAlias = document.getElementById('wmFrom').value;
      const to = document.getElementById('wmTo').value.trim();
      const subject = document.getElementById('wmSubject').value.trim();
      const text = document.getElementById('wmBody').value;

      if (!to || !subject || !text.trim()) {
        msg.style.color = '#f87171';
        msg.textContent = 'Harap isi penerima (email valid), subjek, dan pesan.';
        return;
      }

      btn.disabled = true;
      btn.textContent = 'Mengirim...';
      try {
        const d = await wmPost('/api/webmail/send', { from_alias: fromAlias, to: to, subject: subject, text: text });
        if (d.success) {
          showToast('Email berhasil dikirim ke ' + to, 'success');
          wmCloseCompose();
          if (wm.folder === 'sent') wmLoadMessages();
        } else {
          msg.style.color = '#f87171';
          msg.textContent = d.error || 'Gagal mengirim email';
        }
      } catch (e) {
        msg.style.color = '#f87171';
        msg.textContent = 'Gagal: ' + e.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Kirim';
      }
    }

    async function wmToggleApi() {
      const box = document.getElementById('wmApiBox');
      if (box.style.display === 'block') { box.style.display = 'none'; return; }
      await wmLoadStatus();
      const st = wm.status || {};
      const addr = (wm.aliases[0] && wm.aliases[0].address) || ('qa-contoh@' + wm.domain);
      box.innerHTML = '<div style="font-weight:700; color:#f4f4f5; margin-bottom:0.4rem;">🔑 API untuk otomasi tes (Appium, Playwright, Postman, dll.)</div>' +
        '<div style="color:#94a3b8; margin-bottom:0.5rem;">Daftarkan akun uji dengan alamat <code>nama@' + escapeHtml(wm.domain) + '</code>, lalu skrip menunggu email verifikasi dan mengambil kodenya (long-poll sampai 60 detik).</div>' +
        '<div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; margin-bottom:0.5rem;"><button class="hb-btn hb-btn-primary" onclick="wmMakeKey()">' + (st.has_api_key ? 'Buat ulang kunci API' : 'Buat kunci API') + '</button>' +
        (st.has_api_key ? '<button class="hb-btn" onclick="wmRevokeKey()">Cabut kunci</button>' : '') + '<span id="wmKeyOut" style="font-family:monospace; color:#a3e635; word-break:break-all;"></span></div>' +
        '<pre id="wmCurl" style="background:#0b0c0e; border:1px solid #27272a; border-radius:8px; padding:0.6rem 0.8rem; margin:0; overflow:auto; font-size:0.72rem; color:#a3e635;"></pre>';
      document.getElementById('wmCurl').textContent = wmCurlExample('wpm_KUNCI_ANDA', addr);
      box.style.display = 'block';
    }
    function wmCurlExample(key, addr) {
      const nl = String.fromCharCode(10);
      return '# tunggu email baru ke alamat uji (maks 60 dtk), ambil kode OTP & tautan verifikasi' + nl +
        'curl -s -H "X-API-Key: ' + key + '" ' + String.fromCharCode(92) + nl + '  "' + location.origin + '/api/webmail/wait?address=' + addr + '&timeout=45&subject_contains=verifikasi"' + nl + nl +
        '# daftar email: GET /api/webmail/messages?address=' + addr + nl +
        '# respons wait: {"success":true,"message":{"otp":["482913"],"links":[{"url":"...","verify":true}],...}}';
    }
    async function wmMakeKey() {
      if (wm.status && wm.status.has_api_key && !confirm('Kunci lama akan berhenti berfungsi. Lanjut?')) return;
      const d = await wmPost('/api/webmail/apikey', {});
      if (d.success) {
        document.getElementById('wmKeyOut').textContent = d.key;
        const addr = (wm.aliases[0] && wm.aliases[0].address) || ('qa-contoh@' + wm.domain);
        document.getElementById('wmCurl').textContent = wmCurlExample(d.key, addr);
        wmCopy(d.key, 'Kunci API'); wm.status.has_api_key = true;
      }
    }
    async function wmRevokeKey() { await wmPost('/api/webmail/apikey', { revoke: true }); showToast('Kunci API dicabut', 'success'); document.getElementById('wmApiBox').style.display = 'none'; wmLoadStatus(); }

    // ==================== MICROSOFT TEAMS & AI DIGEST JAVASCRIPT ====================
    window._teamsScreenPolling = null;
    window._teamsStatusPolling = null;
    window._teamsIsBrowserOpen = false;

    function teamsOnShow() {
      if (!IS_ARES_OWNER) return;
      teamsCheckStatus();
      teamsLoadDigests();
      teamsLoadLatestDigest();
      teamsLoadConfig();
      teamsLoadRawChats();
      if (!window._teamsStatusPolling) {
        window._teamsStatusPolling = setInterval(teamsCheckStatus, 8000);
      }
    }

    function teamsStopPolling() {
      if (window._teamsScreenPolling) {
        clearInterval(window._teamsScreenPolling);
        window._teamsScreenPolling = null;
      }
      if (window._teamsStatusPolling) {
        clearInterval(window._teamsStatusPolling);
        window._teamsStatusPolling = null;
      }
    }

    async function teamsCheckStatus() {
      if (!IS_ARES_OWNER) return;
      try {
        const res = await fetch('/api/teams/status');
        if (!res.ok) return;
        const data = await res.json();
        const badge = document.getElementById('teamsStatusBadge');
        const modeBadge = document.getElementById('teamsModeBadge');
        const btnMode = document.getElementById('btnTeamsSwitchMode');
        const navBadge = document.getElementById('navTeamsBadge');
        const pageInfo = document.getElementById('teamsPageInfo');

        window._teamsCurrentHeaded = !!data.headed;

        if (modeBadge) {
          if (data.headed) {
            modeBadge.className = 'hermes-badge badge-warn';
            modeBadge.textContent = '🖥️ MODE: HEADED (LAYAR DESKTOP)';
          } else {
            modeBadge.className = 'hermes-badge badge-ok';
            modeBadge.textContent = '👻 MODE: HEADLESS (HEMAT RESOURCE)';
          }
        }

        if (btnMode) {
          if (data.headed) {
            btnMode.innerHTML = '👻 Kembalikan ke Headless';
            btnMode.style.background = 'rgba(239,68,68,0.2)';
            btnMode.style.color = '#fca5a5';
            btnMode.style.border = '1px solid rgba(239,68,68,0.4)';
            btnMode.title = 'Chromium saat ini tampil di layar desktop server. Klik untuk kembali ke mode Headless (latar belakang) untuk menghemat resource.';
          } else {
            btnMode.innerHTML = '🖥️ Buka di Layar Server (Headed)';
            btnMode.style.background = 'rgba(168,85,247,0.2)';
            btnMode.style.color = '#c084fc';
            btnMode.style.border = '1px solid rgba(168,85,247,0.35)';
            btnMode.title = 'Buka jendela browser Chromium di layar desktop Kali Linux (Headed Mode) agar bisa melihat atau login fisik.';
          }
        }

        if (pageInfo) {
          pageInfo.textContent = (data.title || '') + (data.url ? ' (' + data.url.split('?')[0] + ')' : '');
          pageInfo.title = data.url || '';
        }

        if (badge) {
          if (data.logged_in) {
            badge.className = 'hermes-badge badge-ok';
            badge.textContent = '🟢 SESI TEAMS AKTIF';
            if (navBadge) { navBadge.style.display = 'inline-block'; navBadge.style.background = '#10b981'; navBadge.textContent = 'Aktif'; }
          } else if (data.status === 'needs_mfa') {
            badge.className = 'hermes-badge badge-warn';
            badge.textContent = '🟡 CEK HP: SETUJUI MFA AUTHENTICATOR';
            if (navBadge) { navBadge.style.display = 'inline-block'; navBadge.style.background = '#f59e0b'; navBadge.textContent = 'MFA'; }
            if (!window._teamsIsBrowserOpen) teamsToggleBrowser(true);
          } else if (data.status === 'needs_login') {
            badge.className = 'hermes-badge badge-warn';
            badge.textContent = '🟡 PERLU LOGIN AKUN MICROSOFT';
            if (navBadge) { navBadge.style.display = 'inline-block'; navBadge.style.background = '#f59e0b'; navBadge.textContent = 'Login'; }
            if (!window._teamsIsBrowserOpen) teamsToggleBrowser(true);
          } else if (data.running) {
            badge.className = 'hermes-badge badge-blue';
            badge.textContent = '🔵 ' + (data.message || 'RUNNING');
          } else {
            badge.className = 'hermes-badge';
            badge.style.background = '#3f3f46';
            badge.textContent = '⚪ OFFLINE';
            if (navBadge) navBadge.style.display = 'none';
          }
        }
      } catch (e) {
        console.error('Teams status check error:', e);
      }
    }

    function teamsToggleBrowser(forceOpen) {
      const box = document.getElementById('teamsBrowserContainer');
      const btn = document.getElementById('btnTeamsToggleBrowser');
      if (!box) return;

      if (forceOpen === true) {
        window._teamsIsBrowserOpen = true;
      } else {
        window._teamsIsBrowserOpen = (box.style.display === 'none' || !box.style.display);
      }

      box.style.display = window._teamsIsBrowserOpen ? 'block' : 'none';
      if (btn) btn.textContent = window._teamsIsBrowserOpen ? '✕ Tutup Layar Browser' : '🖥️ Buka Remote Browser (Login/Layar)';

      if (window._teamsIsBrowserOpen) {
        teamsRefreshScreen(true);
        if (!window._teamsScreenPolling) {
          window._teamsScreenPolling = setInterval(function () { teamsRefreshScreen(false); }, 1800);
        }
      } else {
        if (window._teamsScreenPolling) {
          clearInterval(window._teamsScreenPolling);
          window._teamsScreenPolling = null;
        }
      }
    }

    function teamsRefreshScreen(force) {
      if (!window._teamsIsBrowserOpen && !force) return;
      const img = document.getElementById('teamsScreenImg');
      const loading = document.getElementById('teamsScreenLoading');
      if (!img) return;

      const newImg = new Image();
      newImg.onload = function () {
        img.src = newImg.src;
        img.style.display = 'block';
        if (loading) loading.style.display = 'none';
      };
      newImg.onerror = function () {
        if (loading) {
          loading.style.display = 'flex';
          loading.innerHTML = '<div>⚠️ Gagal mengambil layar Teams. Pastikan server aktif.</div>';
        }
      };
      newImg.src = '/api/teams/screen?t=' + Date.now();
    }

    async function teamsClickScreen(e) {
      const img = document.getElementById('teamsScreenImg');
      const ripple = document.getElementById('teamsTapRipple');
      if (!img) return;

      const rect = img.getBoundingClientRect();
      const clickX = e.clientX - rect.left;
      const clickY = e.clientY - rect.top;

      // Real natural dimensions from the loaded image
      const natW = img.naturalWidth || 1280;
      const natH = img.naturalHeight || 661;

      // Letterbox calculation
      const imgAspect = natW / natH;
      const elemAspect = rect.width / rect.height;

      let renderedW = rect.width;
      let renderedH = rect.height;
      let offsetX = 0;
      let offsetY = 0;

      if (elemAspect > imgAspect) {
        renderedW = rect.height * imgAspect;
        offsetX = (rect.width - renderedW) / 2;
      } else {
        renderedH = rect.width / imgAspect;
        offsetY = (rect.height - renderedH) / 2;
      }

      const relX = clickX - offsetX;
      const relY = clickY - offsetY;

      if (relX < 0 || relX > renderedW || relY < 0 || relY > renderedH) return;

      const devX = Math.round((relX / renderedW) * natW);
      const devY = Math.round((relY / renderedH) * natH);

      if (ripple) {
        ripple.style.left = clickX + 'px';
        ripple.style.top = clickY + 'px';
        ripple.style.transform = 'translate(-50%, -50%) scale(1.5)';
        ripple.style.opacity = '0.9';
        setTimeout(function () {
          ripple.style.transform = 'translate(-50%, -50%) scale(0)';
          ripple.style.opacity = '0';
        }, 220);
      }

      try {
        await fetch('/api/teams/click', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ x: devX, y: devY })
        });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 400);
      } catch (err) {
        console.error('Teams click error:', err);
      }
    }

    async function teamsSendText() {
      const inp = document.getElementById('teamsTextInput');
      if (!inp || !inp.value) return;
      const val = inp.value;
      inp.value = '';

      try {
        await fetch('/api/teams/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: val })
        });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 500);
      } catch (err) {
        console.error('Teams send text error:', err);
      }
    }

    async function teamsSendKey(keyName) {
      try {
        await fetch('/api/teams/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: keyName })
        });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 400);
      } catch (err) {
        console.error('Teams key error:', err);
      }
    }

    async function teamsNavigateUrl(url) {
      try {
        showToast('Membuka ' + url + '...', 'info');
        await fetch('/api/teams/navigate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: url })
        });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 1500);
      } catch (err) {
        console.error('Teams navigate error:', err);
      }
    }

    async function teamsReloadBrowser() {
      try {
        showToast('Memuat ulang peramban Teams...', 'info');
        await fetch('/api/teams/reload', { method: 'POST' });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 1500);
      } catch (err) {
        console.error('Teams reload error:', err);
      }
    }

    async function teamsRunAnalysis() {
      const btn = document.getElementById('btnTeamsAnalyze');
      const box = document.getElementById('teamsDigestContainer');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span style="animation:spin 1s infinite linear; display:inline-block;">⚡</span> Membaca &amp; Menganalisis obrolan...';
      }
      showToast('Memulai pembacaan obrolan &amp; analisis AI multi-model (auto-failover aktif)...', 'info');

      try {
        const res = await fetch('/api/teams/analyze', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success' && data.html) {
          if (box) box.innerHTML = data.html;
          const meta = document.getElementById('teamsDigestMeta');
          if (meta) meta.textContent = 'Analisis selesai pada ' + new Date().toLocaleTimeString('id-ID') + ' (' + (data.raw_count || 0) + ' pesan/obrolan)';
          const usedModel = data.ai_model_used ? (' (' + data.ai_model_used + ')') : '';
          showToast('✅ Analisis harian Teams selesai' + usedModel + '!', 'success');
          teamsLoadDigests();
        } else {
          showToast(data.message || 'Gagal menjalankan analisis', 'error');
          if (box) {
            box.innerHTML = '<div style="color:#ef4444; padding:1.5rem; text-align:center;"><b>Gagal:</b> ' + escapeHtml(data.message || 'Terjadi kendala saat membaca obrolan Teams') + '</div>';
          }
        }
      } catch (err) {
        console.error('Analysis error:', err);
        showToast('Gagal menghubungi server untuk analisis', 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '⚡ Jalankan Analisa AI Sekarang';
        }
      }
    }

    async function teamsLoadLatestDigest() {
      try {
        const res = await fetch('/api/teams/digest/latest');
        if (!res.ok) return;
        const data = await res.json();
        const d = data.digest;
        if (d && d.briefing_html) {
          const box = document.getElementById('teamsDigestContainer');
          if (box) box.innerHTML = d.briefing_html;
          const meta = document.getElementById('teamsDigestMeta');
          if (meta) meta.textContent = 'Tanggal: ' + (d.date_str || '-') + ' • Dibuat: ' + (d.created_at || '') + ' (' + (d.raw_messages_count || 0) + ' pesan terbaca)';
        }
      } catch (err) {
        console.error('Error loading latest digest:', err);
      }
    }

    async function teamsLoadDigests() {
      try {
        const res = await fetch('/api/teams/digests');
        if (!res.ok) return;
        const data = await res.json();
        const sel = document.getElementById('teamsHistorySelect');
        if (!sel || !data.items) return;

        let opts = '<option value="latest">Analisis Terkini</option>';
        data.items.forEach(function (it) {
          opts += '<option value="' + it.id + '">' + (it.date_str || 'Hari ini') + ' (' + (it.created_at ? it.created_at.slice(11, 16) : '') + ') - ' + it.raw_messages_count + ' pesan</option>';
        });
        sel.innerHTML = opts;
      } catch (err) {
        console.error('Error loading digest list:', err);
      }
    }

    async function teamsSelectHistoryDigest(val) {
      if (val === 'latest') {
        teamsLoadLatestDigest();
        return;
      }
      try {
        const res = await fetch('/api/teams/digest?id=' + encodeURIComponent(val));
        if (!res.ok) return;
        const data = await res.json();
        const d = data.digest;
        if (d && d.briefing_html) {
          const box = document.getElementById('teamsDigestContainer');
          if (box) box.innerHTML = d.briefing_html;
          const meta = document.getElementById('teamsDigestMeta');
          if (meta) meta.textContent = 'Arsip ' + (d.date_str || '-') + ' (' + (d.created_at || '') + ') • ' + (d.raw_messages_count || 0) + ' pesan';
        }
      } catch (err) {
        console.error('Error loading specific digest:', err);
      }
    }

    async function teamsResetToLogin() {
      if (confirm('Kembali ke halaman awal login Microsoft Teams?')) {
        await teamsNavigateUrl('https://teams.microsoft.com');
      }
    }

    async function teamsClickNext() {
      try {
        showToast('Menekan tombol Next / Submit...', 'info');
        await fetch('/api/teams/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'Submit' })
        });
        setTimeout(function () { teamsRefreshScreen(true); teamsCheckStatus(); }, 500);
      } catch (e) {
        console.error('teamsClickNext error:', e);
      }
    }

    async function teamsFocusForm() {
      try {
        await fetch('/api/teams/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'Tab' })
        });
        setTimeout(function () { teamsRefreshScreen(true); }, 300);
      } catch (e) {}
    }

    function teamsUpdateModelBadge(val) {
      const b = document.getElementById('teamsAiModelBadge');
      if (!b) return;
      if (val === 'auto_rotate') b.textContent = '🤖 MODEL: ROTASI OTOMATIS';
      else if (val.includes('groq') || val.includes('llama')) b.textContent = '⚡ MODEL: GROQ LLAMA 3.3 (SUPER CEPAT)';
      else if (val.includes('deepseek')) b.textContent = '🤖 MODEL: DEEPSEEK V4 (SUMOPOD)';
      else if (val.includes('gpt-4o')) b.textContent = '🤖 MODEL: GPT-4o (COPILOT)';
      else if (val.includes('claude')) b.textContent = '🤖 MODEL: CLAUDE 3.5 SONNET';
      else if (val.includes('2.5-flash')) b.textContent = '🤖 MODEL: GEMINI 2.5 FLASH';
      else if (val.includes('1.5-flash')) b.textContent = '🤖 MODEL: GEMINI 1.5 FLASH';
      else if (val.includes('3.8-flash')) b.textContent = '🤖 MODEL: GEMINI 3.8 FLASH';
      else if (val.includes('1.5-pro')) b.textContent = '🤖 MODEL: GEMINI 1.5 PRO';
      else b.textContent = '🤖 MODEL: ' + (val || '').toUpperCase();
    }

    async function teamsLoadConfig() {
      try {
        const res = await fetch('/api/teams/config');
        if (!res.ok) return;
        const cfg = await res.json();
        const sprintInp = document.getElementById('cfgTeamsSprintName');
        const chanInp = document.getElementById('cfgTeamsChannels');
        const chatInp = document.getElementById('cfgTeamsChats');
        const aiModelInp = document.getElementById('cfgTeamsAiModel');
        if (sprintInp && cfg.sprint_name) sprintInp.value = cfg.sprint_name;
        if (chanInp && cfg.target_channels) chanInp.value = cfg.target_channels;
        if (chatInp && cfg.target_chats) chatInp.value = cfg.target_chats;
        if (aiModelInp && cfg.ai_model) {
          aiModelInp.value = cfg.ai_model;
          teamsUpdateModelBadge(cfg.ai_model);
        }
      } catch (e) {
        console.error('Error loading teams config:', e);
      }
    }

    async function teamsSaveConfig() {
      const sprintName = (document.getElementById('cfgTeamsSprintName')?.value || '').trim();
      const channels = (document.getElementById('cfgTeamsChannels')?.value || '').trim();
      const chats = (document.getElementById('cfgTeamsChats')?.value || '').trim();
      const aiModel = document.getElementById('cfgTeamsAiModel')?.value || 'auto_rotate';

      try {
        const res = await fetch('/api/teams/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sprint_name: sprintName,
            target_channels: channels,
            target_chats: chats,
            sprint_members: chats,
            ai_model: aiModel,
            sprint_focus_only: true
          })
        });
        const d = await res.json();
        if (d.success) {
          teamsUpdateModelBadge(aiModel);
          showToast('Target obrolan sprint & strategi AI berhasil disimpan!', 'success');
        } else {
          showToast('Gagal menyimpan target', 'error');
        }
      } catch (e) {
        console.error('Error saving config:', e);
        showToast('Gagal menghubungi server', 'error');
      }
    }

    function teamsSyncSprintContext() {
      const spec = window._spectrumData;
      if (!spec) {
        showToast('Data SPEctrum belum tersedia. Silakan buka tab SPEctrum terlebih dahulu.', 'info');
        return;
      }
      const sprint = spec.active_sprint || {};
      const sprintName = sprint.name || '';
      if (sprintName) {
        const sprintInp = document.getElementById('cfgTeamsSprintName');
        if (sprintInp) sprintInp.value = sprintName;
      }

      // Ambil seluruh rekan yang memiliki task di sprint berjalan ini (exclude user Aris)
      const tasks = spec.tasks || [];
      const members = new Set();
      tasks.forEach(t => {
        const a = (t.assignee || t.assignee_name || '').trim();
        if (a) {
          const lower = a.toLowerCase();
          if (lower !== 'aris' && !lower.includes('areskretindo')) {
            members.add(a);
          }
        }
      });

      const memberList = Array.from(members).join(', ');
      const chatInp = document.getElementById('cfgTeamsChats');
      if (chatInp && memberList) {
        chatInp.value = memberList;
      }
      showToast(`Sprint & ${members.size} rekan pemegang task berhasil disinkronkan!`, 'success');
    }

    function teamsSyncSprintName() {
      teamsSyncSprintContext();
    }

    function teamsOpenImportModal() {
      const modal = document.getElementById('teamsImportModal');
      if (modal) modal.style.display = 'flex';
    }

    function teamsCloseImportModal() {
      const modal = document.getElementById('teamsImportModal');
      if (modal) modal.style.display = 'none';
    }

    function teamsCopySnippet() {
      const snip = document.getElementById('teamsConsoleSnippet');
      if (snip) {
        snip.select();
        navigator.clipboard.writeText(snip.value).then(function () {
          showToast('Kode snippet disalin! Buka tab Teams Anda, tekan F12 > Console, dan paste.', 'success');
        }).catch(function () {
          showToast('Silakan salin teks di dalam kotak secara manual.', 'info');
        });
      }
    }

    async function teamsSubmitImportSession() {
      const inp = document.getElementById('teamsImportJsonInput');
      if (!inp || !inp.value.trim()) {
        showToast('Masukkan data JSON sesi Teams terlebih dahulu', 'error');
        return;
      }
      try {
        let payload;
        try {
          payload = JSON.parse(inp.value.trim());
        } catch (e) {
          showToast('Format JSON tidak valid: ' + e.message, 'error');
          return;
        }
        showToast('Menerapkan sesi ke server...', 'info');
        const res = await fetch('/api/teams/session', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
          showToast('✅ Sesi Microsoft Teams berhasil diterapkan!', 'success');
          teamsCloseImportModal();
          setTimeout(function () {
            teamsRefreshScreen(true);
            teamsCheckStatus();
          }, 1500);
        } else {
          showToast('Gagal menerapkan sesi: ' + (data.error || 'Unknown error'), 'error');
        }
      } catch (err) {
        showToast('Gagal mengirim sesi: ' + err.message, 'error');
      }
    }

    async function teamsSyncFromFirefox() {
      try {
        showToast('🦊 Membaca sesi Teams dari Firefox server...', 'info');
        const res = await fetch('/api/teams/sync-firefox', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.success) {
          showToast(`✅ Berhasil menyinkronkan ${data.cookies_count || 0} cookies dari Firefox!`, 'success');
          setTimeout(function () {
            teamsRefreshScreen(true);
            teamsCheckStatus();
          }, 1500);
        } else {
          showToast('Gagal sinkron Firefox: ' + (data.error || 'Profil tidak ditemukan'), 'error');
        }
      } catch (err) {
        showToast('Gagal sinkron Firefox: ' + err.message, 'error');
      }
    }

    function teamsToggleMode() {
      const isHeaded = window._teamsCurrentHeaded || false;
      teamsSwitchMode(!isHeaded);
    }

    async function teamsSwitchMode(headed) {
      try {
        const modeName = headed ? 'Headed (Layar Desktop Server)' : 'Headless (Latar Belakang)';
        showToast(`Mengalihkan browser Teams ke mode ${modeName}...`, 'info');
        const res = await fetch('/api/teams/mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ headed: !!headed })
        });
        const data = await res.json();
        if (data.success) {
          showToast(`✅ Browser dialihkan ke mode ${modeName}!`, 'success');
          setTimeout(function () {
            teamsRefreshScreen(true);
            teamsCheckStatus();
          }, 2000);
        } else {
          showToast('Gagal mengubah mode: ' + (data.error || 'Terjadi kesalahan'), 'error');
        }
      } catch (err) {
        showToast('Gagal mengubah mode: ' + err.message, 'error');
      }
    }

    async function teamsCleanProfile() {
      if (!confirm('Bersihkan cookies & cache Teams yang korup untuk menghentikan looping reload?')) return;
      try {
        showToast('Membersihkan cookies/cache Teams...', 'info');
        const res = await fetch('/api/teams/clean-profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.success) {
          showToast('✅ Cache dibersihkan! Silakan login sekarang tanpa loop.', 'success');
          setTimeout(function () {
            teamsRefreshScreen(true);
            teamsCheckStatus();
          }, 2000);
        } else {
          showToast('Gagal: ' + (data.error || 'Terjadi kesalahan'), 'error');
        }
      } catch (err) {
        showToast('Gagal: ' + err.message, 'error');
      }
    }

    // ==================== RAW TEAMS CHAT VIEWER LOGIC ====================
    window._teamsRawData = null;
    window._teamsActiveFilter = 'all';
    window._teamsSelectedChatIndex = null;
    window._teamsSearchQuery = '';

    async function teamsLoadRawChats() {
      try {
        const res = await fetch('/api/teams/raw-chats');
        if (!res.ok) return;
        const data = await res.json();
        if (data && data.chats && data.chats.length) {
          window._teamsRawData = data;
          teamsRenderRawChats();
        }
      } catch (err) {
        console.error('Error loading raw chats:', err);
      }
    }

    async function teamsTriggerHarvestAll() {
      const btn = document.getElementById('btnHarvestAll');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '⏳ Menarik Semua Obrolan &amp; Riwayat (15-20 dtk)...';
      }
      showToast('Sedang memindai grup & personal chat (hingga 25-30 obrolan teratas)...', 'info');
      try {
        const res = await fetch('/api/teams/harvest-all', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ max_chats: 50, max_messages: 50 })
        });
        const data = await res.json();
        if (data.success) {
          window._teamsRawData = data;
          teamsRenderRawChats();
          const count = data.total_chats || data.chats_count || (data.chats ? data.chats.length : 0);
          showToast('✅ Berhasil membaca ' + count + ' obrolan (' + (data.total_messages || 0) + ' pesan)!', 'success');
          teamsRefreshScreen(true);
        } else {
          console.warn('Teams harvest gagal', data);
          showToast('Gagal menarik obrolan: ' + (data.message || data.error || 'Peramban sedang sibuk') + ' (detail: /api/teams/diagnose)', 'error');
        }
      } catch (err) {
        showToast('Koneksi terputus saat membaca obrolan. Silakan klik tarik ulang.', 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '🔄 Tarik &amp; Baca Semua Obrolan Sekarang';
        }
      }
    }

    function teamsFilterChats(filterType) {
      window._teamsActiveFilter = filterType;
      const tabs = ['all', 'unread', 'group', 'personal'];
      tabs.forEach(function (t) {
        const el = document.getElementById('filterTab' + t.charAt(0).toUpperCase() + t.slice(1));
        if (el) {
          if (t === filterType) {
            el.style.background = '#2563eb';
            el.style.color = '#fff';
            el.style.borderColor = '#1d4ed8';
          } else {
            el.style.background = '';
            el.style.color = '';
            el.style.borderColor = '';
          }
        }
      });
      teamsRenderRawChats();
    }

    function teamsSearchChats(val) {
      window._teamsSearchQuery = (val || '').toLowerCase().trim();
      teamsRenderRawChats();
    }

    function teamsRenderRawChats() {
      const d = window._teamsRawData;
      const listEl = document.getElementById('teamsRawChatList');
      const metaEl = document.getElementById('teamsRawMeta');
      if (!listEl) return;

      if (!d || !d.chats || !d.chats.length) {
        listEl.innerHTML = '<div style="color:#71717a; font-size:0.78rem; text-align:center; padding:2rem 1rem;">Belum ada obrolan ditarik. Klik tombol hijau di atas untuk membaca semua chat.</div>';
        if (metaEl) metaEl.textContent = 'Belum ada data obrolan tersimpan';
        return;
      }

      const allCount = d.chats.length;
      const unreadCount = d.chats.filter(function (c) { return c.unread; }).length;
      const grpCount = d.chats.filter(function (c) { return c.type === 'group'; }).length;
      const pcCount = d.chats.filter(function (c) { return c.type === 'personal'; }).length;

      if (metaEl) {
        metaEl.textContent = 'Terakhir disinkronkan: ' + (d.scraped_at || '-') + ' • Total ' + (d.total_chats || d.chats_count || allCount) + ' obrolan (' + (d.total_messages || 0) + ' pesan)';
      }

      const cAll = document.getElementById('countAll');
      const cUnread = document.getElementById('countUnread');
      const cGrp = document.getElementById('countGroup');
      const cPc = document.getElementById('countPersonal');
      if (cAll) cAll.textContent = allCount;
      if (cUnread) cUnread.textContent = unreadCount;
      if (cGrp) cGrp.textContent = grpCount;
      if (cPc) cPc.textContent = pcCount;

      const query = window._teamsSearchQuery;
      const filter = window._teamsActiveFilter;

      const filtered = d.chats.map(function (c, idx) { return Object.assign({}, c, { originalIndex: idx }); }).filter(function (c) {
        if (filter === 'unread' && !c.unread) return false;
        if (filter === 'group' && c.type !== 'group') return false;
        if (filter === 'personal' && c.type !== 'personal') return false;
        if (query) {
          const matchTitle = (c.title || '').toLowerCase().includes(query);
          const matchSnippet = (c.snippet || '').toLowerCase().includes(query);
          const matchMsg = (c.messages || []).some(function (m) {
            return (m.author || '').toLowerCase().includes(query) || (m.text || '').toLowerCase().includes(query);
          });
          if (!matchTitle && !matchSnippet && !matchMsg) return false;
        }
        return true;
      });

      if (!filtered.length) {
        listEl.innerHTML = '<div style="color:#71717a; font-size:0.78rem; text-align:center; padding:2rem 1rem;">Tidak ada obrolan yang cocok dengan pencarian / filter ini.</div>';
        return;
      }

      let html = '';
      filtered.forEach(function (c) {
        const isSelected = window._teamsSelectedChatIndex === c.originalIndex;
        const icon = c.type === 'group' ? '👥' : '👤';
        const typeBg = c.type === 'group' ? 'rgba(59,130,246,0.15)' : 'rgba(168,85,247,0.15)';
        const typeCol = c.type === 'group' ? '#60a5fa' : '#c084fc';
        const typeText = c.type === 'group' ? 'Grup' : 'PC';
        const unreadBadge = c.unread ? '<span style="font-size:0.6rem; padding:0.05rem 0.3rem; border-radius:3px; background:#ef4444; color:#fff; font-weight:700; margin-left:0.35rem;">BARU</span>' : '';

        html += '<div onclick="teamsSelectChat(' + c.originalIndex + ')" style="padding:0.55rem 0.65rem; border-radius:6px; cursor:pointer; transition:all 0.15s; background:' + (isSelected ? '#1e293b; border:1px solid #38bdf8;' : '#121318; border:1px solid #27272a;') + '">';
        html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.25rem;">';
        html += '<div style="font-weight:700; font-size:0.8rem; color:#f8fafc; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:170px;" title="' + escapeHtml(c.title || '') + '">' + icon + ' ' + escapeHtml(c.title || '') + unreadBadge + '</div>';
        html += '<span style="font-size:0.65rem; padding:0.1rem 0.35rem; border-radius:4px; font-weight:700; background:' + typeBg + '; color:' + typeCol + ';">' + typeText + ' (' + (c.messages_count || (c.messages ? c.messages.length : 0)) + ')</span>';
        html += '</div>';
        if (c.snippet) {
          html += '<div style="font-size:0.7rem; color:#94a3b8; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + escapeHtml(c.snippet) + '</div>';
        }
        html += '</div>';
      });

      listEl.innerHTML = html;

      // Automatically select first chat if none selected
      if (window._teamsSelectedChatIndex === null && filtered.length > 0) {
        teamsSelectChat(filtered[0].originalIndex);
      }
    }

    function teamsSelectChat(idx) {
      window._teamsSelectedChatIndex = idx;
      teamsRenderRawChats();

      const d = window._teamsRawData;
      if (!d || !d.chats || !d.chats[idx]) return;
      const chat = d.chats[idx];

      const headerTitle = document.getElementById('teamsActiveChatTitle');
      const headerAction = document.getElementById('teamsActiveChatAction');
      const msgsBox = document.getElementById('teamsActiveChatMessages');

      if (headerTitle) {
        const icon = chat.type === 'group' ? '👥' : '👤';
        const typeLabel = chat.type === 'group' ? 'Group Chat' : 'Personal Chat (1-on-1)';
        headerTitle.innerHTML = '<span style="color:#38bdf8;">' + icon + ' ' + escapeHtml(chat.title || '') + '</span> <span style="font-size:0.72rem; color:#94a3b8; font-weight:400; margin-left:0.5rem;">(' + typeLabel + ' &bull; ' + (chat.messages ? chat.messages.length : 0) + ' pesan)</span>';
      }
      if (headerAction) headerAction.style.display = 'block';

      if (!msgsBox) return;
      const msgs = chat.messages || [];
      if (!msgs.length) {
        msgsBox.innerHTML = '<div style="color:#71717a; font-size:0.8rem; text-align:center; padding:3rem 1rem;">Tidak ada pesan terbaca di obrolan ini.</div>';
        return;
      }

      let mHtml = '';
      msgs.forEach(function (m) {
        const isAris = (m.author || '').toLowerCase().includes('aris') || (m.author || '').toLowerCase().includes('you');
        const bubbleBg = isAris ? 'rgba(59,130,246,0.12)' : '#181920';
        const bubbleBorder = isAris ? 'rgba(59,130,246,0.3)' : '#27272a';
        const authorCol = isAris ? '#60a5fa' : '#34d399';

        mHtml += '<div style="background:' + bubbleBg + '; border:1px solid ' + bubbleBorder + '; border-radius:8px; padding:0.6rem 0.85rem;">';
        mHtml += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.25rem;">';
        mHtml += '<span style="font-weight:700; font-size:0.78rem; color:' + authorCol + ';">' + escapeHtml(m.author || 'User') + '</span>';
        if (m.time) {
          mHtml += '<span style="font-size:0.68rem; color:#64748b;">' + escapeHtml(m.time) + '</span>';
        }
        mHtml += '</div>';
        mHtml += '<div style="font-size:0.8rem; color:#f1f5f9; line-height:1.55; white-space:pre-wrap; word-break:break-word;">' + escapeHtml(m.text || '') + '</div>';

        if (m.attachments && m.attachments.length) {
          mHtml += '<div style="display:flex; flex-wrap:wrap; gap:0.4rem; margin-top:0.45rem;">';
          m.attachments.forEach(function (att) {
            let icon = '📎';
            const ext = (att.ext || '').toLowerCase();
            if (['zip', 'rar', '7z', 'tar', 'gz'].indexOf(ext) >= 0) icon = '📦';
            else if (['pdf'].indexOf(ext) >= 0) icon = '📑';
            else if (['doc', 'docx'].indexOf(ext) >= 0) icon = '📝';
            else if (['xls', 'xlsx', 'csv'].indexOf(ext) >= 0) icon = '📊';
            else if (['png', 'jpg', 'jpeg', 'gif', 'webp'].indexOf(ext) >= 0 || att.type === 'image') icon = '🖼️';

            mHtml += '<div style="display:inline-flex; align-items:center; gap:0.35rem; background:rgba(30,41,59,0.9); border:1px solid #334155; border-radius:6px; padding:0.25rem 0.55rem; font-size:0.72rem; color:#38bdf8;">';
            mHtml += '<span>' + icon + '</span>';
            mHtml += '<span style="font-weight:600; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="' + escapeHtml(att.name || '') + '">' + escapeHtml(att.name || 'File Attachment') + '</span>';
            if (att.ext) {
              mHtml += '<span style="font-size:0.65rem; background:#1e293b; color:#94a3b8; padding:0.05rem 0.3rem; border-radius:3px; text-transform:uppercase;">' + escapeHtml(att.ext) + '</span>';
            }
            mHtml += '</div>';
          });
          mHtml += '</div>';
        }

        if (m.links && m.links.length) {
          mHtml += '<div style="display:flex; flex-wrap:wrap; gap:0.4rem; margin-top:0.35rem;">';
          m.links.forEach(function (lnk) {
            mHtml += '<a href="' + escapeHtml(lnk.url) + '" target="_blank" rel="noopener noreferrer" style="display:inline-flex; align-items:center; gap:0.3rem; font-size:0.72rem; color:#60a5fa; text-decoration:underline; background:rgba(37,99,235,0.1); border:1px solid rgba(59,130,246,0.3); border-radius:4px; padding:0.15rem 0.45rem;">';
            mHtml += '<span>🔗</span> <span>' + escapeHtml(lnk.text || lnk.url) + '</span>';
            mHtml += '</a>';
          });
          mHtml += '</div>';
        }

        mHtml += '</div>';
      });

      msgsBox.innerHTML = mHtml;
      msgsBox.scrollTop = msgsBox.scrollHeight;
    }

    async function teamsOpenActiveInRemote() {
      const d = window._teamsRawData;
      const idx = window._teamsSelectedChatIndex;
      if (!d || !d.chats || idx === null || !d.chats[idx]) return;
      const chat = d.chats[idx];

      showToast('Membuka obrolan "' + chat.title + '" di remote browser...', 'info');
      try {
        await fetch('/api/teams/select-chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: chat.title })
        });
        setTimeout(function () {
          teamsRefreshScreen(true);
          if (!window._teamsIsBrowserOpen) teamsToggleBrowser(true);
        }, 1200);
      } catch (err) {
        showToast('Gagal berpindah obrolan di remote browser', 'error');
      }
    }

    function teamsToggleRawJsonModal() {
      const modal = document.getElementById('teamsRawJsonModal');
      if (!modal) return;
      const isVisible = modal.style.display === 'flex';
      if (!isVisible) {
        const pre = document.getElementById('teamsRawJsonContent');
        if (pre) {
          pre.textContent = window._teamsRawData ? JSON.stringify(window._teamsRawData, null, 2) : 'Belum ada data obrolan.';
        }
        modal.style.display = 'flex';
      } else {
        modal.style.display = 'none';
      }
    }

    function teamsCopyRawJson() {
      const pre = document.getElementById('teamsRawJsonContent');
      if (pre && pre.textContent) {
        navigator.clipboard.writeText(pre.textContent).then(function () {
          showToast('JSON data mentah berhasil disalin ke clipboard!', 'success');
        });
      }
    }


    function renderQuickTaskChips() {
      const box = document.getElementById('quickTaskChips');
      if (!box) return;
      const d = window._spectrumData;
      const mine = (d && d.tasks ? d.tasks : []).filter(function (t) { return typeof isMyTask === 'function' && isMyTask(t); }).slice(0, 2);
      box.innerHTML = mine.map(function (t) {
        return '<button class="quick-chip" title="' + escapeHtml(t.name || '') + '" data-code="' + escapeHtml(t.code || '') + '" data-name="' + escapeHtml(t.name || '') + '" onclick="askHermesAboutTask(this.dataset.code, this.dataset.name, ' + "'test_case'" + ')">TC ' + escapeHtml(t.code || '') + '</button>';
      }).join('');
    }

    function renderBriefingChips() {
      const el = document.getElementById('hbChips');
      if (!el) return;
      const d = window._spectrumData;
      const chips = [];
      const now = new Date();
      chips.push('<span class="hb-chip-stat">📅 <b>' + now.toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'short' }) + '</b></span>');
      if (d && d.tasks && d.tasks.length) {
        const sprint = d.active_sprint || {};
        if (sprint.end_date) {
          const left = Math.max(0, Math.ceil((new Date(sprint.end_date) - now) / 86400000));
          chips.push('<span class="hb-chip-stat' + (left <= 3 ? ' warn' : '') + '">⏳ Sprint <b>' + left + ' hari</b> lagi</span>');
        }
        const mine = d.tasks.filter(function (t) { return typeof isMyTask === 'function' && isMyTask(t); });
        chips.push('<span class="hb-chip-stat">📝 Task saya <b>' + mine.length + '</b></span>');
        const ready = d.tasks.filter(function (t) { return t.status === 'Done-Development' || t.status === 'Task-Finished'; }).length;
        chips.push('<span class="hb-chip-stat ok">✅ Siap QA <b>' + ready + '</b></span>');
        const today = now.toISOString().substring(0, 10);
        const ev = (d.calendar_events || []).filter(function (e) { return String(e.start || e.execute_at || '').startsWith(today); }).length;
        if (ev) chips.push('<span class="hb-chip-stat">🗓️ Agenda hari ini <b>' + ev + '</b></span>');
      }
      el.innerHTML = chips.join('');
    }

    function toggleHermesBriefing() {
      const panel = document.getElementById('hermesBriefingPanel');
      if (!panel) return;
      const collapsed = panel.classList.toggle('hb-collapsed');
      const btn = document.getElementById('btnToggleBriefing');
      if (btn) btn.textContent = collapsed ? '▸' : '▾';
      try { localStorage.setItem('hbCollapsed', collapsed ? '1' : '0'); } catch (e) {}
    }

    async function loadEmailStatus() {
      if (!IS_ARES_OWNER) return;
      try {
        const res = await fetch('/api/email/status');
        const data = await res.json();
        if (!data.success) return;
        const st = data.status || {};
        const badge = document.getElementById('emailSvcBadge');
        if (badge) { badge.textContent = st.configured ? 'AKTIF' : 'BELUM DIKONFIGURASI'; badge.className = 'hermes-badge ' + (st.configured ? 'badge-ok' : 'badge-warn'); }
        const info = document.getElementById('emailSvcInfo');
        if (info) {
          info.innerHTML = st.configured
            ? 'Relay: <b style="color:#e4e4e7;">' + escapeHtml(st.host) + ':' + st.port + '</b> (' + escapeHtml(st.security) + ') • Pengirim: <b style="color:#e4e4e7;">' + escapeHtml(st.from) + '</b> • Tautan email: <b style="color:#e4e4e7;">' + escapeHtml(st.base_url) + '</b>'
            : 'Isi <code>SMTP_HOST</code>, <code>SMTP_PORT</code>, <code>SMTP_USER</code>, <code>SMTP_PASSWORD</code>, <code>SMTP_FROM</code>, dan <code>PUBLIC_BASE_URL</code> di <code>.env</code>, lalu restart service. Cloudflare Tunnel hanya melayani web; email keluar memakai relay SMTP (Brevo, Resend, SES, Gmail App Password).';
        }
        const logs = document.getElementById('emailSvcLogs');
        if (logs) {
          logs.innerHTML = (data.logs || []).length ? data.logs.map(function (l) {
            return '<div style="margin:0.15rem 0; color:' + (l.status === 'terkirim' ? '#34d399' : '#f87171') + ';">' + escapeHtml(l.created_at) + ' • ' + escapeHtml(l.kind) + ' • ' + escapeHtml(l.to_addr) + ' • ' + escapeHtml(l.status) + (l.error ? ' — ' + escapeHtml(l.error) : '') + '</div>';
          }).join('') : 'Belum ada pengiriman.';
        }
      } catch (e) { console.warn('Gagal memuat status email', e); }
    }

    async function sendTestEmail() {
      const btn = document.getElementById('btnEmailTest');
      const out = document.getElementById('emailTestResult');
      const to = document.getElementById('emailTestTo')?.value.trim() || '';
      if (btn) { btn.disabled = true; btn.textContent = 'Mengirim...'; }
      try {
        const res = await fetch('/api/email/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to }) });
        const data = await res.json();
        if (out) {
          out.style.display = 'block';
          out.style.background = data.success ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)';
          out.style.color = data.success ? '#6ee7b7' : '#fca5a5';
          out.textContent = data.success ? data.message : ('Gagal: ' + (data.error || 'tidak diketahui'));
        }
      } catch (e) {
        if (out) { out.style.display = 'block'; out.style.color = '#fca5a5'; out.textContent = 'Gagal: ' + e.message; }
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Kirim email uji'; }
        loadEmailStatus();
      }
    }

    async function loadEmailPrefs() {
      try {
        const res = await fetch('/api/user/email-prefs');
        const d = await res.json();
        const chk = document.getElementById('chkEmailBriefing');
        const hint = document.getElementById('emailPrefsHint');
        if (!d.success || !chk) return;
        chk.checked = !!d.briefing;
        chk.disabled = !d.configured;
        if (hint) hint.textContent = d.configured ? ('Dikirim ke ' + d.email + '. Anda bisa mematikannya kapan saja.') : 'Layanan email belum diaktifkan oleh admin server.';
      } catch (e) {}
    }

    async function saveEmailPrefs(on) {
      try {
        await fetch('/api/user/email-prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ briefing: !!on }) });
        showToast(on ? 'Briefing harian akan dikirim ke email Anda' : 'Briefing email dimatikan', 'success');
      } catch (e) { showToast('Gagal menyimpan: ' + e.message, 'error'); }
    }

    let hbCooldownTimer = null;
    let hbAutoTried = false;

    function renderHermesBriefing(resp) {
      const body = document.getElementById('hermesBriefingBody');
      const meta = document.getElementById('hbMeta');
      const btn = document.getElementById('btnRefreshBriefing');
      if (!body) return;
      try {
        if (localStorage.getItem('hbCollapsed') === '1') {
          document.getElementById('hermesBriefingPanel')?.classList.add('hb-collapsed');
          const tb = document.getElementById('btnToggleBriefing'); if (tb) tb.textContent = '▸';
        }
      } catch (e) {}
      renderBriefingChips();
      const b = resp && resp.briefing;
      if (b && b.html) {
        body.innerHTML = b.html;  // sudah disanitasi server (allowlist tag/class)
        const when = (b.generated_at || '').replace('T', ' ').substring(0, 16);
        const todayStr = new Date().toISOString().substring(0, 10);
        const stale = (b.generated_at || '').substring(0, 10) !== todayStr;
        if (meta) meta.textContent = 'Diperbarui ' + when + ' • ' + (b.trigger === 'auto' ? 'otomatis 02:00' : 'manual') +
          (b.source === 'fallback' ? ' • mode cadangan (LLM tidak tersedia)' : '') + (stale ? ' • belum hari ini' : '');
      } else {
        body.innerHTML = '<div style="color:#71717a;">Belum ada briefing. Klik "Perbarui Briefing" untuk menyusunnya sekarang.</div>';
        if (meta) meta.textContent = 'Belum ada briefing';
      }
      if (btn) {
        clearInterval(hbCooldownTimer);
        let wait = (resp && resp.wait_seconds) || 0;
        const setBtn = function () {
          if (wait > 0) { btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = '↻ Tunggu ' + wait + ' dtk'; }
          else { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = '↻ Perbarui Briefing'; clearInterval(hbCooldownTimer); }
        };
        setBtn();
        if (wait > 0) hbCooldownTimer = setInterval(function () { wait--; setBtn(); }, 1000);
      }
    }

    async function loadHermesBriefing() {
      try {
        const res = await fetch('/api/hermes/briefing');
        if (!res.ok) return;
        const data = await res.json();
        renderHermesBriefing(data);
        // Pengguna baru / belum pernah dibuat: susun sekali otomatis
        if (!data.briefing && !hbAutoTried) { hbAutoTried = true; refreshHermesBriefing(); }
      } catch (e) {
        console.warn('Gagal memuat briefing Hermes:', e);
      }
    }

    async function refreshHermesBriefing() {
      const btn = document.getElementById('btnRefreshBriefing');
      const body = document.getElementById('hermesBriefingBody');
      if (btn) { btn.disabled = true; btn.textContent = 'Hermes menyusun...'; }
      if (body && !body.querySelector('.hb-section')) body.innerHTML = '<div style="color:#71717a;">Hermes sedang menyusun briefing Anda...</div>';
      try {
        const res = await fetch('/api/hermes/briefing/generate', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } });
        const data = await res.json();
        if (data.success) {
          renderHermesBriefing({ briefing: data.briefing, wait_seconds: 60 });
        } else {
          showToast(data.error || 'Gagal menyusun briefing', 'error');
          renderHermesBriefing({ briefing: (await (await fetch('/api/hermes/briefing')).json()).briefing, wait_seconds: data.wait_seconds || 0 });
        }
      } catch (e) {
        showToast('Gagal menyusun briefing: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = '↻ Perbarui Briefing'; }
      }
    }

    async function checkOtpForSuggestion() {
      try {
        const res = await fetch('/api/otp/inbox?limit=5&otp_only=true');
        if (!res.ok) return;
        const emails = await res.json();
        if (!emails || !emails.length) return;

        const latest = emails[0];
        const receivedAt = latest.received_at ? new Date(latest.received_at) : null;
        if (!receivedAt) return;
        const ageMinutes = (Date.now() - receivedAt.getTime()) / 60000;
        if (ageMinutes > 15) return; // Hanya tampilkan kalau < 15 menit

        const otpMatch = latest.body_snippet ? latest.body_snippet.match(/\\b(\\d{4,8})\\b/) : null;
        if (!otpMatch) return;
        const otpCode = otpMatch[1];

        window._latestOtpCode = otpCode;
        const strip = document.getElementById('otpSuggestionStrip');
        const codeEl = document.getElementById('otpSuggestionCode');
        if (strip && codeEl) {
          codeEl.textContent = otpCode;
          strip.style.display = 'flex';
        }

        // Update badge di tab Hermes
        const badge = document.getElementById('navHermesBadge');
        if (badge) {
          badge.textContent = '1';
          badge.style.display = 'inline-block';
        }
      } catch (e) {
        // OTP check optional, no error handling needed
      }
    }

    function useOtpSuggestion() {
      const code = window._latestOtpCode || '';
      const input = document.getElementById('aiInputPrompt');
      if (input && code) {
        input.value = `OTP yang barusan masuk adalah ${code}. Apakah ada hal lain yang perlu disiapkan?`;
        document.getElementById('otpSuggestionStrip').style.display = 'none';
        input.focus();
      }
    }

    function renderSpectrumCalendar(events) {
      const container = document.getElementById('spectrumCalendarList');
      if (!container) return;
      if (!events || !events.length) {
        container.innerHTML = '<div style="color: var(--gf-text-muted); font-size: 0.8rem; padding: 0.5rem 0;">Tidak ada jadwal agenda harian terdekat.</div>';
        return;
      }

      const todayStr = new Date().toISOString().substring(0, 10);
      const sorted = events.slice().sort(function (x, y) { return String(x.start || x.execute_at || '').localeCompare(String(y.start || y.execute_at || '')); });
      container.innerHTML = sorted.map(ev => {
        let timeLabel = '';
        let isToday = false;
        if (ev.start && ev.end) {
          const dStr = ev.start.substring(0, 10);
          isToday = dStr === todayStr;
          timeLabel = (isToday ? 'Hari ini' : dStr) + ' &bull; ' + ev.start.substring(11, 16) + ' - ' + ev.end.substring(11, 16) + ' WIB';
        } else {
          timeLabel = ev.execute_at || 'Hari ini';
        }
        const safeName = escapeHtml(ev.name || '');
        return `
          <div class="agenda-item${isToday ? ' today' : ''}">
            <div style="overflow: hidden; min-width: 0;">
              <div style="font-size: 0.78rem; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                ${isToday ? '<span class="agenda-chip">HARI INI</span>' : ''}${safeName}
              </div>
              <div style="font-size: 0.68rem; color: #a1a1aa; margin-top: 0.15rem;">${timeLabel}</div>
            </div>
            <button data-name="${safeName}" data-time="${escapeHtml(timeLabel)}" onclick="prepareMeetingAgendaPrompt(this.dataset.name, this.dataset.time)" style="background: #27272a; color: #38bdf8; border: 1px solid #3f3f46; border-radius: 6px; padding: 0.22rem 0.55rem; font-size: 0.68rem; font-weight: 600; cursor: pointer; flex-shrink: 0;">Bahan</button>
          </div>
        `;
      }).join('');
    }

    function filterSpectrumByAssignee(assigneeName) {
      window._spectrumAssigneeFilter = assigneeName;
      if (assigneeName) {
        window._spectrumFilter = 'assignee';
        document.querySelectorAll('.spectrum-tab').forEach(btn => btn.classList.remove('active'));
      } else {
        filterSpectrumTasks('all');
      }
      renderSpectrumTasks();
    }

    function filterSpectrumTasks(filterType) {
      window._spectrumFilter = filterType;
      window._spectrumAssigneeFilter = '';
      const sel = document.getElementById('spectrumAssigneeSelect');
      if (sel) sel.value = '';
      document.querySelectorAll('.spectrum-tab').forEach(btn => btn.classList.remove('active'));
      if (filterType === 'my' || filterType === 'aris') document.getElementById('tabTaskAris')?.classList.add('active');
      else if (filterType === 'devdone') document.getElementById('tabTaskDevDone')?.classList.add('active');
      else if (filterType === 'progress') document.getElementById('tabTaskProgress')?.classList.add('active');
      else if (filterType === 'all') document.getElementById('tabTaskAll')?.classList.add('active');
      renderSpectrumTasks();
    }

    function renderSpectrumTasks() {
      const tbody = document.getElementById('spectrumTaskTableBody');
      if (!tbody) return;
      const data = window._spectrumData;
      if (!data || !data.tasks || !data.tasks.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">Belum ada data task sprint. Silakan klik "Perbarui Data".</td></tr>';
        return;
      }

      let filtered = data.tasks;
      if (window._spectrumFilter === 'my' || window._spectrumFilter === 'aris') {
        filtered = data.tasks.filter(t => isMyTask(t));
      } else if (window._spectrumFilter === 'assignee' && window._spectrumAssigneeFilter) {
        filtered = data.tasks.filter(t => (t.assignee || 'Unassigned') === window._spectrumAssigneeFilter);
      } else if (window._spectrumFilter === 'devdone') {
        filtered = data.tasks.filter(t => t.status === 'Done-Development' || t.status === 'Task-Finished');
      } else if (window._spectrumFilter === 'progress') {
        filtered = data.tasks.filter(t => t.status === 'On-Progress');
      }

      const searchQ = (window._spectrumSearch || '').toLowerCase().trim();
      if (searchQ) {
        filtered = filtered.filter(function (t) {
          return ((t.code || '') + ' ' + (t.name || '') + ' ' + (t.assignee || '') + ' ' + (t.status || '')).toLowerCase().includes(searchQ);
        });
      }

      if (!filtered.length) {
        if (window._spectrumFilter === 'my' || window._spectrumFilter === 'aris') {
          const myName = window._myMemberName || (typeof CURRENT_USER_NAME !== 'undefined' ? CURRENT_USER_NAME : '') || 'Anda';
          tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: #94a3b8; padding: 1.5rem 0;">
            <div style="font-size:0.85rem; margin-bottom:0.35rem; color:#f1f5f9; font-weight:600;">ℹ️ Belum ada task sprint yang di-assign secara khusus ke <b>${escapeHtml(myName)}</b> pada Sprint ini.</div>
            <div style="font-size:0.75rem; color:#71717a; margin-bottom:0.75rem;">Anda dapat melihat task seluruh anggota tim atau memilih anggota tim di dropdown filter atas.</div>
            <button onclick="filterSpectrumTasks('all')" style="background:#2563eb; color:#fff; border:none; padding:0.35rem 0.85rem; border-radius:5px; font-size:0.75rem; font-weight:600; cursor:pointer;">
              Lihat Semua Task Sprint (${data.tasks.length} Task)
            </button>
          </td></tr>`;
        } else {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">Tidak ada task pada filter ini.</td></tr>';
        }
        return;
      }

      tbody.innerHTML = filtered.map(t => {
        let badgeStyle = 'background: #27272a; color: #a1a1aa;';
        if (t.status === 'Done-Development') badgeStyle = 'background: rgba(52, 211, 153, 0.15); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3);';
        else if (t.status === 'Task-Finished') badgeStyle = 'background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4);';
        else if (t.status === 'On-Progress') badgeStyle = 'background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);';

        const isMine = isMyTask(t);
        const assigneeBadge = isMine 
          ? `<span style="color:#f59e0b; font-weight:700;">${escapeHtml(t.assignee)}</span> <span style="font-size:0.65rem; background:rgba(245,158,11,0.2); color:#fbbf24; border:1px solid rgba(245,158,11,0.4); padding:0.08rem 0.35rem; border-radius:3px; font-weight:700;">Task Anda</span>`
          : `<span style="color:#cbd5e1;">${escapeHtml(t.assignee || 'Unassigned')}</span>`;

        return `
          <tr${isMine ? ' style="box-shadow: inset 3px 0 0 #f59e0b;"' : ''}>
            <td style="font-family: 'JetBrains Mono', monospace; font-size: 0.74rem; font-weight: 600; color: #38bdf8;">${escapeHtml(t.code || '-')}</td>
            <td style="color: #fff; font-weight: 500;">
              <div>${escapeHtml(t.name)}</div>
            </td>
            <td>${assigneeBadge}</td>
            <td><span class="hermes-badge" style="${badgeStyle}; font-size:0.68rem; padding:0.1rem 0.4rem;">${escapeHtml(t.status || 'To-do')}</span></td>
            <td style="text-align: center; color: #94a3b8; font-family: 'JetBrains Mono', monospace;">${t.weight || 0}</td>
            <td style="text-align: right; white-space: nowrap;">
              <button onclick="askHermesAboutTask('${escapeHtml(t.code || '')}', '${escapeHtml(t.name || '')}', 'test_case')" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 4px; padding: 0.22rem 0.5rem; font-size: 0.7rem; cursor: pointer; font-weight: 500; margin-right: 0.3rem;">
                Buat Test Case
              </button>
              <button onclick="exportSingleTaskToSheet('${escapeHtml(t.code || '')}', '${escapeHtml(t.name || '')}', this)" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 4px; padding: 0.22rem 0.45rem; font-size: 0.7rem; cursor: pointer; margin-right: 0.3rem;" title="Simpan skenario task ini ke Google Sheets">
                + Sheet
              </button>
              <button onclick="askHermesAboutTask('${escapeHtml(t.code || '')}', '${escapeHtml(t.name || '')}', 'criteria')" style="background: #27272a; color: #a1a1aa; border: 1px solid #3f3f46; border-radius: 4px; padding: 0.22rem 0.45rem; font-size: 0.7rem; cursor: pointer;" title="Analisa kriteria penerimaan dan resiko">
                Analisa
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    async function exportSingleTaskToSheet(code, name, btn) {
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Menyimpan...';
      }
      try {
        const authorName = window._myMemberName || (typeof CURRENT_USER_NAME !== 'undefined' ? CURRENT_USER_NAME : '') || 'QA Engineer';
        const sampleTc = [{
          id: code,
          module: 'Sprint 6 PNRI',
          title: name,
          type: 'Positive',
          preconditions: 'User sudah login, build Sprint 6 terinstall',
          steps: ['Buka menu terkait di aplikasi YAP!', `Eksekusi skenario pengujian [${code}] ${name}`, 'Verifikasi respon dan tampilan'],
          expected: 'Skenario berhasil dieksekusi sesuai kriteria penerimaan FSD',
          notes: 'Auto-exported from Workspace QA SPEctrum'
        }];
        const res = await fetch('/api/sheets/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ test_cases: sampleTc, author: authorName })
        });
        const data = await res.json();
        if (data.success) {
          if (btn) {
            btn.textContent = '✓ Tersimpan';
            btn.style.color = '#34d399';
            setTimeout(() => {
              btn.textContent = '+ Sheet';
              btn.disabled = false;
            }, 3000);
          }
        } else {
          alert('Gagal simpan ke Google Sheet: ' + (data.error || 'Unknown error'));
          if (btn) {
            btn.textContent = '+ Sheet';
            btn.disabled = false;
          }
        }
      } catch (err) {
        alert('Gagal menghubungi server: ' + err.message);
        if (btn) {
          btn.textContent = '+ Sheet';
          btn.disabled = false;
        }
      }
    }

    function getSpectrumContext() {
      const data = window._spectrumData || {};
      const proj = data.active_project || {};
      const sprint = data.active_sprint || {};
      const user = data.user || {};
      return {
        projName: proj.name || 'Proyek QA',
        sprintName: sprint.name || 'Sprint Berjalan',
        myName: window._myMemberName || user.name || (typeof CURRENT_USER_NAME !== 'undefined' ? CURRENT_USER_NAME : '') || 'QA Member',
        sprintGoal: sprint.description || sprint.target || ''
      };
    }

    function askHermesAboutTask(code, name, actionType) {
      const ctx = getSpectrumContext();
      let prompt = '';
      if (actionType === 'test_case') {
        prompt = `Sebagai QA Engineer untuk proyek ${ctx.projName} ${ctx.sprintName}, buatkan test case detail dan komprehensif untuk task [${code}] "${name}".\n\nSertakan:\n1. Positive Scenarios (Happy path)\n2. Negative & Validation Scenarios\n3. Edge Cases / Boundary Tests\n4. Kriteria Keberhasilan (Acceptance Criteria)\n5. Contoh Data Uji (Test Data Dummy)`;
      } else {
        prompt = `Untuk task [${code}] "${name}" di proyek ${ctx.projName} ${ctx.sprintName}, berikan analisis QA:\n1. Titik kritis pengujian & potensi bug\n2. Dependensi sistem/API backend yang perlu dipastikan\n3. Checklist verifikasi sebelum sign-off QA`;
      }
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareDailyUpdatePrompt() {
      const ctx = getSpectrumContext();
      const prompt = `Buatkan draf update Daily Standup untuk saya (${ctx.myName} - QA) untuk ${ctx.sprintName} proyek ${ctx.projName}.\n${ctx.sprintGoal ? `Sasaran sprint: ${ctx.sprintGoal}\n` : ''}\nFormatkan dengan format standar Agile Standup:\n1. Yesterday / Yang sudah dikerjakan:\n2. Today / Yang akan dikerjakan:\n3. Blockers / Kendala:`;
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareMeetingAgendaPrompt(meetingName, timeStr) {
      const ctx = getSpectrumContext();
      const prompt = `Tolong siapkan poin-poin penting dan bahan laporan QA yang perlu saya sampaikan untuk agenda rapat "${meetingName}" (${timeStr}) terkait status pengujian ${ctx.sprintName} proyek ${ctx.projName}.`;
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareEndOfDayPrompt() {
      const ctx = getSpectrumContext();
      const prompt = `Tolong buatkan Rekap Harian (End-of-day Summary) untuk saya (${ctx.myName} - QA) hari ini terkait ${ctx.sprintName} proyek ${ctx.projName}.\n\nRangkum:\n1. Aktivitas QA & pengujian yang telah selesai dilakukan hari ini.\n2. Isu atau bug yang ditemukan hari ini (jika ada) dan statusnya.\n3. Rencana fokus pengujian untuk besok pagi.\nFormat rapi dan ringkas agar mudah dibaca tim.`;
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function copyChatReply(btn) {
      const bubble = btn.closest('.chat-bubble');
      if (!bubble) return;
      const contentEl = bubble.querySelector('.chat-reply-content') || bubble;
      const text = contentEl.innerText || contentEl.textContent || '';
      navigator.clipboard.writeText(text.trim()).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'Tersalin!';
        btn.style.color = '#34d399';
        btn.style.borderColor = 'rgba(52, 211, 153, 0.4)';
        setTimeout(() => {
          btn.textContent = orig;
          btn.style.color = '#94a3b8';
          btn.style.borderColor = '#2d2d35';
        }, 2000);
      }).catch(err => {
        console.error('Gagal menyalin:', err);
      });
    }

    async function syncSpectrumLive() {
      const btn = document.getElementById('btnSyncSpectrum');
      const spinner = document.getElementById('syncSpectrumSpinner');
      if (btn) btn.disabled = true;
      if (spinner) spinner.style.display = 'inline-block';
      try {
        const res = await fetch('/api/user/spectrum/sync', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } });
        const data = await res.json();
        await loadSpectrumWorkspaceData();
      } catch (e) {
        console.error('Gagal sinkronkan SPEctrum:', e);
      } finally {
        if (btn) btn.disabled = false;
        if (spinner) spinner.style.display = 'none';
      }
    }

    // ==================== USER PROFILE & SPECTRUM CONFIG LOGIC ====================
    async function loadUserSpectrumConfig() {
      try {
        const res = await fetch('/api/user/spectrum/config');
        if (res.status === 401) {
          window.location.href = '/login';
          return;
        }
        const data = await res.json();
        if (data.success) {
          const cfg = data.config || {};
          const user = data.user || {};

          // Populate User info in Profile & Navbar
          const emailDisplay = document.getElementById('cfgUserEmailDisplay');
          if (emailDisplay) emailDisplay.textContent = user.email || 'User';
          const emailInput = document.getElementById('cfgUserEmailInput');
          if (emailInput) emailInput.value = user.email || '';

          const userNameInput = document.getElementById('cfgUserNameInput');
          if (userNameInput) userNameInput.value = user.name || '';

          const roleBadge = document.getElementById('cfgUserRoleBadge');
          if (roleBadge) roleBadge.textContent = (user.role || 'USER').toUpperCase();
          const idDisplay = document.getElementById('cfgUserIdDisplay');
          if (idDisplay) idDisplay.textContent = user.id || '-';
          const lastLogin = document.getElementById('cfgUserLastLoginDisplay');
          if (lastLogin) lastLogin.textContent = user.last_login || '-';

          if (user.name) {
            CURRENT_USER_NAME = user.name;
            const navUserName = document.getElementById('navUserName');
            if (navUserName) navUserName.textContent = user.name;
          }

          const oldPassNote = document.getElementById('cfgOldPassNote');
          if (oldPassNote) {
            if (user.has_password) {
              oldPassNote.textContent = 'Masukkan kata sandi lama Anda untuk verifikasi keamanan.';
              oldPassNote.style.color = '#71717a';
            } else {
              oldPassNote.textContent = 'Akun ini belum memiliki kata sandi aktif. Kosongkan field kata sandi lama dan langsung masukkan kata sandi baru.';
              oldPassNote.style.color = '#38bdf8';
            }
          }

          // Populate SPEctrum Config form
          const emailInputSpec = document.getElementById('cfgSpecEmail');
          if (emailInputSpec) emailInputSpec.value = cfg.spectrum_email || user.email || '';

          const passInput = document.getElementById('cfgSpecPassword');
          if (passInput) passInput.value = '';
          const passNote = document.getElementById('cfgPassStatusNote');
          if (cfg.has_password) {
            if (passInput) passInput.placeholder = 'Password tersimpan (' + cfg.spectrum_password_masked + ') - Kosongkan jika tidak diubah';
            if (passNote) passNote.innerHTML = '<span style="color:#10b981;">✓ Password tersimpan</span>';
          } else {
            if (passInput) passInput.placeholder = 'Masukkan password SPEctrum Anda';
            if (passNote) passNote.innerHTML = '<span style="color:#f59e0b;">Belum diatur</span>';
          }

          const projInput = document.getElementById('cfgSpecProjectId');
          if (projInput) projInput.value = cfg.project_id || '';

          const memberInput = document.getElementById('cfgSpecMemberName');
          if (memberInput) memberInput.value = cfg.member_name || user.name || '';

          const autoSync = document.getElementById('cfgSpecAutoSync');
          if (autoSync) autoSync.checked = !!cfg.auto_sync;

          // Status Badge
          const badge = document.getElementById('specUserStatusBadge');
          if (badge) {
            if (cfg.spectrum_email && cfg.has_password) {
              badge.className = 'hermes-badge badge-ok';
              badge.textContent = 'Terkonfigurasi';
            } else {
              badge.className = 'hermes-badge badge-blue';
              badge.textContent = 'Belum Lengkap';
            }
          }

          const lastSyncTime = document.getElementById('cfgLastSyncTime');
          if (lastSyncTime) lastSyncTime.textContent = cfg.last_synced || 'Belum pernah';

          const lastStatus = document.getElementById('cfgLastStatusText');
          if (lastStatus) lastStatus.textContent = cfg.last_status || 'Siap';
        }
      } catch (err) {
        console.error('Gagal memuat config SPEctrum user:', err);
      }
    }

    function showProfileAlert(msg, isSuccess = true) {
      const box = document.getElementById('profileAlertBox');
      if (!box) return;
      box.textContent = msg;
      box.style.display = 'block';
      box.style.background = isSuccess ? 'rgba(16, 185, 129, 0.12)' : 'rgba(239, 68, 68, 0.12)';
      box.style.border = isSuccess ? '1px solid rgba(16, 185, 129, 0.3)' : '1px solid rgba(239, 68, 68, 0.3)';
      box.style.color = isSuccess ? '#34d399' : '#f87171';
    }

    async function saveUserProfileName() {
      const btn = document.getElementById('btnSaveProfileName');
      const name = document.getElementById('cfgUserNameInput').value.trim();
      if (!name) {
        showProfileAlert('Nama profil tidak boleh kosong.', false);
        return;
      }
      if (btn) { btn.disabled = true; btn.textContent = 'Menyimpan...'; }
      try {
        const res = await fetch('/api/user/profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name })
        });
        const data = await res.json();
        if (data.success) {
          showProfileAlert('Nama profil berhasil disimpan!', true);
          showToast('Nama profil disimpan', 'success');
          CURRENT_USER_NAME = name;
          const navUserName = document.getElementById('navUserName');
          if (navUserName) navUserName.textContent = name;
          const greetEl = document.getElementById('briefingGreeting');
          if (greetEl) greetEl.textContent = 'Hei ' + name + ', selamat ' + (new Date().getHours() < 11 ? 'pagi' : new Date().getHours() < 15 ? 'siang' : 'sore') + '!';
        } else {
          showProfileAlert(data.error || 'Gagal menyimpan nama profil.', false);
        }
      } catch (err) {
        showProfileAlert('Kesalahan: ' + err.message, false);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '💾 Simpan Nama Profil'; }
      }
    }

    async function saveUserPasswordChange() {
      const btn = document.getElementById('btnSaveNewPassword');
      const old_password = document.getElementById('cfgOldPassword').value;
      const new_password = document.getElementById('cfgNewPassword').value;
      const confirm_password = document.getElementById('cfgConfirmNewPassword').value;

      if (!new_password || new_password.length < 6) {
        showProfileAlert('Kata sandi baru minimal 6 karakter.', false);
        return;
      }
      if (new_password !== confirm_password) {
        showProfileAlert('Konfirmasi kata sandi baru tidak cocok.', false);
        return;
      }

      if (btn) { btn.disabled = true; btn.textContent = 'Menyimpan...'; }
      try {
        const res = await fetch('/api/user/password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_password, new_password, confirm_password })
        });
        const data = await res.json();
        if (data.success) {
          showProfileAlert(data.message || 'Kata sandi berhasil diperbarui!', true);
          showToast('Password berhasil diperbarui', 'success');
          document.getElementById('cfgOldPassword').value = '';
          document.getElementById('cfgNewPassword').value = '';
          document.getElementById('cfgConfirmNewPassword').value = '';
          const oldPassNote = document.getElementById('cfgOldPassNote');
          if (oldPassNote) {
            oldPassNote.textContent = 'Masukkan kata sandi lama Anda untuk verifikasi keamanan.';
            oldPassNote.style.color = '#71717a';
          }
        } else {
          showProfileAlert(data.error || 'Gagal mengubah kata sandi.', false);
        }
      } catch (err) {
        showProfileAlert('Kesalahan: ' + err.message, false);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔑 Perbarui Kata Sandi'; }
      }
    }

    function showSpectrumConfigAlert(msg, isSuccess = true) {
      showProfileAlert(msg, isSuccess);
    }

    async function saveSpectrumUserConfig() {
      const btn = document.getElementById('btnSaveSpecConfig');
      if (btn) { btn.disabled = true; btn.textContent = 'Menyimpan...'; }

      const spectrum_email = document.getElementById('cfgSpecEmail').value.trim();
      const spectrum_password = document.getElementById('cfgSpecPassword').value;
      const project_id = document.getElementById('cfgSpecProjectId').value.trim() || '';
      const member_name = document.getElementById('cfgSpecMemberName').value.trim();
      const auto_sync = document.getElementById('cfgSpecAutoSync').checked;

      try {
        const res = await fetch('/api/user/spectrum/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ spectrum_email, spectrum_password, project_id, member_name, auto_sync })
        });
        const data = await res.json();
        if (data.success) {
          showSpectrumConfigAlert('Konfigurasi akun SPEctrum berhasil disimpan!', true);
          showToast('Pengaturan SPEctrum disimpan', 'success');
          await loadUserSpectrumConfig();
        } else {
          showSpectrumConfigAlert(data.error || 'Gagal menyimpan konfigurasi.', false);
        }
      } catch (err) {
        showSpectrumConfigAlert('Kesalahan: ' + err.message, false);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '💾 Simpan Konfigurasi'; }
      }
    }

    async function testSpectrumUserConnection() {
      const btn = document.getElementById('btnTestSpecConfig');
      if (btn) { btn.disabled = true; btn.textContent = 'Menguji...'; }

      const spectrum_email = document.getElementById('cfgSpecEmail').value.trim();
      const spectrum_password = document.getElementById('cfgSpecPassword').value;

      try {
        const res = await fetch('/api/user/spectrum/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ spectrum_email, spectrum_password })
        });
        const data = await res.json();
        if (data.success) {
          showSpectrumConfigAlert(data.message, true);
          showToast('Tes koneksi SPEctrum berhasil!', 'success');
          loadUserSpectrumConfig();
        } else {
          showSpectrumConfigAlert('Uji koneksi gagal: ' + (data.error || 'Autentikasi ditolak'), false);
          showToast('Uji koneksi SPEctrum gagal', 'error');
        }
      } catch (err) {
        showSpectrumConfigAlert('Gagal menguji koneksi: ' + err.message, false);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🧪 Uji Koneksi SPEctrum'; }
      }
    }

    async function syncSpectrumUserData() {
      const btn = document.getElementById('btnSyncSpectrumNow');
      const spinner = document.getElementById('cfgSyncSpinner');
      if (btn) { btn.disabled = true; }
      if (spinner) spinner.style.display = 'inline-block';

      try {
        const res = await fetch('/api/user/spectrum/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        if (data.success) {
          showSpectrumConfigAlert(data.message, true);
          showToast('Sinkronisasi SPEctrum selesai!', 'success');
          await loadUserSpectrumConfig();
          if (typeof loadSpectrumWorkspaceData === 'function') {
            loadSpectrumWorkspaceData();
          }
        } else {
          showSpectrumConfigAlert(data.error || 'Gagal melakukan sinkronisasi SPEctrum.', false);
          showToast('Gagal sinkronisasi SPEctrum', 'error');
        }
      } catch (err) {
        showSpectrumConfigAlert('Gagal sinkronisasi: ' + err.message, false);
      } finally {
        if (btn) { btn.disabled = false; }
        if (spinner) spinner.style.display = 'none';
      }
    }

    // Send AI prompt to Hermes with conversational bubbles & markdown formatting
    async function sendAiPrompt() {
      const input = document.getElementById('aiInputPrompt');
      const prompt = input ? input.value.trim() : '';
      if (!prompt) return;

      const bubbles = document.getElementById('aiChatBubbles');
      const now = new Date();
      const timeStr = ('0' + now.getHours()).slice(-2) + ':' + ('0' + now.getMinutes()).slice(-2);

      if (bubbles) {
        const userBubble = document.createElement('div');
        userBubble.className = 'chat-bubble chat-bubble-user';
        userBubble.innerHTML = '<div>' + escapeHtml(prompt) + '</div><div class="chat-bubble-meta"><span>' + timeStr + '</span></div>';
        bubbles.appendChild(userBubble);

        const loadingBubble = document.createElement('div');
        loadingBubble.className = 'chat-bubble chat-bubble-ai';
        loadingBubble.id = 'aiChatLoadingBubble';
        loadingBubble.innerHTML = '<div style="display:flex;align-items:center;gap:0.5rem;color:#94a3b8;"><span style="animation:blink 1s infinite;">●</span> <i>Menyiapkan respons...</i></div>';
        bubbles.appendChild(loadingBubble);
        bubbles.scrollTop = bubbles.scrollHeight;
      }
      if (input) input.value = '';

      const btn = document.getElementById('btnSendAiPrompt');
      if (btn) btn.disabled = true;

      try {
        const selectedModel = document.getElementById('hermesModelSelect')?.value || undefined;
        if (IS_ARES_OWNER && document.getElementById('chkAgentMode')?.checked) {
          await runAgentPrompt(prompt, selectedModel, bubbles, timeStr);
          return;
        }
        const res = await fetch('/api/hermes/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, model: selectedModel })
        });
        const data = await res.json();
        document.getElementById('aiChatLoadingBubble')?.remove();

        if (bubbles) {
          const replyText = data.reply || '';
          const replyHtml = (typeof marked !== 'undefined' && marked.parse) ? marked.parse(replyText) : escapeHtml(replyText);
          const aiBubble = document.createElement('div');
          aiBubble.className = 'chat-bubble chat-bubble-ai';
          const modelBadge = data.model ? ('<span class="hermes-badge badge-blue" style="font-size:0.6rem;padding:0.05rem 0.35rem;">' + escapeHtml(data.model) + '</span>') : '';
          aiBubble.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.4rem;">
              <div style="display:flex;align-items:center;gap:0.4rem;font-weight:600;color:#38bdf8;font-size:0.78rem;">
                <span>Hermes</span>${modelBadge}
              </div>
              <button onclick="copyChatReply(this)" style="background:transparent; border:1px solid #2d2d35; color:#94a3b8; font-size:0.68rem; padding:0.15rem 0.45rem; border-radius:4px; cursor:pointer;" title="Salin jawaban">Salin</button>
            </div>
            <div class="chat-reply-content">${replyHtml}</div>
            <div class="chat-bubble-meta"><span>${timeStr}</span></div>
          `;
          bubbles.appendChild(aiBubble);
          bubbles.scrollTop = bubbles.scrollHeight;
        }
        fetchHermesStatus();
      } catch (err) {
        document.getElementById('aiChatLoadingBubble')?.remove();
        if (bubbles) {
          const errBubble = document.createElement('div');
          errBubble.className = 'chat-bubble chat-bubble-ai';
          errBubble.style.borderColor = 'rgba(239,68,68,0.4)';
          errBubble.innerHTML = '<div style="color:#f87171;"><b>Gagal menghubungi konsol:</b> ' + escapeHtml(err.message || 'Server offline') + '</div>';
          bubbles.appendChild(errBubble);
          bubbles.scrollTop = bubbles.scrollHeight;
        }
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    // ==================== HERMES MODE AGEN (alat terminal terbatas) ====================
    function agentBubble(html, borderColor) {
      const b = document.createElement('div');
      b.className = 'chat-bubble chat-bubble-ai';
      if (borderColor) b.style.borderColor = borderColor;
      b.innerHTML = html;
      return b;
    }

    async function runAgentPrompt(prompt, model, bubbles, timeStr) {
      const res = await fetch('/api/hermes/agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, model })
      });
      const data = await res.json();
      document.getElementById('aiChatLoadingBubble')?.remove();
      if (!bubbles) return;

      if (Array.isArray(data.steps) && data.steps.length) {
        const rows = data.steps.map(function (st) {
          return '<details style="margin:0.2rem 0;"><summary style="cursor:pointer; color:' + (st.ok ? '#34d399' : '#f87171') + '; font-size:0.74rem; font-family:monospace;">' +
            (st.ok ? '✓ ' : '✗ ') + escapeHtml(st.tool) + ' ' + escapeHtml(JSON.stringify(st.args || {})) + '</summary>' +
            '<pre style="white-space:pre-wrap; max-height:220px; overflow:auto; background:#0b0c0e; border:1px solid #27272a; border-radius:6px; padding:0.5rem; font-size:0.7rem; color:#cbd5e1; margin:0.25rem 0;">' + escapeHtml(st.output || '') + '</pre></details>';
        }).join('');
        bubbles.appendChild(agentBubble('<div style="font-weight:600; color:#a1a1aa; font-size:0.72rem; margin-bottom:0.25rem;">🔧 Langkah yang dijalankan Hermes</div>' + rows));
      }

      const replyText = data.reply || data.error || '';
      const replyHtml = (typeof marked !== 'undefined' && marked.parse) ? marked.parse(replyText) : escapeHtml(replyText);
      bubbles.appendChild(agentBubble(
        '<div style="display:flex; align-items:center; gap:0.4rem; font-weight:600; color:#38bdf8; font-size:0.78rem; margin-bottom:0.4rem;"><span>Hermes</span>' +
        '<span class="hermes-badge badge-ok" style="font-size:0.6rem; padding:0.05rem 0.35rem;">AGEN</span></div>' +
        '<div class="chat-reply-content">' + replyHtml + '</div><div class="chat-bubble-meta"><span>' + timeStr + '</span></div>',
        data.error ? 'rgba(239,68,68,0.4)' : ''));

      (data.pending || []).forEach(function (p) {
        bubbles.appendChild(agentBubble(
          '<div style="font-weight:600; color:#fbbf24; font-size:0.78rem; margin-bottom:0.3rem;">⚠️ Menunggu persetujuan Anda</div>' +
          '<div style="font-size:0.78rem; color:#e4e4e7;"><code>' + escapeHtml(p.tool) + ' ' + escapeHtml(JSON.stringify(p.args || {})) + '</code></div>' +
          '<div style="font-size:0.72rem; color:#94a3b8; margin:0.25rem 0 0.5rem;">' + escapeHtml(p.description || '') + '</div>' +
          '<div style="display:flex; gap:0.5rem;"><button data-id="' + p.id + '" onclick="confirmAgentAction(this.dataset.id, this)" style="background:#059669; color:#fff; border:none; border-radius:5px; padding:0.3rem 0.8rem; font-size:0.74rem; font-weight:600; cursor:pointer;">Jalankan</button>' +
          '<button data-id="' + p.id + '" onclick="cancelAgentAction(this.dataset.id, this)" style="background:#202026; color:#cbd5e1; border:1px solid #353540; border-radius:5px; padding:0.3rem 0.8rem; font-size:0.74rem; cursor:pointer;">Batal</button></div>',
          'rgba(245,158,11,0.4)'));
      });
      bubbles.scrollTop = bubbles.scrollHeight;
    }

    async function confirmAgentAction(id, btn) {
      const box = btn.closest('.chat-bubble');
      const toolLabel = (box.querySelector('code')?.textContent || 'aksi').trim();
      const t0 = Date.now();
      const tick = function () {
        box.innerHTML = '<div style="color:#fbbf24; font-weight:600; font-size:0.78rem;"><span style="animation:blink 1s infinite;">●</span> Menjalankan ' + escapeHtml(toolLabel) + '... ' + Math.floor((Date.now() - t0) / 1000) + ' dtk</div>' +
          '<div style="font-size:0.7rem; color:#71717a; margin-top:0.25rem;">Scan jaringan bisa memakan waktu hingga ~1-2 menit. Jangan tutup halaman.</div>';
      };
      tick();
      const timer = setInterval(tick, 1000);
      try {
        const res = await fetch('/api/hermes/agent/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) });
        const data = await res.json();
        box.innerHTML = data.success
          ? '<div style="color:#34d399; font-weight:600; font-size:0.78rem;">✓ Dijalankan: ' + escapeHtml(data.tool) + '</div><pre style="white-space:pre-wrap; max-height:260px; overflow:auto; background:#0b0c0e; border:1px solid #27272a; border-radius:6px; padding:0.5rem; font-size:0.7rem; color:#cbd5e1; margin-top:0.3rem;">' + escapeHtml(data.output || '') + '</pre>'
          : '<div style="color:#f87171; font-size:0.78rem;">✗ ' + escapeHtml(data.error || 'Gagal menjalankan aksi') + '</div>';
      } catch (e) {
        box.innerHTML = '<div style="color:#f87171; font-size:0.78rem;">✗ ' + escapeHtml(e.message) + '</div>';
      } finally {
        clearInterval(timer);
      }
    }

    async function cancelAgentAction(id, btn) {
      const box = btn.closest('.chat-bubble');
      try { await fetch('/api/hermes/agent/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) }); } catch (e) {}
      box.innerHTML = '<div style="color:#94a3b8; font-size:0.78rem;">Aksi dibatalkan.</div>';
    }

    // ==================== UNGGAH REKAMAN / ANALISA AUDIO (file utuh ke AI) ====================
    window._lastMeetingAudio = null;  // {blob, name} dari unggahan atau rekaman terakhir

    // Bahasa: 'mix' = Indonesia + istilah teknis Inggris. Browser (Web Speech) hanya satu bahasa -> id-ID.
    function meetingLangForBrowser() {
      const v = document.getElementById('meetingLangSelect')?.value || 'mix';
      return v === 'mix' ? 'id-ID' : v;
    }

    // Task yang relevan dengan meeting: fokus ke proyek yang disebut di judul, task milik user di depan
    function meetingRelevantTasks() {
      const d = window._spectrumData;
      if (!d || !d.tasks) return [];
      const title = (document.getElementById('meetingTitleInput')?.value || '').toLowerCase();
      let pool = d.tasks;
      const byProject = pool.filter(function (t) {
        const tokens = String(t.project_name || (d.active_project && d.active_project.name) || '').toLowerCase().split(/[^a-z0-9]+/).filter(function (w) { return w.length >= 3; });
        return tokens.some(function (tok) { return title.includes(tok); });
      });
      if (byProject.length && byProject.length < pool.length) pool = byProject;
      const mine = pool.filter(function (t) { return isMyTask(t); });
      const others = pool.filter(function (t) { return !mine.includes(t) && t.status !== 'Task-Finished'; });
      return mine.concat(others);
    }

    function buildMeetingContext(maxChars) {
      const d = window._spectrumData;
      if (!d || !d.tasks) return '';
      const sprint = d.active_sprint || {};
      const proj = (d.active_project && d.active_project.name) || '';
      const lines = ['Sprint: ' + (sprint.name || '-') + (proj ? ' (Proyek: ' + proj + ')' : '')];
      if (sprint.description) lines.push('Target: ' + String(sprint.description).substring(0, 200));
      lines.push('Task terkait (tanda * = task pengguna):');
      meetingRelevantTasks().slice(0, 25).forEach(function (t) {
        lines.push('- ' + (isMyTask(t) ? '*' : '') + '[' + (t.code || '-') + '] ' + t.name + ' (' + (t.status || 'To-do') + ', ' + (t.assignee || '-') + ')');
      });
      if ((document.getElementById('meetingLangSelect')?.value || 'mix') === 'mix') {
        lines.push('Bahasa rapat: campuran Indonesia dan Inggris; pertahankan istilah teknis Inggris apa adanya.');
      }
      return lines.join(String.fromCharCode(10)).substring(0, maxChars || 3000);
    }

    function renderMeetingContext() {
      const box = document.getElementById('meetingContextTasks');
      const cnt = document.getElementById('meetingContextCount');
      const tasks = meetingRelevantTasks();
      if (cnt) cnt.textContent = tasks.length + ' task';
      if (box) {
        box.innerHTML = tasks.length ? tasks.slice(0, 12).map(function (t) {
          const mine = isMyTask(t);
          return '<span title="' + escapeHtml(t.name || '') + '" style="font-size:0.68rem; padding:0.12rem 0.5rem; border-radius:10px; background:' + (mine ? 'rgba(245,158,11,0.14)' : 'rgba(255,255,255,0.05)') + '; color:' + (mine ? '#fbbf24' : '#a1a1aa') + '; border:1px solid ' + (mine ? 'rgba(245,158,11,0.35)' : '#2d2d35') + ';">' + escapeHtml(t.code || '-') + ' · ' + escapeHtml(String(t.name || '').substring(0, 34)) + '</span>';
        }).join('') + (tasks.length > 12 ? '<span style="font-size:0.68rem; color:#71717a;">+' + (tasks.length - 12) + ' lainnya</span>' : '')
          : '<span style="font-size:0.72rem; color:#71717a;">Belum ada data task. Hubungkan akun SPEctrum di menu Profil.</span>';
      }
      const own = document.getElementById('meetingOwnerTcTools');
      if (own) own.style.display = IS_ARES_OWNER ? 'flex' : 'none';
    }

    function setUploadStatus(text, color) {
      const el = document.getElementById('uploadTranscribeStatus');
      if (el) { el.textContent = text; el.style.color = color || '#38bdf8'; }
    }

    async function postAudioJob(blob, name, extraHeaders) {
      if (blob.size > 120 * 1024 * 1024) throw new Error('File terlalu besar (maks 120 MB). Kompres ke MP3/M4A dulu.');
      const lang = document.getElementById('meetingLangSelect')?.value || 'id-ID';
      const headers = Object.assign({ 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(name), 'X-Language': lang, 'X-Engine': 'auto' }, extraHeaders || {});
      const up = await fetch('/api/meetings/transcribe-file', { method: 'POST', headers, body: blob });
      const data = await up.json();
      if (!data.success) throw new Error(data.error || 'Unggah gagal');
      return data.job_id;
    }

    async function waitAudioJob(jobId) {
      let job = null;
      for (let i = 0; i < 1800; i++) {  // maks ~1 jam menunggu
        await new Promise(function (r) { setTimeout(r, 2000); });
        const r = await fetch('/api/meetings/transcribe-file/' + jobId);
        job = await r.json();
        const prog = job.total ? (' ' + (job.done || 0) + '/' + job.total) : '';
        setUploadStatus((job.message || 'Memproses...') + prog + (job.engine ? ' [' + job.engine + ']' : ''));
        if (job.status === 'done' || job.status === 'error') break;
      }
      if (!job || job.status !== 'done') throw new Error((job && job.error) || 'Proses gagal atau terlalu lama');
      return job;
    }

    function meetingElapsedSeconds() {
      return (typeof meetingStartTime !== 'undefined' && meetingStartTime) ? Math.floor((Date.now() - meetingStartTime) / 1000) : 0;
    }

    async function uploadMeetingAudio(input) {
      const file = input.files && input.files[0];
      input.value = '';
      if (!file) return;
      window._lastMeetingAudio = { blob: file, name: file.name };
      const basis = document.getElementById('meetingAnalysisBasis')?.value || 'transcript';
      const btn = document.getElementById('btnUploadMeetingAudio');
      if (basis === 'audio') {
        setUploadStatus('Rekaman siap: ' + file.name + '. Menganalisa langsung dari audio...');
        analyzeMeeting();
        return;
      }
      if (btn) btn.disabled = true;
      try {
        setUploadStatus('Mengunggah ' + file.name + ' (' + (file.size / 1048576).toFixed(1) + ' MB)...');
        const job = await waitAudioJob(await postAudioJob(file, file.name));
        const ta = document.getElementById('meetingTranscriptText');
        if (ta) ta.value = (ta.value.trim() ? ta.value.trim() + String.fromCharCode(10, 10) : '') + job.text;
        setUploadStatus('Selesai (' + (job.engine || 'AI') + '). Transkrip ditambahkan; klik Analisa & Susun Notulen.', '#34d399');
        showToast('Transkrip dari rekaman berhasil dibuat', 'success');
        if (document.getElementById('chkAutoPolishTranscript')?.checked && typeof polishMeetingTranscript === 'function') {
          await polishMeetingTranscript();
        }
      } catch (e) {
        setUploadStatus('Gagal: ' + e.message, '#f87171');
        showToast('Gagal transkripsi rekaman: ' + e.message, 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function renderMeetingSummary(markdown) {
      const outBox = document.getElementById('meetingSummaryOutput');
      currentMeetingSummaryMarkdown = markdown;
      if (outBox) outBox.innerHTML = window.marked ? marked.parse(markdown) : escapeHtml(markdown);
    }

    function showMeetingBusy(title, sub, color) {
      const outBox = document.getElementById('meetingSummaryOutput');
      if (outBox) outBox.innerHTML = '<div style="text-align:center; margin-top:5rem; color:' + (color || '#94a3b8') + ';"><div style="font-weight:600;">' + title + '</div><div style="font-size:0.75rem; color:var(--gf-text-muted); margin-top:0.25rem;">' + sub + '</div></div>';
    }

    // Analisa dari TEKS transkrip (alur lama, dipakai juga saat selesai merekam)
    async function summarizeCurrentTranscript(title, transcript, elapsed) {
      showMeetingBusy('Menganalisis transkrip & menyusun notulen...', 'Memproses ringkasan, poin keputusan & rencana tindak lanjut...');
      const outBox = document.getElementById('meetingSummaryOutput');
      try {
        const selectedModel = document.getElementById('meetingModelSelect')?.value || undefined;
        const res = await fetch('/api/meetings/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, transcript, duration: elapsed, model: selectedModel, context: buildMeetingContext(3500) })
        });
        const data = await res.json();
        if (!data.summary) throw new Error(data.error || 'Ringkasan kosong');
        renderMeetingSummary(data.summary);
        loadPastMeetings();
      } catch (err) {
        if (outBox) outBox.innerHTML = '<div style="color: #f87171;">Gagal merangkum meeting: ' + escapeHtml(err.message) + '</div>';
      }
    }

    // Analisa langsung dari AUDIO (tanpa transkrip perantara)
    async function runAudioAnalysis(blob, name) {
      const title = document.getElementById('meetingTitleInput')?.value.trim() || 'Meeting Koordinasi';
      const elapsed = meetingElapsedSeconds();
      const btn = document.getElementById('btnAnalyzeMeeting');
      if (btn) btn.disabled = true;
      showMeetingBusy('Menganalisis rekaman audio langsung...', 'Rekaman dikirim utuh ke AI multimodal; ini bisa beberapa menit untuk rapat panjang.', '#c084fc');
      try {
        setUploadStatus('Mengunggah ' + name + ' (' + (blob.size / 1048576).toFixed(1) + ' MB)...');
        const jobId = await postAudioJob(blob, name, { 'X-Mode': 'analyze', 'X-Title': encodeURIComponent(title), 'X-Context': encodeURIComponent(buildMeetingContext(1200)) });
        const job = await waitAudioJob(jobId);
        renderMeetingSummary(job.summary);
        setUploadStatus('Analisa audio selesai.', '#34d399');
        const transcript = document.getElementById('meetingTranscriptText')?.value.trim() || '';
        await fetch('/api/meetings/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, transcript, summary: job.summary, duration: elapsed }) });
        loadPastMeetings();
      } catch (e) {
        setUploadStatus('Analisa audio gagal: ' + e.message, '#f87171');
        const transcript = document.getElementById('meetingTranscriptText')?.value.trim() || '';
        if (transcript) {
          showToast('Analisa audio gagal, memakai transkrip yang ada', 'error');
          await summarizeCurrentTranscript(title, transcript, elapsed);
        } else {
          // Tidak ada transkrip: transkripsikan audionya dulu lalu analisa dari teks
          try {
            showMeetingBusy('Analisa audio langsung belum tersedia, mentranskripsi dulu...', 'Memakai jalur transkrip sebagai cadangan.', '#c084fc');
            const tJob = await waitAudioJob(await postAudioJob(blob, name));
            const ta = document.getElementById('meetingTranscriptText');
            if (ta) ta.value = tJob.text;
            await summarizeCurrentTranscript(title, tJob.text, elapsed);
          } catch (e2) {
            const outBox = document.getElementById('meetingSummaryOutput');
            if (outBox) outBox.innerHTML = '<div style="color:#f87171;">Gagal menganalisa rekaman: ' + escapeHtml(e2.message) + '</div>';
          }
        }
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function analyzeMeeting() {
      const basis = document.getElementById('meetingAnalysisBasis')?.value || 'transcript';
      if (basis === 'audio') {
        const a = window._lastMeetingAudio;
        if (!a || !a.blob || !a.blob.size) {
          showToast('Belum ada rekaman. Unggah file atau rekam dulu.', 'error');
          return;
        }
        await runAudioAnalysis(a.blob, a.name);
        return;
      }
      const transcript = document.getElementById('meetingTranscriptText')?.value.trim() || '';
      if (!transcript) {
        showToast('Transkrip masih kosong. Rekam, unggah rekaman, atau ketik catatan dulu.', 'error');
        return;
      }
      const title = document.getElementById('meetingTitleInput')?.value.trim() || 'Meeting Koordinasi';
      await summarizeCurrentTranscript(title, transcript, meetingElapsedSeconds());
    }

    // ==================== ANDROID REMOTE CONTROL & APPIUM ====================
    window._androidDevices = [];
    window._activeDeviceSerial = null;
    window._screenStreamTimer = null;
    window._swipeStartPos = null;

    async function fetchTailscaleStatus() {
      try {
        const res = await fetch('/api/tailscale/status');
        if (!res.ok) return;
        const data = await res.json();
        const textEl = document.getElementById('tailscaleStatusText');
        const boxEl = document.getElementById('tailscaleAuthBox');
        const linkEl = document.getElementById('tailscaleAuthLink');
        if (!textEl) return;

        if (data.is_logged_in && data.ips && data.ips.length) {
          textEl.style.color = '#34d399';
          textEl.textContent = `ONLINE (${data.ips[0]})`;
          if (boxEl) boxEl.style.display = 'none';
        } else if (data.auth_url) {
          textEl.style.color = '#fbbf24';
          textEl.textContent = 'Perlu Login';
          if (boxEl) {
            boxEl.style.display = 'block';
            if (linkEl) linkEl.href = data.auth_url;
          }
        } else {
          textEl.style.color = '#94a3b8';
          textEl.textContent = data.state || 'Inactive';
        }
      } catch (e) {
        console.error('Gagal fetch tailscale status:', e);
      }
    }

    async function fetchAppiumDevices() {
      if (IS_ARES_OWNER) fetchTailscaleStatus();
      try {
        const res = await fetch('/api/appium/devices');
        if (!res.ok) return;
        const data = await res.json();
        // Non-owner hanya melihat perangkat yang disewanya; perangkat kosong cukup dihitung
        window._androidMeta = { my: data.my_serial || null, free: data.free_count || 0, owner: !!data.is_owner };
        if (!window._androidMeta.owner) {
          data.devices = (data.devices || []).filter(function (d) { return d.lease && d.lease.status === 'mine'; });
          if (window._androidMeta.my) window._activeDeviceSerial = window._androidMeta.my;
        }
        window._androidDevices = data.devices || [];
        renderAndroidLeaseBar();
        if (!window._androidMeta.owner && !window._androidMeta.my && window._androidMeta.free > 0 && !window._androidAutoClaimTried) {
          window._androidAutoClaimTried = true;
          claimAndroidDevice(true);
        }
        
        const badge = document.getElementById('appiumBadge');
        if (badge) {
          if (data.appium && data.appium.ready) {
            badge.className = 'hermes-badge badge-ok';
            badge.textContent = `ONLINE (v${data.appium.version || '3.8.0'})`;
          } else {
            badge.className = 'hermes-badge badge-warn';
            badge.textContent = 'OFFLINE';
          }
        }

        const devList = document.getElementById('appiumDevicesList');
        if (!data.devices || !data.devices.length) {
          if (!window._androidMeta.owner) {
            devList.innerHTML = '<div style="color:var(--gf-text-muted); font-size:0.82rem; padding:0.75rem;">' + (window._androidMeta.free > 0 ? 'Klik <b>Ambil perangkat kosong</b> untuk mendapat perangkat.' : 'Belum ada perangkat kosong. Semua perangkat sedang dipakai atau belum tersambung ke server; coba lagi nanti.') + '</div>';
            const sw = document.getElementById('ctrlDeviceSwitcher'); if (sw) sw.style.display = 'none';
            return;
          }
          devList.innerHTML = '<div style="color:var(--gf-text-muted); font-size:0.8rem; padding:0.5rem;">Tidak ada perangkat Android terdeteksi lewat USB maupun Wi-Fi.<br>Gunakan kabel USB atau klik <b>Auto-Scan & Sambungkan ADB Wi-Fi</b> di bawah.</div>';
          const switcher = document.getElementById('ctrlDeviceSwitcher');
          if (switcher) switcher.style.display = 'none';
          return;
        }

        // Update ctrlDeviceSwitcher in remote control header
        const switcher = document.getElementById('ctrlDeviceSwitcher');
        if (switcher) {
          const devs = data.devices.filter(d => d.state === 'device');
          if (devs.length > 1) {
            switcher.style.display = 'inline-block';
            switcher.innerHTML = devs.map(d => `
              <option value="${d.serial}" ${d.serial === window._activeDeviceSerial ? 'selected' : ''}>
                ${d.model} (${d.serial.includes(':') ? 'Wi-Fi' : 'USB'}: ${d.serial})
              </option>
            `).join('');
          } else {
            switcher.style.display = 'none';
          }
        }

        devList.innerHTML = data.devices.map(d => {
          const isDev = d.state === 'device';
          const isSelected = window._activeDeviceSerial === d.serial;
          const isWifi = d.serial && d.serial.includes(':');
          const bat = d.battery !== null ? `Baterai: ${d.battery}%` : 'Baterai: --%';
          const ipText = d.wifi_ip ? `IP: <b>${d.wifi_ip}</b>` : '';
          const resText = (d.width && d.height) ? `${d.width}x${d.height}` : '';
          const stateBadge = isDev 
            ? `<span class="hermes-badge badge-ok" style="font-size:0.7rem;">TERHUBUNG</span>`
            : `<span class="hermes-badge badge-warn" style="font-size:0.7rem;">${d.state.toUpperCase()}</span>`;
          const connBadge = isWifi
            ? `<span style="background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3); font-size:0.65rem; padding:0.12rem 0.45rem; border-radius:4px; font-weight:700;">Wi-Fi ADB</span>`
            : `<span style="background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(59,130,246,0.3); font-size:0.65rem; padding:0.12rem 0.45rem; border-radius:4px; font-weight:700;">USB KABEL</span>`;

          const selectBtn = isDev ? `
            <button onclick="selectAndroidDevice('${d.serial}')" style="background:${isSelected ? '#2563eb' : '#202026'}; color:${isSelected ? '#fff' : '#38bdf8'}; border:1px solid ${isSelected ? '#2563eb' : '#353540'}; border-radius:4px; padding:0.25rem 0.65rem; font-size:0.72rem; font-weight:600; cursor:pointer;" title="Pilih perangkat ini untuk diremote">
              ${isSelected ? 'Sedang Dikontrol' : 'Pilih Kontrol'}
            </button>
          ` : '';

          const disconnectBtn = isWifi ? `
            <button onclick="disconnectSpecificDevice('${d.serial}')" style="background:#202026; color:#ef4444; border:1px solid #7f1d1d; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; font-weight:600; cursor:pointer;" title="Putuskan koneksi ADB Wi-Fi">Putuskan Wi-Fi</button>
          ` : '';

          const actionButtons = `
            <div style="display:flex; gap:0.35rem; margin-top:0.45rem; flex-wrap:wrap; align-items:center;">
              ${selectBtn}
              ${isDev ? `
                <button onclick="triggerDeviceAction('${d.serial}', 'wake')" style="background:#202026; color:#ef4444; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; font-weight:600; cursor:pointer;" title="Toggle Power / Wake Layar">Power</button>
                <button onclick="enableWirelessAdb('${d.serial}', '${d.wifi_ip || ''}')" style="background:#202026; color:#34d399; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; font-weight:600; cursor:pointer;" title="Buka port ADB TCP 5555 untuk koneksi Wi-Fi">Aktifkan TCP 5555</button>
                <button onclick="triggerDeviceAction('${d.serial}', 'battery')" style="background:#202026; color:#fbbf24; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; font-weight:600; cursor:pointer;" title="Info Diagnostik Baterai">Status Baterai</button>
              ` : ''}
              ${disconnectBtn}
            </div>
          `;

          return `
            <div style="background:#131317; border:1px solid ${isSelected ? '#2563eb' : '#222228'}; border-radius:6px; padding:0.6rem 0.85rem; display:flex; justify-content:space-between; align-items:flex-start; transition:border-color 0.2s;">
              <div style="flex:1;">
                <div style="font-weight:600; color:#fff; font-size:0.85rem; display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
                  <span>${d.model}</span>
                  <span style="font-size:0.72rem; color:var(--gf-text-dim);">(${d.product || 'Android'})</span>
                  ${connBadge}
                  ${leaseBadge(d)}
                </div>
                <div style="font-size:0.72rem; color:var(--gf-text-dim); font-family:'JetBrains Mono', monospace; margin-top:0.25rem; display:flex; gap:0.65rem; flex-wrap:wrap;">
                  <span>Serial: <b style="color:#f1f5f9;">${d.serial}</b></span>
                  <span>${bat}</span>
                  ${resText ? `<span>${resText}</span>` : ''}
                  ${ipText ? `<span>${ipText}</span>` : ''}
                </div>
                ${actionButtons}
              </div>
              <div>${stateBadge}</div>
            </div>
          `;
        }).join('');

        // Jika belum ada device terpilih, atau device lama sudah tidak ada, pilih device aktif pertama
        const activeDevs = data.devices.filter(d => d.state === 'device');
        if (activeDevs.length > 0) {
          if (!window._activeDeviceSerial || !activeDevs.some(d => d.serial === window._activeDeviceSerial)) {
            selectAndroidDevice(activeDevs[0].serial, false);
          }
        }
      } catch (e) {
        console.error('Gagal fetch appium devices:', e);
      }
    }

    function leaseBadge(d) {
      const l = d.lease || {};
      if (l.status === 'mine') return '<span class="lease-chip lease-mine">PERANGKAT ANDA</span>';
      if (l.status === 'busy') return '<span class="lease-chip lease-busy">DIPAKAI ' + escapeHtml(String(l.by || 'user lain').toUpperCase()) + '</span>';
      if (l.status === 'free') return '<span class="lease-chip lease-free">KOSONG</span>';
      return '';
    }

    function renderAndroidLeaseBar() {
      const m = window._androidMeta || {};
      const title = document.getElementById('androidLeaseTitle');
      const sub = document.getElementById('androidLeaseSub');
      const claim = document.getElementById('btnClaimDevice');
      const rel = document.getElementById('btnReleaseDevice');
      const caps = document.getElementById('btnAppiumCaps');
      if (!title) return;
      const mine = (window._androidDevices || []).find(function (d) { return d.serial === m.my; });
      if (m.my) {
        title.textContent = '📱 Perangkat Anda: ' + (mine ? mine.model : m.my);
        sub.textContent = 'Serial ' + m.my + ' • hanya Anda yang bisa mengontrolnya • otomatis dilepas bila tidak aktif 30 menit • ' + m.free + ' perangkat lain kosong';
      } else {
        title.textContent = m.free > 0 ? 'Belum memegang perangkat' : 'Tidak ada perangkat kosong';
        sub.textContent = m.owner ? 'Sebagai pemilik Anda bisa mengontrol semua perangkat. ' + m.free + ' perangkat kosong; klik Ambil bila ingin menandai satu untuk Anda.' : (m.free > 0 ? m.free + ' perangkat kosong tersedia. Anda akan diarahkan otomatis ke yang kosong.' : 'Semua perangkat sedang dipakai atau belum tersambung ke server.');
      }
      if (claim) claim.style.display = (m.my && !m.owner) ? 'none' : (m.free > 0 ? 'inline-block' : 'none');
      if (rel) rel.style.display = m.my ? 'inline-block' : 'none';
      if (caps) caps.style.display = m.my ? 'inline-block' : 'none';
    }

    async function claimAndroidDevice(silent) {
      try {
        const res = await fetch('/api/android/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const data = await res.json();
        if (data.success) {
          if (!silent || !data.reused) showToast('Perangkat ' + data.serial + ' ditetapkan untuk Anda', 'success');
          window._activeDeviceSerial = data.serial;
        } else if (!silent) {
          showToast(data.error || 'Tidak ada perangkat kosong', 'error');
        }
      } catch (e) {
        if (!silent) showToast('Gagal mengambil perangkat: ' + e.message, 'error');
      }
      fetchAppiumDevices();
    }

    async function releaseAndroidDevice() {
      if (!confirm('Lepas perangkat ini agar bisa dipakai user lain?')) return;
      try {
        await fetch('/api/android/release', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        window._activeDeviceSerial = null;
        window._androidAutoClaimTried = true;  // jangan langsung diambil lagi
        const chk = document.getElementById('chkAutoStream');
        if (chk && chk.checked) { chk.checked = false; if (typeof toggleScreenAutoStream === 'function') toggleScreenAutoStream(); }
        showToast('Perangkat dilepas', 'success');
      } catch (e) {
        showToast('Gagal melepas: ' + e.message, 'error');
      }
      fetchAppiumDevices();
    }

    async function showAppiumCaps() {
      const box = document.getElementById('appiumCapsBox');
      if (!box) return;
      if (box.style.display === 'block') { box.style.display = 'none'; return; }
      try {
        const res = await fetch('/api/android/appium-caps');
        const data = await res.json();
        box.textContent = data.success
          ? JSON.stringify(data.capabilities, null, 2) + String.fromCharCode(10, 10) + '// ' + data.note
          : (data.error || 'Gagal memuat capabilities');
        box.style.display = 'block';
      } catch (e) {
        showToast('Gagal memuat Appium caps', 'error');
      }
    }

    function selectAndroidDevice(serial, reloadDevices = true) {
      window._activeDeviceSerial = serial;
      const dev = (window._androidDevices || []).find(d => d.serial === serial);
      if (!dev) return;

      const titleEl = document.getElementById('ctrlActiveDeviceTitle');
      const resEl = document.getElementById('ctrlActiveDeviceRes');
      const switcher = document.getElementById('ctrlDeviceSwitcher');
      if (titleEl) titleEl.textContent = `Remote: ${dev.model} (${dev.serial.includes(':') ? 'Wi-Fi' : 'USB'})`;
      if (resEl) resEl.textContent = `${dev.width || 1080} x ${dev.height || 2400}`;
      if (switcher && switcher.value !== serial) switcher.value = serial;

      if (reloadDevices) {
        fetchAppiumDevices();
      }
      refreshDeviceScreen();
      initScreenInteractiveListeners();
    }

    function changeScreenQuality(quality) {
      window._androidScreenQuality = quality;
      try { localStorage.setItem('android_screen_quality', quality); } catch(e){}
      const sel = document.getElementById('selScreenQuality');
      if (sel) sel.value = quality;
      const fsSel = document.getElementById('fsScreenQuality');
      if (fsSel) fsSel.value = quality;
      refreshDeviceScreen();
    }

    function refreshDeviceScreen() {
      if (!window._activeDeviceSerial) return;
      const img = document.getElementById('androidScreenImg');
      const fsImg = document.getElementById('fsAndroidScreenImg');
      const placeholder = document.getElementById('screenLoadingPlaceholder');
      if (!img && !fsImg) return;

      const quality = window._androidScreenQuality || localStorage.getItem('android_screen_quality') || 'low';
      const tStart = Date.now();
      const srcUrl = '/api/appium/screenshot?serial=' + encodeURIComponent(window._activeDeviceSerial) + '&quality=' + quality + '&t=' + tStart;

      const newImg = new Image();
      newImg.onload = function() {
        const elapsed = Date.now() - tStart;
        if (img) {
          img.src = newImg.src;
          img.style.display = 'block';
        }
        if (fsImg) {
          fsImg.src = newImg.src;
          fsImg.style.display = 'block';
        }
        if (placeholder) placeholder.style.display = 'none';

        const fpsBadge = document.getElementById('streamFpsBadge');
        if (fpsBadge) {
          fpsBadge.textContent = `${elapsed}ms`;
        }

        const chk = document.getElementById('chkAutoStream');
        if (chk && chk.checked) {
          if (window._screenStreamTimer) clearTimeout(window._screenStreamTimer);
          window._screenStreamTimer = setTimeout(refreshDeviceScreen, 130);
        }
      };
      newImg.onerror = function() {
        const chk = document.getElementById('chkAutoStream');
        if (chk && chk.checked) {
          if (window._screenStreamTimer) clearTimeout(window._screenStreamTimer);
          window._screenStreamTimer = setTimeout(refreshDeviceScreen, 700);
        }
      };
      newImg.src = srcUrl;
    }

    function toggleAndroidFullscreen() {
      const modal = document.getElementById('androidFullscreenModal');
      if (!modal) return;
      modal.style.display = 'flex';
      const dev = (window._androidDevices || []).find(d => d.serial === window._activeDeviceSerial);
      const title = document.getElementById('fsDeviceTitle');
      if (title) title.textContent = (dev ? `${dev.model || dev.serial} (${dev.serial})` : window._activeDeviceSerial);
      const res = document.getElementById('fsDeviceRes');
      if (res) res.textContent = (dev && dev.width ? `${dev.width}x${dev.height}` : '-- x --');
      
      const q = window._androidScreenQuality || localStorage.getItem('android_screen_quality') || 'low';
      const fsSel = document.getElementById('fsScreenQuality');
      if (fsSel) fsSel.value = q;
      
      refreshDeviceScreen();
      initFsScreenListeners();
      
      try {
        if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(()=>{});
        }
      } catch(e) {}
    }

    function exitAndroidFullscreen() {
      const modal = document.getElementById('androidFullscreenModal');
      if (modal) modal.style.display = 'none';
      try {
        if (document.fullscreenElement && document.exitFullscreen) {
          document.exitFullscreen().catch(()=>{});
        }
      } catch(e) {}
      refreshDeviceScreen();
    }

    window.addEventListener('keydown', function(e) {
      const fsModal = document.getElementById('androidFullscreenModal');
      if (fsModal && fsModal.style.display === 'flex') {
        if (e.key === 'Escape') {
          exitAndroidFullscreen();
        } else if (e.key === 'ArrowLeft') {
          triggerActiveDeviceAction('swipe_left');
        } else if (e.key === 'ArrowRight') {
          triggerActiveDeviceAction('swipe_right');
        } else if (e.key === 'ArrowUp') {
          triggerActiveDeviceAction('swipe_up');
        } else if (e.key === 'ArrowDown') {
          triggerActiveDeviceAction('swipe_down');
        } else if (e.key === 'Backspace') {
          triggerActiveDeviceAction('back');
        }
      }
    });

    function initFsScreenListeners() {
      const container = document.getElementById('fsScreenContainer');
      if (!container || container._hasFsListeners) return;
      container._hasFsListeners = true;

      container.addEventListener('mousedown', function(e) {
        window._fsSwipeStart = getFsScreenRelCoords(e, container);
      });

      container.addEventListener('mouseup', function(e) {
        if (!window._fsSwipeStart) return;
        const endPos = getFsScreenRelCoords(e, container);
        const startPos = window._fsSwipeStart;
        window._fsSwipeStart = null;

        const dx = endPos.x - startPos.x;
        const dy = endPos.y - startPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 25) {
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          showFsTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      });

      container.addEventListener('touchstart', function(e) {
        if (e.touches.length > 0) {
          window._fsSwipeStart = getFsScreenRelCoords(e.touches[0], container);
        }
      }, { passive: true });

      container.addEventListener('touchend', function(e) {
        if (!window._fsSwipeStart || e.changedTouches.length === 0) return;
        const endPos = getFsScreenRelCoords(e.changedTouches[0], container);
        const startPos = window._fsSwipeStart;
        window._fsSwipeStart = null;

        const dx = endPos.x - startPos.x;
        const dy = endPos.y - startPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 25) {
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          showFsTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      }, { passive: true });
    }

    function getFsScreenRelCoords(e, container) {
      const img = document.getElementById('fsAndroidScreenImg');
      const targetElem = img || container;
      const rect = targetElem.getBoundingClientRect();
      const elemX = e.clientX - rect.left;
      const elemY = e.clientY - rect.top;

      const normX = Math.max(0, Math.min(1, elemX / rect.width));
      const normY = Math.max(0, Math.min(1, elemY / rect.height));

      const dev = (window._androidDevices || []).find(d => d.serial === window._activeDeviceSerial);
      const devW = dev && dev.width ? dev.width : 1080;
      const devH = dev && dev.height ? dev.height : 2400;

      return { x: e.clientX, y: e.clientY, elemX, elemY, devX: Math.round(normX * devW), devY: Math.round(normY * devH) };
    }

    function showFsTapRipple(x, y) {
      const rip = document.getElementById('fsTapRipple');
      if (!rip) return;
      rip.style.left = x + 'px';
      rip.style.top = y + 'px';
      rip.style.transform = 'translate(-50%, -50%) scale(1.5)';
      rip.style.opacity = '0.9';
      setTimeout(() => {
        rip.style.transform = 'translate(-50%, -50%) scale(0)';
        rip.style.opacity = '0';
      }, 250);
    }

    function toggleScreenAutoStream() {
      const chk = document.getElementById('chkAutoStream');
      if (window._screenStreamTimer) {
        clearTimeout(window._screenStreamTimer);
        clearInterval(window._screenStreamTimer);
        window._screenStreamTimer = null;
      }
      if (chk && chk.checked) {
        refreshDeviceScreen();
      }
    }

    async function sendAndroidKey(keyVal) {
      if (!window._activeDeviceSerial) return;
      try {
        await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ 
            serial: window._activeDeviceSerial, 
            action: 'key', 
            key: keyVal,
            code: keyVal,
            cmd: String(keyVal),
            text: String(keyVal)
          })
        });
        setTimeout(refreshDeviceScreen, 60);
        setTimeout(refreshDeviceScreen, 200);
      } catch (err) {
        console.error('sendAndroidKey error:', err);
      }
    }

    async function sendAndroidDirectText(text) {
      if (!window._activeDeviceSerial || !text) return;
      try {
        await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'text', text: text })
        });
        setTimeout(refreshDeviceScreen, 80);
        setTimeout(refreshDeviceScreen, 220);
      } catch (err) {
        console.error('sendAndroidDirectText error:', err);
      }
    }

    function initScreenInteractiveListeners() {
      const container = document.getElementById('screenContainer');
      const bezel = document.getElementById('phoneBezel');
      const keyPill = document.getElementById('keyboardActivePill');
      const svg = document.getElementById('swipeTrailSvg');
      const line = document.getElementById('swipeTrailLine');
      const pStart = document.getElementById('swipeTrailStart');
      const pEnd = document.getElementById('swipeTrailEnd');

      if (!container || container._hasInteractiveListeners) return;
      container._hasInteractiveListeners = true;

      container.addEventListener('focus', function() {
        if (bezel) bezel.style.borderColor = '#0284c7';
        if (keyPill) keyPill.style.display = 'block';
      });
      container.addEventListener('blur', function() {
        if (bezel) bezel.style.borderColor = '#27272a';
        if (keyPill) keyPill.style.display = 'none';
      });

      container.addEventListener('keydown', function(e) {
        if (!window._activeDeviceSerial) return;

        if (['Backspace', 'Enter', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Escape'].includes(e.key)) {
          e.preventDefault();
          e.stopPropagation();
        }

        if (e.key === 'Backspace') {
          sendAndroidKey(67);
          return;
        }
        if (e.key === 'Enter') {
          sendAndroidKey(66);
          return;
        }
        if (e.key === 'Escape') {
          sendAndroidKey(4);
          return;
        }
        if (e.key === 'Tab') {
          sendAndroidKey(61);
          return;
        }
        if (e.key === 'ArrowUp') {
          sendAndroidKey(19);
          return;
        }
        if (e.key === 'ArrowDown') {
          sendAndroidKey(20);
          return;
        }
        if (e.key === 'ArrowLeft') {
          sendAndroidKey(21);
          return;
        }
        if (e.key === 'ArrowRight') {
          sendAndroidKey(22);
          return;
        }

        if (e.key && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault();
          e.stopPropagation();
          sendAndroidDirectText(e.key);
        }
      });

      container.addEventListener('paste', function(e) {
        if (!window._activeDeviceSerial) return;
        e.preventDefault();
        e.stopPropagation();
        const text = (e.clipboardData || window.clipboardData).getData('text');
        if (text) {
          sendAndroidDirectText(text);
        }
      });

      container.addEventListener('mousedown', function(e) {
        container.focus();
        window._isDraggingScreen = true;
        window._swipeStartPos = getScreenRelCoords(e, container);

        if (svg && line && pStart && pEnd && window._swipeStartPos) {
          line.setAttribute('x1', window._swipeStartPos.elemX);
          line.setAttribute('y1', window._swipeStartPos.elemY);
          line.setAttribute('x2', window._swipeStartPos.elemX);
          line.setAttribute('y2', window._swipeStartPos.elemY);
          pStart.setAttribute('cx', window._swipeStartPos.elemX);
          pStart.setAttribute('cy', window._swipeStartPos.elemY);
          pEnd.setAttribute('cx', window._swipeStartPos.elemX);
          pEnd.setAttribute('cy', window._swipeStartPos.elemY);
          svg.style.display = 'block';
        }
      });

      window.addEventListener('mousemove', function(e) {
        if (!window._isDraggingScreen || !window._swipeStartPos) return;
        const currentPos = getScreenRelCoords(e, container);
        if (svg && line && pEnd) {
          line.setAttribute('x2', currentPos.elemX);
          line.setAttribute('y2', currentPos.elemY);
          pEnd.setAttribute('cx', currentPos.elemX);
          pEnd.setAttribute('cy', currentPos.elemY);
        }
      });

      window.addEventListener('mouseup', function(e) {
        if (!window._isDraggingScreen || !window._swipeStartPos) return;
        window._isDraggingScreen = false;
        if (svg) svg.style.display = 'none';

        const endPos = getScreenRelCoords(e, container);
        const startPos = window._swipeStartPos;
        window._swipeStartPos = null;

        const dx = endPos.elemX - startPos.elemX;
        const dy = endPos.elemY - startPos.elemY;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 10) {
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          showTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      });

      container.addEventListener('touchstart', function(e) {
        if (e.touches.length > 0) {
          container.focus();
          window._isDraggingTouch = true;
          window._touchStartPos = getScreenRelCoords(e.touches[0], container);
          if (svg && line && pStart && pEnd && window._touchStartPos) {
            line.setAttribute('x1', window._touchStartPos.elemX);
            line.setAttribute('y1', window._touchStartPos.elemY);
            line.setAttribute('x2', window._touchStartPos.elemX);
            line.setAttribute('y2', window._touchStartPos.elemY);
            pStart.setAttribute('cx', window._touchStartPos.elemX);
            pStart.setAttribute('cy', window._touchStartPos.elemY);
            pEnd.setAttribute('cx', window._touchStartPos.elemX);
            pEnd.setAttribute('cy', window._touchStartPos.elemY);
            svg.style.display = 'block';
          }
        }
      }, { passive: true });

      container.addEventListener('touchmove', function(e) {
        if (!window._isDraggingTouch || !window._touchStartPos || e.touches.length === 0) return;
        const currentPos = getScreenRelCoords(e.touches[0], container);
        if (svg && line && pEnd) {
          line.setAttribute('x2', currentPos.elemX);
          line.setAttribute('y2', currentPos.elemY);
          pEnd.setAttribute('cx', currentPos.elemX);
          pEnd.setAttribute('cy', currentPos.elemY);
        }
      }, { passive: true });

      container.addEventListener('touchend', function(e) {
        if (!window._isDraggingTouch || !window._touchStartPos || e.changedTouches.length === 0) return;
        window._isDraggingTouch = false;
        if (svg) svg.style.display = 'none';

        const endPos = getScreenRelCoords(e.changedTouches[0], container);
        const startPos = window._touchStartPos;
        window._touchStartPos = null;

        const dx = endPos.elemX - startPos.elemX;
        const dy = endPos.elemY - startPos.elemY;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 10) {
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          showTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      }, { passive: true });
    }

    function getScreenRelCoords(e, container) {
      const img = document.getElementById('androidScreenImg');
      const targetElem = (img && img.offsetWidth > 0 && img.style.display !== 'none') ? img : container;
      const rect = targetElem.getBoundingClientRect();
      const elemX = e.clientX - rect.left;
      const elemY = e.clientY - rect.top;

      const normX = Math.max(0, Math.min(1, elemX / (rect.width || 1)));
      const normY = Math.max(0, Math.min(1, elemY / (rect.height || 1)));

      const dev = (window._androidDevices || []).find(d => d.serial === window._activeDeviceSerial);
      const devW = dev && dev.width ? dev.width : 1080;
      const devH = dev && dev.height ? dev.height : 2400;

      const devX = Math.round(normX * devW);
      const devY = Math.round(normY * devH);

      return { x: e.clientX, y: e.clientY, elemX, elemY, devX, devY };
    }

    function showTapRipple(x, y) {
      const rip = document.getElementById('tapRipple');
      if (!rip) return;
      rip.style.left = x + 'px';
      rip.style.top = y + 'px';
      rip.style.transform = 'translate(-50%, -50%) scale(1.5)';
      rip.style.opacity = '0.9';
      setTimeout(() => {
        rip.style.transform = 'translate(-50%, -50%) scale(0)';
        rip.style.opacity = '0';
      }, 250);
    }

    async function executeDeviceTap(x, y) {
      if (!window._activeDeviceSerial) return;
      try {
        await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'tap', x: x, y: y })
        });
        setTimeout(refreshDeviceScreen, 80);
        setTimeout(refreshDeviceScreen, 260);
      } catch (err) {
        console.error('Gagal execute tap:', err);
      }
    }

    async function executeDeviceSwipe(x1, y1, x2, y2) {
      if (!window._activeDeviceSerial) return;
      try {
        await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'swipe', x1: x1, y1: y1, x2: x2, y2: y2, duration: 120 })
        });
        setTimeout(refreshDeviceScreen, 60);
        setTimeout(refreshDeviceScreen, 180);
        setTimeout(refreshDeviceScreen, 380);
      } catch (err) {
        console.error('Gagal execute swipe:', err);
      }
    }

    async function triggerActiveDeviceAction(action) {
      if (!window._activeDeviceSerial) {
        alert('Pilih perangkat terlebih dahulu.');
        return;
      }
      const dev = (window._androidDevices || []).find(d => d.serial === window._activeDeviceSerial);
      const w = dev && dev.width ? dev.width : 1080;
      const h = dev && dev.height ? dev.height : 2400;

      if (action === 'swipe_up') {
        executeDeviceSwipe(Math.round(w / 2), Math.round(h * 0.8), Math.round(w / 2), Math.round(h * 0.2));
        return;
      }
      if (action === 'swipe_down') {
        executeDeviceSwipe(Math.round(w / 2), Math.round(h * 0.2), Math.round(w / 2), Math.round(h * 0.8));
        return;
      }
      if (action === 'swipe_left') {
        executeDeviceSwipe(Math.round(w * 0.85), Math.round(h * 0.5), Math.round(w * 0.15), Math.round(h * 0.5));
        return;
      }
      if (action === 'swipe_right') {
        executeDeviceSwipe(Math.round(w * 0.15), Math.round(h * 0.5), Math.round(w * 0.85), Math.round(h * 0.5));
        return;
      }

      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: action })
        });
        const ret = await res.json();
        if (ret.success) {
          setTimeout(refreshDeviceScreen, 350);
        } else {
          alert('Gagal: ' + (ret.error || 'Unknown error'));
        }
      } catch (e) {
        alert('Gagal kirim perintah: ' + e);
      }
    }

    async function autoScanAdbWifi() {
      const btn = document.getElementById('btnAutoScanAdb');
      const msg = document.getElementById('adbNetworkStatusMsg');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '🔍 Memindai Seluruh Subnet Wi-Fi...';
      }
      if (msg) {
        msg.style.display = 'block';
        msg.style.color = '#38bdf8';
        msg.textContent = 'Memindai seluruh subnet Wi-Fi untuk mencari semua perangkat Android (Port 5555, 5554-5558, & mDNS)...';
      }
      try {
        const res = await fetch('/api/devices/scan-wifi', { method: 'POST' });
        const ret = await res.json();
        if (msg) {
          if (ret.success && ret.connected && ret.connected.length > 0) {
            const connectedList = ret.connected.filter(c => c.status === 'connected');
            let htmlMsg = `
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem; flex-wrap:wrap; gap:0.4rem;">
                <span style="font-weight:700; color:#34d399; font-size:0.82rem;">
                  ✅ Pemindaian Menyeluruh Selesai: Telah memindai ${ret.total_hosts_scanned || 254} host di subnet ${ret.scanned_subnets ? ret.scanned_subnets.join(', ') : 'Wi-Fi'}.
                </span>
                <span style="font-size:0.7rem; color:#94a3b8; font-weight:600; background:#18181b; padding:0.15rem 0.5rem; border-radius:4px; border:1px solid #27272a;">
                  Ditemukan: ${ret.connected.length} Perangkat (${connectedList.length} Terhubung)
                </span>
              </div>
              <div style="display:flex; flex-direction:column; gap:0.4rem; margin-top:0.4rem;">
            `;
            ret.connected.forEach(c => {
              const isOk = (c.status === 'connected');
              const icon = isOk ? '🟢' : '🟡';
              const stLabel = isOk ? '<span style="color:#34d399; font-weight:600;">Terhubung</span>' : '<span style="color:#fbbf24; font-weight:600;">Perlu Otorisasi Layar HP</span>';
              htmlMsg += `
                <div style="display:flex; align-items:center; justify-content:space-between; background:#121216; padding:0.45rem 0.75rem; border-radius:5px; border:1px solid #27272a; flex-wrap:wrap; gap:0.4rem;">
                  <div style="display:flex; align-items:center; gap:0.55rem;">
                    <span>${icon}</span>
                    <span style="font-family:'JetBrains Mono',monospace; font-weight:700; color:#f1f5f9; font-size:0.78rem;">${c.target}</span>
                    <span style="color:#38bdf8; font-weight:600; font-size:0.78rem;">${c.model || 'Android Device'}</span>
                    <span style="font-size:0.72rem;">(${stLabel})</span>
                  </div>
                  <div>
                    ${isOk ? `<button onclick="selectAndroidDevice('${c.target}')" style="background:#2563eb; color:#fff; border:none; padding:0.25rem 0.65rem; border-radius:4px; font-size:0.7rem; font-weight:600; cursor:pointer;">Pilih Kontrol</button>` : ''}
                  </div>
                </div>
              `;
            });
            htmlMsg += `</div>`;
            msg.style.color = '#e2e8f0';
            msg.innerHTML = htmlMsg;
            if (connectedList.length > 0) {
              showToast(`Berhasil menemukan & menghubungkan ${connectedList.length} perangkat ADB Wi-Fi!`, 'success');
            }
          } else {
            msg.style.color = '#94a3b8';
            msg.innerHTML = `ℹ️ Pemindaian seluruh subnet selesai (${ret.total_hosts_scanned || 254} host di subnet ${ret.scanned_subnets ? ret.scanned_subnets.join(', ') : 'Wi-Fi'}). Tidak ada perangkat ADB port 5555 yang aktif.<br><small style="color:#71717a;">Tip: Sambungkan HP via kabel USB sekali lalu klik 'Aktifkan Mode TCP/IP', atau aktifkan 'Wireless Debugging' di Pengaturan Pengembang HP.</small>`;
          }
        }
        await fetchAppiumDevices();
      } catch (e) {
        if (msg) {
          msg.style.color = '#f87171';
          msg.textContent = 'Gagal auto-scan: ' + e;
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '🔍 Auto-Scan & Sambungkan ADB Wi-Fi';
        }
      }
    }

    async function disconnectSpecificDevice(target) {
      if (!confirm(`Putuskan koneksi ADB Wi-Fi untuk ${target}?`)) return;
      try {
        const res = await fetch('/api/devices/disconnect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: target })
        });
        const ret = await res.json();
        showToast(`Perangkat ${target} telah diputuskan`, 'info');
        await fetchAppiumDevices();
      } catch (e) {
        alert('Gagal memutuskan perangkat: ' + e);
      }
    }

    async function connectAdbNetwork() {
      const input = document.getElementById('adbNetworkTargetInput');
      const msg = document.getElementById('adbNetworkStatusMsg');
      const btn = document.getElementById('btnAdbConnect');
      const target = (input?.value || '').trim();
      if (!target) {
        alert('Masukkan IP Tailscale atau Wi-Fi HP (contoh: 100.85.12.34:5555)');
        return;
      }
      if (btn) btn.textContent = 'Menyambungkan...';
      if (msg) {
        msg.style.display = 'block';
        msg.style.color = '#38bdf8';
        msg.textContent = 'Menghubungkan ke ' + target + '...';
      }
      try {
        const res = await fetch('/api/devices/connect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: target })
        });
        const ret = await res.json();
        if (msg) {
          if (ret.success) {
            msg.style.color = '#34d399';
            msg.textContent = (ret.output || 'Berhasil terhubung ke ' + target);
          } else {
            msg.style.color = '#f87171';
            msg.textContent = 'Gagal: ' + (ret.output || ret.error || 'Tidak dapat terhubung');
          }
        }
        await fetchAppiumDevices();
      } catch (e) {
        if (msg) {
          msg.style.color = '#f87171';
          msg.textContent = 'Error: ' + e;
        }
      } finally {
        if (btn) btn.textContent = 'Sambungkan';
      }
    }

    async function disconnectAdbNetwork() {
      const input = document.getElementById('adbNetworkTargetInput');
      const msg = document.getElementById('adbNetworkStatusMsg');
      const target = (input?.value || '').trim();
      if (!target) {
        alert('Masukkan IP Tailscale perangkat yang ingin diputuskan');
        return;
      }
      try {
        const res = await fetch('/api/devices/disconnect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: target })
        });
        const ret = await res.json();
        if (msg) {
          msg.style.display = 'block';
          msg.style.color = '#cbd5e1';
          msg.textContent = (ret.output || 'Terputus dari ' + target);
        }
        await fetchAppiumDevices();
      } catch (e) {
        alert('Gagal disconnect: ' + e);
      }
    }

    async function enableActiveDeviceTcpip() {
      if (!window._activeDeviceSerial) {
        alert('Pilih perangkat USB yang aktif terlebih dahulu.');
        return;
      }
      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'tcpip', port: 5555 })
        });
        const ret = await res.json();
        if (ret.success) {
          alert('Mode TCP/IP Port 5555 Aktif!\\n\\nSekarang Anda bisa memasukkan IP Tailscale HP Anda di kotak input dan klik "Sambungkan". Kabel USB sudah bisa dicabut.');
          fetchAppiumDevices();
        } else {
          alert('Gagal aktifkan port 5555: ' + (ret.error || 'Error'));
        }
      } catch (e) {
        alert('Gagal: ' + e);
      }
    }

    async function sendAndroidText() {
      if (!window._activeDeviceSerial) {
        alert('Pilih perangkat terlebih dahulu.');
        return;
      }
      const input = document.getElementById('androidTextInput');
      const text = input ? input.value : '';
      if (!text) return;

      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'text', text: text })
        });
        const ret = await res.json();
        if (ret.success) {
          if (input) input.value = '';
          setTimeout(refreshDeviceScreen, 350);
        } else {
          alert('Gagal mengirim teks: ' + (ret.error || 'Error'));
        }
      } catch (e) {
        alert('Gagal kirim teks: ' + e);
      }
    }

    async function openAndroidUrl() {
      if (!window._activeDeviceSerial) {
        alert('Pilih perangkat terlebih dahulu.');
        return;
      }
      const input = document.getElementById('androidUrlInput');
      const url = input ? input.value : '';
      if (!url) return;

      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'open_url', url: url })
        });
        const ret = await res.json();
        if (ret.success) {
          setTimeout(refreshDeviceScreen, 600);
        } else {
          alert('Gagal membuka URL: ' + (ret.error || 'Error'));
        }
      } catch (e) {
        alert('Gagal buka URL: ' + e);
      }
    }

    async function runAndroidShellCmd() {
      if (!window._activeDeviceSerial) {
        alert('Pilih perangkat terlebih dahulu.');
        return;
      }
      const input = document.getElementById('androidShellCmdInput');
      const out = document.getElementById('androidShellOutput');
      const cmd = input ? input.value : '';
      if (!cmd) return;

      if (out) out.textContent = 'Menjalankan: ' + cmd + '...';
      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'shell', cmd: cmd })
        });
        const ret = await res.json();
        if (ret.success) {
          let text = ret.stdout || '';
          if (ret.stderr) text += '\\n[STDERR]: ' + ret.stderr;
          if (!text.trim()) text = '(Perintah berhasil dijalankan tanpa output)';
          if (out) out.textContent = text;
        } else {
          if (out) out.textContent = 'ERROR: ' + (ret.error || 'Gagal eksekusi');
        }
      } catch (e) {
        if (out) out.textContent = 'ERROR: ' + e;
      }
    }

    async function enableWirelessAdb(serial, ip) {
      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: serial, action: 'tcpip', port: 5555 })
        });
        const ret = await res.json();
        if (ret.success) {
          const targetIp = ip || '<IP_HP_ANDA>';
          alert(`Wireless ADB Aktif di Port 5555!\n\nDi terminal laptop Anda yang satu Wi-Fi, jalankan:\nadb connect ${targetIp}:5555`);
          fetchAppiumDevices();
        } else {
          alert('Gagal aktifkan wireless: ' + (ret.error || 'Error'));
        }
      } catch (e) {
        alert('Gagal: ' + e);
      }
    }

    async function triggerDeviceAction(serial, action) {
      try {
        const res = await fetch('/api/appium/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial: serial, action: action })
        });
        const ret = await res.json();
        if (ret.success) {
          if (action === 'battery' && ret.battery_info) {
            const lvl = ret.battery_info.level || '--';
            const temp = ret.battery_info.temperature ? (ret.battery_info.temperature / 10).toFixed(1) + '°C' : '--';
            const volt = ret.battery_info.voltage ? (ret.battery_info.voltage / 1000).toFixed(2) + 'V' : '--';
            alert(`Diagnostik Baterai (${serial}):\n• Level: ${lvl}%\n• Suhu: ${temp}\n• Tegangan: ${volt}\n• Status: ${ret.battery_info.status || 'OK'}`);
          } else {
            alert(`Perintah '${action}' berhasil dikirim ke ${serial}.`);
          }
          fetchAppiumDevices();
        } else {
          alert('Gagal: ' + (ret.error || ret.message || 'Unknown error'));
        }
      } catch (e) {
        alert('Gagal kirim perintah: ' + e);
      }
    }

    // Gmail IMAP OTP Config
    let configuredMailAccounts = [];

    async function loadMailConfig() {
      try {
        const res = await fetch('/api/mail/config');
        if (!res.ok) return;
        const data = await res.json();
        configuredMailAccounts = data.accounts || [];
        renderMailAccountsList();
        loadOtpRules();
      } catch (e) {
        console.error('Gagal load mail config:', e);
      }
    }

    function toggleOtpRulesPanel() {
      const el = document.getElementById('otpRulesContent');
      const btn = document.getElementById('toggleOtpRulesBtn');
      if (!el) return;
      const isOpen = el.style.display === 'flex';
      el.style.display = isOpen ? 'none' : 'flex';
      if (btn) btn.textContent = isOpen ? '▼ Buka' : '▲ Tutup';
    }

    async function loadOtpRules() {
      try {
        const res = await fetch('/api/otp/rules');
        if (!res.ok) return;
        const data = await res.json();
        const incSenders = document.getElementById('ruleIncludeSenders');
        const incKws = document.getElementById('ruleIncludeKeywords');
        const excSenders = document.getElementById('ruleExcludeSenders');
        const excKws = document.getElementById('ruleExcludeKeywords');
        if (incSenders) incSenders.value = (data.include_senders || []).join(', ');
        if (incKws) incKws.value = (data.include_keywords || []).join(', ');
        if (excSenders) excSenders.value = (data.exclude_senders || []).join(', ');
        if (excKws) excKws.value = (data.exclude_keywords || []).join(', ');
      } catch (e) {
        console.warn('Gagal load OTP rules:', e);
      }
    }

    async function saveOtpRules() {
      const incSenders = (document.getElementById('ruleIncludeSenders')?.value || '').split(',').map(s => s.trim()).filter(Boolean);
      const incKeywords = (document.getElementById('ruleIncludeKeywords')?.value || '').split(/[\\r\\n,]+/).map(s => s.trim()).filter(Boolean);
      const excSenders = (document.getElementById('ruleExcludeSenders')?.value || '').split(',').map(s => s.trim()).filter(Boolean);
      const excKeywords = (document.getElementById('ruleExcludeKeywords')?.value || '').split(/[\\r\\n,]+/).map(s => s.trim()).filter(Boolean);

      const resBox = document.getElementById('otpRulesSaveResult');
      if (resBox) {
        resBox.style.display = 'block';
        resBox.style.background = 'rgba(56,189,248,0.15)';
        resBox.style.color = '#38bdf8';
        resBox.textContent = 'Menyimpan aturan filter...';
      }

      try {
        const res = await fetch('/api/otp/rules', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            include_senders: incSenders,
            include_keywords: incKeywords,
            exclude_senders: excSenders,
            exclude_keywords: excKeywords,
            fetch_limit: 30
          })
        });
        if (res.ok) {
          if (resBox) {
            resBox.style.background = 'rgba(16,185,129,0.15)';
            resBox.style.color = '#34d399';
            resBox.textContent = 'Aturan filter tersimpan.';
          }
          showToast('Aturan filter OTP berhasil disimpan.', 'success');
          syncImapInboxNow();
        } else {
          throw new Error('HTTP ' + res.status);
        }
      } catch (e) {
        if (resBox) {
          resBox.style.background = 'rgba(239,68,68,0.15)';
          resBox.style.color = '#f87171';
          resBox.textContent = 'Gagal: ' + e.message;
        }
      }
    }

    function resetOtpRulesDefault() {
      const incSenders = document.getElementById('ruleIncludeSenders');
      const incKws = document.getElementById('ruleIncludeKeywords');
      const excSenders = document.getElementById('ruleExcludeSenders');
      const excKws = document.getElementById('ruleExcludeKeywords');
      if (incSenders) incSenders.value = '';
      if (incKws) incKws.value = 'otp, verifikasi, verification, kode, code, token, pin, sandi, password, konfirmasi, confirm, security code, passcode, bni, yap';
      if (excSenders) excSenders.value = 'newsletter, promo, marketing, noreply-offer, updates';
      if (excKws) excKws.value = 'promo, diskon, cashback, newsletter, webinar, unsubscribe, voucher';
    }

    async function syncImapInboxNow() {
      const btn = document.getElementById('btnSyncImap');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Menarik Email...';
      }
      try {
        const res = await fetch('/api/otp/sync', { method: 'POST' });
        const data = await res.json();
        showToast(`Sinkronisasi selesai: ${data.synced || 0} email OTP terdeteksi.`, 'success');
        await loadOtpInbox();
      } catch (e) {
        showToast('Gagal sinkronisasi: ' + e.message, 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = 'Tarik Ulang (Sync)';
        }
      }
    }

    function renderMailAccountsList() {
      const container = document.getElementById('mailAccountsList');
      const countEl = document.getElementById('mailAccountsCount');
      if (countEl) countEl.textContent = `${configuredMailAccounts.length} / 20 Akun`;
      if (!container) return;

      if (!configuredMailAccounts.length) {
        container.innerHTML = '<div style="font-size:0.72rem; color:var(--gf-text-muted); text-align:center; padding:0.5rem; background:#141720; border-radius:6px;">Belum ada akun IMAP. Tambahkan di bawah.</div>';
        return;
      }

      container.innerHTML = configuredMailAccounts.map((acc, idx) => {
        const isAres = acc.email.toLowerCase() === 'areskretindo@gmail.com';
        return `
        <div style="background:#141720; border:1px solid ${isAres ? 'rgba(56, 189, 248, 0.4)' : '#272a31'}; border-radius:6px; padding:0.45rem 0.65rem; display:flex; align-items:center; justify-content:space-between; gap:0.5rem;">
          <div style="min-width:0; flex:1;">
            <div style="font-size:0.78rem; font-weight:600; color:#f1f5f9; display:flex; align-items:center; gap:0.35rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
              <span>${acc.email}</span>
              ${isAres ? '<span style="background:rgba(245,158,11,0.2); color:#fbbf24; font-size:0.62rem; padding:0.1rem 0.35rem; border-radius:3px; font-weight:700;">UTAMA</span>' : ''}
            </div>
            <div style="font-size:0.68rem; color:${acc.has_password ? '#10b981' : '#f59e0b'};">
              ${acc.has_password ? 'Password Tersimpan' : 'Password Belum Ada'} &bull; ${acc.imap_server}
            </div>
          </div>
          <button onclick="deleteMailAccount('${acc.email}')" style="background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3); border-radius:4px; padding:0.25rem 0.5rem; cursor:pointer; font-size:0.7rem;" title="Hapus Akun">Hapus</button>
        </div>
      `;
      }).join('');
    }

    // ===================== OTP INBOX =====================
    let otpInboxData = [];
    let otpSelectedIds = new Set();

    async function loadOtpInbox() {
      const keyword = (document.getElementById('otpFilterKeyword')?.value || '').trim();
      const mode = document.getElementById('otpFilterMode')?.value || 'otp';
      const limit = document.getElementById('otpFilterLimit')?.value || '100';
      const otpOnly = mode === 'otp';
      let url = `/api/otp/inbox?limit=${limit}`;
      if (otpOnly) url += '&otp_only=true';
      if (keyword) url += '&keyword=' + encodeURIComponent(keyword);
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        otpInboxData = await res.json();
        renderOtpInbox();
      } catch(e) {
        const el = document.getElementById('otpInboxList');
        if (el) el.innerHTML = `<div style="color:#f87171; font-size:0.8rem; padding:1rem; text-align:center;">Gagal memuat kotak masuk: ${e}<br><small>Pastikan mail-collector berjalan dan sudah dikonfigurasi.</small></div>`;
      }
    }

    async function clearOtpInbox() {
      if (!confirm('Yakin ingin membersihkan semua email di kotak masuk?')) return;
      try {
        await fetch('/api/otp/inbox', { method: 'DELETE' });
        otpSelectedIds.clear();
        const panel = document.getElementById('otpSelectedPanel');
        if (panel) panel.style.display = 'none';
        loadOtpInbox();
      } catch (e) {
        alert('Gagal membersihkan kotak masuk: ' + e);
      }
    }

    function renderOtpInbox() {
      const el = document.getElementById('otpInboxList');
      const countEl = document.getElementById('otpInboxCount');
      if (!el) return;
      if (countEl) countEl.textContent = `${otpInboxData.length} email`;
      if (!otpInboxData.length) {
        el.innerHTML = `<div style="color:var(--gf-text-muted); font-size:0.85rem; padding:2rem; text-align:center;">Kotak masuk kosong.<br><small>Email OTP baru akan muncul di sini secara otomatis saat mail-collector mendeteksinya.</small></div>`;
        return;
      }
      el.innerHTML = otpInboxData.map(m => {
        const isOtp = m.has_otp;
        const isSel = otpSelectedIds.has(m.id);
        const dt = m.received_at ? new Date(m.received_at).toLocaleString('id-ID', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}) : m.date;
        const borderColor = isOtp ? '#f59e0b' : '#2d3139';
        const bg = isOtp ? 'rgba(245,158,11,0.06)' : '#14161b';
        const selBg = isSel ? 'rgba(56,189,248,0.12)' : '';
        const otpBadge = isOtp
          ? `<span style="background:#27272a; color:#f4f4f5; border:1px solid #3f3f46; font-weight:700; border-radius:4px; padding:0.15rem 0.5rem; font-size:0.75rem; font-family:'JetBrains Mono',monospace; letter-spacing:0.1em;">${m.otp_code}</span>`
          : '';
        const selChk = `<input type="checkbox" onchange="toggleOtpSelection(${m.id})" ${isSel ? 'checked' : ''} style="width:16px; height:16px; accent-color:#38bdf8; cursor:pointer; flex-shrink:0;">`;
        return `
          <div id="otp-row-${m.id}" onclick="toggleOtpSelection(${m.id})" style="background:${selBg || bg}; border:1px solid ${isSel ? '#38bdf8' : borderColor}; border-radius:6px; padding:0.6rem 0.85rem; cursor:pointer; display:flex; align-items:flex-start; gap:0.65rem; transition:all 0.15s;">
            <div style="padding-top:0.1rem;">${selChk}</div>
            <div style="flex:1; min-width:0;">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem; margin-bottom:0.2rem; flex-wrap:wrap;">
                <div style="font-weight:600; font-size:0.82rem; color:${isOtp ? '#fbbf24' : '#cbd5e1'}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1;">${m.subject || '(Tanpa Subjek)'}</div>
                <div style="font-size:0.68rem; color:var(--gf-text-dim); white-space:nowrap; flex-shrink:0;">${dt}</div>
              </div>
              <div style="font-size:0.75rem; color:var(--gf-text-muted); margin-bottom:${isOtp ? '0.35rem' : '0'}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                <span style="color:${isOtp ? '#a3e635' : 'var(--gf-text-dim)'};">${m.sender}</span>
              </div>
              ${isOtp ? otpBadge : ''}
              ${!isOtp && m.snippet ? `<div style="font-size:0.72rem; color:var(--gf-text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${m.snippet}</div>` : ''}
            </div>
          </div>`;
      }).join('');
    }

    function toggleOtpSelection(id) {
      if (otpSelectedIds.has(id)) {
        otpSelectedIds.delete(id);
      } else {
        otpSelectedIds.add(id);
      }
      // Re-render this row
      renderOtpInbox();
      // Update selected chips panel
      const panel = document.getElementById('otpSelectedPanel');
      const chipList = document.getElementById('otpSelectedList');
      if (!panel || !chipList) return;
      if (otpSelectedIds.size === 0) {
        panel.style.display = 'none';
        return;
      }
      panel.style.display = 'block';
      const selected = otpInboxData.filter(m => otpSelectedIds.has(m.id));
      chipList.innerHTML = selected.map(m => `
        <div style="background:linear-gradient(135deg,#92400e,#b45309); border-radius:8px; padding:0.4rem 0.75rem; display:flex; flex-direction:column; gap:0.15rem; min-width:140px; position:relative;">
          <div style="font-size:0.68rem; color:#fde68a; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:200px;">${m.subject}</div>
          ${m.has_otp ? `<div style="font-size:1rem; font-weight:700; color:#fff; font-family:'JetBrains Mono',monospace; letter-spacing:0.15em;">${m.otp_code}</div>` : '<div style="font-size:0.75rem; color:#fde68a;">Tidak ada OTP</div>'}
          <div style="font-size:0.65rem; color:#fcd34d;">${m.sender}</div>
          <button onclick="event.stopPropagation(); otpSelectedIds.delete(${m.id}); renderOtpInbox(); toggleOtpSelection(-999);" style="position:absolute; top:4px; right:4px; background:rgba(0,0,0,0.35); border:none; color:#fff; border-radius:50%; width:16px; height:16px; font-size:0.6rem; cursor:pointer; display:flex; align-items:center; justify-content:center;">✕</button>
        </div>
      `).join('');
    }

    function clearOtpSelection() {
      otpSelectedIds.clear();
      renderOtpInbox();
      const panel = document.getElementById('otpSelectedPanel');
      if (panel) panel.style.display = 'none';
    }

    function copyOtpPublicUrl() {
      const url = window.location.origin + '/api/otp/public?otp_only=true&limit=20';
      navigator.clipboard.writeText(url).then(() => {
        showToast('URL API Publik OTP berhasil disalin.', 'success');
      });
    }

    function toggleMailPasswordVisibility() {
      const inp = document.getElementById('mailInputPassword');
      const btn = document.getElementById('mailTogglePassBtn');
      if (inp.type === 'password') {
        inp.type = 'text';
        btn.textContent = 'Tutup';
      } else {
        inp.type = 'password';
        btn.textContent = 'Lihat';
      }
    }

    async function testMailConnection() {
      const email = document.getElementById('mailInputEmail').value.trim();
      const password = document.getElementById('mailInputPassword').value.trim();
      const resultBox = document.getElementById('mailTestResult');
      if (!email || !password) {
        resultBox.style.display = 'block';
        resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
        resultBox.style.color = '#f87171';
        resultBox.textContent = 'Silakan isi alamat email dan App Password terlebih dahulu!';
        return;
      }
      resultBox.style.display = 'block';
      resultBox.style.background = 'rgba(56, 189, 248, 0.15)';
      resultBox.style.color = '#38bdf8';
      resultBox.textContent = 'Menguji koneksi ke imap.gmail.com:993...';

      try {
        const res = await fetch('/api/mail/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password, imap_server: 'imap.gmail.com', imap_port: 993 })
        });
        const data = await res.json();
        if (data.success) {
          resultBox.style.background = 'rgba(16, 185, 129, 0.15)';
          resultBox.style.color = '#34d399';
          resultBox.textContent = data.message;
        } else {
          resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
          resultBox.style.color = '#f87171';
          resultBox.textContent = 'Gagal Login IMAP: ' + data.error;
        }
      } catch (e) {
        resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
        resultBox.style.color = '#f87171';
        resultBox.textContent = 'Error: ' + e.message;
      }
    }

    async function saveMailConfig() {
      const email = document.getElementById('mailInputEmail').value.trim();
      const password = document.getElementById('mailInputPassword').value.trim();
      const resultBox = document.getElementById('mailTestResult');
      if (!email) {
        alert('Email tidak boleh kosong!');
        return;
      }
      resultBox.style.display = 'block';
      resultBox.style.background = 'rgba(56, 189, 248, 0.15)';
      resultBox.style.color = '#38bdf8';
      resultBox.textContent = 'Menyimpan konfigurasi & merestart mail-collector.service...';

      // Check if existing or new account
      const existingIdx = configuredMailAccounts.findIndex(a => a.email.toLowerCase() === email.toLowerCase());
      if (existingIdx < 0 && configuredMailAccounts.length >= 20) {
        alert('Maksimal 20 akun Gmail yang dapat dipantau sekaligus.');
        resultBox.style.display = 'none';
        return;
      }
      const accountsToSend = configuredMailAccounts.map(a => ({
        account_name: a.account_name || 'Gmail_OTP',
        imap_server: a.imap_server || 'imap.gmail.com',
        imap_port: a.imap_port || 993,
        email: a.email,
        password: (a.email.toLowerCase() === email.toLowerCase() && password) ? password : ''
      }));

      if (existingIdx >= 0) {
        if (password) accountsToSend[existingIdx].password = password;
      } else {
        accountsToSend.push({
          account_name: 'Gmail_' + (configuredMailAccounts.length + 1),
          imap_server: 'imap.gmail.com',
          imap_port: 993,
          email: email,
          password: password
        });
      }

      try {
        const res = await fetch('/api/mail/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ accounts: accountsToSend })
        });
        const data = await res.json();
        if (data.status === 'saved') {
          resultBox.style.background = 'rgba(16, 185, 129, 0.15)';
          resultBox.style.color = '#34d399';
          resultBox.textContent = `Akun ${email} berhasil disimpan. Total: ${data.accounts} akun aktif.`;
          document.getElementById('mailInputEmail').value = '';
          document.getElementById('mailInputPassword').value = '';
          await loadMailConfig();
          fetchHermesStatus();
        } else {
          resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
          resultBox.style.color = '#f87171';
          resultBox.textContent = 'Gagal menyimpan konfigurasi';
        }
      } catch (e) {
        resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
        resultBox.style.color = '#f87171';
        resultBox.textContent = 'Error: ' + e.message;
      }
    }

    async function deleteMailAccount(email) {
      if (!confirm(`Hapus akun ${email} dari pemantauan IMAP?`)) return;
      try {
        const res = await fetch(`/api/mail/config?email=${encodeURIComponent(email)}`, { method: 'DELETE' });
        if (res.ok) {
          await loadMailConfig();
          showToast(`Akun ${email} berhasil dihapus.`, 'info');
        } else {
          alert('Gagal menghapus akun.');
        }
      } catch (e) {
        alert('Gagal: ' + e.message);
      }
    }

    // ==================== MEETING MODE JAVASCRIPT ====================
    let speechRecognition = null;
    let isRecordingMeeting = false;
    let meetingStartTime = null;
    let meetingTimerInterval = null;
    let currentMeetingSummaryMarkdown = '';
    let pastMeetingsData = [];
    let mediaRecorder = null;
    let recordedAudioChunks = [];
    let audioStream = null;
    let micStream = null;
    let systemStream = null;
    let mixerAudioCtx = null;

    function checkSpeechEngine() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const badge = document.getElementById('speechEngineBadge');
      const detail = document.getElementById('speechEngineDetail');
      if (!badge || !detail) return;
      if (SpeechRecognition) {
        badge.className = 'hermes-badge badge-ok';
        badge.textContent = 'Web Speech Streaming Aktif';
        detail.textContent = 'Browser mendukung transkripsi teks live mengalir saat berbicara (Chrome/Edge/Safari/HP).';
      } else {
        badge.className = 'hermes-badge badge-blue';
        badge.textContent = 'Mode Rekam Mikrofon Universal';
        detail.innerHTML = 'Browser ini tidak memiliki Web Speech bawaan (misal Firefox/Brave). Audio direkam & otomatis ditranskripsi via server saat menekan tombol Selesai & Rangkum.';
      }
    }

    // Dictionary & Instant Phonetic Normalizer for Common Speech-to-Text Mishearings
    function normalizeKnownSpokenTerms(raw) {
      if (!raw) return '';
      let text = raw;
      const replacements = [
        [new RegExp('\\\\b(tidak\\\\s*haid)\\\\b', 'gi'), 'tidak di-hide'],
        [new RegExp('\\\\b(di\\\\s*haid|dihaid)\\\\b', 'gi'), 'di-hide'],
        [new RegExp('\\\\bhaid\\\\b', 'gi'), 'hide'],
        [new RegExp('\\\\b(di\\\\s*ut|diut)\\\\b', 'gi'), 'di-mute'],
        [new RegExp('\\\\bpenari\\\\b', 'gi'), 'PNRI'],
        [new RegExp('\\\\b(tes\\\\s*ke|tes\\\\s*kes)\\\\b', 'gi'), 'test case'],
        [new RegExp('\\\\b(bubble\\\\s*di)\\\\b', 'gi'), 'publish di'],
        [new RegExp('\\\\b(dikonsumsi\\\\s*skenario)\\\\b', 'gi'), 'dikonfirmasi skenario'],
        [new RegExp('\\\\bkcek\\\\b', 'gi'), 'cek'],
        [new RegExp('\\\\b(siap\\\\s*ya)\\\\b', 'gi'), 'YAP!']
      ];
      for (const [pattern, rep] of replacements) {
        text = text.replace(pattern, rep);
      }
      return text;
    }

    function clearMeetingTranscript() {
      const textarea = document.getElementById('meetingTranscriptText');
      if (textarea) textarea.value = '';
      const interimBox = document.getElementById('liveSpeechInterimBox');
      if (interimBox) interimBox.style.display = 'none';
      const statusEl = document.getElementById('transcriptStatusText');
      if (statusEl) statusEl.textContent = 'Transkrip telah dikosongkan.';
    }

    async function polishMeetingTranscript() {
      const textarea = document.getElementById('meetingTranscriptText');
      const text = textarea ? textarea.value.trim() : '';
      if (!text) {
        alert('Tidak ada teks transkrip untuk dirapikan. Ketik atau rekam suara terlebih dahulu.');
        return;
      }
      const btn = document.getElementById('btnPolishTranscript');
      const statusEl = document.getElementById('transcriptStatusText');
      const originalBtnText = btn ? btn.innerHTML : '';
      if (btn) {
        btn.innerHTML = '⏳ Merapikan...';
        btn.disabled = true;
      }
      if (statusEl) statusEl.innerHTML = '<span style="color:#c084fc;">⏳ AI Hermes sedang mengoreksi salah dengar fonetik & merapikan transkrip...</span>';

      try {
        const selectedModel = document.getElementById('meetingModelSelect')?.value || undefined;
        const res = await fetch('/api/meetings/polish-transcript', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcript: text, model: selectedModel })
        });
        const data = await res.json();
        if (data.success && data.cleaned) {
          textarea.value = data.cleaned;
          textarea.scrollTop = 0;
          if (statusEl) statusEl.innerHTML = '<span style="color:#34d399; font-weight:600;">✅ Transkrip berhasil dikoreksi & dirapikan oleh AI Hermes!</span>';
        } else {
          alert('Gagal merapikan transkrip: ' + (data.error || 'Respons server kosong'));
        }
      } catch (err) {
        alert('Gagal menghubungi server: ' + err.message);
      } finally {
        if (btn) {
          btn.innerHTML = originalBtnText;
          btn.disabled = false;
        }
      }
    }

    let currentLiveInterimText = '';

    function initSpeechRecognition() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        return null;
      }
      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = meetingLangForBrowser();

      recognition.onresult = (event) => {
        let finalTranscript = '';
        let interimTranscript = '';
        for (let i = event.resultIndex; i < event.results.length; ++i) {
          if (event.results[i].isFinal) {
            finalTranscript += event.results[i][0].transcript + ' ';
          } else {
            interimTranscript += event.results[i][0].transcript;
          }
        }
        currentLiveInterimText = interimTranscript;
        const interimBox = document.getElementById('liveSpeechInterimBox');
        const interimText = document.getElementById('liveSpeechInterimText');
        if (interimBox && interimText) {
          if (interimTranscript.trim()) {
            interimBox.style.display = 'block';
            interimText.textContent = interimTranscript.trim();
          } else {
            interimBox.style.display = 'none';
          }
        }
        const textarea = document.getElementById('meetingTranscriptText');
        if (finalTranscript) {
          currentLiveInterimText = '';
          finalTranscript = normalizeKnownSpokenTerms(finalTranscript);
          textarea.value += finalTranscript;
          textarea.scrollTop = textarea.scrollHeight;
        }
      };

      recognition.onerror = (event) => {
        console.warn('Speech recognition warning:', event.error);
        if (event.error === 'not-allowed') {
          stopMeetingRecording(false);
          openPermissionModal();
        }
      };

      recognition.onend = () => {
        const interimBox = document.getElementById('liveSpeechInterimBox');
        if (interimBox) interimBox.style.display = 'none';
        // Selamatkan kata yang sedang diucapkan agar tidak kepotong saat recognition me-reload
        if (currentLiveInterimText && currentLiveInterimText.trim()) {
          const textarea = document.getElementById('meetingTranscriptText');
          if (textarea) {
            textarea.value += normalizeKnownSpokenTerms(currentLiveInterimText.trim()) + ' ';
            textarea.scrollTop = textarea.scrollHeight;
          }
          currentLiveInterimText = '';
        }
        if (isRecordingMeeting) {
          setTimeout(() => {
            if (isRecordingMeeting) {
              try {
                speechRecognition = initSpeechRecognition();
                speechRecognition.start();
              } catch (e) {
                console.warn('Speech recognition restart:', e);
              }
            }
          }, 50);
        }
      };

      return recognition;
    }

    async function blobToWavBase64(blob) {
      const arrayBuffer = await blob.arrayBuffer();
      const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
      const audioCtx = new AudioCtxClass();
      const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);

      const sampleRate = audioBuffer.sampleRate;
      const channelData = audioBuffer.getChannelData(0);
      const numSamples = channelData.length;
      const buffer = new ArrayBuffer(44 + numSamples * 2);
      const view = new DataView(buffer);

      function writeStr(offset, str) {
        for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
      }

      writeStr(0, 'RIFF');
      view.setUint32(4, 36 + numSamples * 2, true);
      writeStr(8, 'WAVE');
      writeStr(12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true); // PCM
      view.setUint16(22, 1, true); // Mono
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeStr(36, 'data');
      view.setUint32(40, numSamples * 2, true);

      let offset = 44;
      for (let i = 0; i < numSamples; i++) {
        let s = Math.max(-1, Math.min(1, channelData[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        offset += 2;
      }

      let binary = '';
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
      }
      return btoa(binary);
    }

    async function startMeetingRecording() {
      const sourceMode = document.getElementById('meetingAudioSourceSelect')?.value || 'both';

      micStream = null;
      systemStream = null;
      if (mixerAudioCtx && mixerAudioCtx.state !== 'closed') {
        try { mixerAudioCtx.close(); } catch(e){}
      }
      mixerAudioCtx = null;

      // 1. Ambil Audio Laptop/Sistem (Tab Meet/Zoom/Windows) jika mode 'both' atau 'system'
      if (sourceMode === 'both' || sourceMode === 'system') {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
          alert('Browser Anda tidak mendukung perekaman audio sistem/tab. Silakan gunakan browser Chrome atau Microsoft Edge.');
          return;
        }
        try {
          // getDisplayMedia memerlukan video: true pada standar W3C
          systemStream = await navigator.mediaDevices.getDisplayMedia({
            video: { displaySurface: 'browser' },
            audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
          });
          // Matikan konsumsi video agar hemat RAM dan CPU
          systemStream.getVideoTracks().forEach(t => {
            t.enabled = false;
          });

          const sysAudioTracks = systemStream.getAudioTracks();
          if (!sysAudioTracks.length) {
            alert('Perhatian: Anda tidak mencentang kotak "Share tab audio" atau "Bagikan audio sistem" saat memilih tab/layar di popup browser. Suara rekan rapat dari laptop tidak akan tertangkap.');
            if (sourceMode === 'system') {
              systemStream.getTracks().forEach(t => t.stop());
              return;
            }
          } else {
            localStorage.setItem('westpro_sys_audio_granted', '1');
            try { checkAllPermissions(); } catch (e) {}
          }
        } catch (err) {
          console.warn('System audio capture cancelled or error:', err);
          if (sourceMode === 'system') {
            return;
          }
          // Jika mode 'both' tapi user batal share tab, lanjutkan dengan mikrofon saja
          alert('Perekaman audio laptop dibatalkan. Sistem akan melanjutkan perekaman menggunakan Mikrofon saja.');
        }
      }

      // 2. Ambil Audio Mikrofon jika mode 'both' atau 'mic'
      if (sourceMode === 'both' || sourceMode === 'mic') {
        if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
          try {
            micStream = await navigator.mediaDevices.getUserMedia({
              audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
            });
          } catch (err) {
            console.warn('Microphone getUserMedia error:', err);
            if (!systemStream) {
              openPermissionModal();
              return;
            }
          }
        }
      }

      if (!micStream && !systemStream) {
        alert('Gagal mengakses mikrofon maupun audio sistem laptop. Pastikan izin telah diberikan.');
        return;
      }

      // 3. Mixing kedua stream via Web Audio API (jika keduanya aktif)
      try {
        const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
        mixerAudioCtx = new AudioCtxClass();
        const destination = mixerAudioCtx.createMediaStreamDestination();

        if (micStream && micStream.getAudioTracks().length > 0) {
          const micSource = mixerAudioCtx.createMediaStreamSource(micStream);
          micSource.connect(destination);
        }
        if (systemStream && systemStream.getAudioTracks().length > 0) {
          const sysSource = mixerAudioCtx.createMediaStreamSource(systemStream);
          sysSource.connect(destination);
        }

        audioStream = destination.stream;
      } catch (e) {
        console.warn('Web Audio mixing error, fallback:', e);
        audioStream = micStream || systemStream;
      }

      isRecordingMeeting = true;
      meetingStartTime = Date.now();
      document.getElementById('btnStartMeeting').style.display = 'none';
      document.getElementById('btnStopMeeting').style.display = 'inline-block';
      const badge = document.getElementById('meetingStatusBadge');
      badge.textContent = 'Merekam...';
      badge.className = 'hermes-badge badge-warn';
      const wave = document.getElementById('audioWaveIndicator');
      if (wave) {
        wave.style.display = 'inline-flex';
        const waveText = document.getElementById('audioWaveText');
        if (waveText) {
          waveText.textContent = (micStream && systemStream) ? 'Audio Mic + Laptop Aktif' : (systemStream ? 'Audio Laptop Aktif' : 'Audio Mikrofon Aktif');
        }
      }

      // Start timer
      clearInterval(meetingTimerInterval);
      meetingTimerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - meetingStartTime) / 1000);
        const hrs = String(Math.floor(elapsed / 3600)).padStart(2, '0');
        const mins = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
        const secs = String(elapsed % 60).padStart(2, '0');
        document.getElementById('meetingTimerDisplay').textContent = `${hrs}:${mins}:${secs}`;
      }, 1000);

      // Start Native Speech Recognition
      speechRecognition = initSpeechRecognition();
      if (speechRecognition) {
        try { speechRecognition.start(); } catch(e){}
      }

      // Start MediaRecorder dengan audio hasil mixing
      try {
        recordedAudioChunks = [];
        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
        mediaRecorder = mimeType ? new MediaRecorder(audioStream, { mimeType }) : new MediaRecorder(audioStream);
        mediaRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) recordedAudioChunks.push(e.data);
        };
        mediaRecorder.start();
      } catch(err) {
        console.warn('MediaRecorder start error:', err);
      }
    }

    async function stopMeetingRecording(shouldSummarize = true) {
      isRecordingMeeting = false;
      clearInterval(meetingTimerInterval);
      if (speechRecognition) {
        try { speechRecognition.stop(); } catch(e){}
      }
      if (currentLiveInterimText && currentLiveInterimText.trim()) {
        const textarea = document.getElementById('meetingTranscriptText');
        if (textarea) {
          textarea.value += normalizeKnownSpokenTerms(currentLiveInterimText.trim()) + ' ';
          textarea.scrollTop = textarea.scrollHeight;
        }
        currentLiveInterimText = '';
      }

      const wave = document.getElementById('audioWaveIndicator');
      if (wave) wave.style.display = 'none';

      // Stop MediaRecorder & kumpulkan audio hasil mixing
      let recordedBlob = null;
      if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        const stopPromise = new Promise(resolve => {
          mediaRecorder.onstop = () => {
            recordedBlob = new Blob(recordedAudioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
            resolve();
          };
        });
        mediaRecorder.stop();
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        if (systemStream) systemStream.getTracks().forEach(t => t.stop());
        if (audioStream) audioStream.getTracks().forEach(t => t.stop());
        if (mixerAudioCtx && mixerAudioCtx.state !== 'closed') {
          try { mixerAudioCtx.close(); } catch(e){}
        }
        await stopPromise;
      }

      document.getElementById('btnStartMeeting').style.display = 'inline-block';
      document.getElementById('btnStopMeeting').style.display = 'none';
      const badge = document.getElementById('meetingStatusBadge');
      badge.textContent = 'Selesai';
      badge.className = 'hermes-badge badge-ok';

      if (recordedBlob && recordedBlob.size > 0) {
        window._lastMeetingAudio = { blob: recordedBlob, name: 'rekaman-rapat.webm' };
      }

      if (!shouldSummarize) return;

      // Dasar analisa = audio langsung: kirim rekaman utuh ke AI (transkrip live tetap disimpan bila ada)
      if ((document.getElementById('meetingAnalysisBasis')?.value || 'transcript') === 'audio' && recordedBlob && recordedBlob.size > 0) {
        await runAudioAnalysis(recordedBlob, 'rekaman-rapat.webm');
        return;
      }

      let transcript = document.getElementById('meetingTranscriptText').value.trim();

      // If transcript is still empty and we have an audio blob, transcribe it via server!
      if (!transcript && recordedBlob && recordedBlob.size > 0) {
        const outBox = document.getElementById('meetingSummaryOutput');
        outBox.innerHTML = `
          <div style="text-align: center; margin-top: 5rem; color: #94a3b8;">
            <div style="font-weight: 600;">Mentranskripsikan audio rekaman via server...</div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.25rem;">Mengonversi rekaman suara ke teks...</div>
          </div>
        `;

        try {
          const wavBase64 = await blobToWavBase64(recordedBlob);
          const lang = meetingLangForBrowser();
          const trRes = await fetch('/api/meetings/transcribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ audio_b64: wavBase64, language: lang })
          });
          const trData = await trRes.json();
          if (trData.success && trData.text) {
            transcript = trData.text;
            document.getElementById('meetingTranscriptText').value = transcript;
          }
        } catch (e) {
          console.warn('Gagal transcribe audio via server:', e);
        }
      }

      const title = document.getElementById('meetingTitleInput').value.trim() || 'Meeting Koordinasi';
      const elapsed = meetingStartTime ? Math.floor((Date.now() - meetingStartTime) / 1000) : 0;

      if (!transcript) {
        alert('Transkrip masih kosong. Ucapkan sesuatu ke mikrofon atau ketik catatan meeting.');
        return;
      }

      // 3. Auto-koreksi transkrip dengan AI jika opsi aktif
      const autoPolish = document.getElementById('chkAutoPolishTranscript')?.checked ?? true;
      if (autoPolish && transcript.length > 25) {
        const outBox = document.getElementById('meetingSummaryOutput');
        outBox.innerHTML = `
          <div style="text-align: center; margin-top: 5rem; color: #a855f7;">
            <div style="font-weight: 700; font-size: 1rem;">✨ AI Sedang Mengoreksi & Merapikan Transkrip...</div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.35rem;">Memperbaiki salah dengar fonetik, menyusun tanda baca, & membagi paragraf...</div>
          </div>
        `;
        try {
          const selectedModel = document.getElementById('meetingModelSelect')?.value || undefined;
          const pRes = await fetch('/api/meetings/polish-transcript', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ transcript, model: selectedModel })
          });
          const pData = await pRes.json();
          if (pData.success && pData.cleaned) {
            transcript = pData.cleaned;
            document.getElementById('meetingTranscriptText').value = transcript;
            const statusEl = document.getElementById('transcriptStatusText');
            if (statusEl) statusEl.innerHTML = '<span style="color: #c084fc; font-weight: 600;">✨ Transkrip otomatis dirapikan oleh AI Hermes</span>';
          }
        } catch (e) {
          console.warn('Gagal auto-polish transkrip:', e);
        }
      }

      await summarizeCurrentTranscript(title, transcript, elapsed);
    }

    function clearMeetingData() {
      if (isRecordingMeeting) stopMeetingRecording(false);
      document.getElementById('meetingTranscriptText').value = '';
      document.getElementById('meetingTimerDisplay').textContent = '⏱️ 00:00:00';
      document.getElementById('meetingSummaryOutput').innerHTML = `
        <div style="color: var(--gf-text-muted); text-align: center; margin-top: 5rem;">
          Tekan <b>"Mulai Dengerin Meeting"</b> untuk mulai mencatat dan merangkum suara pembicaraan secara otomatis.
        </div>
      `;
    }

    function copyMeetingSummary() {
      if (!currentMeetingSummaryMarkdown) {
        alert('Belum ada rangkuman untuk disalin.');
        return;
      }
      navigator.clipboard.writeText(currentMeetingSummaryMarkdown);
      alert('Rangkuman notulen meeting berhasil disalin ke clipboard!');
    }

    function downloadMeetingMarkdown() {
      if (!currentMeetingSummaryMarkdown) {
        alert('Belum ada rangkuman untuk diunduh.');
        return;
      }
      const title = document.getElementById('meetingTitleInput').value.trim() || 'notulen_meeting';
      const blob = new Blob([currentMeetingSummaryMarkdown], { type: 'text/markdown;charset=utf-8;' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${title.replace(/\\s+/g, '_')}_${new Date().toISOString().slice(0,10)}.md`;
      link.click();
    }

    function downloadMeetingText() {
      if (!currentMeetingSummaryMarkdown) {
        alert('Belum ada rangkuman untuk diunduh.');
        return;
      }
      const title = document.getElementById('meetingTitleInput').value.trim() || 'notulen_meeting';
      const transcript = document.getElementById('meetingTranscriptText').value.trim();
      const content = `JUDUL: ${title}\nTANGGAL: ${new Date().toLocaleString('id-ID')}\n\n=== RANGKUMAN / NOTULEN ===\n\n${currentMeetingSummaryMarkdown}\n\n=== TRANSKRIP ASLI ===\n\n${transcript}`;
      const blob = new Blob([content], { type: 'text/plain;charset=utf-8;' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${title.replace(/\\s+/g, '_')}_${new Date().toISOString().slice(0,10)}.txt`;
      link.click();
    }

    async function loadPastMeetings() {
      try {
        const res = await fetch('/api/meetings');
        if (!res.ok) return;
        pastMeetingsData = await res.json();
        const tbody = document.getElementById('pastMeetingsTableBody');
        if (!pastMeetingsData.length) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--gf-text-muted);">Belum ada arsip notulen meeting yang tersimpan.</td></tr>';
          return;
        }
        tbody.innerHTML = pastMeetingsData.map((m, idx) => {
          const mins = Math.floor(m.duration_seconds / 60);
          const secs = m.duration_seconds % 60;
          const durStr = `${mins}m ${secs}s`;
          return `
            <tr>
              <td>${m.created_at}</td>
              <td><b>${m.title}</b></td>
              <td>${durStr}</td>
              <td>${m.transcript.length} karakter</td>
              <td>
                <div style="display:flex; gap:0.35rem; flex-wrap:wrap;">
                  <button onclick="viewPastMeeting(${idx})" style="background:#0284c7; color:#fff; border:none; border-radius:4px; padding:0.2rem 0.5rem; cursor:pointer; font-size:0.75rem;">Buka</button>
                  <button onclick="polishPastMeeting(${idx})" style="background:rgba(168,85,247,0.15); color:#c084fc; border:1px solid rgba(168,85,247,0.35); border-radius:4px; padding:0.2rem 0.5rem; cursor:pointer; font-size:0.75rem;" title="Koreksi & rapikan transkrip meeting ini via AI Hermes">✨ Rapikan</button>
                  <button onclick="extractTcFromPastMeeting(${idx})" style="background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3); border-radius:4px; padding:0.2rem 0.5rem; cursor:pointer; font-size:0.75rem;" title="Ekstrak & Pilih Test Case">Ekstrak TC</button>
                  <button onclick="deletePastMeeting(${m.id})" style="background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3); border-radius:4px; padding:0.2rem 0.45rem; cursor:pointer; font-size:0.75rem;" title="Hapus Notulen">Hapus</button>
                </div>
              </td>
            </tr>
          `;
        }).join('');
      } catch(e) {
        console.error('Gagal load past meetings:', e);
      }
    }

    async function deletePastMeeting(id) {
      if (!confirm('Hapus notulen meeting ini dari database?')) return;
      try {
        let res = await fetch(`/api/meetings/${id}`, { method: 'DELETE' });
        if (!res.ok) {
          res = await fetch('/api/meetings/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id })
          });
        }
        if (res.ok) {
          loadPastMeetings();
        } else {
          alert('Gagal menghapus notulen.');
        }
      } catch (e) {
        alert('Gagal menghapus notulen: ' + e);
      }
    }

    function viewPastMeeting(index) {
      const m = pastMeetingsData[index];
      if (!m) return;
      document.getElementById('meetingTitleInput').value = m.title;
      document.getElementById('meetingTranscriptText').value = m.transcript;
      currentMeetingSummaryMarkdown = m.summary;
      const outBox = document.getElementById('meetingSummaryOutput');
      if (window.marked) {
        outBox.innerHTML = marked.parse(m.summary);
      } else {
        outBox.textContent = m.summary;
      }
      window.scrollTo({ top: 300, behavior: 'smooth' });
    }

    async function polishPastMeeting(index) {
      const m = pastMeetingsData[index];
      if (!m) return;
      viewPastMeeting(index);
      await polishMeetingTranscript();
      const updatedTranscript = document.getElementById('meetingTranscriptText').value;
      if (confirm(`Simpan transkrip bersih ke notulen "${m.title}" dan buat rangkuman ulang dengan AI?`)) {
        await fetch('/api/meetings/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: m.id, transcript: updatedTranscript })
        });
        loadPastMeetings();
        const title = document.getElementById('meetingTitleInput').value.trim() || m.title;
        const outBox = document.getElementById('meetingSummaryOutput');
        outBox.innerHTML = `
          <div style="text-align: center; margin-top: 5rem; color: #a855f7;">
            <div style="font-weight: 600;">Menyusun ulang notulen resmi dengan transkrip bersih...</div>
          </div>
        `;
        try {
          const selectedModel = document.getElementById('meetingModelSelect')?.value || undefined;
          const sRes = await fetch('/api/meetings/summarize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title, transcript: updatedTranscript, duration: m.duration_seconds || 0, model: selectedModel })
          });
          const sData = await sRes.json();
          currentMeetingSummaryMarkdown = sData.summary;
          if (window.marked) outBox.innerHTML = marked.parse(sData.summary);
          else outBox.textContent = sData.summary;
          await fetch('/api/meetings/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: m.id, summary: sData.summary })
          });
          loadPastMeetings();
        } catch(e) {
          console.warn('Gagal re-summarize past meeting:', e);
        }
      }
    }

    async function loadMeetingSchedulesAndSprint() {
      try {
        const res = await fetch('/api/spectrum/data');
        if (!res.ok) return;
        const data = await res.json();
        
        if (data.needs_config) {
          if (document.getElementById('meetingSprintBadge')) {
            document.getElementById('meetingSprintBadge').textContent = 'SPEctrum Belum Konek';
          }
          if (document.getElementById('meetingSprintGoal')) {
            document.getElementById('meetingSprintGoal').textContent = 'Akun SPEctrum belum terhubung. Atur email & password di tab Config SPEctrum.';
          }
          const select = document.getElementById('meetingScheduleSelect');
          if (select) {
            select.innerHTML = '<option value="">-- SPEctrum Belum Terhubung (Atur di Config SPEctrum) --</option>';
          }
          return;
        }

        // 1. Update Sprint Badge & Goal
        const sprint = data.active_sprint || {};
        if (document.getElementById('meetingSprintBadge')) {
          document.getElementById('meetingSprintBadge').textContent = sprint.name || 'Sprint Aktif';
        }
        if (document.getElementById('meetingSprintGoal')) {
          document.getElementById('meetingSprintGoal').textContent = sprint.description || sprint.target || sprint.goal || '-';
        }

        // 2. Populate Dropdown Jadwal Kalender
        const select = document.getElementById('meetingScheduleSelect');
        if (select) {
          if (data.calendar_events && data.calendar_events.length) {
            const events = data.calendar_events;
            select.innerHTML = '<option value="">-- Pilih dari Jadwal SPEctrum --</option>' + events.map(ev => {
              const timeStr = ev.start ? ev.start.substring(11, 16) + ' WIB' : (ev.execute_at || '');
              const dateStr = ev.start ? ev.start.substring(0, 10) : '';
              return `<option value="${escapeHtml(ev.name)}">${escapeHtml(ev.name)} (${dateStr} ${timeStr})</option>`;
            }).join('');
          } else {
            select.innerHTML = '<option value="">-- Tidak ada agenda kalender terdekat --</option>';
          }
        }
      } catch (e) {
        console.warn('Gagal memuat jadwal rapat dari SPEctrum:', e);
      }
    }

    function onSelectScheduledMeeting(val) {
      if (!val) return;
      const titleInput = document.getElementById('meetingTitleInput');
      if (titleInput) {
        titleInput.value = val;
      }
      renderMeetingContext();
    }

    // ==================== MEETING TEST CASE EXTRACTION & APPROVAL ====================
    window._extractedTcList = [];

    async function extractTcFromPastMeeting(index) {
      viewPastMeeting(index);
      await extractTcFromCurrentMeeting();
      const panel = document.getElementById('meetingTcPanel');
      if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    async function extractTcFromCurrentMeeting() {
      const tcPanel = document.getElementById('meetingTcPanel');
      if (tcPanel) tcPanel.open = true;
      const transcript = document.getElementById('meetingTranscriptText')?.value.trim() || '';
      const summary = currentMeetingSummaryMarkdown || '';
      
      if (!transcript && !summary) {
        alert('Transkrip atau notulen rapat masih kosong. Buka notulen dari arsip atau rekam rapat terlebih dahulu.');
        return;
      }

      const btn = document.getElementById('btnExtractTcMeeting');
      const spinner = document.getElementById('extractTcSpinner');
      if (btn) btn.disabled = true;
      if (spinner) spinner.style.display = 'inline-block';

      const tbody = document.getElementById('extractedTcTableBody');
      if (tbody) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: #38bdf8; padding: 1.5rem 0;"><span style="animation:blink 1s infinite;">●</span> Hermes sedang menganalisis notulen rapat dan menyusun usulan Test Case...</td></tr>';
      }

      try {
        const res = await fetch('/api/meetings/extract-tc', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcript, summary })
        });
        const data = await res.json();
        if (data.success && Array.isArray(data.test_cases)) {
          window._extractedTcList = data.test_cases.map(tc => ({ ...tc, approved: true }));
          renderExtractedTcTable();
        } else {
          alert('Gagal mengekstrak test case: ' + (data.error || 'Format tidak sesuai'));
          if (tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: #f87171; padding: 1.5rem 0;">Gagal mengekstrak: ' + (data.error || 'Unknown error') + '</td></tr>';
        }
      } catch (err) {
        alert('Gagal menghubungi server: ' + err.message);
        if (tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: #f87171; padding: 1.5rem 0;">Error: ' + err.message + '</td></tr>';
      } finally {
        if (btn) btn.disabled = false;
        if (spinner) spinner.style.display = 'none';
      }
    }

    // ==================== SPRINT TEST CASES MODAL VIEWER ====================
    window._sprintTcCache = [];

    async function openSprintTestCasesModal() {
      const modal = document.getElementById('modalSprintTestCases');
      if (modal) modal.style.display = 'flex';
      
      const tbody = document.getElementById('modalSprintTcTableBody');
      const badge = document.getElementById('modalTcCountBadge');
      
      if (!window._sprintTcCache || !window._sprintTcCache.length) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #38bdf8; padding: 2rem;">Memuat daftar 71 Test Case dari Skenario Testing...</td></tr>';
        try {
          const res = await fetch('/api/sheets/testcases');
          const data = await res.json();
          if (data.success && Array.isArray(data.test_cases)) {
            window._sprintTcCache = data.test_cases;
          }
        } catch (e) {
          console.warn('Gagal memuat test cases:', e);
        }
      }
      
      renderSprintTestCasesModal(window._sprintTcCache);
    }

    function closeSprintTestCasesModal() {
      const modal = document.getElementById('modalSprintTestCases');
      if (modal) modal.style.display = 'none';
    }

    function filterSprintTestCases(q) {
      const query = (q || '').toLowerCase().trim();
      if (!query) {
        renderSprintTestCasesModal(window._sprintTcCache);
        return;
      }
      const filtered = (window._sprintTcCache || []).filter(tc => {
        return (tc.id || '').toLowerCase().includes(query) ||
               (tc.module || '').toLowerCase().includes(query) ||
               (tc.section || '').toLowerCase().includes(query) ||
               (tc.title || '').toLowerCase().includes(query) ||
               (tc.expected || '').toLowerCase().includes(query);
      });
      renderSprintTestCasesModal(filtered);
    }

    function renderSprintTestCasesModal(list) {
      const tbody = document.getElementById('modalSprintTcTableBody');
      const badge = document.getElementById('modalTcCountBadge');
      if (badge) badge.textContent = `${list.length} TC`;
      if (!tbody) return;

      if (!list || !list.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #71717a; padding: 2rem;">Tidak ada test case yang cocok dengan pencarian.</td></tr>';
        return;
      }

      tbody.innerHTML = list.map(tc => {
        const isNeg = tc.type && tc.type.toLowerCase().includes('negative');
        const badgeStyle = isNeg 
          ? 'background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3);' 
          : 'background:rgba(52,211,153,0.15); color:#34d399; border:1px solid rgba(52,211,153,0.3);';
        
        return `
          <tr>
            <td style="font-family: monospace; font-size: 0.74rem; font-weight: 600; color: #38bdf8;">${escapeHtml(tc.id || '-')}</td>
            <td style="color: #cbd5e1; font-weight: 500;">${escapeHtml(tc.module || '-')}</td>
            <td style="color: #94a3b8; font-size: 0.7rem;">${escapeHtml(tc.section || '-')}</td>
            <td style="color: #fff; font-weight: 500;">${escapeHtml(tc.title || '-')}</td>
            <td><span class="hermes-badge" style="${badgeStyle}; font-size: 0.62rem;">${escapeHtml(tc.type || 'Positive')}</span></td>
            <td style="color: #93c5fd; font-size: 0.72rem; line-height: 1.4; max-width: 320px; word-break: break-word;">${escapeHtml(tc.expected || '-').split('\\n').join('<br>')}</td>
            <td style="text-align: center;"><span class="hermes-badge badge-blue" style="font-size: 0.62rem;">${escapeHtml(tc.status_dev || 'Not Tested')}</span></td>
          </tr>
        `;
      }).join('');
    }

    function renderExtractedTcTable() {
      const tbody = document.getElementById('extractedTcTableBody');
      const countBadge = document.getElementById('meetingTcCountBadge');
      const exportBtn = document.getElementById('btnApproveAndExportTc');
      if (!tbody) return;

      const list = window._extractedTcList || [];
      if (countBadge) countBadge.textContent = `${list.length} Skenario Terdeteksi`;

      if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">Tidak ada skenario test case yang terdeteksi dari rapat ini.</td></tr>';
        if (exportBtn) exportBtn.style.display = 'none';
        return;
      }

      if (exportBtn) exportBtn.style.display = 'inline-flex';

      tbody.innerHTML = list.map((tc, idx) => {
        const isUpdate = (tc.action || '').toUpperCase() === 'UPDATE';
        const actionBadge = isUpdate
          ? `<span class="hermes-badge" style="background:rgba(56,189,248,0.15); color:#38bdf8; border:1px solid rgba(56,189,248,0.3); font-size: 0.64rem; font-weight:700;">UPDATE (${escapeHtml(tc.target_id || tc.id)})</span>`
          : `<span class="hermes-badge" style="background:rgba(52,211,153,0.15); color:#34d399; border:1px solid rgba(52,211,153,0.3); font-size: 0.64rem; font-weight:700;">CREATE BARU</span>`;

        const stepsStr = Array.isArray(tc.steps) 
          ? tc.steps.map((s, i) => `${i+1}. ${escapeHtml(s)}`).join('<br>') 
          : escapeHtml(tc.steps || '-').split('\\n').join('<br>');

        return `
          <tr style="${tc.approved ? '' : 'opacity: 0.5; background: rgba(0,0,0,0.2);'}">
            <td style="text-align: center;">
              <input type="checkbox" ${tc.approved ? 'checked' : ''} onchange="toggleTcApproval(${idx}, this.checked)" title="Centang untuk menyetujui">
            </td>
            <td style="text-align: center;">${actionBadge}</td>
            <td style="font-family: monospace; font-size: 0.74rem; font-weight: 600; color: #38bdf8;">${escapeHtml(tc.id || tc.target_id || `PNRI-${idx+10}`)}</td>
            <td style="color: #fff; font-weight: 500; font-size: 0.78rem;">
              <div>${escapeHtml(tc.title || tc.testcase || '')}</div>
              <div style="font-size: 0.68rem; color: #71717a; margin-top: 2px;">Modul: ${escapeHtml(tc.module || 'Umum')} • ${escapeHtml(tc.type || 'Positive')}</div>
            </td>
            <td style="font-size: 0.72rem; color: #cbd5e1; line-height: 1.4;">${stepsStr}</td>
            <td style="font-size: 0.72rem; color: #93c5fd; line-height: 1.4;">${escapeHtml(tc.expected || '-').split('\\n').join('<br>')}</td>
            <td style="font-size: 0.71rem; color: #fbbf24; line-height: 1.4;">${escapeHtml(tc.notes || '-')}</td>
            <td style="text-align: center;">
              <span class="hermes-badge ${tc.approved ? 'badge-ok' : 'badge-blue'}" style="font-size: 0.62rem;">
                ${tc.approved ? 'Disetujui' : 'Lewati'}
              </span>
            </td>
          </tr>
        `;
      }).join('');

      updateSelectedTcCount();
    }

    function toggleTcApproval(idx, approved) {
      if (window._extractedTcList[idx]) {
        window._extractedTcList[idx].approved = approved;
      }
      renderExtractedTcTable();
    }

    function toggleSelectAllTc(checked) {
      (window._extractedTcList || []).forEach(tc => tc.approved = checked);
      renderExtractedTcTable();
    }

    function updateSelectedTcCount() {
      const selected = (window._extractedTcList || []).filter(tc => tc.approved);
      const el = document.getElementById('selectedTcCount');
      if (el) el.textContent = selected.length;
      const btn = document.getElementById('btnApproveAndExportTc');
      if (btn) btn.disabled = (selected.length === 0);
    }

    async function approveAndExportSelectedTc() {
      const selected = (window._extractedTcList || []).filter(tc => tc.approved);
      if (!selected.length) {
        alert('Pilih minimal 1 test case yang disetujui untuk dikirim ke Google Sheet.');
        return;
      }

      const tabInput = document.getElementById('targetSheetTabName');
      const sheet_name = (tabInput ? tabInput.value.trim() : '') || 'BNI YAP! - Sprint 6';
      const author = CURRENT_USER_NAME || 'QA Engineer';

      if (!confirm(`Konfirmasi: Kirim ${selected.length} Test Case yang Anda setujui ke Google Spreadsheet pada Tab Sheet '${sheet_name}' (Author: ${author})?`)) {
        return;
      }

      const btn = document.getElementById('btnApproveAndExportTc');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Menyimpan ke Google Sheet...';
      }

      try {
        const res = await fetch('/api/sheets/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ test_cases: selected, sheet_name, author })
        });
        const data = await res.json();
        if (data.success) {
          const summaryMsg = (data.updated ? `${data.updated} TC di-update, ` : '') + (data.created ? `${data.created} TC baru ditambahkan.` : `${data.count} TC tersinkronisasi.`);
          alert(`Berhasil! ${summaryMsg} Sinkronisasi ke Tab '${sheet_name}' sukses.`);
          if (btn) {
            btn.innerHTML = '✓ Selesai Disinkronkan';
            btn.style.color = '#34d399';
          }
        } else {
          alert('Gagal ekspor ke Google Sheet: ' + (data.error || 'Unknown error'));
          if (btn) btn.innerHTML = `✓ Setujui & Terapkan ke Google Sheet (${selected.length})`;
        }
      } catch (err) {
        alert('Gagal menghubungi server: ' + err.message);
        if (btn) btn.innerHTML = `✓ Setujui & Terapkan ke Google Sheet (${selected.length})`;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function fetchDeployStatus() {
      try {
        const res = await fetch('/api/deploy/status');
        if (!res.ok) return;
        const data = await res.json();
        const commitEl = document.getElementById('deployCommitText');
        const timeEl = document.getElementById('deployTimeText');
        const msgEl = document.getElementById('deployMsgText');
        const badgeEl = document.getElementById('deployStatusBadge');
        const badgeHeader = document.getElementById('deployBadgeHeader');

        if (commitEl) commitEl.textContent = `${data.commit_short || '--'} (${data.commit_author || 'system'})`;
        if (timeEl) timeEl.textContent = data.timestamp ? new Date(data.timestamp).toLocaleString('id-ID') : '--';
        if (msgEl) msgEl.textContent = data.commit_message || '--';

        if (badgeEl && data.status) {
          if (data.status === 'success' || data.status === 'up_to_date') {
            badgeEl.className = 'hermes-badge badge-ok';
            badgeEl.textContent = 'AKTIF (Tersinkron)';
            if (badgeHeader) {
              badgeHeader.className = 'hermes-badge badge-ok';
              badgeHeader.textContent = 'AKTIF (Tersinkron)';
            }
          } else if (data.status === 'deploying') {
            badgeEl.className = 'hermes-badge badge-warn';
            badgeEl.textContent = 'SEDANG DEPLOY...';
            if (badgeHeader) {
              badgeHeader.className = 'hermes-badge badge-warn';
              badgeHeader.textContent = 'SEDANG DEPLOY...';
            }
          }
        }
      } catch (e) {
        console.warn('Gagal fetch deploy status:', e);
      }
    }

    async function triggerManualDeploy() {
      const btn = document.getElementById('btnManualDeploy');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Sedang Memulai Deploy...';
      }
      try {
        const res = await fetch('/api/deploy/trigger', { method: 'POST' });
        const data = await res.json();
        alert('Perintah deploy berhasil dikirim ke server. Server sedang menarik pembaruan dari origin/main di background!');
        setTimeout(fetchDeployStatus, 2000);
      } catch (err) {
        alert('Gagal memicu deploy: ' + err.message);
      } finally {
        if (btn) {
          setTimeout(() => {
            btn.disabled = false;
            btn.textContent = 'Deploy Sekarang (Tarik origin/main)';
          }, 3000);
        }
      }
    }

    function copyWebhookUrl() {
      const input = document.getElementById('webhookUrlInput');
      if (input) {
        navigator.clipboard.writeText(input.value);
        alert('Webhook URL berhasil disalin: ' + input.value);
      }
    }

    async function fetchSystemResources(force = false) {
      try {
        const res = await fetch('/api/system/resources');
        if (!res.ok) return;
        const data = await res.json();
        if (!data.success) return;

        // 1. CPU
        const cpu = data.cpu || {};
        const cpuPct = Math.round(cpu.percent || 0);
        const cpuPctEl = document.getElementById('sysCpuPct');
        if (cpuPctEl) cpuPctEl.textContent = cpuPct;
        const cpuBar = document.getElementById('sysCpuBar');
        if (cpuBar) {
          cpuBar.style.width = Math.min(cpuPct, 100) + '%';
          cpuBar.style.background = cpuPct >= 80 ? 'linear-gradient(90deg, #ef4444, #dc2626)' : (cpuPct >= 60 ? 'linear-gradient(90deg, #f59e0b, #d97706)' : 'linear-gradient(90deg, #38bdf8, #0284c7)');
        }
        const cpuTempBadge = document.getElementById('sysCpuTempBadge');
        if (cpuTempBadge) {
          cpuTempBadge.textContent = cpu.temp_c ? `${cpu.temp_c} °C` : '-- °C';
          cpuTempBadge.className = (cpu.temp_c && cpu.temp_c >= 70) ? 'hermes-badge badge-warn' : 'hermes-badge badge-blue';
        }
        const cpuFreqEl = document.getElementById('sysCpuFreq');
        if (cpuFreqEl) cpuFreqEl.textContent = cpu.freq_mhz || 1800;
        const loadAvgEl = document.getElementById('sysLoadAvg');
        if (loadAvgEl && cpu.load_avg) loadAvgEl.textContent = cpu.load_avg.join(', ');

        // 2. RAM
        const mem = data.memory || {};
        const memPct = Math.round(mem.percent || 0);
        const memPctEl = document.getElementById('sysMemPct');
        if (memPctEl) memPctEl.textContent = memPct;
        const memBar = document.getElementById('sysMemBar');
        if (memBar) {
          memBar.style.width = Math.min(memPct, 100) + '%';
          memBar.style.background = memPct >= 85 ? 'linear-gradient(90deg, #ef4444, #dc2626)' : (memPct >= 70 ? 'linear-gradient(90deg, #f59e0b, #d97706)' : 'linear-gradient(90deg, #10b981, #059669)');
        }
        const memUsedEl = document.getElementById('sysMemUsed');
        if (memUsedEl) memUsedEl.textContent = mem.used_mb || '--';
        const memTotalEl = document.getElementById('sysMemTotal');
        if (memTotalEl) memTotalEl.textContent = mem.total_mb || '--';
        const memFreeText = document.getElementById('sysMemFreeText');
        if (memFreeText) memFreeText.textContent = `${mem.free_mb || '--'} MB Bebas`;

        // 3. Disk NVMe
        const disk = data.disk || {};
        const diskPct = Math.round(disk.percent || 0);
        const diskPctEl = document.getElementById('sysDiskPct');
        if (diskPctEl) diskPctEl.textContent = diskPct;
        const diskBar = document.getElementById('sysDiskBar');
        if (diskBar) diskBar.style.width = Math.min(diskPct, 100) + '%';
        const diskUsedEl = document.getElementById('sysDiskUsed');
        if (diskUsedEl) diskUsedEl.textContent = disk.used_gb || '--';
        const diskFreeEl = document.getElementById('sysDiskFree');
        if (diskFreeEl) diskFreeEl.textContent = disk.free_gb || '--';
        const nvmeTempBadge = document.getElementById('sysNvmeTempBadge');
        if (nvmeTempBadge) nvmeTempBadge.textContent = disk.nvme_temp_c ? `${disk.nvme_temp_c} °C` : '-- °C';

        // 4. Uptime & Network
        const uptime = data.uptime || {};
        const uptimeEl = document.getElementById('sysUptime');
        if (uptimeEl) uptimeEl.textContent = uptime.formatted || '--';
        const bootTimeEl = document.getElementById('sysBootTime');
        if (bootTimeEl) bootTimeEl.textContent = uptime.boot_time || '--';
        const netIpEl = document.getElementById('sysNetworkIp');
        if (netIpEl && data.network) netIpEl.textContent = data.network.ip || '--';

        // 5. Services List
        const sListEl = document.getElementById('sysServicesList');
        if (sListEl && Array.isArray(data.services)) {
          sListEl.innerHTML = data.services.map(s => {
            const badgeClass = s.is_active ? 'hermes-badge badge-ok' : 'hermes-badge badge-warn';
            const badgeText = s.is_active ? 'ACTIVE' : s.status.toUpperCase();
            return `
              <div class="stat-sub" style="border: none; padding: 0; display: flex; justify-content: space-between; align-items: center;">
                <div>
                  <span style="color: #cbd5e1; font-weight: 600; font-family: monospace; font-size: 0.78rem;">${escapeHtml(s.name)}</span>
                  <div style="font-size: 0.7rem; color: #71717a;">${escapeHtml(s.description)}</div>
                </div>
                <span class="${badgeClass}">${badgeText}</span>
              </div>
            `;
          }).join('');
        }
      } catch (err) {
        console.warn('Gagal memuat system resources:', err);
      }
    }

    // ==================== WEBHOOK & API MONITOR LOGIC ====================
    let whSelectedReqId = null;
    let whMethodFilter = 'ALL';
    let whSearchQuery = '';
    let whStreamTimer = null;
    let whIsPaused = false;
    let whRequestsCache = [];
    let whSearchDebounceTimer = null;

    function getWhActiveEndpoint() {
      const origin = window.location.origin;
      return origin + '/catch/demo';
    }

    function copyWebhookActiveUrl() {
      const url = getWhActiveEndpoint();
      navigator.clipboard.writeText(url).then(() => {
        showToast('URL Webhook publik berhasil disalin.', 'success');
      });
    }

    async function sendQuickTestWebhook() {
      const url = '/catch/demo';
      try {
        const payload = {
          event: 'test.webhook',
          service: 'Westpro Monitor',
          timestamp: new Date().toISOString(),
          data: {
            user_id: 101,
            message: 'Hello from Webhook Test Button!'
          }
        };
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Test-Sender': 'Dashboard-UI' },
          body: JSON.stringify(payload)
        });
        const ret = await res.json();
        showToast('Webhook uji coba berhasil dikirim. Response: ' + res.status, 'success');
        await loadWebhookRequests(false);
      } catch (e) {
        showToast('Gagal kirim test webhook: ' + e, 'error');
      }
    }

    function toggleWebhookStreamPause() {
      whIsPaused = !whIsPaused;
      const btn = document.getElementById('btnWhPauseStream');
      const ind = document.getElementById('whLiveIndicator');
      if (btn) btn.textContent = whIsPaused ? 'Lanjutkan' : 'Jeda';
      if (ind) {
        ind.textContent = whIsPaused ? 'DIJEDA' : 'LIVE STREAM';
        ind.className = whIsPaused ? 'hermes-badge badge-warn' : 'hermes-badge badge-ok';
      }
    }

    function startWebhookAutoStream() {
      if (whStreamTimer) clearInterval(whStreamTimer);
      whStreamTimer = setInterval(() => {
        if (!whIsPaused) {
          loadWebhookRequests(false);
        }
      }, 2000);
    }

    function stopWebhookAutoStream() {
      if (whStreamTimer) {
        clearInterval(whStreamTimer);
        whStreamTimer = null;
      }
    }

    function setWhMethodFilter(method, el) {
      whMethodFilter = method;
      document.querySelectorAll('.wh-filter-btn').forEach(b => b.classList.remove('active'));
      if (el) el.classList.add('active');
      loadWebhookRequests(false);
    }

    function debounceWhSearch() {
      clearTimeout(whSearchDebounceTimer);
      whSearchDebounceTimer = setTimeout(() => {
        const inp = document.getElementById('whSearchInput');
        whSearchQuery = (inp?.value || '').trim();
        loadWebhookRequests(false);
      }, 300);
    }

    async function loadWebhookRequests(initial = false) {
      const epInp = document.getElementById('whActiveEndpointInput');
      if (epInp && !epInp.value) epInp.value = getWhActiveEndpoint();

      let url = '/api/monitor/requests?limit=60';
      if (whMethodFilter && whMethodFilter !== 'ALL') url += '&method=' + encodeURIComponent(whMethodFilter);
      if (whSearchQuery) url += '&search=' + encodeURIComponent(whSearchQuery);

      try {
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();
        whRequestsCache = data.requests || [];

        const totalEl = document.getElementById('whReqTotalCount');
        if (totalEl) totalEl.textContent = data.total || 0;

        const navBadge = document.getElementById('navWebhookBadge');
        if (navBadge) {
          if (data.total > 0) {
            navBadge.style.display = 'inline-block';
            navBadge.textContent = data.total;
          } else {
            navBadge.style.display = 'none';
          }
        }

        renderWebhookRequestsList(whRequestsCache);

        if (initial && whRequestsCache.length > 0 && !whSelectedReqId) {
          inspectWebhookRequest(whRequestsCache[0].id);
        }
      } catch (e) {
        console.warn('Gagal load webhook requests:', e);
      }
    }

    function getMethodBadgeClass(m) {
      m = (m || '').toUpperCase();
      if (m === 'POST') return 'badge-post';
      if (m === 'GET') return 'badge-get';
      if (m === 'PUT') return 'badge-put';
      if (m === 'DELETE') return 'badge-delete';
      if (m === 'PATCH') return 'badge-patch';
      return 'badge-other';
    }

    function renderWebhookRequestsList(items) {
      const container = document.getElementById('whRequestsListContainer');
      if (!container) return;
      if (!items || !items.length) {
        container.innerHTML = '<div style="color:#64748b; font-size:0.75rem; text-align:center; padding:2rem;">Belum ada request yang tertangkap.<br><small style="color:#94a3b8;">Kirim request ke URL webhook publik Anda.</small></div>';
        return;
      }

      container.innerHTML = items.map(r => {
        const isSel = r.id === whSelectedReqId;
        const dt = r.timestamp ? new Date(r.timestamp).toLocaleTimeString('id-ID', { hour:'2-digit', minute:'2-digit', second:'2-digit' }) : '--';
        const stColor = r.status_code >= 400 ? '#f87171' : '#34d399';
        const badgeClass = getMethodBadgeClass(r.method);
        return `
          <div class="wh-req-item ${isSel ? 'active' : ''}" onclick="inspectWebhookRequest(${r.id})">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:0.4rem;">
              <div style="display:flex; align-items:center; gap:0.4rem; overflow:hidden;">
                <span class="wh-badge-method ${badgeClass}">${r.method}</span>
                <span style="font-weight:600; font-size:0.78rem; color:#f1f5f9; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:'JetBrains Mono',monospace;">${r.path}</span>
              </div>
              <span style="font-size:0.68rem; color:#64748b; flex-shrink:0;">${dt}</span>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; font-size:0.68rem; color:#94a3b8;">
              <div style="display:flex; align-items:center; gap:0.35rem;">
                <span style="color:${stColor}; font-weight:700;">${r.status_code}</span>
                <span>&bull;</span>
                <span>${r.duration_ms}ms</span>
                ${r.is_mocked ? '<span style="background:rgba(245,158,11,0.2); color:#fbbf24; padding:0 0.3rem; border-radius:2px; font-weight:700;">MOCK</span>' : ''}
              </div>
              <span style="color:#64748b; font-family:monospace;">${r.client_ip || ''}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    async function inspectWebhookRequest(reqId) {
      whSelectedReqId = reqId;
      renderWebhookRequestsList(whRequestsCache);

      const emptyEl = document.getElementById('whInspectorEmpty');
      const activeEl = document.getElementById('whInspectorActive');
      if (emptyEl) emptyEl.style.display = 'none';
      if (activeEl) activeEl.style.display = 'flex';

      try {
        const res = await fetch('/api/monitor/requests/' + reqId);
        if (!res.ok) return;
        const d = await res.json();
        window._currInspectedReq = d;

        const mBadge = document.getElementById('inspMethodBadge');
        if (mBadge) {
          mBadge.textContent = d.method;
          mBadge.className = 'wh-badge-method ' + getMethodBadgeClass(d.method);
        }
        const pathEl = document.getElementById('inspPath');
        if (pathEl) pathEl.textContent = d.path;

        const stBadge = document.getElementById('inspStatusBadge');
        if (stBadge) {
          stBadge.textContent = d.status_code + (d.status_code === 200 ? ' OK' : '');
          stBadge.style.color = d.status_code >= 400 ? '#f87171' : '#10b981';
          stBadge.style.background = d.status_code >= 400 ? 'rgba(239,68,68,0.15)' : 'rgba(16,185,129,0.15)';
        }

        const mockBadge = document.getElementById('inspMockedBadge');
        if (mockBadge) {
          mockBadge.style.display = d.is_mocked ? 'inline-block' : 'none';
          if (d.rule_name) mockBadge.title = 'Matched rule: ' + d.rule_name;
        }

        const timeEl = document.getElementById('inspTimestamp');
        if (timeEl) timeEl.textContent = d.timestamp ? new Date(d.timestamp).toLocaleString('id-ID') : '--';

        const durEl = document.getElementById('inspDuration');
        if (durEl) durEl.textContent = d.duration_ms + ' ms';

        const ipEl = document.getElementById('inspClientIp');
        if (ipEl) ipEl.textContent = d.client_ip || '--';

        const sizeEl = document.getElementById('inspBodySize');
        if (sizeEl) sizeEl.textContent = (d.body_bytes_len || 0) + ' bytes';

        const ctBadge = document.getElementById('inspContentTypeBadge');
        if (ctBadge) ctBadge.textContent = d.content_type || 'plain';

        const bodyPre = document.getElementById('inspBodyPre');
        if (bodyPre) {
          if (!d.body) {
            bodyPre.textContent = '(Tidak ada body payload)';
            bodyPre.style.color = '#64748b';
          } else {
            try {
              const parsed = JSON.parse(d.body);
              bodyPre.textContent = JSON.stringify(parsed, null, 2);
              bodyPre.style.color = '#a5b4fc';
            } catch(e) {
              bodyPre.textContent = d.body;
              bodyPre.style.color = '#f1f5f9';
            }
          }
        }

        const hdrs = d.headers || {};
        const hdrsCount = document.getElementById('inspHeadersCount');
        if (hdrsCount) hdrsCount.textContent = Object.keys(hdrs).length;
        const hdrsTable = document.getElementById('inspHeadersTable');
        if (hdrsTable) {
          const rows = Object.entries(hdrs).map(([k, v]) => `
            <div style="display:flex; border-bottom:1px solid #1e293b; font-size:0.75rem; font-family:'JetBrains Mono',monospace;">
              <div style="width:200px; padding:0.45rem 0.65rem; color:#38bdf8; font-weight:600; background:#141720; border-right:1px solid #1e293b; overflow:hidden; text-overflow:ellipsis;">${k}</div>
              <div style="flex:1; padding:0.45rem 0.65rem; color:#cbd5e1; word-break:break-all;">${v}</div>
            </div>
          `).join('');
          hdrsTable.innerHTML = rows || '<div style="padding:1rem; color:#64748b; font-size:0.75rem; text-align:center;">Tidak ada headers</div>';
        }

        const params = d.query_params || {};
        const paramsCount = document.getElementById('inspParamsCount');
        if (paramsCount) paramsCount.textContent = Object.keys(params).length;
        const paramsTable = document.getElementById('inspParamsTable');
        if (paramsTable) {
          const rows = Object.entries(params).map(([k, v]) => `
            <div style="display:flex; border-bottom:1px solid #1e293b; font-size:0.75rem; font-family:'JetBrains Mono',monospace;">
              <div style="width:160px; padding:0.45rem 0.65rem; color:#a3e635; font-weight:600; background:#141720; border-right:1px solid #1e293b;">${k}</div>
              <div style="flex:1; padding:0.45rem 0.65rem; color:#cbd5e1;">${v}</div>
            </div>
          `).join('');
          paramsTable.innerHTML = rows || '<div style="padding:1rem; color:#64748b; font-size:0.75rem; text-align:center;">Tidak ada query parameters</div>';
        }

        const respStatus = document.getElementById('inspRespStatus');
        if (respStatus) respStatus.textContent = d.status_code;
        const respBodyPre = document.getElementById('inspRespBodyPre');
        if (respBodyPre) {
          if (!d.response_body) {
            respBodyPre.textContent = '(Tidak ada response body)';
          } else {
            try {
              const parsed = JSON.parse(d.response_body);
              respBodyPre.textContent = JSON.stringify(parsed, null, 2);
            } catch(e) {
              respBodyPre.textContent = d.response_body;
            }
          }
        }

        const curlPre = document.getElementById('inspCurlPre');
        if (curlPre) {
          const origin = window.location.origin;
          let cmd = 'curl -X ' + d.method + ' "' + origin + d.url + '"';
          for (const [k, v] of Object.entries(hdrs)) {
            if (['host', 'content-length'].includes(k.toLowerCase())) continue;
            cmd += ' \\\n  -H "' + k + ': ' + v + '"';
          }
          if (d.body && ['POST', 'PUT', 'PATCH'].includes(d.method)) {
            cmd += ' \\\n  --data-raw ' + JSON.stringify(d.body);
          }
          curlPre.textContent = cmd;
        }

      } catch (e) {
        console.error('Gagal fetch request detail:', e);
      }
    }

    function switchInspTab(tabId, el) {
      document.querySelectorAll('.insp-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.insp-tab-content').forEach(c => c.style.display = 'none');
      if (el) el.classList.add('active');
      const target = document.getElementById(tabId);
      if (target) target.style.display = 'flex';
    }

    function copyInspBody() {
      const el = document.getElementById('inspBodyPre');
      if (el) {
        navigator.clipboard.writeText(el.textContent).then(() => {
          showToast('Body payload disalin.', 'success');
        });
      }
    }

    function copyInspHeaders() {
      if (window._currInspectedReq && window._currInspectedReq.headers) {
        navigator.clipboard.writeText(JSON.stringify(window._currInspectedReq.headers, null, 2)).then(() => {
          showToast('Headers disalin sebagai JSON.', 'success');
        });
      }
    }

    function copyInspCurl() {
      const el = document.getElementById('inspCurlPre');
      if (el) {
        navigator.clipboard.writeText(el.textContent).then(() => {
          showToast('Perintah cURL disalin ke clipboard.', 'success');
        });
      }
    }

    async function clearAllWebhookRequests() {
      if (!confirm('Bersihkan semua riwayat request webhook?')) return;
      try {
        await fetch('/api/monitor/requests', { method: 'DELETE' });
        whSelectedReqId = null;
        whRequestsCache = [];
        renderWebhookRequestsList([]);
        const emptyEl = document.getElementById('whInspectorEmpty');
        const activeEl = document.getElementById('whInspectorActive');
        if (emptyEl) emptyEl.style.display = 'flex';
        if (activeEl) activeEl.style.display = 'none';
        const totalEl = document.getElementById('whReqTotalCount');
        if (totalEl) totalEl.textContent = '0';
        showToast('Semua request berhasil dibersihkan.', 'success');
      } catch (e) {
        alert('Gagal membersihkan: ' + e);
      }
    }

    async function deleteCurrentInspectedRequest() {
      if (!whSelectedReqId) return;
      try {
        await fetch('/api/monitor/requests/' + whSelectedReqId, { method: 'DELETE' });
        showToast('Request berhasil dihapus', 'success');
        whSelectedReqId = null;
        loadWebhookRequests(false);
      } catch (e) {
        alert('Gagal menghapus request: ' + e);
      }
    }

    function exportWebhookJson() {
      if (!whRequestsCache.length) {
        alert('Tidak ada request untuk diekspor');
        return;
      }
      const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(whRequestsCache, null, 2));
      const dl = document.createElement('a');
      dl.setAttribute("href", dataStr);
      dl.setAttribute("download", `webhook_requests_${Date.now()}.json`);
      dl.click();
    }

    function openReplayModal() {
      if (!window._currInspectedReq) return;
      const modal = document.getElementById('replayModal');
      const inp = document.getElementById('replayTargetUrlInput');
      const box = document.getElementById('replayStatusBox');
      if (modal) modal.style.display = 'flex';
      if (inp) inp.value = window.location.origin + window._currInspectedReq.path;
      if (box) box.style.display = 'none';
    }

    function closeReplayModal() {
      const modal = document.getElementById('replayModal');
      if (modal) modal.style.display = 'none';
    }

    async function submitReplayRequest() {
      if (!whSelectedReqId) return;
      const inp = document.getElementById('replayTargetUrlInput');
      const box = document.getElementById('replayStatusBox');
      const btn = document.getElementById('btnSubmitReplay');
      const targetUrl = (inp?.value || '').trim();

      if (btn) btn.textContent = 'Mengirim ulang...';
      if (box) {
        box.style.display = 'block';
        box.style.background = 'rgba(56,189,248,0.15)';
        box.style.color = '#38bdf8';
        box.textContent = 'Mengirim request ke ' + targetUrl + '...';
      }

      try {
        const res = await fetch('/api/monitor/replay', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: whSelectedReqId, target_url: targetUrl })
        });
        const ret = await res.json();
        if (box) {
          if (ret.success) {
            box.style.background = 'rgba(16,185,129,0.15)';
            box.style.color = '#34d399';
            box.textContent = `Berhasil dikirim. Status: ${ret.status_code} (${ret.duration_ms}ms)\n\nResponse:\n${ret.response_body || '(Kosong)'}`;
          } else {
            box.style.background = 'rgba(239,68,68,0.15)';
            box.style.color = '#f87171';
            box.textContent = `Gagal: ${ret.error || 'HTTP ' + ret.status_code}\n\n${ret.response_body || ''}`;
          }
        }
        await loadWebhookRequests(false);
      } catch (e) {
        if (box) {
          box.style.background = 'rgba(239,68,68,0.15)';
          box.style.color = '#f87171';
          box.textContent = 'Error: ' + e;
        }
      } finally {
        if (btn) btn.textContent = 'Kirim Sekarang';
      }
    }

    function openMockRulesModal() {
      const modal = document.getElementById('mockRulesModal');
      if (modal) modal.style.display = 'flex';
      loadWebhookMockRules();
    }

    function closeMockRulesModal() {
      const modal = document.getElementById('mockRulesModal');
      if (modal) modal.style.display = 'none';
    }

    async function loadWebhookMockRules() {
      try {
        const res = await fetch('/api/monitor/rules');
        if (!res.ok) return;
        const data = await res.json();
        const rules = Array.isArray(data) ? data : (data.rules || []);
        window._currentMockRules = rules;

        const countEl = document.getElementById('whRulesCount');
        if (countEl) countEl.textContent = rules.length;

        renderWebhookMockRules(rules);
      } catch (e) {
        console.warn('Gagal load mock rules:', e);
      }
    }

    function renderWebhookMockRules(rules) {
      const container = document.getElementById('mockRulesListContainer');
      if (!container) return;
      if (!rules.length) {
        container.innerHTML = '<div style="color:#64748b; font-size:0.75rem; text-align:center; padding:1rem;">Belum ada aturan mock. Buat aturan di formulir atas.</div>';
        return;
      }

      container.innerHTML = rules.map(r => {
        const isEn = !!r.enabled;
        return `
          <div style="background:#141720; border:1px solid ${isEn ? '#334155' : '#272a31'}; border-radius:6px; padding:0.65rem 0.85rem; display:flex; justify-content:space-between; align-items:center; gap:0.5rem;">
            <div style="min-width:0; flex:1;">
              <div style="display:flex; align-items:center; gap:0.4rem; margin-bottom:0.2rem; flex-wrap:wrap;">
                <span style="font-weight:700; font-size:0.8rem; color:#f1f5f9;">${r.name}</span>
                <span style="font-size:0.65rem; background:rgba(56,189,248,0.15); color:#38bdf8; padding:0.1rem 0.35rem; border-radius:3px; font-weight:700;">${r.method}</span>
                <span style="font-size:0.65rem; background:rgba(16,185,129,0.15); color:#34d399; padding:0.1rem 0.35rem; border-radius:3px; font-weight:700;">HTTP ${r.status_code}</span>
                ${r.delay_ms > 0 ? `<span style="font-size:0.65rem; color:#94a3b8;">${r.delay_ms}ms</span>` : ''}
              </div>
              <div style="font-size:0.72rem; color:#a3e635; font-family:'JetBrains Mono',monospace;">Pattern: ${r.path_pattern}</div>
            </div>
            <div style="display:flex; align-items:center; gap:0.4rem; flex-shrink:0;">
              <button onclick="editMockRule('${r.id}')" style="background:rgba(56,189,248,0.12); color:#38bdf8; border:1px solid rgba(56,189,248,0.3); border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; font-weight:600; cursor:pointer;" title="Edit aturan ini">Edit</button>
              <button onclick="toggleMockRule('${r.id}')" style="background:${isEn ? '#10b981' : '#334155'}; color:#fff; border:none; border-radius:4px; padding:0.25rem 0.6rem; font-size:0.7rem; font-weight:700; cursor:pointer;" title="Klik untuk mengaktifkan / menonaktifkan">${isEn ? 'Aktif' : 'Mati'}</button>
              <button onclick="deleteMockRule('${r.id}')" style="background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3); border-radius:4px; padding:0.25rem 0.5rem; font-size:0.7rem; cursor:pointer;" title="Hapus aturan">Hapus</button>
            </div>
          </div>
        `;
      }).join('');
    }

    function editMockRule(ruleId) {
      const rule = (window._currentMockRules || []).find(r => r.id === ruleId);
      if (!rule) return;
      document.getElementById('newRuleName').value = rule.name || '';
      document.getElementById('newRulePath').value = rule.path_pattern || '';
      document.getElementById('newRuleMethod').value = rule.method || 'ALL';
      document.getElementById('newRuleStatus').value = String(rule.status_code || 200);
      document.getElementById('newRuleDelay').value = String(rule.delay_ms || 0);
      document.getElementById('newRuleBody').value = (typeof rule.response_body === 'string') ? rule.response_body : JSON.stringify(rule.response_body, null, 2);
      window._editingRuleId = rule.id;

      const title = document.getElementById('mockRuleFormTitle');
      if (title) title.textContent = 'Edit Aturan Mock: ' + rule.name;
      const cancelBtn = document.getElementById('btnCancelEditRule');
      if (cancelBtn) cancelBtn.style.display = 'inline-block';
      const saveBtn = document.getElementById('btnSubmitMockRule');
      if (saveBtn) saveBtn.textContent = 'Perbarui Aturan';

      document.getElementById('newRuleName')?.focus();
    }

    function cancelEditMockRule() {
      window._editingRuleId = null;
      document.getElementById('newRuleName').value = '';
      document.getElementById('newRulePath').value = '/catch/*';
      document.getElementById('newRuleMethod').value = 'POST';
      document.getElementById('newRuleStatus').value = '200';
      document.getElementById('newRuleDelay').value = '0';
      document.getElementById('newRuleBody').value = '{\\n  "status": "success",\\n  "message": "Custom Mock Response"\\n}';

      const title = document.getElementById('mockRuleFormTitle');
      if (title) title.textContent = 'Buat Aturan Mock Baru';
      const cancelBtn = document.getElementById('btnCancelEditRule');
      if (cancelBtn) cancelBtn.style.display = 'none';
      const saveBtn = document.getElementById('btnSubmitMockRule');
      if (saveBtn) saveBtn.textContent = 'Simpan Aturan';
    }

    async function saveNewMockRule() {
      const name = (document.getElementById('newRuleName')?.value || '').trim();
      const path_pattern = (document.getElementById('newRulePath')?.value || '').trim();
      const method = document.getElementById('newRuleMethod')?.value || 'ALL';
      const status_code = parseInt(document.getElementById('newRuleStatus')?.value || '200');
      const delay_ms = parseInt(document.getElementById('newRuleDelay')?.value || '0');
      const response_body = document.getElementById('newRuleBody')?.value || '';

      if (!name || !path_pattern) {
        alert('Nama aturan dan Path pattern wajib diisi');
        return;
      }

      const saveBtn = document.getElementById('btnSubmitMockRule');
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Menyimpan...';
      }

      const payload = {
        name, path_pattern, method, status_code, delay_ms, response_body
      };
      if (window._editingRuleId) {
        payload.id = window._editingRuleId;
      }

      try {
        const res = await fetch('/api/monitor/rules', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const ret = await res.json();
        if (ret.success) {
          showToast(window._editingRuleId ? 'Aturan Mock berhasil diperbarui!' : 'Aturan Mock berhasil disimpan!', 'success');
          cancelEditMockRule();
          await loadWebhookMockRules();
        } else {
          alert('Gagal simpan rule: ' + (ret.error || 'Terjadi kesalahan'));
        }
      } catch (e) {
        alert('Gagal: ' + e);
      } finally {
        if (saveBtn) {
          saveBtn.disabled = false;
          saveBtn.textContent = window._editingRuleId ? 'Perbarui Aturan' : 'Simpan Aturan';
        }
      }
    }

    async function toggleMockRule(ruleId) {
      try {
        const res = await fetch(`/api/monitor/rules/${encodeURIComponent(ruleId)}/toggle`, { method: 'POST' });
        const ret = await res.json();
        if (ret.success) {
          showToast('Status aturan mock diperbarui', 'info');
        }
        await loadWebhookMockRules();
      } catch (e) {
        alert('Gagal toggle: ' + e);
      }
    }

    async function deleteMockRule(ruleId) {
      if (!confirm('Hapus aturan mock ini?')) return;
      try {
        const res = await fetch(`/api/monitor/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' });
        const ret = await res.json();
        if (ret.success) {
          showToast('Aturan mock dihapus', 'success');
        } else {
          alert('Gagal hapus: ' + (ret.error || 'Terjadi kesalahan'));
        }
        await loadWebhookMockRules();
      } catch (e) {
        alert('Gagal hapus: ' + e);
      }
    }

    // Startup
    function initDashboard() {
      // 0. Clean DOM for non-owner
      if (!IS_ARES_OWNER) {
        const pSolar = document.getElementById('page-solar');
        if (pSolar) pSolar.remove();
        const pHist = document.getElementById('page-history');
        if (pHist) pHist.remove();
        const nSolar = document.getElementById('nav-solar');
        if (nSolar) nSolar.remove();
        const nHist = document.getElementById('nav-history');
        if (nHist) nHist.remove();
        ['otp', 'webhook', 'system', 'inverter-settings', '9router', 'teams', 'saham'].forEach(function (pg) {
          const sec = document.getElementById('page-' + pg);
          if (sec) sec.remove();
          const nav = document.getElementById('nav-' + pg);
          if (nav) nav.remove();
        });
      }

      if (IS_ARES_OWNER) {
        const agentWrap = document.getElementById('aiAgentToggleWrap');
        if (agentWrap) agentWrap.style.display = 'flex';
      } else {
        // Fitur khusus pemilik server disembunyikan untuk user lain
        ['chipOtp', 'hermesSolarCfg'].forEach(function (id) { const el = document.getElementById(id); if (el) el.remove(); });
        document.querySelectorAll('.owner-only').forEach(function (el) { el.remove(); });
        const devPanel = document.getElementById('androidDevicesPanel');
        if (devPanel) devPanel.className = 'panel col-12';
      }

      // 1. Initial page navigation
      try {
        let initial = '__INITIAL_PAGE__';
        if (!initial || initial.startsWith('__')) {
          const path = window.location.pathname.startsWith('/') ? window.location.pathname.slice(1) : window.location.pathname;
          initial = path || (IS_ARES_OWNER ? 'solar' : 'hermes');
        }
        if (initial === 'spectrum-config') initial = 'profile';
        if (!IS_ARES_OWNER && OWNER_ONLY_PAGES.includes(initial)) {
          initial = 'hermes';
        }
        navigateToPage(initial, false);
      } catch (err) {
        console.warn('Navigation error:', err);
      }

      // 2. CHECK PERMISSIONS STATUS (Non-blocking banner / navbar)
      try {
        checkAllPermissions().catch(err => {
          console.warn('Initial permission check error:', err);
        });
      } catch (err) {}

      // 3. Init charts and telemetry safely (KHUSUS areskretindo@gmail.com)
      if (IS_ARES_OWNER) {
        try { initLiveChart(); } catch (e) { console.warn('initLiveChart error:', e); }
        try { updateLiveData(); } catch (e) { console.warn('updateLiveData error:', e); }
        setInterval(updateLiveData, 2000);

        try { updateSolarWeatherAndAdvisory(); } catch (e) {}
        setInterval(updateSolarWeatherAndAdvisory, 60000);
        try { fetchEnergyAnalytics(); } catch (e) {}
        setInterval(fetchEnergyAnalytics, 30000);
      }

      try { fetchModels(); } catch (e) {}
      try { fetchHermesStatus(); } catch (e) {}
      try { loadSpectrumWorkspaceData(); } catch (e) {}
      try { loadUserSpectrumConfig(); } catch (e) {}
      try { loadPastMeetings(); } catch (e) {}
      try { loadMeetingSchedulesAndSprint(); } catch (e) {}
      try { checkSpeechEngine(); } catch (e) {}

      // Data/perangkat bersama: hanya dimuat untuk pemilik server
      if (IS_ARES_OWNER) {
        try { fetchAppiumDevices(); } catch (e) {}
        try { loadMailConfig(); } catch (e) {}
        try { fetchDeployStatus(); } catch (e) {}
        setInterval(fetchDeployStatus, 15000);
        try { fetchSystemResources(); } catch (e) {}
        setInterval(() => {
          const isSysActive = document.getElementById('page-system')?.classList.contains('active');
          if (isSysActive) {
            fetchSystemResources();
          }
        }, 3000);
        setInterval(fetchSystemResources, 15000);
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initDashboard);
    } else {
      initDashboard();
    }
  

// SPEctrum MyTask Client JS for Westpro Dashboard

let currentSpectrumDateData = null;
let currentAuditData = null;
let currentAuditFilter = 'all';

function getStatusColor(statusStr) {
  const s = (statusStr || '').toLowerCase();
  if (s.includes('finish') || s.includes('done')) return '#22c55e'; // green
  if (s.includes('progress')) return '#38bdf8'; // blue
  return '#f59e0b'; // amber / orange for to do list
}

function isStatusMatch(currentVal, targetVal) {
  const c = (currentVal || '').toLowerCase().replace(/\\s+/g, '');
  const t = (targetVal || '').toLowerCase().replace(/\\s+/g, '');
  return c === t;
}

function updateTaskStatusStyle(taskId) {
  const sel = document.getElementById(`task_status_${taskId}`);
  if (sel) {
    sel.style.color = getStatusColor(sel.value);
  }
}

async function loadSpectrumMyTaskPage() {
  const dateInput = document.getElementById('inputSpectrumDate');
  if (dateInput && !dateInput.value) {
    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, '0');
    const dd = String(now.getDate()).padStart(2, '0');
    dateInput.value = `${yyyy}-${mm}-${dd}`;
  }
  const dateToLoad = dateInput ? dateInput.value : '';
  await Promise.all([
    loadSpectrumDateData(dateToLoad),
    runSpectrumAudit3Months(false)
  ]);
}

async function refreshSpectrumMyTask() {
  const dateInput = document.getElementById('inputSpectrumDate');
  const d = dateInput ? dateInput.value : '';
  await loadSpectrumDateData(d);
  await runSpectrumAudit3Months(true);
}

function loadSpectrumDateDataToday() {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');
  const dStr = `${yyyy}-${mm}-${dd}`;
  const input = document.getElementById('inputSpectrumDate');
  if (input) input.value = dStr;
  loadSpectrumDateData(dStr);
}

function changeSpectrumDate(offset) {
  const input = document.getElementById('inputSpectrumDate');
  if (!input || !input.value) return;
  const parts = input.value.split('-');
  const d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
  d.setDate(d.getDate() + offset);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  input.value = `${yyyy}-${mm}-${dd}`;
  loadSpectrumDateData(input.value);
}

async function loadSpectrumDateData(dateStr) {
  const container = document.getElementById('backlogTasksList');
  if (container) {
    container.innerHTML = '<div style="color:#64748b; font-size:0.8rem; text-align:center; padding:1.5rem;">Memuat rincian task & meeting...</div>';
  }

  try {
    const res = await fetch(`/api/spectrum/mytask/status?date=${dateStr || ''}`);
    const data = await res.json();
    if (!data.success && data.error) {
      if (container) container.innerHTML = `<div style="color:#ef4444; font-size:0.8rem; padding:1rem;">Gagal memuat: ${data.error}</div>`;
      return;
    }
    currentSpectrumDateData = data;
    renderSpectrumDateData(data);
  } catch(err) {
    if (container) container.innerHTML = `<div style="color:#ef4444; font-size:0.8rem; padding:1rem;">Kesalahan koneksi: ${err.message}</div>`;
  }
}

function renderSpectrumDateData(data) {
  // Update header & badges
  const dateBadge = document.getElementById('activeDateBadge');
  if (dateBadge) dateBadge.textContent = data.date;

  const statusBadge = document.getElementById('specStatusBadge');
  if (statusBadge) {
    if (data.meets_8_hours) {
      statusBadge.className = 'hermes-badge badge-ok';
      statusBadge.textContent = '✅ LENGKAP (\u2265 8 JAM)';
    } else {
      statusBadge.className = 'hermes-badge badge-yellow';
      statusBadge.textContent = `\u26A0\uFE0F KURANG (${data.deficit_minutes ? Math.floor(data.deficit_minutes/60) + 'h ' + (data.deficit_minutes%60) + 'm' : ''})`;
    }
  }

  // Update top metrics
  const mHours = document.getElementById('metricTodayHours');
  if (mHours) mHours.textContent = `${data.current_hours || 0}h ${data.current_minutes || 0}m`;
  const mNote = document.getElementById('metricTodayNote');
  if (mNote) {
    mNote.textContent = data.meets_8_hours ? 'Target 8 jam terpenuhi' : `Kurang ${Math.floor(data.deficit_minutes/60)}h ${data.deficit_minutes%60}m`;
    mNote.style.color = data.meets_8_hours ? '#10b981' : '#f59e0b';
  }

  const mMeetDur = document.getElementById('metricMeetingDuration');
  if (mMeetDur) mMeetDur.textContent = data.total_meeting_formatted || '0h 0m';
  const mMeetCnt = document.getElementById('metricMeetingCount');
  if (mMeetCnt) mMeetCnt.textContent = `${data.meeting_count || 0} Jadwal Teams`;

  const mBackCnt = document.getElementById('metricBacklogCount');
  if (mBackCnt) mBackCnt.textContent = `${data.backlog_count || 0} Task`;

  // Render Teams Meetings (Read Only)
  const meetList = document.getElementById('teamsMeetingList');
  const meetSubtotal = document.getElementById('meetingSubtotalText');
  if (meetSubtotal) meetSubtotal.textContent = `Total: ${data.total_meeting_formatted || '0m'}`;

  if (meetList) {
    if (!data.meetings || data.meetings.length === 0) {
      meetList.innerHTML = '<div style="color:#64748b; font-size:0.75rem; font-style:italic; padding:0.5rem 0.25rem;">Tidak ada jadwal meeting dari Teams untuk tanggal ini.</div>';
    } else {
      meetList.innerHTML = data.meetings.map(m => `
        <div style="background:#181922; border:1px solid #2a2c3a; border-radius:6px; padding:0.55rem 0.85rem; display:flex; justify-content:space-between; align-items:center;">
          <div style="display:flex; align-items:center; gap:0.5rem; flex:1; min-width:0;">
            <span style="background:rgba(167,139,250,0.15); color:#a78bfa; font-weight:700; font-size:0.68rem; padding:0.15rem 0.45rem; border-radius:4px; white-space:nowrap;">MEETING</span>
            <span style="font-size:0.8rem; color:#e2e8f0; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(m.description)}</span>
          </div>
          <div style="display:flex; align-items:center; gap:0.6rem;">
            <span style="font-size:0.75rem; font-weight:700; color:#cbd5e1; background:#242636; padding:0.2rem 0.5rem; border-radius:4px;">${m.hours}h ${m.minutes}m</span>
            <span style="font-size:0.68rem; color:#94a3b8; font-style:italic;">Read-Only</span>
          </div>
        </div>
      `).join('');
    }
  }

  // Render Backlog Tasks (Editable & AI Auto-fill)
  const taskList = document.getElementById('backlogTasksList');
  if (taskList) {
    if (!data.recommendations || data.recommendations.length === 0) {
      taskList.innerHTML = '<div style="color:#64748b; font-size:0.8rem; font-style:italic; padding:1rem 0.25rem;">Tidak ada task backlog proyek untuk tanggal ini.</div>';
    } else {
      taskList.innerHTML = data.recommendations.map((t, idx) => {
        const curStat = t.current_status || 'To Do List';
        const propStat = t.proposed_status || 'On Progress';
        return `
        <div class="spectrum-task-item" data-id="${t.id}" data-desc="${escapeHtml(t.description)}" data-weight="${t.weight || 1}" style="background:#181922; border:1px solid #2a2c3a; border-radius:8px; padding:0.85rem 1rem; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
          <div style="flex:1; min-width:240px;">
            <div style="display:flex; align-items:center; gap:0.45rem; margin-bottom:0.25rem; flex-wrap:wrap;">
              <span style="background:rgba(56,189,248,0.12); color:#38bdf8; font-weight:700; font-size:0.68rem; padding:0.15rem 0.45rem; border-radius:4px;">${escapeHtml(t.project_name || 'Proyek')}</span>
              <span style="background:rgba(255,255,255,0.06); color:#94a3b8; font-size:0.68rem; padding:0.15rem 0.4rem; border-radius:4px;">Bobot: ${t.weight || 1}</span>
            </div>
            <div style="font-size:0.825rem; font-weight:600; color:#f1f5f9; line-height:1.35;">${escapeHtml(t.description)}</div>
            <div style="font-size:0.7rem; color:#94a3b8; margin-top:0.35rem; line-height:1.4;">
              Saat ini: <b style="color:#e2e8f0;">${t.current_formatted}</b> &bull; Rekomendasi AI: <b style="color:#34d399;">${t.proposed_formatted}</b>
              &bull; Status AI: <b style="color:${getStatusColor(propStat)};">${propStat}</b> <span style="color:#64748b; font-size:0.67rem;">(${escapeHtml(t.status_rationale || '')})</span>
            </div>
          </div>

          <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
            <!-- Status Dropdown -->
            <div style="display:flex; flex-direction:column; gap:0.15rem;">
              <span style="font-size:0.65rem; color:#64748b; font-weight:600;">STATUS TASK</span>
              <select id="task_status_${t.id}" onchange="updateTaskStatusStyle('${t.id}')" style="background:#12131a; color:${getStatusColor(curStat)}; border:1px solid #35384d; border-radius:6px; padding:0.3rem 0.5rem; font-size:0.75rem; font-weight:700; cursor:pointer; outline:none;">
                <option value="To Do List" ${isStatusMatch(curStat, 'To Do List') ? 'selected' : ''} style="background:#181922; color:#f59e0b;">To Do List</option>
                <option value="On Progress" ${isStatusMatch(curStat, 'On Progress') ? 'selected' : ''} style="background:#181922; color:#38bdf8;">On Progress</option>
                <option value="Task Finished" ${isStatusMatch(curStat, 'Task Finished') ? 'selected' : ''} style="background:#181922; color:#22c55e;">Task Finished</option>
              </select>
            </div>

            <!-- Hour and Minute Inputs -->
            <div style="display:flex; flex-direction:column; gap:0.15rem;">
              <span style="font-size:0.65rem; color:#64748b; font-weight:600;">DURASI JAM</span>
              <div style="display:flex; align-items:center; gap:0.25rem; background:#12131a; padding:0.25rem 0.45rem; border:1px solid #35384d; border-radius:6px;">
                <input type="number" min="0" max="24" id="task_h_${t.id}" value="${t.current_hours}" oninput="recalculateLiveTotal()" style="width:36px; background:transparent; border:none; color:#38bdf8; font-weight:700; font-size:0.85rem; text-align:right; outline:none;">
                <span style="color:#64748b; font-size:0.75rem;">h</span>
                <input type="number" min="0" max="59" step="5" id="task_m_${t.id}" value="${t.current_minutes}" oninput="recalculateLiveTotal()" style="width:36px; background:transparent; border:none; color:#38bdf8; font-weight:700; font-size:0.85rem; text-align:right; outline:none;">
                <span style="color:#64748b; font-size:0.75rem;">m</span>
              </div>
            </div>

            <!-- Single AI Fill Button -->
            <div style="display:flex; flex-direction:column; gap:0.15rem;">
              <span style="font-size:0.65rem; color:transparent;">AKSI</span>
              <button type="button" onclick="applySingleAiRec('${t.id}', ${t.proposed_hours}, ${t.proposed_minutes}, '${propStat}')" title="Terapkan jam & status AI untuk task ini" style="background:#20202b; color:#34d399; border:1px solid #2f3244; border-radius:6px; padding:0.35rem 0.65rem; font-size:0.72rem; font-weight:700; cursor:pointer; height:31px; display:flex; align-items:center; gap:0.3rem;">
                ⚡ Pakai AI
              </button>
            </div>
          </div>
        </div>
      `;
      }).join('');
    }
  }

  recalculateLiveTotal();
}

function applySingleAiRec(taskId, propH, propM, propStatus) {
  const hInput = document.getElementById(`task_h_${taskId}`);
  const mInput = document.getElementById(`task_m_${taskId}`);
  const sInput = document.getElementById(`task_status_${taskId}`);
  if (hInput) hInput.value = propH;
  if (mInput) mInput.value = propM;
  if (sInput && propStatus) {
    sInput.value = propStatus;
    updateTaskStatusStyle(taskId);
  }
  recalculateLiveTotal();
}

function applyAiRecommendationsToInputs() {
  if (!currentSpectrumDateData || !currentSpectrumDateData.recommendations) return;
  currentSpectrumDateData.recommendations.forEach(r => {
    applySingleAiRec(r.id, r.proposed_hours, r.proposed_minutes, r.proposed_status);
  });
  recalculateLiveTotal();
  if (typeof showSpectrumConfigAlert === 'function') {
    showSpectrumConfigAlert('Nilai durasi dan status rekomendasi AI berhasil dimasukkan ke form. Silakan periksa atau klik Simpan & Terapkan.', true);
  }
}

function recalculateLiveTotal() {
  let backlogMinutes = 0;
  const items = document.querySelectorAll('.spectrum-task-item');
  items.forEach(el => {
    const id = el.getAttribute('data-id');
    const h = parseInt(document.getElementById(`task_h_${id}`)?.value || 0, 10);
    const m = parseInt(document.getElementById(`task_m_${id}`)?.value || 0, 10);
    backlogMinutes += (h * 60 + m);
  });

  const meetingMinutes = currentSpectrumDateData ? (currentSpectrumDateData.total_meeting_minutes || 0) : 0;
  const totalMin = meetingMinutes + backlogMinutes;
  const totH = Math.floor(totalMin / 60);
  const totM = totalMin % 60;

  const liveEl = document.getElementById('liveCalculatedTotal');
  if (liveEl) liveEl.textContent = `${totH}h ${totM}m`;

  const badge = document.getElementById('liveTargetBadge');
  if (badge) {
    if (totalMin >= 480) {
      badge.className = 'hermes-badge badge-ok';
      badge.textContent = '\u2265 8 Jam (Sesuai Target)';
    } else {
      const def = 480 - totalMin;
      badge.className = 'hermes-badge badge-yellow';
      badge.textContent = `Kurang ${Math.floor(def/60)}h ${def%60}m`;
    }
  }
}

async function submitSpectrumHoursToApi() {
  const btn = document.getElementById('btnSubmitSpectrumHours');
  if (!btn) return;
  const oldText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = 'Menyimpan ke SPEctrum...';

  const updates = [];
  const items = document.querySelectorAll('.spectrum-task-item');
  items.forEach(el => {
    const id = el.getAttribute('data-id');
    const description = el.getAttribute('data-desc');
    const weight = parseInt(el.getAttribute('data-weight') || '1', 10);
    const status = document.getElementById(`task_status_${id}`)?.value || 'On Progress';
    const hour = parseInt(document.getElementById(`task_h_${id}`)?.value || 0, 10);
    const minute = parseInt(document.getElementById(`task_m_${id}`)?.value || 0, 10);

    updates.push({ id, description, status, hour, minute, weight });
  });

  try {
    const res = await fetch('/api/spectrum/mytask/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates })
    });
    const result = await res.json();
    if (result.success) {
      alert(`Berhasil menyimpan ${result.updated_count} task (durasi & status) ke SPEctrum!`);
      const dateInput = document.getElementById('inputSpectrumDate');
      await loadSpectrumDateData(dateInput ? dateInput.value : '');
      await runSpectrumAudit3Months(true);
    } else {
      alert('Gagal menyimpan: ' + (result.error || 'Terjadi kesalahan'));
    }
  } catch(err) {
    alert('Kesalahan jaringan: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = oldText;
  }
}

async function runSpectrumAudit3Months(forceRefresh = false) {
  const badge = document.getElementById('auditBadgeCount');
  if (badge && !currentAuditData) badge.textContent = 'Memeriksa...';

  try {
    const res = await fetch(`/api/spectrum/mytask/audit?days=90${forceRefresh ? '&refresh=1' : ''}`);
    const data = await res.json();
    if (data.success && data.audit) {
      currentAuditData = data.audit;
      renderAuditSummaryAndList(data.audit);
    }
  } catch(e) {
    console.error("Audit error:", e);
  }
}

function renderAuditSummaryAndList(audit) {
  // Update metric
  const mSummary = document.getElementById('metricAuditSummary');
  if (mSummary) {
    mSummary.textContent = `${audit.deficient_count || 0} Hari Kurang`;
    mSummary.style.color = (audit.deficient_count > 0 || audit.empty_count > 0) ? '#fbbf24' : '#10b981';
  }
  const mNote = document.getElementById('metricAuditNote');
  if (mNote) {
    mNote.textContent = `${audit.ok_count || 0} dari ${audit.total_weekdays_checked || 0} hari kerja lengkap`;
  }

  const badge = document.getElementById('auditBadgeCount');
  if (badge) {
    badge.textContent = `${audit.attention_count || 0} Perlu Perhatian`;
    badge.className = audit.attention_count > 0 ? 'hermes-badge badge-yellow' : 'hermes-badge badge-ok';
  }

  filterAuditList(currentAuditFilter);
}

function filterAuditList(type) {
  currentAuditFilter = type;
  ['fltAll', 'fltDeficient', 'fltEmpty'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) {
      btn.style.background = 'transparent';
      btn.style.color = '#94a3b8';
    }
  });

  const activeBtnId = type === 'all' ? 'fltAll' : (type === 'deficient' ? 'fltDeficient' : 'fltEmpty');
  const activeBtn = document.getElementById(activeBtnId);
  if (activeBtn) {
    activeBtn.style.background = '#202026';
    activeBtn.style.color = '#fff';
  }

  const container = document.getElementById('auditListContainer');
  if (!container || !currentAuditData) return;

  let list = currentAuditData.attention_list || [];
  if (type === 'deficient') {
    list = list.filter(d => d.status === 'DEFICIENT');
  } else if (type === 'empty') {
    list = list.filter(d => d.status === 'EMPTY');
  }

  if (list.length === 0) {
    container.innerHTML = '<div style="color:#10b981; font-size:0.75rem; text-align:center; padding:1.5rem;">✅ Luar biasa! Tidak ada hari kerja yang kurang atau kosong dalam kategori ini.</div>';
    return;
  }

  container.innerHTML = list.map(item => `
    <div style="background:#181922; border:1px solid #2a2c3a; border-radius:6px; padding:0.65rem 0.85rem; display:flex; justify-content:space-between; align-items:center; gap:0.5rem;">
      <div>
        <div style="display:flex; align-items:center; gap:0.4rem;">
          <span style="font-size:0.8rem; font-weight:700; color:#f8fafc;">${item.date}</span>
          <span style="font-size:0.7rem; color:#94a3b8;">(${item.day_name || ''})</span>
          <span style="background:${item.status === 'EMPTY' ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.15)'}; color:${item.status === 'EMPTY' ? '#ef4444' : '#f59e0b'}; font-size:0.65rem; font-weight:700; padding:0.1rem 0.35rem; border-radius:3px;">
            ${item.status === 'EMPTY' ? 'KOSONG 0h' : 'KURANG < 8h'}
          </span>
        </div>
        <div style="font-size:0.7rem; color:#94a3b8; margin-top:0.2rem;">
          Tercatat: <b style="color:#cbd5e1;">${item.formatted || (item.hours + 'h ' + item.minutes + 'm')}</b> &bull; Kekurangan: <b style="color:#f87171;">${item.deficit_formatted}</b> &bull; ${item.task_count || 0} task
        </div>
      </div>
      <button onclick="selectAuditDateToFill('${item.date}')" style="background:#20202b; color:#38bdf8; border:1px solid #2f3244; border-radius:4px; padding:0.35rem 0.65rem; font-size:0.72rem; font-weight:600; cursor:pointer; white-space:nowrap;">
        🔍 Periksa &amp; Isi
      </button>
    </div>
  `).join('');
}

function selectAuditDateToFill(dateStr) {
  const input = document.getElementById('inputSpectrumDate');
  if (input) input.value = dateStr;
  loadSpectrumDateData(dateStr);
  const leftCol = document.getElementById('inputSpectrumDate');
  if (leftCol) leftCol.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

</script>
</body>
</html>
"""


class InverterHTTPHandler(http.server.BaseHTTPRequestHandler):
    inverter: PowMrInverter | None = None
    history_db: HistoryDatabase | None = None

    def log_message(self, format, *args):
        pass

    OWNER_EMAIL = "areskretindo@gmail.com"
    # Endpoint yang hanya boleh diakses pemilik server (data/perangkat bersama, bukan data per-user).
    OWNER_ONLY_API_PREFIXES = (
        "/api/state", "/data", "/api/history", "/api/weather/",
        "/api/inverter/", "/api/bms/",
        "/api/mail/", "/api/otp/", "/api/tailscale/",
        "/api/appium/scan-wifi", "/api/devices/scan-wifi", "/api/devices/connect", "/api/devices/disconnect",
        "/api/monitor/", "/api/sheets/", "/api/deploy/", "/api/system/", "/api/model/select",
        "/api/hermes/agent", "/api/email/", "/api/teams/", "/api/saham/",
    )
    TRANSCRIBE_JOBS: dict = {}  # job_id -> user_id (agar user hanya bisa melihat job miliknya)
    MAX_AUDIO_UPLOAD = 120 * 1024 * 1024
    # Endpoint publik (autentikasi sendiri / tanpa sesi).
    PUBLIC_API_PATHS = (
        "/api/auth/login", "/api/auth/register", "/api/auth/spectrum-login", "/api/auth/logout",
        "/api/auth/forgot", "/api/auth/reset", "/api/mail/inbound",
        "/api/auth/me", "/api/webhook/github", "/webhook/github", "/api/webhook/deploy",
    )

    def is_owner(self, user: dict | None) -> bool:
        return bool(user) and (user.get("email", "").strip().lower() == self.OWNER_EMAIL)

    def is_trusted_local(self) -> bool:
        """Request internal (hermes-agent dsb). Request dari tunnel Cloudflare juga datang dari
        127.0.0.1, jadi header forwarding berarti BUKAN lokal."""
        ip = self.client_address[0] if self.client_address else ""
        if ip not in ("127.0.0.1", "::1", "localhost"):
            return False
        for h in ("X-Forwarded-For", "CF-Connecting-IP", "CF-Ray", "X-Real-IP", "Forwarded"):
            if self.headers.get(h):
                return False
        return True

    def authorize_api(self, clean_path: str, user: dict | None) -> bool:
        """Gerbang otorisasi terpusat. Mengirim 401/403 sendiri dan mengembalikan False bila ditolak."""
        if clean_path in self.PUBLIC_API_PATHS or self.is_trusted_local():
            return True
        status, msg = 0, ""
        if not user:
            status, msg = 401, "Silakan login terlebih dahulu."
        elif clean_path.startswith(self.OWNER_ONLY_API_PREFIXES) and not self.is_owner(user):
            status, msg = 403, "Akses dibatasi untuk pemilik server."
        if not status:
            return True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"success": False, "error": msg}).encode("utf-8"))
        return False

    def build_user_spectrum_context(self, user: dict) -> str:
        """Konteks AI dari data SPEctrum milik user itu sendiri (tidak pernah dari data global)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{user['id']}.json")
        name = user.get("name") or user.get("email", "User")
        base = ("Anda adalah Hermes QA Assistant di Westpro Home Server. Bantu pengguna membuat skenario pengujian, "
                "bug report, ringkasan daily standup, dan verifikasi alur. Jawab ringkas, terstruktur, Bahasa Indonesia.\n"
                f"Pengguna: {name}\n")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return base + "Akun SPEctrum pengguna belum terhubung; jangan mengarang data sprint/task."
        sprint = data.get("active_sprint") or {}
        tasks = data.get("tasks") or []
        cal = data.get("calendar_events") or []
        key = (data.get("user", {}).get("name") or name).split(" ")[0].lower()
        mine = [t for t in tasks if key and (key in str(t.get("assignee", "")).lower() or key in str(t.get("created_by", "")).lower())]
        lines = [base,
                 f"Sprint Aktif: {sprint.get('name')} ({sprint.get('start_date')} s/d {sprint.get('end_date')}, Status: {sprint.get('status')})",
                 f"Tujuan Sprint: {sprint.get('description')}", "TASK MILIK PENGGUNA:"]
        lines += [f"- [{t.get('status')}] {t.get('name')} (Kode: {t.get('code')})" for t in mine] or ["- Tidak ada task spesifik"]
        lines.append("AGENDA KALENDER:")
        lines += [f"- {c.get('name', '')} (Mulai: {c.get('start', '')})" for c in cal]
        return "\n".join(lines)

    def android_devices_raw(self) -> dict:
        req = urllib.request.Request("http://localhost:8000/api/devices")
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def device_access(self, user: dict, serial: str | None) -> str | None:
        """Izin akses perangkat: pemilik bebas; user lain hanya perangkat yang disewanya. Mengirim 403 bila ditolak."""
        if self.is_owner(user):
            return serial or ""
        mine = al.lease_of(self.history_db, user["id"])
        if mine and (not serial or serial == mine):
            al.touch(self.history_db, user["id"], mine)
            return mine
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"success": False, "error": "Anda belum memegang perangkat ini. Klik 'Ambil perangkat kosong' dulu.",
                                     "need_claim": True}).encode("utf-8"))
        return None

    def client_ip(self) -> str:
        """IP klien. Header Cloudflare hanya dipercaya bila request datang dari tunnel lokal (127.0.0.1)."""
        peer = self.client_address[0] if self.client_address else ""
        if peer in ("127.0.0.1", "::1"):
            ip = (self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]).strip()
            if ip:
                return ip[:64]
        return peer

    def get_current_user(self) -> dict | None:
        # Otomasi tes: header X-API-Key hanya berlaku untuk /api/webmail/*
        api_key = self.headers.get("X-API-Key", "")
        if api_key and self.path.startswith("/api/webmail/") and self.history_db:
            u = wm_mail.user_by_key(self.history_db, api_key)
            if u:
                return u
        cookie_header = self.headers.get("Cookie", "")
        if "westpro_session=" in cookie_header and self.history_db:
            import http.cookies
            try:
                cookies = http.cookies.SimpleCookie(cookie_header)
                if "westpro_session" in cookies:
                    token = cookies["westpro_session"].value
                    return self.history_db.validate_session(token)
            except Exception:
                pass
        return None

    def handle_webhook_monitor_catch(self, method: str):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            headers = {k: v for k, v in self.headers.items()}
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if "X-Forwarded-For" in self.headers:
                client_ip = self.headers["X-Forwarded-For"].split(",")[0].strip()
            elif "CF-Connecting-IP" in self.headers:
                client_ip = self.headers["CF-Connecting-IP"].strip()

            resp_status, resp_headers, resp_body = wm.record_incoming_request(
                method=method,
                path=self.path,
                headers=headers,
                body=body,
                ip=client_ip
            )
            self.send_response(resp_status)
            for hk, hv in resp_headers.items():
                self.send_header(hk, hv)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if isinstance(resp_body, str):
                self.wfile.write(resp_body.encode("utf-8"))
            elif isinstance(resp_body, bytes):
                self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Webhook capture failed: {e}"}).encode("utf-8"))

    def do_HEAD(self):
        self.do_GET(is_head=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
        self.end_headers()

    def do_PUT(self):
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path.startswith("/catch") or clean_path.startswith("/api/webhook/catch"):
            self.handle_webhook_monitor_catch("PUT")
            return
        self.send_response(404)
        self.end_headers()

    def do_PATCH(self):
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path.startswith("/catch") or clean_path.startswith("/api/webhook/catch"):
            self.handle_webhook_monitor_catch("PATCH")
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self, is_head: bool = False):
        clean_path = self.path.split("?")[0].rstrip("/") or "/"
        user = self.get_current_user()

        def _send(data: bytes | str, content_type: str = "application/json", status: int = 200, extra_headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if not is_head:
                if isinstance(data, str):
                    self.wfile.write(data.encode("utf-8"))
                else:
                    self.wfile.write(data)

        # 0. Webhook Catch Intercept
        if clean_path.startswith("/catch") or clean_path.startswith("/api/webhook/catch"):
            self.handle_webhook_monitor_catch("GET")
            return

        # 1. Favicon & Web App Manifest
        if clean_path in ("/favicon.ico", "/favicon.png"):
            svg_icon = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#38bdf8"><path d="M12 2a1 1 0 0 1 1 1v2a1 1 0 1 1-2 0V3a1 1 0 0 1 1-1zm0 15a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm-9-6a1 1 0 1 1 0-2h2a1 1 0 1 1 0 2H3zm16 0a1 1 0 1 1 0-2h2a1 1 0 1 1 0 2h-2z"/></svg>"""
            _send(svg_icon, "image/svg+xml", extra_headers={"Cache-Control": "public, max-age=86400"})
            return

        if clean_path == "/manifest.json":
            manifest = {
                "name": "Westpro Solar & Home Server",
                "short_name": "Westpro",
                "start_url": "/solar",
                "display": "standalone",
                "background_color": "#0b0c0e",
                "theme_color": "#111217",
                "icons": [
                    {
                        "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23f59e0b'%3E%3Cpath d='M12 2a1 1 0 0 1 1 1v2a1 1 0 1 1-2 0V3a1 1 0 0 1 1-1zm0 15a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm-9-6a1 1 0 1 1 0-2h2a1 1 0 1 1 0 2H3zm16 0a1 1 0 1 1 0-2h2a1 1 0 1 1 0 2h-2z'/%3E%3C/svg%3E",
                        "sizes": "192x192 512x512",
                        "type": "image/svg+xml"
                    }
                ]
            }
            _send(json.dumps(manifest), "application/manifest+json")
            return

        # 1b. Halaman atur ulang kata sandi (publik; token di query)
        if clean_path == "/reset":
            _send(pr.RESET_PAGE, "text/html; charset=utf-8", extra_headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return

        # 2. Login & Register Routes
        if clean_path in ("/login", "/register"):
            if user:
                is_ares = (user.get("email", "").strip().lower() == "areskretindo@gmail.com")
                self.send_response(302)
                self.send_header("Location", "/solar" if is_ares else "/hermes")
                self.end_headers()
                return
            _send(HTML_LOGIN_TEMPLATE, "text/html; charset=utf-8")
            return

        # 3. Dedicated Feature Pages (Protected by Session)
        if clean_path in ("/", "/index.html", "/solar", "/history", "/inverter-settings", "/hermes", "/meeting", "/android", "/otp", "/webhook", "/spectrum-config", "/profile", "/webmail", "/system", "/9router", "/spectrum", "/teams", "/saham"):
            if not user:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            is_ares = (user.get("email", "").strip().lower() == "areskretindo@gmail.com")

            # Check query params for ?page=
            page_param = ""
            if "?" in self.path:
                try:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    page_param = q.get("page", [""])[0].strip().lower()
                except Exception:
                    pass

            if clean_path in ("/", "/index.html"):
                if page_param in ("solar", "history", "inverter-settings", "hermes", "meeting", "android", "otp", "webhook", "spectrum-config", "profile", "webmail", "system", "9router", "spectrum", "teams", "saham"):
                    initial_page = "profile" if page_param in ("spectrum-config", "profile") else page_param
                else:
                    initial_page = "solar" if is_ares else "hermes"
            elif clean_path in ("/spectrum-config", "/profile"):
                initial_page = "profile"
            else:
                initial_page = clean_path.lstrip("/")

            # Protection: solar, history, inverter-settings, teams, etc. are strictly for areskretindo@gmail.com
            if initial_page in ("solar", "history", "inverter-settings", "otp", "webhook", "system", "9router", "teams", "saham") and not is_ares:
                initial_page = "hermes"

            user_name = user.get("name") or user.get("email", "Ares").split("@")[0].capitalize()
            html = HTML_DASHBOARD_TEMPLATE.replace("__SAHAM_PAGE__", SAHAM_PAGE_HTML).replace("__INITIAL_PAGE__", initial_page)
            html = html.replace("__USER_EMAIL__", user.get("email", "areskretindo@gmail.com"))
            html = html.replace("__USER_NAME__", user_name)
            html = html.replace("__USER_ROLE__", user.get("role", "owner").upper())

            # If initial page is not solar, activate the correct tab and section in the static HTML
            if initial_page != "solar":
                html = html.replace('class="tab-btn active" id="nav-solar"', 'class="tab-btn" id="nav-solar"')
                html = html.replace('id="page-solar" class="view-section active"', 'id="page-solar" class="view-section"')
                html = html.replace(f'class="tab-btn" id="nav-{initial_page}"', f'class="tab-btn active" id="nav-{initial_page}"')
                html = html.replace(f'id="page-{initial_page}" class="view-section"', f'id="page-{initial_page}" class="view-section active"')

            # If not Ares, hide solar live, history, teams, and owner-only tabs/sections from the DOM
            if not is_ares:
                html = html.replace('id="nav-solar"', 'id="nav-solar" style="display:none;"')
                html = html.replace('id="nav-history"', 'id="nav-history" style="display:none;"')
                for _pg in ("inverter-settings", "otp", "webhook", "system", "9router", "teams", "saham"):
                    html = html.replace(f'id="nav-{_pg}"', f'id="nav-{_pg}" style="display:none;"')
                html = html.replace('id="page-teams"', 'id="page-teams" style="display:none;"')
                html = html.replace('id="page-saham"', 'id="page-saham" style="display:none;"')
            _send(html, "text/html; charset=utf-8", extra_headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
            return

        # 4. Auth Session Check API
        if clean_path == "/api/auth/me":
            payload = json.dumps({"authenticated": True, "user": user}) if user else '{"authenticated": false}'
            _send(payload)
            return

        # 4b. Gerbang otorisasi: semua /api/* butuh login; data bersama khusus pemilik
        if (clean_path.startswith("/api/") or clean_path == "/data") and not self.authorize_api(clean_path, user):
            return

        # 5. Telemetry Live API (Khusus areskretindo@gmail.com dan internal local services)
        elif self.path in ("/api/state", "/data"):
            is_local_client = self.is_trusted_local()
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Solar Live khusus untuk areskretindo@gmail.com"}), status=403)
                return
            data = dict(self.inverter.last_state) if self.inverter and self.inverter.last_state else {}
            if self.history_db:
                try:
                    data["battery_stats"] = self.history_db.get_battery_telemetry_stats(current_soc=data.get("soc"))
                except Exception as e:
                    logger.debug("Failed attaching battery_stats to /api/state: %s", e)
            bms_file = "/home/ares/Documents/myserver/data/bms_state.json"
            if os.path.exists(bms_file):
                try:
                    with open(bms_file, "r") as bf:
                        data["bms"] = json.load(bf)
                except Exception as e:
                    logger.debug("Failed attaching bms to /api/state: %s", e)
            _send(json.dumps(data, indent=2))

        # 5b. Weather & Solar Advisory API (Khusus areskretindo@gmail.com)
        elif self.path.startswith("/api/weather/solar"):
            is_local_client = self.is_trusted_local()
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Weather & Solar khusus untuk areskretindo@gmail.com"}), status=403)
                return
            try:
                from solar_weather import get_solar_weather_data, generate_smart_solar_advisory
                force = "force=1" in self.path
                weather_info = get_solar_weather_data(force=force)
                inv_state = self.inverter.last_state if self.inverter else {}
                advice = generate_smart_solar_advisory(weather_info, inv_state)
                weather_info["advisory"] = advice
                _send(json.dumps(weather_info))
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        # 5c. Energy Analytics & Daily/Monthly Projections API (Khusus areskretindo@gmail.com)
        elif self.path == "/api/energy/analytics":
            is_local_client = self.is_trusted_local()
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Analisis energi khusus untuk areskretindo@gmail.com"}), status=403)
                return
            try:
                analytics = self.history_db.get_energy_analytics() if self.history_db else {}
                _send(json.dumps(analytics))
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        # 5d. Inverter Settings & Live Hardware Profile API (Khusus areskretindo@gmail.com)
        elif clean_path == "/api/inverter/settings":
            is_local_client = self.is_trusted_local()
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Setelan Inverter khusus untuk areskretindo@gmail.com"}), status=403)
                return
            settings = self.inverter.read_settings() if self.inverter else {}
            bms_file = "/home/ares/Documents/myserver/data/bms_state.json"
            bms_info = {}
            if os.path.exists(bms_file):
                try:
                    with open(bms_file, "r") as bf:
                        bms_info = json.load(bf)
                except Exception as e:
                    pass
            _send(json.dumps({
                "success": True,
                "settings": settings,
                "bms": bms_info,
                "last_state": self.inverter.last_state if self.inverter else {}
            }, indent=2))

        # 5e. BMS Configuration API (Khusus areskretindo@gmail.com)
        elif clean_path == "/api/bms/config":
            is_local_client = self.is_trusted_local()
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Konfigurasi BMS khusus untuk areskretindo@gmail.com"}), status=403)
                return
            bms_cfg_file = "/home/ares/Documents/myserver/data/bms_config.json"
            cfg = get_default_bms_config()
            if os.path.exists(bms_cfg_file):
                try:
                    with open(bms_cfg_file, "r") as cf:
                        loaded = json.load(cf)
                        for sec, val in loaded.items():
                            if isinstance(val, dict) and sec in cfg:
                                cfg[sec].update(val)
                            else:
                                cfg[sec] = val
                except Exception as e:
                    logger.warning("Gagal membaca bms_config.json: %s", e)

            # Auto-detect live connected packs from bms_state.json
            bms_state_file = "/home/ares/Documents/myserver/data/bms_state.json"
            if os.path.exists(bms_state_file):
                try:
                    with open(bms_state_file, "r") as bsf:
                        bdata = json.load(bsf)
                        conn_slaves = bdata.get("connected_slaves") or sorted([int(k) for k in bdata.get("packs", {}).keys() if str(k).isdigit()])
                        if conn_slaves:
                            cfg["connected_slaves"] = conn_slaves
                            cfg["connected_slave_ids"] = ", ".join(str(s) for s in conn_slaves)
                            if not cfg.get("system", {}).get("slave_ids") or cfg.get("system", {}).get("slave_ids") == "1, 5":
                                cfg["system"]["slave_ids"] = ", ".join(str(s) for s in conn_slaves)
                                cfg["system"]["pack_count"] = len(conn_slaves)
                                cfg["system"]["nominal_capacity_ah"] = len(conn_slaves) * 100
                except Exception:
                    pass

            _send(json.dumps({"success": True, "config": cfg}, indent=2))

        # 6. Telemetry History API (SQLite & CSV, Khusus areskretindo@gmail.com)
        elif clean_path in ("/api/history/download", "/api/history.csv"):
            if not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com":
                _send("Akses dibatasi. Unduh riwayat khusus untuk areskretindo@gmail.com", content_type="text/plain", status=403)
                return
            range_val = "24h"
            if "range=" in self.path:
                try:
                    range_val = self.path.split("range=")[1].split("&")[0]
                except Exception:
                    pass
            history_data = self.history_db.get_history(range_val) if self.history_db else []
            headers = ['Waktu', 'Mode', 'PV_Watt', 'PV_Volt', 'PV_Amp', 'Batt_Volt', 'Batt_SOC', 'Batt_Chg_A', 'Batt_Dischg_A', 'Batt_Watt', 'Load_Watt', 'Load_VA', 'Load_Pct', 'AC_Out_Volt', 'Grid_Volt', 'Temp_C']
            csv_lines = [",".join(headers)]
            for r in history_data:
                csv_lines.append(",".join([
                    f'"{r.get("time", "")}"',
                    f'"{r.get("mode", "")}"',
                    str(r.get("pv_w", 0)),
                    str(r.get("pv_v", 0.0)),
                    str(r.get("pv_a", 0.0)),
                    str(r.get("batt_v", 0.0)),
                    str(r.get("batt_soc", 0)),
                    str(r.get("batt_charge_a", 0.0)),
                    str(r.get("batt_discharge_a", 0.0)),
                    str(r.get("batt_power_w", 0)),
                    str(r.get("load_w", 0)),
                    str(r.get("load_va", 0)),
                    str(r.get("load_pct", 0)),
                    str(r.get("ac_out_v", 0.0)),
                    str(r.get("grid_v", 0.0)),
                    str(r.get("temp_c", 0)),
                ]))
            csv_content = "\r\n".join(csv_lines)
            _send(csv_content, "text/csv; charset=utf-8")
            return

        elif self.path.startswith("/api/history"):
            if not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com":
                _send(json.dumps({"error": "Akses dibatasi. Riwayat data khusus untuk areskretindo@gmail.com"}), status=403)
                return
            range_val = "1h"
            if "range=" in self.path:
                try:
                    range_val = self.path.split("range=")[1].split("&")[0]
                except Exception:
                    pass
            history_data = self.history_db.get_history(range_val) if self.history_db else []
            _send(json.dumps(history_data))

        # 7. Hermes Proxy Status
        elif self.path == "/api/hermes/status":
            try:
                req = urllib.request.Request("http://localhost:8000/api/status")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"status": "offline", "error": str(e), "events": []}))

        elif clean_path == "/api/spectrum/mytask/status":
            try:
                cfg = self.history_db.get_user_spectrum_config(user["id"]) if (user and self.history_db) else {}
                spec_email = (cfg.get("spectrum_email") or "").strip() or "aris.aris@spesolution.com"
                spec_pass = (cfg.get("spectrum_password") or "").strip() or "jetak1998@SPE3"
                
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                req_date = q.get("date", [""])[0].strip() or None
                
                import spectrum_mytask
                sm = spectrum_mytask.SpectrumMyTask(email=spec_email, password=spec_pass)
                analysis = sm.analyze_day(req_date)
                _send(json.dumps({"success": True, **analysis}))
            except Exception as e:
                _send(json.dumps({"success": False, "error": str(e)}))
            return

        elif clean_path == "/api/spectrum/mytask/audit":
            try:
                cfg = self.history_db.get_user_spectrum_config(user["id"]) if (user and self.history_db) else {}
                spec_email = (cfg.get("spectrum_email") or "").strip() or "aris.aris@spesolution.com"
                spec_pass = (cfg.get("spectrum_password") or "").strip() or "jetak1998@SPE3"
                
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                days = int(q.get("days", [90])[0])
                
                import spectrum_mytask
                sm = spectrum_mytask.SpectrumMyTask(email=spec_email, password=spec_pass)
                audit_res = sm.audit_past_days(days_back=days, max_workers=6)
                _send(json.dumps({"success": True, "audit": audit_res}))
            except Exception as e:
                _send(json.dumps({"success": False, "error": str(e)}))
            return

        elif clean_path == "/api/spectrum/data":
            if not user or not self.history_db:
                _send(json.dumps({
                    "needs_config": True,
                    "user": {},
                    "active_sprint": {},
                    "tasks": [],
                    "calendar_events": [],
                    "message": "Silakan login terlebih dahulu."
                }))
                return

            user_id = user["id"]
            user_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{user_id}.json")

            cfg = self.history_db.get_user_spectrum_config(user_id)
            spec_email = (cfg.get("spectrum_email") or "").strip()
            spec_pass = (cfg.get("spectrum_password") or "").strip()
            member_name = (cfg.get("member_name") or "").strip() or user.get("name") or ""
            proj_id = (cfg.get("project_id") or "").strip()

            # 1. Jika belum mengisi email atau password SPEctrum
            if not spec_email or not spec_pass:
                _send(json.dumps({
                    "needs_config": True,
                    "user": {
                        "name": member_name or user.get("name", "User"),
                        "email": spec_email or user.get("email", ""),
                        "role": user.get("role", "user")
                    },
                    "active_sprint": {},
                    "tasks": [],
                    "calendar_events": [],
                    "message": "Akun SPEctrum Anda belum dikonfigurasi. Silakan isi email dan kata sandi SPEctrum Anda di menu 'Config SPEctrum'."
                }))
                return

            # 2. Akun sudah memiliki kredensial: Cek apakah file cache user sudah ada
            if os.path.exists(user_cache_path):
                try:
                    with open(user_cache_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        _send(json.dumps(data))
                        return
                except Exception:
                    pass

            # 3. Kredensial ada tapi data belum ada: Lakukan auto-sync jika diizinkan
            if cfg.get("auto_sync", 1):
                try:
                    import sync_spectrum
                    summary = sync_spectrum.sync_spectrum(
                        email=spec_email,
                        password=spec_pass,
                        project_id=proj_id,
                        member_name=member_name,
                        output_path=user_cache_path
                    )
                    task_count = len(summary.get("tasks", []))
                    self.history_db.update_spectrum_sync_status(user_id, f"Sinkronisasi Sukses ({task_count} Task)")
                    _send(json.dumps(summary))
                    return
                except Exception as e:
                    self.history_db.update_spectrum_sync_status(user_id, f"Gagal Sync: {str(e)[:40]}")
                    _send(json.dumps({
                        "needs_config": False,
                        "error": str(e),
                        "user": {"name": member_name, "email": spec_email},
                        "active_sprint": {},
                        "tasks": [],
                        "calendar_events": [],
                        "message": f"Gagal sinkronisasi SPEctrum: {str(e)}"
                    }))
                    return

            _send(json.dumps({
                "needs_config": False,
                "user": {"name": member_name, "email": spec_email},
                "active_sprint": {},
                "tasks": [],
                "calendar_events": [],
                "message": "Belum ada data sinkronisasi. Klik 'Perbarui Data' untuk menyinkronkan."
            }))
            return

        elif clean_path in ("/api/user/spectrum/config", "/api/user/profile"):
            if not user or not self.history_db:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                return

            cfg = self.history_db.get_user_spectrum_config(user["id"])
            cfg_copy = dict(cfg)
            raw_pass = cfg_copy.get("spectrum_password") or ""
            cfg_copy["has_password"] = bool(raw_pass)
            cfg_copy["spectrum_password_masked"] = ("•" * min(len(raw_pass), 12)) if raw_pass else ""
            cfg_copy["spectrum_password"] = ""

            # Check if user has an active server password
            user_copy = dict(user)
            with self.history_db.lock, self.history_db._get_conn() as conn:
                p_row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
                user_copy["has_password"] = bool(p_row and p_row["password_hash"])

            _send(json.dumps({"success": True, "config": cfg_copy, "user": user_copy}))
            return

        elif self.path == "/api/appium/devices":
            try:
                data = self.android_devices_raw()
                owner = self.is_owner(user)
                leases = al.all_leases(self.history_db)
                shown = []
                free_count = 0
                for d in data.get("devices", []):
                    l = leases.get(d.get("serial"))
                    status = "free" if not l else ("mine" if l["user_id"] == user["id"] else "busy")
                    d["lease"] = {"status": status, "by": (l or {}).get("name"), "idle_sec": (l or {}).get("idle_sec")}
                    if status == "free" and d.get("state") == "device":
                        free_count += 1
                    if owner or status == "mine":
                        shown.append(d)
                    elif status == "free" and d.get("state") == "device":
                        # user lain hanya perlu tahu ada perangkat kosong (tanpa detail jaringan)
                        d.pop("wifi_ip", None)
                        shown.append(d)
                data["devices"] = shown
                data["free_count"] = free_count
                data["my_serial"] = al.lease_of(self.history_db, user["id"])
                data["is_owner"] = owner
                if not owner:
                    data.pop("appium_url", None)
                _send(json.dumps(data))
            except Exception as e:
                _send(json.dumps({"appium": {"ready": False, "error": str(e)}, "devices": []}))

        elif clean_path == "/api/android/appium-caps":
            serial = al.lease_of(self.history_db, user["id"])
            if self.is_owner(user) and not serial:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                serial = (q.get("serial", [""])[0] or "").strip() or None
            if not serial:
                _send(json.dumps({"success": False, "error": "Ambil perangkat dulu."}), status=409)
            else:
                _send(json.dumps({"success": True, "serial": serial, "capabilities": al.appium_caps(self.history_db, serial),
                                  "note": "Satu server Appium bisa melayani banyak perangkat paralel selama systemPort/chromedriverPort/mjpegServerPort tiap perangkat berbeda (sudah diatur di sini)."}))

        elif self.path == "/api/tailscale/status":
            try:
                req = urllib.request.Request("http://localhost:8000/api/tailscale/status")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"running": False, "state": "Error", "error": str(e)}))

        elif self.path == "/api/sheets/info":
            sheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs")
            sheet_title = os.environ.get("GOOGLE_SHEET_NAME", "Sprint 6 PNRI")
            token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
            _send(json.dumps({
                "configured": os.path.exists(token_path),
                "spreadsheet_id": sheet_id,
                "sheet_name": sheet_title,
                "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            }))

        elif self.path == "/api/sheets/testcases":
            try:
                from google_sheets_qa import load_existing_test_cases
                tcs = load_existing_test_cases()
                _send(json.dumps({
                    "success": True,
                    "sprint": "Sprint 6 - 2026",
                    "file": "Skenario Testing - Sprint PNRI.xlsx",
                    "total": len(tcs),
                    "test_cases": tcs
                }))
            except Exception as e:
                _send(json.dumps({"success": False, "error": str(e)}), status=500)

        elif self.path.startswith("/api/appium/screenshot") or self.path.startswith("/api/devices/screenshot"):
            try:
                # Proxy screenshot ke hermes-agent /api/devices/screenshot (hanya pemegang sewa / pemilik)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                req_serial = (q.get("serial", [""])[0] or "").strip()
                allowed = self.device_access(user, req_serial or None)
                if allowed is None:
                    return
                if not req_serial and allowed:
                    q["serial"] = [allowed]
                q = {k: v[0] for k, v in q.items()}
                clean = "/api/devices/screenshot?" + urllib.parse.urlencode(q)
                target_url = "http://localhost:8000" + clean
                req = urllib.request.Request(target_url)
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    img_bytes = resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(img_bytes)))
                    self.end_headers()
                    self.wfile.write(img_bytes)
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        elif self.path == "/api/mail/config":
            try:
                req = urllib.request.Request("http://localhost:8000/api/mail/config")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        elif self.path == "/api/models":
            try:
                req = urllib.request.Request("http://localhost:8000/api/models")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        elif self.path.startswith("/api/otp/"):
            # Proxy ke hermes-agent: /api/otp/inbox, /api/otp/public
            try:
                target_url = "http://localhost:8000" + self.path
                req = urllib.request.Request(target_url)
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"error": str(e), "inbox": []}), status=500)

        # SMS OTP via ADB - Public endpoint (tanpa login)
        # GET /otp/<nomor_hp>   -> OTP terbaru dari SMS HP
        # GET /otp/status       -> status koneksi ADB
        elif clean_path == "/otp" or clean_path.startswith("/otp/"):
            try:
                target_url = "http://localhost:3010" + self.path
                req = urllib.request.Request(target_url)
                req.add_header("X-Forwarded-For", self.client_address[0])
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    raw = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache, no-store")
                    self.end_headers()
                    self.wfile.write(raw)
            except urllib.error.HTTPError as e:
                _send(json.dumps({"success": False, "error": f"SMS OTP: {e.code}"}), status=e.code)
            except Exception as e:
                _send(json.dumps({"success": False, "otp": None, "error": f"SMS OTP service tidak aktif: {str(e)}"}), status=503)

        elif clean_path.startswith("/api/meetings/transcribe-file/"):
            job_id = clean_path.rsplit("/", 1)[-1]
            if self.TRANSCRIBE_JOBS.get(job_id) != user["id"]:
                _send(json.dumps({"status": "error", "error": "Job tidak ditemukan"}), status=404)
                return
            try:
                with urllib.request.urlopen(f"http://localhost:8000/api/transcribe-file/{urllib.parse.quote(job_id)}", timeout=10.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"status": "error", "error": str(e)}), status=502)

        elif clean_path.startswith("/api/webmail/"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            owner = self.is_owner(user)
            db_ = self.history_db
            sub = clean_path[len("/api/webmail/"):]
            if sub == "status":
                st = wm_mail.stats(db_)
                _send(json.dumps({"success": True, "domain": wm_mail.mail_domain(), "owner": owner,
                                  "inbound_configured": bool(wm_mail.inbound_secret()),
                                  "webhook_url": mailer.cfg()["base_url"] + "/api/mail/inbound",
                                  "smtp_configured": mailer.is_configured(), "has_api_key": wm_mail.has_api_key(db_, user["id"]),
                                  "max_aliases": wm_mail.MAX_ALIASES_PER_USER, "retention_days": wm_mail.RETENTION_DAYS, **st}))
            elif sub == "addresses":
                _send(json.dumps({"success": True, "domain": wm_mail.mail_domain(), "addresses": wm_mail.list_aliases(db_, user["id"], owner)}))
            elif sub == "messages":
                folder = q.get("folder") if q.get("folder") in ("inbox", "sent") else "inbox"
                res = wm_mail.list_messages(db_, user["id"], owner, folder, q.get("address", ""), q.get("q", "")[:100],
                                            q.get("unread") == "1", int(q.get("limit", 50) or 50), int(q.get("offset", 0) or 0))
                _send(json.dumps({"success": True, **res}))
            elif sub == "message":
                m = wm_mail.get_message(db_, user["id"], owner, int(q.get("id", 0) or 0), q.get("mark_read", "1") != "0")
                _send(json.dumps({"success": True, "message": m}) if m else json.dumps({"success": False, "error": "Email tidak ditemukan"}), status=200 if m else 404)
            elif sub == "wait":
                m = wm_mail.wait_for(db_, user["id"], owner, q.get("address", ""), int(q.get("since_id", 0) or 0),
                                     float(q.get("timeout", 30) or 30), q.get("subject_contains", "")[:100])
                _send(json.dumps({"success": True, "message": m}) if m else json.dumps({"success": False, "error": "Timeout: belum ada email baru"}), status=200 if m else 408)
            else:
                _send(json.dumps({"success": False, "error": "Tidak ditemukan"}), status=404)

        elif clean_path.startswith("/api/saham/"):
            sub = clean_path[len("/api/saham/"):]
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            st, payload = ss.handle_api("GET", sub, q, {})
            _send(json.dumps(payload, default=str), status=st)

        elif clean_path.startswith("/api/teams/"):
            sub = clean_path[len("/api/teams/"):]
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            if sub == "status":
                _send(json.dumps(ts.teams_get_status()))
            elif sub == "screen":
                q_val = int(q.get("quality", 70) or 70)
                img_bytes = ts.teams_get_screenshot(quality=q_val)
                if img_bytes:
                    _send(img_bytes, content_type="image/jpeg", extra_headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
                else:
                    _send(b"", content_type="image/jpeg", status=503)
            elif sub == "digest/latest":
                d = ts.teams_get_latest_digest()
                _send(json.dumps({"success": True, "digest": d}))
            elif sub == "digests":
                limit = int(q.get("limit", 15) or 15)
                items = ts.teams_get_digest_history(limit=limit)
                _send(json.dumps({"success": True, "items": items}))
            elif sub == "digest":
                dig_id = int(q.get("id", 0) or 0)
                d = ts.teams_get_digest_by_id(dig_id)
                _send(json.dumps({"success": True, "digest": d}))
            elif sub == "logs":
                _send(json.dumps({"success": True, "logs": ts.teams_get_logs(int(q.get("limit", 150) or 150))}, default=str))
            elif sub == "diagnose":
                _send(json.dumps(ts.teams_diagnose(), default=str))
            elif sub == "config":
                _send(json.dumps(ts.teams_get_config()))
            elif sub == "raw-chats":
                _send(json.dumps(ts.teams_get_latest_raw_chats()))
            else:
                _send(json.dumps({"success": False, "error": "Tidak ditemukan"}), status=404)

        elif clean_path == "/api/email/status":
            _send(json.dumps({"success": True, "status": mailer.status(), "logs": mailer.recent_logs(self.history_db)}))

        elif clean_path == "/api/user/email-prefs":
            prefs = self.history_db.get_email_prefs(user["id"])
            _send(json.dumps({"success": True, "configured": mailer.is_configured(), **prefs}))

        elif clean_path == "/api/hermes/briefing":
            b = hb.get_briefing(self.history_db, user["id"]) if self.history_db else None
            ok, wait = hb.manual_allowed(self.history_db, user["id"]) if self.history_db else (False, 0)
            _send(json.dumps({"success": True, "briefing": b, "can_refresh": ok, "wait_seconds": wait}))

        elif clean_path == "/api/meetings":
            meetings = self.history_db.get_meetings(user_id=user["id"]) if self.history_db else []
            _send(json.dumps(meetings))

        elif self.path == "/api/deploy/status":
            status_file = "/home/ares/Documents/myserver/data/deploy.json"
            deploy_data = {
                "status": "ready",
                "webhook_url": "https://westpro.my.id/api/webhook/github",
                "branch": "main",
                "watcher_timer": "Active (Setiap 2 Menit)",
                "timestamp": datetime.now().isoformat()
            }
            if os.path.exists(status_file):
                try:
                    with open(status_file, "r", encoding="utf-8") as f:
                        deploy_data.update(json.load(f))
                except Exception:
                    pass
            _send(json.dumps(deploy_data))

        elif clean_path in ("/api/system/resources", "/api/system/status"):
            try:
                # 1. CPU
                cpu_pct = psutil.cpu_percent(interval=None)
                cpu_count = psutil.cpu_count(logical=True)
                cpu_freq = psutil.cpu_freq()
                load_1, load_5, load_15 = os.getloadavg()

                # 2. Memory
                mem = psutil.virtual_memory()

                # 3. Disk
                disk = psutil.disk_usage("/")

                # 4. Temperatures
                temps = {}
                try:
                    raw_temps = getattr(psutil, "sensors_temperatures", lambda: {})()
                    if raw_temps:
                        for sensor, entries in raw_temps.items():
                            if entries:
                                temps[sensor] = entries[0].current
                except Exception:
                    pass
                cpu_temp = temps.get("coretemp") or temps.get("cpu_thermal") or temps.get("acpitz") or 0
                nvme_temp = temps.get("nvme") or 0

                # 5. Uptime
                boot_time = psutil.boot_time()
                uptime_sec = int(time.time() - boot_time)
                days = uptime_sec // 86400
                hours = (uptime_sec % 86400) // 3600
                minutes = (uptime_sec % 3600) // 60
                uptime_str = f"{days}h {hours}j {minutes}m" if days > 0 else f"{hours} jam {minutes} menit"

                # 6. Network IP
                ip_addr = "127.0.0.1"
                try:
                    for iface, addrs in psutil.net_if_addrs().items():
                        if iface.startswith(("wlan", "eth", "en", "wl")):
                            for addr in addrs:
                                if addr.family.name == "AF_INET" and not addr.address.startswith("127."):
                                    ip_addr = f"{addr.address} ({iface})"
                                    break
                except Exception:
                    pass

                # 7. Services Status
                services_to_check = [
                    ("powmr.service", "Serial RS232 & Web Gateway (:3005)"),
                    ("hermes-agent.service", "Hermes Agent AI & Solar Watcher (:8000)"),
                    ("cloudflared.service", "Cloudflare Tunnel (westpro.my.id)"),
                    ("mail-collector.service", "IMAP OTP Mail Collector"),
                    ("deploy-watcher.timer", "Auto-Deploy Failsafe Watcher (2m)"),
                    ("appium.service", "Appium Android Automation (:4723)")
                ]
                services_status = []
                for s_name, desc in services_to_check:
                    res = subprocess.run(["systemctl", "--user", "is-active", s_name], capture_output=True, text=True)
                    state = res.stdout.strip() or "inactive"
                    services_status.append({
                        "name": s_name,
                        "description": desc,
                        "status": state,
                        "is_active": (state == "active")
                    })

                _send(json.dumps({
                    "success": True,
                    "cpu": {
                        "percent": cpu_pct,
                        "cores": cpu_count,
                        "freq_mhz": round(cpu_freq.current, 0) if cpu_freq else 1800,
                        "temp_c": round(cpu_temp, 1) if cpu_temp else None,
                        "load_avg": [round(load_1, 2), round(load_5, 2), round(load_15, 2)]
                    },
                    "memory": {
                        "total_mb": round(mem.total / (1024 * 1024), 0),
                        "used_mb": round(mem.used / (1024 * 1024), 0),
                        "free_mb": round(mem.available / (1024 * 1024), 0),
                        "percent": mem.percent
                    },
                    "disk": {
                        "total_gb": round(disk.total / (1024**3), 1),
                        "used_gb": round(disk.used / (1024**3), 1),
                        "free_gb": round(disk.free / (1024**3), 1),
                        "percent": disk.percent,
                        "nvme_temp_c": round(nvme_temp, 1) if nvme_temp else None
                    },
                    "uptime": {
                        "seconds": uptime_sec,
                        "formatted": uptime_str,
                        "boot_time": datetime.fromtimestamp(boot_time).strftime("%Y-%m-%d %H:%M:%S")
                    },
                    "network": {
                        "ip": ip_addr,
                        "domain": "westpro.my.id (HTTPS)"
                    },
                    "services": services_status
                }))
            except Exception as e:
                _send(json.dumps({"success": False, "error": str(e)}))
            return

        elif self.path in ("/api/webhook/github", "/webhook/github"):
            info = {
                "service": "Westpro Auto-Deploy Webhook",
                "webhook_url": "https://westpro.my.id/api/webhook/github",
                "branch": "main",
                "method": "POST",
                "supported_events": ["push", "ping"],
                "watcher_timer": "Active (deploy-watcher.timer every 2m)",
                "secret_configured": bool(os.getenv("GITHUB_WEBHOOK_SECRET")),
            }
            _send(json.dumps(info))

        # Webhook Monitor APIs
        elif clean_path == "/api/monitor/requests":
            try:
                limit = 50
                search = ""
                method_filter = ""
                if "?" in self.path:
                    query_str = self.path.split("?", 1)[1]
                    params = urllib.parse.parse_qs(query_str)
                    limit = int(params.get("limit", [50])[0])
                    search = params.get("search", [""])[0]
                    method_filter = params.get("method", [""])[0]
                reqs = wm.get_requests(limit=limit, search=search, method=method_filter)
                _send(json.dumps(reqs))
            except Exception as e:
                _send(json.dumps({"error": str(e), "requests": []}), status=500)

        elif clean_path.startswith("/api/monitor/requests/"):
            try:
                req_id = int(clean_path.split("/")[-1])
                req_item = wm.get_request_by_id(req_id)
                if req_item:
                    _send(json.dumps(req_item))
                else:
                    _send(json.dumps({"error": "Request not found"}), status=404)
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

        elif clean_path == "/api/monitor/rules":
            try:
                rules = wm.get_mock_rules()
                _send(json.dumps({"success": True, "rules": rules}))
            except Exception as e:
                _send(json.dumps({"success": False, "error": str(e), "rules": []}), status=500)

        elif self.path == "/healthz":
            _send('{"status": "ok"}')

        else:
            _send("Not Found", "text/plain", 404)

    def do_POST(self):
        clean_path = self.path.split("?")[0].rstrip("/")

        # Webhook Catch Intercept
        if clean_path.startswith("/catch") or clean_path.startswith("/api/webhook/catch"):
            self.handle_webhook_monitor_catch("POST")
            return

        # Gerbang otorisasi terpusat (login wajib; data/perangkat bersama khusus pemilik)
        if not self.authorize_api(clean_path, self.get_current_user()):
            return

        # Inverter Settings API (Owner only)
        if clean_path == "/api/inverter/settings":
            if not self.is_owner(self.get_current_user()) and not self.is_trusted_local():
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": false, "error": "Khusus pemilik server"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8")) if body else {}
                action = payload.get("action", "")

                if action == "apply_preset":
                    mode = payload.get("preset", "safe_15s")
                    res = self.inverter.apply_preset(mode) if self.inverter else {"success": False, "error": "Inverter offline"}
                    self.send_response(200 if res.get("success") else 400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(res).encode("utf-8"))
                    return

                elif action == "update_setting":
                    key = payload.get("key", "")
                    val = payload.get("value", "")
                    ok, msg = self.inverter.update_setting(key, val) if self.inverter else (False, "Inverter offline")
                    self.send_response(200 if ok else 400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": ok, "message": msg}).encode("utf-8"))
                    return

                elif action == "update_batch":
                    settings = payload.get("settings", {})
                    results = {}
                    overall_success = True
                    for k, v in settings.items():
                        ok, msg = self.inverter.update_setting(k, v) if self.inverter else (False, "Inverter offline")
                        results[k] = {"success": ok, "message": msg}
                        if not ok:
                            overall_success = False
                    self.send_response(200 if overall_success else 400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": overall_success, "results": results}).encode("utf-8"))
                    return

                elif action == "execute_raw":
                    cmd = payload.get("command", "")
                    ok, msg = self.inverter.execute_command(cmd) if self.inverter else (False, "Inverter offline")
                    self.send_response(200 if ok else 400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": ok, "message": msg}).encode("utf-8"))
                    return

                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Aksi tidak dikenali"}')
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                return

        # BMS Configuration API (Owner only)
        if clean_path == "/api/bms/config":
            if not self.is_owner(self.get_current_user()) and not self.is_trusted_local():
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": false, "error": "Khusus pemilik server"}')
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8")) if body else {}
                bms_cfg_file = "/home/ares/Documents/myserver/data/bms_config.json"
                os.makedirs(os.path.dirname(bms_cfg_file), exist_ok=True)
                cur_cfg = get_default_bms_config()
                if os.path.exists(bms_cfg_file):
                    try:
                        with open(bms_cfg_file, "r") as cf:
                            loaded = json.load(cf)
                            for sec, val in loaded.items():
                                if isinstance(val, dict) and sec in cur_cfg:
                                    cur_cfg[sec].update(val)
                                else:
                                    cur_cfg[sec] = val
                    except Exception:
                        pass

                # Merge changes
                for sec in ("system", "cell_protection", "pack_protection", "current_limits", "thermal_protection", "notifications"):
                    if sec in payload and isinstance(payload[sec], dict):
                        if sec not in cur_cfg:
                            cur_cfg[sec] = {}
                        cur_cfg[sec].update(payload[sec])
                    elif sec in payload:
                        cur_cfg[sec] = payload[sec]

                with open(bms_cfg_file, "w") as cf:
                    json.dump(cur_cfg, cf, indent=2)

                logger.info("[BMS] Konfigurasi proteksi & telemetri BMS berhasil disimpan: %s", bms_cfg_file)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "message": "Konfigurasi BMS berhasil disimpan", "config": cur_cfg}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                return

        # 0. Auth APIs
        if clean_path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                email = data.get("email", "").strip().lower()
                password = data.get("password", "")

                if not self.history_db:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Database tidak aktif"}')
                    return

                if not self.history_db.validate_email_format(email):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Format email tidak valid (harus mengandung @ dan domain, misal user@domain.com)"}')
                    return

                if not password or len(password) < 6:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Kata sandi minimal 6 karakter"}')
                    return

                user, err = self.history_db.authenticate_user(email, password)
                if not user:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": err or "Email atau kata sandi tidak cocok"}).encode("utf-8"))
                    return

                token = self.history_db.create_session(email)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"westpro_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "user": user}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/auth/register":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                email = data.get("email", "").strip().lower()
                name = data.get("name", "").strip()
                password = data.get("password", "")
                spectrum_email = data.get("spectrum_email", "").strip()
                spectrum_password = data.get("spectrum_password", "")
                spectrum_member_name = data.get("spectrum_member_name", "").strip()
                spectrum_project_id = data.get("spectrum_project_id", "").strip() or ""

                if not self.history_db:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Database tidak aktif"}')
                    return

                if not self.history_db.validate_email_format(email):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Format email tidak valid"}')
                    return

                if not password or len(password) < 6:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Password minimal 6 karakter"}')
                    return

                user = self.history_db.register_user(
                    email=email,
                    password=password,
                    name=name,
                    spectrum_email=spectrum_email,
                    spectrum_password=spectrum_password,
                    spectrum_member_name=spectrum_member_name,
                    spectrum_project_id=spectrum_project_id
                )

                # Trigger background sync if SPEctrum credentials provided
                if spectrum_email and spectrum_password:
                    def _do_bg_sync_reg(u_id, s_email, s_pwd, s_pid, s_mname):
                        try:
                            user_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{u_id}.json")
                            import sync_spectrum
                            summary = sync_spectrum.sync_spectrum(
                                email=s_email, password=s_pwd, project_id=s_pid,
                                member_name=s_mname or s_email.split("@")[0],
                                output_path=user_cache_path
                            )
                            task_cnt = len(summary.get("tasks", []))
                            if self.history_db:
                                self.history_db.update_spectrum_sync_status(u_id, f"Sinkronisasi Sukses ({task_cnt} Task)")
                        except Exception as e:
                            logger.warning(f"Background sync failed for user {u_id}: {e}")
                    threading.Thread(
                        target=_do_bg_sync_reg,
                        args=(user["id"], spectrum_email, spectrum_password, spectrum_project_id, spectrum_member_name),
                        daemon=True
                    ).start()

                token = self.history_db.create_session(email)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"westpro_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "user": user}).encode("utf-8"))
            except ValueError as ve:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(ve)}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/auth/spectrum-login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}

                spec_email = data.get("email", "").strip()
                spec_pass = data.get("password", "")
                name = data.get("name", "").strip()
                member_name = data.get("member_name", "").strip()
                project_id = data.get("project_id", "").strip() or ""

                if not spec_email or not spec_pass:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Email dan password SPEctrum wajib diisi"}')
                    return

                if not self.history_db:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Database tidak aktif"}')
                    return

                # Authenticate against Lifeinspectrum API
                sign_url = 'https://api.lifeinspectrum.com/v1/basic-auth/sign'
                headers = {
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                    'Referer': 'https://lifeinspectrum.com/',
                    'Origin': 'https://lifeinspectrum.com'
                }
                payload = json.dumps({'email': spec_email, 'password': spec_pass}).encode('utf-8')
                req = urllib.request.Request(sign_url, data=payload, headers=headers, method='POST')
                try:
                    with urllib.request.urlopen(req, timeout=12.0) as resp:
                        resp_data = json.loads(resp.read().decode('utf-8'))
                        id_token = resp_data.get('id_token')
                except urllib.error.HTTPError as he:
                    err_msg = "Email atau password SPEctrum salah"
                    try:
                        err_json = json.loads(he.read().decode('utf-8'))
                        if err_json.get("message"):
                            err_msg = err_json["message"]
                    except Exception:
                        pass
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": f"Autentikasi SPEctrum gagal: {err_msg}"}).encode("utf-8"))
                    return
                except Exception as he:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": f"Gagal menghubungi server Lifeinspectrum: {str(he)}"}).encode("utf-8"))
                    return

                if not id_token:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Gagal mendapatkan token otentikasi dari SPEctrum"}')
                    return

                # Get or create user
                user = self.history_db.get_or_create_spectrum_user(
                    email=spec_email,
                    name=name,
                    spectrum_password=spec_pass,
                    project_id=project_id,
                    member_name=member_name
                )
                self.history_db.update_spectrum_sync_status(user["id"], "Koneksi Berhasil")

                # Launch async sync in background thread
                def _do_bg_sync(u_id, s_email, s_pwd, s_pid, s_mname):
                    try:
                        user_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{u_id}.json")
                        import sync_spectrum
                        summary = sync_spectrum.sync_spectrum(
                            email=s_email, password=s_pwd, project_id=s_pid,
                            member_name=s_mname or s_email.split("@")[0],
                            output_path=user_cache_path
                        )
                        task_cnt = len(summary.get("tasks", []))
                        if self.history_db:
                            self.history_db.update_spectrum_sync_status(u_id, f"Sinkronisasi Sukses ({task_cnt} Task)")
                    except Exception as e:
                        logger.warning(f"Background sync failed for user {u_id}: {e}")

                threading.Thread(
                    target=_do_bg_sync,
                    args=(user["id"], spec_email, spec_pass, project_id, member_name),
                    daemon=True
                ).start()

                token = self.history_db.create_session(user["email"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"westpro_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": "Berhasil login dengan akun SPEctrum!",
                    "user": user
                }).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        # 0b. User Profile & Password APIs
        elif clean_path == "/api/user/profile":
            try:
                user = self.get_current_user()
                if not user or not self.history_db:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}
                new_name = data.get("name", "").strip()
                if not new_name:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Nama tidak boleh kosong"}')
                    return

                updated_user = self.history_db.update_user_profile(user["id"], new_name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": "Nama profil berhasil disimpan!",
                    "user": updated_user
                }).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/user/password":
            try:
                user = self.get_current_user()
                if not user or not self.history_db:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}
                old_pass = data.get("old_password", "")
                new_pass = data.get("new_password", "")
                confirm_pass = data.get("confirm_password", "")

                if not new_pass or len(new_pass) < 6:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Password baru minimal 6 karakter"}')
                    return

                if new_pass != confirm_pass:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Konfirmasi password baru tidak cocok"}')
                    return

                ok, msg = self.history_db.update_user_password(user["id"], old_pass, new_pass)
                if not ok:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": msg}).encode("utf-8"))
                    return

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "message": msg}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        # 0c. User SPEctrum Config & Testing APIs
        elif clean_path == "/api/user/spectrum/config":
            try:
                user = self.get_current_user()
                if not user or not self.history_db:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}

                updated_cfg = self.history_db.save_user_spectrum_config(user["id"], data)
                cfg_copy = dict(updated_cfg)
                raw_pass = cfg_copy.get("spectrum_password") or ""
                cfg_copy["has_password"] = bool(raw_pass)
                cfg_copy["spectrum_password_masked"] = ("•" * min(len(raw_pass), 12)) if raw_pass else ""
                cfg_copy["spectrum_password"] = ""
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "message": "Konfigurasi akun SPEctrum berhasil disimpan!", "config": cfg_copy}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/user/spectrum/test":
            try:
                user = self.get_current_user()
                if not user or not self.history_db:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}

                spec_email = data.get("spectrum_email", "").strip()
                spec_pass = data.get("spectrum_password", "").strip()

                if not spec_email or not spec_pass:
                    curr_cfg = self.history_db.get_user_spectrum_config(user["id"])
                    spec_email = spec_email or curr_cfg.get("spectrum_email")
                    spec_pass = spec_pass or curr_cfg.get("spectrum_password")

                if not spec_email or not spec_pass:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Email dan password SPEctrum wajib diisi"}')
                    return

                sign_url = 'https://api.lifeinspectrum.com/v1/basic-auth/sign'
                headers = {
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
                    'Referer': 'https://lifeinspectrum.com/',
                    'Origin': 'https://lifeinspectrum.com'
                }
                payload = json.dumps({'email': spec_email, 'password': spec_pass}).encode('utf-8')
                req = urllib.request.Request(sign_url, data=payload, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=12.0) as resp:
                    resp_data = json.loads(resp.read().decode('utf-8'))
                    id_token = resp_data.get('id_token')
                    spe_name = resp_data.get('name') or ''

                if id_token:
                    detected_projects = []
                    try:
                        import sync_spectrum
                        detected_projects = sync_spectrum.discover_projects({
                            'Authorization': f'Bearer {id_token}',
                            'User-Agent': headers['User-Agent'],
                            'Accept': 'application/json'
                        })
                    except Exception as e:
                        print(f"Error discovering projects during test: {e}")

                    proj_info = [f"{p['name']} ({p.get('tasks_count', 0)} task)" if p.get('tasks_count') else p['name'] for p in detected_projects]
                    summary_str = f" Proyek terdeteksi: {', '.join(proj_info)}." if proj_info else ""

                    # Auto-update member_name jika di DB masih kosong
                    if spe_name:
                        curr_cfg = self.history_db.get_user_spectrum_config(user["id"])
                        if not (curr_cfg.get("member_name") or "").strip():
                            self.history_db.save_user_spectrum_config(
                                user_id=user["id"],
                                spectrum_email=spec_email,
                                spectrum_password=spec_pass,
                                project_id=curr_cfg.get("project_id", ""),
                                member_name=spe_name,
                                auto_sync=curr_cfg.get("auto_sync", 1)
                            )

                    self.history_db.update_spectrum_sync_status(user["id"], "Koneksi Berhasil")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True,
                        "name": spe_name,
                        "projects": detected_projects,
                        "message": f"Koneksi berhasil! Akun {spe_name or spec_email} terverifikasi ke lifeinspectrum.com.{summary_str}"
                    }).encode("utf-8"))
                else:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Autentikasi gagal. Token tidak diperoleh."}')
            except urllib.error.HTTPError as he:
                err_text = f"HTTP Error {he.code}: {he.reason}"
                try:
                    err_body = he.read().decode("utf-8")
                    err_json = json.loads(err_body)
                    err_text += f" - {err_json.get('message', err_body)}"
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": err_text}).encode("utf-8"))
            except Exception as e:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/spectrum/mytask/update":
            try:
                user = self.get_current_user()
                cfg = self.history_db.get_user_spectrum_config(user["id"]) if (user and self.history_db) else {}
                spec_email = (cfg.get("spectrum_email") or "").strip() or "aris.aris@spesolution.com"
                spec_pass = (cfg.get("spectrum_password") or "").strip() or "jetak1998@SPE3"
                
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                updates = data.get("updates", [])
                
                import spectrum_mytask
                sm = spectrum_mytask.SpectrumMyTask(email=spec_email, password=spec_pass)
                updated_count = 0
                for u in updates:
                    tid = u.get("id")
                    desc = u.get("description", "")
                    stat = u.get("status", "TODO")
                    hour = int(u.get("hour", 0))
                    minute = int(u.get("minute", 0))
                    weight = int(u.get("weight", 1))
                    if tid:
                        sm.update_task_duration(tid, desc, stat, hour, minute, weight)
                        updated_count += 1
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "updated_count": updated_count}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path in ("/api/user/spectrum/sync", "/api/spectrum/sync"):
            try:
                user = self.get_current_user()
                if not user or not self.history_db:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                cfg = self.history_db.get_user_spectrum_config(user["id"])
                spec_email = (cfg.get("spectrum_email") or "").strip()
                spec_pass = (cfg.get("spectrum_password") or "").strip()
                proj_id = (cfg.get("project_id") or "").strip()
                member_name = (cfg.get("member_name") or "").strip() or user.get("name") or ""

                if not spec_email or not spec_pass:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Kredensial SPEctrum belum diatur untuk akun ini. Buka tab Config SPEctrum."}')
                    return

                user_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{user['id']}.json")
                import sync_spectrum
                summary = sync_spectrum.sync_spectrum(
                    email=spec_email,
                    password=spec_pass,
                    project_id=proj_id,
                    member_name=member_name,
                    output_path=user_cache_path
                )
                task_count = len(summary.get("tasks", []))
                self.history_db.update_spectrum_sync_status(user["id"], f"Sinkronisasi Sukses ({task_count} Task)")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": f"Sinkronisasi berhasil! {task_count} task dan kalender diperbarui.",
                    "tasks_count": task_count,
                    "data": summary
                }).encode("utf-8"))
            except Exception as e:
                if self.history_db and user:
                    self.history_db.update_spectrum_sync_status(user["id"], f"Gagal Sync: {str(e)[:40]}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/auth/logout":
            try:
                cookie_header = self.headers.get("Cookie", "")
                if "westpro_session=" in cookie_header and self.history_db:
                    import http.cookies
                    try:
                        cookies = http.cookies.SimpleCookie(cookie_header)
                        if "westpro_session" in cookies:
                            self.history_db.delete_session(cookies["westpro_session"].value)
                    except Exception:
                        pass

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "westpro_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        # Unggah file rekaman -> transkripsi (job latar belakang di hermes-agent)
        if clean_path == "/api/meetings/transcribe-file":
            def _jr(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            up_user = self.get_current_user()
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    _jr(400, {"success": False, "error": "File audio kosong"})
                    return
                if length > self.MAX_AUDIO_UPLOAD:
                    _jr(413, {"success": False, "error": f"File terlalu besar (maks {self.MAX_AUDIO_UPLOAD // (1024 * 1024)} MB). Kompres ke MP3/M4A dulu."})
                    return
                buf = bytearray()
                while len(buf) < length:
                    chunk = self.rfile.read(min(1024 * 1024, length - len(buf)))
                    if not chunk:
                        break
                    buf.extend(chunk)
                fwd_headers = {
                    "Content-Type": "application/octet-stream",
                    "X-Filename": urllib.parse.quote(self.headers.get("X-Filename", "rekaman"))[:200],
                    "X-Language": self.headers.get("X-Language", "id-ID"),
                    "X-Engine": self.headers.get("X-Engine", "auto"),
                    "X-Mode": "analyze" if self.headers.get("X-Mode") == "analyze" else "transcribe",
                    "X-Title": urllib.parse.quote(urllib.parse.unquote(self.headers.get("X-Title", ""))[:200]),
                    "X-Context": urllib.parse.quote(urllib.parse.unquote(self.headers.get("X-Context", ""))[:1500]),
                }
                req = urllib.request.Request("http://localhost:8000/api/transcribe-file", data=bytes(buf), headers=fwd_headers, method="POST")
                with urllib.request.urlopen(req, timeout=120.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.TRANSCRIBE_JOBS[data["job_id"]] = up_user["id"]
                _jr(200, {"success": True, "job_id": data["job_id"]})
            except urllib.error.HTTPError as e:
                _jr(e.code, {"success": False, "error": e.read().decode("utf-8", "ignore")[:300]})
            except Exception as e:
                _jr(500, {"success": False, "error": str(e)})
            return

        # ---- Westpro Mail: webhook masuk dari Cloudflare Email Worker (HMAC) ----
        if clean_path == "/api/mail/inbound":
            def _ij(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            secret = wm_mail.inbound_secret()
            if not secret:
                _ij(503, {"ok": False, "error": "MAIL_INBOUND_SECRET belum diatur"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length <= 0 or length > wm_mail.MAX_RAW_BYTES:
                _ij(413 if length > 0 else 400, {"ok": False, "error": "Ukuran email tidak valid atau terlalu besar"})
                return
            raw = self.rfile.read(length)
            if not wm_mail.verify_signature(secret, self.headers.get("X-Timestamp", ""), self.headers.get("X-Signature", ""), raw):
                _ij(401, {"ok": False, "error": "Tanda tangan tidak valid"})
                return
            try:
                res = wm_mail.ingest(self.history_db, raw, self.headers.get("X-Mail-To", ""))
                _ij(200, {"ok": True, **res})
            except Exception as e:
                _ij(500, {"ok": False, "error": str(e)[:200]})
            return

        # ---- Westpro Mail: kelola alamat, hapus/tandai, kirim, API key ----
        if clean_path.startswith("/api/webmail/"):
            wu = self.get_current_user()
            owner = self.is_owner(wu)
            def _wj(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            try:
                length = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(min(length, 65536)).decode("utf-8")) if length else {}
            except Exception:
                d = {}
            sub = clean_path[len("/api/webmail/"):]
            db_ = self.history_db
            if sub == "addresses":
                addr, msg = wm_mail.create_alias(db_, wu["id"], d.get("local"), str(d.get("label", "")), owner)
                _wj(200 if addr else 400, {"success": bool(addr), "address": addr, "error": None if addr else msg})
            elif sub == "addresses/delete":
                _wj(200, {"success": wm_mail.delete_alias(db_, wu["id"], str(d.get("address", "")), owner)})
            elif sub == "delete":
                _wj(200, {"success": True, "deleted": wm_mail.delete_messages(db_, wu["id"], owner, [int(i) for i in d.get("ids", [])])})
            elif sub == "read":
                _wj(200, {"success": True, "updated": wm_mail.set_read(db_, wu["id"], owner, [int(i) for i in d.get("ids", [])], bool(d.get("read", True)))})
            elif sub == "apikey":
                if d.get("revoke"):
                    wm_mail.revoke_api_key(db_, wu["id"])
                    _wj(200, {"success": True})
                else:
                    _wj(200, {"success": True, "key": wm_mail.create_api_key(db_, wu["id"]), "note": "Simpan sekarang; kunci tidak ditampilkan lagi."})
            elif sub == "send":
                to = str(d.get("to", "")).strip()
                subject = str(d.get("subject", "")).strip()[:200]
                text = str(d.get("text", ""))[:20000]
                if not mailer.is_configured():
                    _wj(503, {"success": False, "error": "SMTP belum dikonfigurasi (menu Sistem → Layanan Email)."})
                elif not mailer.valid_address(to) or not subject or not text.strip():
                    _wj(400, {"success": False, "error": "Isi penerima (email valid), subjek, dan pesan."})
                elif not owner and not pr.ip_allowed("send:" + str(wu["id"])):  # 10/jam untuk user biasa, bebas untuk owner
                    _wj(429, {"success": False, "error": "Terlalu banyak pengiriman. Coba lagi nanti."})
                else:
                    mine = wm_mail.my_addresses(db_, wu["id"])
                    frm = str(d.get("from_alias", "")).strip().lower()
                    if frm not in mine and not (owner and frm.endswith("@" + wm_mail.mail_domain())):
                        frm = mine[0] if mine else ""
                    sender_name = wu.get("username") or "Westpro"
                    from_hdr = f"{sender_name} <{frm}>" if frm else None
                    ok = mailer.send_async(db_, to, subject, text, None, kind="webmail", reply_to=frm or None, from_addr=from_hdr or frm or None)
                    if ok:
                        wm_mail.store_sent(db_, wu["id"], frm or mailer.cfg()["from"], to, subject, text)
                    _wj(200 if ok else 503, {"success": ok, "from": frm, "error": None if ok else "Antrean pengiriman penuh"})
            else:
                _wj(404, {"success": False, "error": "Tidak ditemukan"})
            return

        # ---- Analisis Saham IHSG ----
        if clean_path.startswith("/api/saham/"):
            sub = clean_path[len("/api/saham/"):]
            try:
                _len = int(self.headers.get("Content-Length", 0) or 0)
                _body = json.loads(self.rfile.read(_len).decode("utf-8")) if _len else {}
            except Exception:
                _body = {}
            st, payload = ss.handle_api("POST", sub, {}, _body)
            self.send_response(st)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode("utf-8"))
            return

        # ---- Microsoft Teams Remote Browser & AI Digest ----
        if clean_path.startswith("/api/teams/"):
            sub = clean_path[len("/api/teams/"):]
            def _tj(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            try:
                length = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(min(length, 10 * 1024 * 1024)).decode("utf-8")) if length else {}
            except Exception:
                d = {}
            if sub == "click":
                x = int(d.get("x", 0))
                y = int(d.get("y", 0))
                ok = ts.teams_dispatch_click(x, y)
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "type":
                text = d.get("text")
                key = d.get("key")
                ok = ts.teams_dispatch_key(text=text, key=key)
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "navigate":
                url = str(d.get("url") or "https://teams.microsoft.com")
                ok = ts.teams_navigate(url)
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "reload":
                ok = ts.teams_reload()
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "analyze":
                res = ts.teams_run_analysis()
                _tj(200 if res.get("status") == "success" else 500, res)
            elif sub == "config":
                ok = ts.teams_save_config(d)
                _tj(200 if ok else 500, {"success": ok})
            elif sub in ("session", "import-session"):
                res = ts.teams_import_session(d)
                _tj(200 if res.get("success") else 500, res)
            elif sub == "harvest-all" or sub == "pull":
                max_retries = int(d.get("max_retries", 3) or 3)
                max_chats = int(d.get("max_chats", 35) or 35)
                max_messages = int(d.get("max_messages", 60) or 60)
                res = ts.teams_pull_chats(max_retries=max_retries, max_chats=max_chats, max_messages=max_messages)
                _tj(200 if res.get("success") else 500, res)
            elif sub == "backup-session":
                ok = ts.teams_backup_current_session()
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "restore-session":
                ok = ts.teams_restore_session()
                _tj(200 if ok else 500, {"success": ok})
            elif sub == "sync-firefox":
                res = ts.teams_sync_from_firefox()
                _tj(200 if res.get("success") else 500, res)
            elif sub == "mode":
                headed = bool(d.get("headed", False))
                res = ts.teams_switch_mode(headed=headed)
                _tj(200 if res.get("success") else 500, res)
            elif sub == "clean-profile":
                res = ts.teams_clean_profile()
                _tj(200 if res.get("success") else 500, res)
            elif sub == "select-chat":
                title = str(d.get("title", "")).strip()
                res = ts.teams_select_chat(title)
                _tj(200 if res.get("success") else 500, res)
            else:
                _tj(404, {"success": False, "error": "Tidak ditemukan"})
            return

        # ---- Email: lupa / reset kata sandi, email uji (pemilik), preferensi email user
        if clean_path in ("/api/auth/forgot", "/api/auth/reset", "/api/email/test", "/api/user/email-prefs"):
            def _ej(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            try:
                length = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(min(length, 8192)).decode("utf-8")) if length else {}
            except Exception:
                d = {}
            em_user = self.get_current_user()

            if clean_path == "/api/auth/forgot":
                if not mailer.is_configured():
                    _ej(503, {"success": False, "error": "Layanan email belum dikonfigurasi di server. Hubungi admin."})
                    return
                if not pr.ip_allowed(self.client_ip()):
                    _ej(429, {"success": False, "error": "Terlalu banyak permintaan. Coba lagi dalam beberapa menit."})
                    return
                email = str(d.get("email", "")).strip().lower()[:200]
                if mailer.valid_address(email):
                    target = self.history_db.find_user_by_email(email)
                    token = pr.create_token(self.history_db, target["id"]) if target else None
                    if token:
                        link = f"{mailer.cfg()['base_url']}/reset?token={token}"
                        text, html = mailer.render(
                            "Atur ulang kata sandi",
                            [f"Halo {target.get('name') or email.split('@')[0]}, kami menerima permintaan mengatur ulang kata sandi akun Westpro Anda.",
                             "Tautan berlaku 30 menit dan hanya bisa dipakai sekali."],
                            button=("Atur ulang kata sandi", link),
                            footer="Jika Anda tidak memintanya, abaikan email ini; kata sandi Anda tidak berubah.")
                        mailer.send_async(self.history_db, email, "Atur ulang kata sandi Westpro", text, html, kind="reset")
                # respons selalu sama agar tidak membocorkan email yang terdaftar
                _ej(200, {"success": True, "message": "Jika email terdaftar, tautan atur ulang telah dikirim. Periksa kotak masuk dan folder spam."})
                return

            if clean_path == "/api/auth/reset":
                token = str(d.get("token", ""))
                password = str(d.get("password", ""))
                if len(password) < 6 or len(password) > 200:
                    _ej(400, {"success": False, "error": "Kata sandi minimal 6 karakter."})
                    return
                uid = pr.consume(self.history_db, token)
                if uid is None:
                    _ej(400, {"success": False, "error": "Tautan tidak valid, sudah dipakai, atau kedaluwarsa. Minta tautan baru."})
                    return
                ok, msg = self.history_db.set_user_password(uid, password)
                if not ok:
                    _ej(400, {"success": False, "error": msg})
                    return
                self.history_db.delete_user_sessions(uid)  # keluarkan semua perangkat
                addr = self.history_db.get_email_prefs(uid).get("email", "")
                text, html = mailer.render("Kata sandi Anda telah diubah",
                                           ["Kata sandi akun Westpro Anda baru saja diubah dan semua perangkat telah dikeluarkan.",
                                            "Jika ini bukan Anda, segera atur ulang kata sandi lagi dan hubungi admin."])
                mailer.send_async(self.history_db, addr, "Kata sandi Westpro diubah", text, html, kind="reset-selesai")
                _ej(200, {"success": True})
                return

            if clean_path == "/api/email/test":  # khusus pemilik (digerbang authorize_api)
                to = str(d.get("to", "")).strip() or em_user.get("email", "")
                if not mailer.valid_address(to):
                    _ej(400, {"success": False, "error": "Alamat tujuan tidak valid."})
                    return
                text, html = mailer.render("Email uji Westpro", ["Jika Anda membaca ini, layanan email Westpro berfungsi.",
                                                                  f"Dikirim {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} dari {mailer.cfg()['host']}."])
                try:
                    mailer.send_now(to, "Email uji Westpro", text, html)
                    mailer._log(self.history_db, to, "Email uji Westpro", "uji", "terkirim")
                    _ej(200, {"success": True, "message": f"Email uji terkirim ke {to}."})
                except Exception as e:
                    mailer._log(self.history_db, to, "Email uji Westpro", "uji", "gagal", str(e))
                    _ej(200, {"success": False, "error": str(e)})
                return

            if clean_path == "/api/user/email-prefs":
                self.history_db.set_email_prefs(em_user["id"], bool(d.get("briefing")))
                _ej(200, {"success": True})
                return

        # Perangkat Android per user: ambil perangkat kosong / lepas
        if clean_path in ("/api/android/claim", "/api/android/release"):
            lu = self.get_current_user()
            def _lj(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            try:
                length = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                serial = (d.get("serial") or "").strip() or None
                if clean_path.endswith("/release"):
                    n = al.release(self.history_db, lu["id"], serial, force=self.is_owner(lu))
                    _lj(200, {"success": True, "released": n})
                else:
                    lease, msg = al.claim(self.history_db, lu["id"], self.android_devices_raw().get("devices", []), serial, self.is_owner(lu))
                    if lease:
                        _lj(200, {"success": True, "serial": lease["serial"], "message": msg, "reused": lease.get("reused", False)})
                    else:
                        _lj(409, {"success": False, "error": msg})
            except Exception as e:
                _lj(500, {"success": False, "error": str(e)})
            return

        # Simpan notulen hasil analisa (mis. dari audio langsung) ke riwayat meeting milik user
        if clean_path == "/api/meetings/save":
            sv_user = self.get_current_user()
            try:
                length = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                m_id = self.history_db.save_meeting(
                    title=str(d.get("title") or "Meeting")[:200],
                    duration_sec=int(d.get("duration") or 0),
                    transcript=str(d.get("transcript") or ""),
                    summary=str(d.get("summary") or ""),
                    user_id=sv_user["id"],
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "id": m_id}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        # Hermes Agen (khusus pemilik; digerbang authorize_api): jalankan alat / setujui aksi
        if clean_path in ("/api/hermes/agent", "/api/hermes/agent/confirm", "/api/hermes/agent/cancel"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                target = "http://localhost:8000" + clean_path.replace("/api/hermes/agent", "/api/agent")
                req = urllib.request.Request(target, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=240.0) as resp:
                    out = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"reply": "", "error": f"Agen tidak tersedia: {e}", "steps": [], "pending": []}).encode("utf-8"))
            return

        # Hermes Briefing: generate manual (dibatasi cooldown per user)
        if clean_path == "/api/hermes/briefing/generate":
            def _json(status: int, payload: dict):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            b_user = self.get_current_user()
            ok, wait = hb.manual_allowed(self.history_db, b_user["id"])
            if not ok:
                _json(429, {"success": False, "error": f"Tunggu {wait} detik sebelum memperbarui lagi.", "wait_seconds": wait})
                return
            try:
                extra = None
                if self.is_owner(b_user) and self.inverter and self.inverter.last_state:
                    st = self.inverter.last_state
                    extra = {"solar": {"baterai_persen": st.get("soc"), "beban_watt": st.get("ac_output_watt"),
                                       "pv_watt": st.get("pv_power_watt")}}
                result = hb.generate_for_user(self.history_db, b_user, trigger="manual", extra_ctx=extra)
                _json(200, {"success": True, "briefing": result})
            except Exception as e:
                _json(500, {"success": False, "error": str(e)})
            return

        # Hermes AI Chat Proxy
        if self.path == "/api/hermes/chat":
            try:
                length = int(self.headers.get("Content-Length", 0))
                chat_user = self.get_current_user()
                chat_req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                chat_req["system_prompt"] = self.build_user_spectrum_context(chat_user) if chat_user else ""
                body = json.dumps(chat_req).encode("utf-8")
                req = urllib.request.Request(
                    "http://localhost:8000/api/chat",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=35.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
        elif self.path == "/api/sheets/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                spreadsheet_id = body.get("spreadsheet_id") or os.environ.get("GOOGLE_SPREADSHEET_ID", "1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs")
                sheet_name = body.get("sheet_name") or os.environ.get("GOOGLE_SHEET_NAME", "Sprint 6 PNRI")
                test_cases = body.get("test_cases", [])
                author = body.get("author", "Aris")
                
                from google_sheets_qa import sync_extracted_test_cases
                result = sync_extracted_test_cases(spreadsheet_id, sheet_name, test_cases, author=author)
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
                self.wfile.write(json.dumps({
                    "success": True, 
                    "url": sheet_url, 
                    "updated": result.get("updated", 0),
                    "created": result.get("created", 0),
                    "count": result.get("total", len(test_cases))
                }).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return
        elif self.path in ("/api/mail/config", "/api/mail/test", "/api/model/select", "/api/otp/rules", "/api/otp/sync") or self.path.startswith("/api/otp/"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                target_url = f"http://localhost:8000{self.path}"
                req = urllib.request.Request(
                    target_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=60.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/meetings/transcribe":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req = urllib.request.Request(
                    "http://localhost:8000/api/transcribe",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=35.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
        elif self.path == "/api/meetings/polish-transcript":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req = urllib.request.Request(
                    "http://localhost:8000/api/clean-transcript",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=35.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "success": False}).encode("utf-8"))
        elif self.path == "/api/meetings/update":
            try:
                user = self.get_current_user()
                if not user:
                    raise PermissionError("Silakan login terlebih dahulu.")
                user_id = user["id"]
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                m_id = data.get("id")
                transcript = data.get("transcript")
                summary = data.get("summary")
                if m_id and self.history_db:
                    self.history_db.update_meeting(m_id, transcript=transcript, summary=summary, user_id=user_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "success": False}).encode("utf-8"))
        elif self.path == "/api/meetings/summarize":
            try:
                user = self.get_current_user()
                if not user:
                    raise PermissionError("Silakan login terlebih dahulu.")
                user_id = user["id"]
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req_data = json.loads(body.decode("utf-8"))

                # Forward ke hermes-agent untuk rangkuman LLM
                req = urllib.request.Request(
                    "http://localhost:8000/api/summarize-meeting",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=45.0) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))

                    # Simpan ke SQLite history_db dengan user_id
                    if self.history_db:
                        m_id = self.history_db.save_meeting(
                            title=req_data.get("title", "Meeting"),
                            duration_sec=req_data.get("duration", 0),
                            transcript=req_data.get("transcript", ""),
                            summary=resp_data.get("summary", ""),
                            user_id=user_id
                        )
                        resp_data["id"] = m_id
                        resp_data["user_id"] = user_id

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/meetings/extract-tc":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                transcript = body.get("transcript", "")
                summary = body.get("summary", "")
                
                content_to_analyze = transcript if len(transcript) > 50 else summary
                if not content_to_analyze:
                    raise ValueError("Transkrip atau rangkuman meeting masih kosong.")
                
                # Baca 71 Test Case eksisting yang sedang berjalan (fokus pada 3 data inti: Test Case, Steps, Expected)
                from google_sheets_qa import load_existing_test_cases
                existing_tcs = load_existing_test_cases()
                existing_context_items = []
                for tc in existing_tcs:
                    t_id = tc.get("id") or "-"
                    t_tit = tc.get("title") or "-"
                    t_steps = tc.get("steps", "").replace("\n", " ; ")[:100]
                    t_exp = tc.get("expected", "").replace("\n", " ")[:100]
                    existing_context_items.append(f"• [{t_id}] Test Case: {t_tit} | Steps: {t_steps} | Expected: {t_exp}")
                existing_context_str = "\n".join(existing_context_items)
                
                prompt = f"""Kamu adalah QA Lead Senior untuk proyek BNI YAP! Sprint 6 (PNRI & AHU).
Berikut adalah rangkuman keputusan meeting koordinasi:

=== ISI RAPAT / NOTULEN ===
{content_to_analyze}

=== DAFTAR {len(existing_tcs)} TEST CASE EKSISTING DI SPRINT 6 PNRI ===
{existing_context_str}

TUGAS UTAMA:
Data pengujian yang dianalisis dan dikelola HANYA berfokus pada 3 elemen inti:
1. Test Case (Judul/Deskripsi Skenario)
2. Test Steps (Langkah-langkah Pengujian)
3. Expected Result (Hasil yang Diharapkan)

Bandingkan keputusan rapat dengan {len(existing_tcs)} Test Case di atas:
A. Jika keputusan rapat MENGUBAH / MEREVISI Test Case yang sudah ada (misal: tombol Cek Status & Checkout di-hide bukan rollback; icon default PNRI digunakan; banner promo dihapus):
   - "action": "UPDATE"
   - "target_id": ID test case eksisting terkait (contoh: "PNRI-010" atau yang sesuai dari daftar di atas)
   - "id": ID yang sama dengan target_id
   - "title": Judul Test Case yang disesuaikan
   - "steps": Langkah-langkah pengujian (steps) yang disesuaikan
   - "expected": Hasil yang diharapkan (expected result) setelah keputusan rapat
   - "notes": Alasan/dasar perubahan dari rapat
B. Jika keputusan rapat memunculkan Test Case BARU yang belum pernah ada:
   - "action": "CREATE"
   - "id": ID lanjutan (contoh "PNRI-039", dst)
   - "title": Judul Test Case baru
   - "steps": Langkah-langkah pengujian (steps)
   - "expected": Hasil yang diharapkan (expected result)
   - "notes": Catatan keputusan rapat

Formatkan response HANYA berupa JSON array valid (tanpa markdown explanation dan tanpa code fence):
[
  {{
    "action": "UPDATE",
    "target_id": "PNRI-010",
    "id": "PNRI-010",
    "module": "Billing",
    "title": "Verifikasi tombol Checkout disembunyikan sesuai keputusan rapat",
    "type": "Positive",
    "steps": ["Buka halaman billing PNRI", "Periksa ketersediaan tombol checkout"],
    "expected": "Tombol checkout tidak ditampilkan di antarmuka pengguna",
    "notes": "Rapat 12 Okt: Dev belum siap prod, fitur checkout di-hide bukan rollback"
  }}
]"""
                req = urllib.request.Request(
                    "http://localhost:8000/api/chat",
                    data=json.dumps({"prompt": prompt}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=50.0) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    reply_text = resp_data.get("reply", "").strip()
                    
                    if reply_text.startswith("```json"):
                        reply_text = reply_text[7:]
                    if reply_text.startswith("```"):
                        reply_text = reply_text[3:]
                    if reply_text.endswith("```"):
                        reply_text = reply_text[:-3]
                    reply_text = reply_text.strip()
                    
                    test_cases = json.loads(reply_text)
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True, 
                        "test_cases": test_cases,
                        "existing_tc_count": len(existing_tcs)
                    }).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return
        elif self.path in ("/api/webhook/github", "/webhook/github", "/api/webhook/deploy"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""
                event = self.headers.get("X-GitHub-Event", "push")

                # Verify Secret if GITHUB_WEBHOOK_SECRET is set
                secret = os.getenv("GITHUB_WEBHOOK_SECRET")
                if secret:
                    sig_header = self.headers.get("X-Hub-Signature-256", "")
                    expected_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(sig_header, expected_sig):
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "Signature mismatch / invalid secret"}')
                        return

                if event == "ping":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "ok", "message": "Pong! Webhook is active and reachable"}')
                    return

                # Check branch for push event
                ref = "refs/heads/main"
                commit_id = "unknown"
                if body:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                        ref = payload.get("ref", ref)
                        commit_id = payload.get("after", payload.get("head_commit", {}).get("id", "unknown"))
                    except Exception:
                        pass

                if ref != "refs/heads/main" and not ref.endswith("/main"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ignored", "ref": ref, "message": "Push ignored (not main branch)"}).encode("utf-8"))
                    return

                # Trigger deploy via systemd
                subprocess.Popen(["systemctl", "--user", "start", "deploy.service"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "deploying",
                    "message": "Auto-deploy triggered successfully for main branch",
                    "commit": commit_id,
                    "event": event
                }).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "status": "failed"}).encode("utf-8"))
        elif self.path == "/api/deploy/trigger":
            try:
                subprocess.Popen(["systemctl", "--user", "start", "deploy.service"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "started", "message": "Deployment job launched via deploy.service"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif clean_path == "/api/meetings/delete":
            try:
                user = self.get_current_user()
                if not user:
                    raise PermissionError("Silakan login terlebih dahulu.")
                user_id = user["id"]
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req_data = json.loads(body.decode("utf-8"))
                meeting_id = int(req_data.get("id", 0))
                if self.history_db and meeting_id > 0:
                    self.history_db.delete_meeting(meeting_id, user_id=user_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"success": true}')
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Invalid meeting ID"}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/appium/action":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                act_user = self.get_current_user()
                act = json.loads(body.decode("utf-8")) if body else {}
                allowed = self.device_access(act_user, (act.get("serial") or "").strip() or None)
                if allowed is None:
                    return
                if allowed and not act.get("serial"):
                    act["serial"] = allowed
                body = json.dumps(act).encode("utf-8")
                req = urllib.request.Request(
                    "http://localhost:8000/api/devices/action",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "success": False}).encode("utf-8"))
            return
        elif clean_path in ("/api/devices/connect", "/api/devices/disconnect", "/api/devices/scan-wifi", "/api/appium/scan-wifi"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                sub_path = "/api/devices/scan-wifi" if clean_path.endswith("/scan-wifi") else self.path
                target_url = f"http://localhost:8000{sub_path}"
                req = urllib.request.Request(
                    target_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=60.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "success": False}).encode("utf-8"))
            return

        elif clean_path == "/api/monitor/rules":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                rule_data = json.loads(body.decode("utf-8"))
                rule_res = wm.add_mock_rule(rule_data)
                rule_id = rule_res.get("id") if isinstance(rule_res, dict) else rule_res
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "id": rule_id, "rule": rule_res}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        elif clean_path.startswith("/api/monitor/rules/") and clean_path.endswith("/toggle"):
            try:
                rule_id = clean_path.split("/")[-2]
                new_state = wm.toggle_mock_rule(rule_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": new_state is not None, "enabled": new_state}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        elif clean_path == "/api/monitor/replay":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                replay_data = json.loads(body.decode("utf-8"))
                res = wm.replay_request(
                    request_id=replay_data.get("request_id"),
                    target_url=replay_data.get("target_url"),
                    custom_headers=replay_data.get("custom_headers"),
                    custom_body=replay_data.get("custom_body")
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(res).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_DELETE(self):
        clean_path = self.path.split("?")[0].rstrip("/")

        if clean_path.startswith("/catch") or clean_path.startswith("/api/webhook/catch"):
            self.handle_webhook_monitor_catch("DELETE")
            return

        if not self.authorize_api(clean_path, self.get_current_user()):
            return

        if clean_path == "/api/monitor/requests":
            try:
                wm.clear_requests()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif clean_path.startswith("/api/monitor/requests/"):
            try:
                req_id = int(clean_path.split("/")[-1])
                success = wm.delete_request(req_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": success}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif clean_path.startswith("/api/monitor/rules/"):
            try:
                rule_id = clean_path.split("/")[-1]
                success = wm.delete_mock_rule(rule_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": success}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif clean_path.startswith("/api/meetings/"):
            try:
                user = self.get_current_user()
                if not user:
                    raise PermissionError("Silakan login terlebih dahulu.")
                user_id = user["id"]
                meeting_id = int(clean_path.split("/")[-1])
                if self.history_db and meeting_id > 0:
                    self.history_db.delete_meeting(meeting_id, user_id=user_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"success": true}')
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                return
        elif clean_path == "/api/otp/inbox":
            try:
                req = urllib.request.Request("http://localhost:8000/api/otp/inbox", method="DELETE")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                return
        elif clean_path == "/api/mail/config":
            try:
                target_url = "http://localhost:8000" + self.path
                req = urllib.request.Request(target_url, method="DELETE")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    resp_body = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                return
        self.send_response(404)
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ==============================================================================
# Terminal Live Dashboard
# ==============================================================================

def print_terminal_dashboard(data: dict):
    sys.stdout.write("\033[2J\033[H")
    
    soc = data.get("soc", 0)
    bar_len = 20
    filled_len = int(round(bar_len * soc / 100))
    bar = "█" * filled_len + "░" * (bar_len - filled_len)

    print("\033[1;36m======================================================================\033[0m")
    print("\033[1;37m     POWMR HYBRID INVERTER MONITOR — THINKCENTRE M90n SERVER          \033[0m")
    print(f"\033[1;36m======================================================================\033[0m")
    print(f" Status: \033[1;32mONLINE\033[0m  |  Mode: \033[1;33m{data.get('mode')}\033[0m  |  Waktu: {data.get('timestamp')[:19]}")
    print("----------------------------------------------------------------------")
    print(f" \033[1;33m[SOLAR PV]\033[0m      Daya: \033[1;33m{data.get('pv_power_watt', 0):>5} W\033[0m  |  Tegangan: {data.get('pv_voltage', 0):>5.1f} V  |  Arus: {data.get('pv_current', 0):>4.1f} A")
    print(f" \033[1;32m[BATERAI]\033[0m       Tegangan: \033[1;32m{data.get('battery_voltage', 0):>5.2f} V\033[0m  |  SOC: [{bar}] \033[1;32m{soc}%\033[0m")
    print(f"                 Charge:   {data.get('battery_charging_current', 0):>4} A  |  Discharge: {data.get('battery_discharge_current', 0):>4} A  |  Net: {data.get('battery_power_watt', 0):>5} W")
    print(f" \033[1;34m[BEBAN AC]\033[0m      Daya: \033[1;34m{data.get('ac_output_watt', 0):>5} W\033[0m  |  Apparent: {data.get('ac_output_va', 0):>5} VA  |  Load: \033[1;34m{data.get('load_percent', 0)}%\033[0m")
    print(f"                 Output:   {data.get('ac_output_voltage', 0):>5.1f} V  |  Frekuensi: {data.get('ac_output_frequency', 0):>4.1f} Hz")
    print(f" \033[1;35m[PLN / GRID]\033[0m    Input:    {data.get('grid_voltage', 0):>5.1f} V  |  Frekuensi: {data.get('grid_frequency', 0):>4.1f} Hz  |  Bus: {data.get('bus_voltage', 0)} V")
    print("----------------------------------------------------------------------")
    print(f" Suhu Heatsink: {data.get('temperature', 0)} °C  |  Web UI: \033[1;32mhttps://westpro.my.id\033[0m  (Ctrl+C stop)")
    print("\033[1;36m======================================================================\033[0m")
    sys.stdout.flush()


# ==============================================================================
# Main Runner Loop
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="PowMr Hybrid Inverter Serial Reader & Grafana Server")
    parser.add_argument("--port", "-p", default=os.environ.get("POWMR_PORT", "/dev/ttyS0"), help="Serial port (/dev/ttyS0)")
    parser.add_argument("--baud", "-b", type=int, default=int(os.environ.get("POWMR_BAUD", "2400")), help="Baud rate (default: 2400)")
    parser.add_argument("--web-port", "-w", type=int, default=int(os.environ.get("POWMR_WEB_PORT", "3005")), help="Port Web Server (default: 3005)")
    parser.add_argument("--interval", "-i", type=float, default=2.0, help="Polling interval detik (default: 2.0)")
    parser.add_argument("--no-cli", action="store_true", help="Nonaktifkan live dashboard di terminal (mode daemon)")
    args = parser.parse_args()

    history_db = HistoryDatabase()
    inv = PowMrInverter(port=args.port, baudrate=args.baud)
    if not inv.connect():
        print(f"[FATAL] Gagal membuka port {args.port}. Cek kabel serial atau izin pengguna dialout.", file=sys.stderr)
        return 1

    ratings = inv.read_ratings()
    if ratings:
        logger.info("Inverter terdeteksi: Rated Power %s W, Rated Batt %s V", ratings.get("ac_output_rated_active_power"), ratings.get("battery_rated_voltage"))

    InverterHTTPHandler.inverter = inv
    InverterHTTPHandler.history_db = history_db

    def _sync_user_spectrum(user: dict):
        """Segarkan cache SPEctrum user sebelum briefing otomatis (hanya yang auto_sync & kredensial lengkap)."""
        cfg = history_db.get_user_spectrum_config(user["id"]) or {}
        if not (cfg.get("spectrum_email") and cfg.get("spectrum_password") and cfg.get("auto_sync", 1)):
            return
        import sync_spectrum
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"spectrum_user_{user['id']}.json")
        sync_spectrum.sync_spectrum(email=cfg["spectrum_email"], password=cfg["spectrum_password"],
                                    project_id=(cfg.get("project_id") or "").strip(),
                                    member_name=(cfg.get("member_name") or user.get("name") or ""), output_path=out)
        history_db.update_spectrum_sync_status(user["id"], "Sinkronisasi otomatis 02:00")

    def _start_spectrum_daily_reminder(h_db):
        def reminder_loop():
            last_reminded_date = None
            while True:
                try:
                    now = datetime.datetime.now()
                    # Cek hari kerja Senin-Jumat pada sore hari (16:00 - 18:00 WIB)
                    if now.weekday() < 5 and now.hour in (16, 17, 18):
                        today_str = now.strftime('%Y-%m-%d')
                        if last_reminded_date != today_str:
                            import spectrum_mytask
                            sm = spectrum_mytask.SpectrumMyTask(email='aris.aris@spesolution.com', password='jetak1998@SPE3')
                            analysis = sm.analyze_day(today_str)
                            if not analysis.get('meets_8_hours'):
                                cur_h = analysis.get('current_hours', 0)
                                cur_m = analysis.get('current_minutes', 0)
                                def_m = analysis.get('deficit_minutes', 0)
                                logger.warning('[SPEctrum Reminder] ⚠️ PERINGATAN: Jam kerja hari ini (%s) baru %dh %dm (Kurang %dh %dm dari target 8 jam)! Silakan isi via Westpro MyTask.',
                                               today_str, cur_h, cur_m, def_m // 60, def_m % 60)
                                last_reminded_date = today_str
                except Exception as e:
                    logger.debug('[SPEctrum Reminder Error]: %s', e)
                time.sleep(900)  # Cek setiap 15 menit

        t = threading.Thread(target=reminder_loop, daemon=True)
        t.start()

    _start_spectrum_daily_reminder(history_db)

    hb.start_scheduler(history_db, sync_fn=_sync_user_spectrum)

    try:
        ts.teams_start_periodic_harvester(interval_sec=1800)
    except Exception as te:
        logger.warning("Gagal memulai Teams periodic harvester: %s", te)

    try:
        ss.start_scheduler()
    except Exception as se:
        logger.warning("Gagal memulai scheduler saham: %s", se)

    try:
        httpd = ThreadedHTTPServer(("0.0.0.0", args.web_port), InverterHTTPHandler)
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        logger.info("Web Dashboard & API aktif di: http://0.0.0.0:%d", args.web_port)
    except Exception as e:
        logger.warning("Gagal memulai Web Server di port %d: %s", args.web_port, e)

    logger.info("Mulai polling inverter setiap %.1fs...", args.interval)

    insert_counter = 0
    try:
        while True:
            data = inv.read_telemetry()
            if data:
                # Simpan ke SQLite history setiap 2 tick (setiap 4 detik)
                insert_counter += 1
                if insert_counter % 2 == 0:
                    try:
                        history_db.insert(data)
                    except Exception as e:
                        logger.error("Gagal simpan ke SQLite: %s", e)

                if not args.no_cli:
                    print_terminal_dashboard(data)
                else:
                    logger.debug("Telemetry update: PV=%sW, Batt=%sV (SOC %s%%), Load=%sW",
                                 data.get("pv_power_watt"), data.get("battery_voltage"), data.get("soc"), data.get("ac_output_watt"))
            else:
                if not args.no_cli:
                    print(f"[WARN] Timeout membaca dari {args.port}, mencoba lagi...")
                if inv.error_count > 5:
                    logger.warning("Koneksi serial bermasalah, mencoba re-init port...")
                    inv.connect()
                    inv.error_count = 0

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[INFO] Menghentikan service PowMr Inverter...")
    finally:
        inv.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
