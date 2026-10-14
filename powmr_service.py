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
      justify-content: space-between;
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
      <a class="tab-btn" id="nav-hermes" href="/hermes" onclick="event.preventDefault(); navigateToPage('hermes')">Workspace QA <span id="navHermesBadge" style="display:none; background:#0284c7; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-meeting" href="/meeting" onclick="event.preventDefault(); navigateToPage('meeting')">Notulen Rapat</a>
      <a class="tab-btn" id="nav-android" href="/android" onclick="event.preventDefault(); navigateToPage('android')">Android Remote</a>
      <a class="tab-btn" id="nav-otp" href="/otp" onclick="event.preventDefault(); navigateToPage('otp')">Kotak OTP <span id="navOtpBadge" style="display:none; background:#d97706; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-webhook" href="/webhook" onclick="event.preventDefault(); navigateToPage('webhook')">Webhook Monitor <span id="navWebhookBadge" style="display:none; background:#0284c7; color:#fff; font-weight:600; border-radius:10px; padding:0.05rem 0.35rem; font-size:0.65rem; margin-left:0.25rem;"></span></a>
      <a class="tab-btn" id="nav-profile" href="/profile" onclick="event.preventDefault(); navigateToPage('profile')">Profil &amp; SPEctrum</a>
      <a class="tab-btn" id="nav-system" href="/system" onclick="event.preventDefault(); navigateToPage('system')">Sistem</a>
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

          <!-- 3D ISOMETRIC SOLAR PANEL SVG -->
          <div style="margin: 0.5rem 0; text-align: center;">
            <svg viewBox="0 0 400 170" width="100%" height="auto" style="filter: drop-shadow(0 12px 20px rgba(0,0,0,0.55));">
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

          <!-- ANIMATED ENERGY FLOW DIAGRAM (FROM REFERENCE IMAGE 2) -->
          <div class="energy-flow-container">
            <!-- NODE 1: PV -->
            <div class="energy-node">
              <div class="energy-circle" style="background: radial-gradient(circle, #f59e0b 0%, #b45309 100%); color: #fff;">
                ☀️
              </div>
              <div style="font-size: 0.7rem; font-weight: 600; color: #fbbf24;">Solar PV</div>
              <div style="font-size: 0.8rem; font-weight: 700; color: #fff; font-family: monospace;" id="flowPvWatt">-- W</div>
            </div>

            <div style="color: rgba(255,255,255,0.25); font-size: 1.2rem; animation: pulseArrow 1.5s infinite;">➔</div>

            <!-- NODE 2: INVERTER -->
            <div class="energy-node">
              <div class="energy-circle" style="background: radial-gradient(circle, #38bdf8 0%, #0369a1 100%); color: #fff;">
                ⚡
              </div>
              <div style="font-size: 0.7rem; font-weight: 600; color: #38bdf8;">Inverter PowMr</div>
              <div style="font-size: 0.72rem; color: #94a3b8;" id="ovInverterMode">Loading...</div>
            </div>

            <div style="color: rgba(255,255,255,0.25); font-size: 1.2rem; animation: pulseArrow 1.5s infinite;">➔</div>

            <!-- NODE 3: HOME LOAD -->
            <div class="energy-node">
              <div class="energy-circle" style="background: radial-gradient(circle, #3b82f6 0%, #1d4ed8 100%); color: #fff;">
                🏠
              </div>
              <div style="font-size: 0.7rem; font-weight: 600; color: #60a5fa;">Beban Rumah</div>
              <div style="font-size: 0.8rem; font-weight: 700; color: #fff; font-family: monospace;" id="flowLoadWatt">-- W</div>
            </div>

            <div style="color: rgba(255,255,255,0.25); font-size: 1.2rem;">⇅</div>

            <!-- NODE 4: BATTERY -->
            <div class="energy-node">
              <div class="energy-circle" style="background: radial-gradient(circle, #10b981 0%, #047857 100%); color: #fff;">
                🔋
              </div>
              <div style="font-size: 0.7rem; font-weight: 600; color: #34d399;">LiFePO4 48V</div>
              <div style="font-size: 0.8rem; font-weight: 700; color: #fff; font-family: monospace;" id="flowBattSocText">--%</div>
            </div>

            <div style="color: rgba(255,255,255,0.25); font-size: 1.2rem;">⇅</div>

            <!-- NODE 5: GRID -->
            <div class="energy-node">
              <div class="energy-circle" style="background: radial-gradient(circle, #a855f7 0%, #7e22ce 100%); color: #fff;">
                🔌
              </div>
              <div style="font-size: 0.7rem; font-weight: 600; color: #c084fc;">Grid PLN</div>
              <div style="font-size: 0.8rem; font-weight: 700; color: #fff; font-family: monospace;" id="ovGridVolt">-- V</div>
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
                <span>Grafik Perbandingan Energi Harian: Produksi Surya vs Beban (kWh)</span>
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
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">🏠 Beban (kWh)</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: right;">Hemat (Rp)</th>
                      <th style="padding: 0.4rem 0.5rem; text-align: center;">Status</th>
                    </tr>
                  </thead>
                  <tbody id="energyDailyTableBody">
                    <tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: #64748b;">Memuat data riwayat...</td></tr>
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

    <!-- ==================== HALAMAN 3: WORKSPACE QA ==================== -->
    <div id="page-hermes" class="view-section">
      <!-- DAILY BRIEFING CARD -->
      <div id="dailyBriefingCard" style="display:none; background: linear-gradient(135deg, rgba(56,189,248,0.07) 0%, rgba(16,185,129,0.05) 100%); border: 1px solid rgba(56,189,248,0.15); border-radius: 10px; padding: 0.85rem 1.1rem; margin-bottom: 1.1rem; display:flex; align-items:flex-start; justify-content:space-between; gap:1rem;">
        <div style="display:flex; align-items:flex-start; gap:0.75rem;">
          <div style="width:34px; height:34px; background:#18181b; border:1px solid #27272a; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:1rem; flex-shrink:0;">H</div>
          <div>
            <div id="briefingGreeting" style="font-size:0.875rem; font-weight:600; color:#f4f4f5; line-height:1.4;">Hei __USER_NAME__, selamat pagi!</div>
            <div id="briefingBody" style="font-size:0.78rem; color:#94a3b8; margin-top:0.2rem; line-height:1.45;">Memuat briefing harian...</div>
          </div>
        </div>
        <button onclick="document.getElementById('dailyBriefingCard').style.display='none'" style="background:transparent; border:none; color:#52525b; cursor:pointer; font-size:1rem; flex-shrink:0; padding:0.1rem;">✕</button>
      </div>

      <div class="page-header" style="margin-bottom: 1.25rem; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.75rem;">
        <div>
          <h1 style="font-size: 1.15rem; font-weight: 700; color: #fff; margin: 0;">
            Workspace QA — Sprint 6 BNI YAP!
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
              <span id="spectrumSprintTitle" style="font-weight: 600; color: #fff;">Sprint 6 - 2026</span>
              <span id="spectrumSprintStatusBadge" class="hermes-badge badge-ok" style="font-size: 0.65rem;">Berjalan</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.65rem;">
              <span id="spectrumDaysRemaining" style="font-size: 0.72rem; font-weight: 600; color: #f59e0b; background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.2); border-radius: 4px; padding: 0.1rem 0.45rem;">-- hari tersisa</span>
              <span id="spectrumSprintPeriod" style="font-size: 0.72rem; color: #a1a1aa;">07 Okt - 20 Okt 2026</span>
            </div>
          </div>

          <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 6px; padding: 0.75rem 0.85rem; margin-top: 0.5rem; margin-bottom: 0.85rem;">
            <div style="font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; color: #52525b; font-weight: 600; margin-bottom: 0.25rem;">Target Sprint</div>
            <div id="spectrumSprintGoal" style="font-size: 0.82rem; color: #e4e4e7; line-height: 1.45;">
              User PNRI dapat melihat daftar tagihan PNRI (dalam bentuk nomer VA), memilih, dan menyelesaikan pembayaran langsung dari aplikasi YAP!
            </div>
          </div>

          <!-- SPRINT METRICS PILLS -->
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-bottom: 0.75rem;">
            <div style="background: #18181b; border: 1px solid #27272a; border-radius: 6px; padding: 0.5rem 0.65rem; text-align: center;">
              <div style="font-size: 0.68rem; color: #52525b;">Story Points</div>
              <div id="metricTotalPoints" style="font-size: 1.15rem; font-weight: 700; color: #38bdf8; margin-top: 0.15rem;">0</div>
            </div>
            <div style="background: #18181b; border: 1px solid #27272a; border-radius: 6px; padding: 0.5rem 0.65rem; text-align: center;">
              <div style="font-size: 0.68rem; color: #52525b;">Total Task</div>
              <div id="metricTotalTasks" style="font-size: 1.15rem; font-weight: 700; color: #f4f4f5; margin-top: 0.15rem;">0</div>
            </div>
            <div style="background: #18181b; border: 1px solid rgba(16,185,129,0.3); border-radius: 6px; padding: 0.5rem 0.65rem; text-align: center;">
              <div style="font-size: 0.68rem; color: #52525b;">Ready QA</div>
              <div id="metricDevDone" style="font-size: 1.15rem; font-weight: 700; color: #10b981; margin-top: 0.15rem;">0</div>
            </div>
            <div style="background: #18181b; border: 1px solid rgba(245,158,11,0.25); border-radius: 6px; padding: 0.5rem 0.65rem; text-align: center;">
              <div style="font-size: 0.68rem; color: #52525b;">Task Saya</div>
              <div id="metricMyTasks" style="font-size: 1.15rem; font-weight: 700; color: #f59e0b; margin-top: 0.15rem;">0</div>
            </div>
          </div>

          <!-- PROGRESS BAR -->
          <div style="margin-top: 0.35rem;">
            <div style="display: flex; justify-content: space-between; font-size: 0.72rem; color: #71717a; margin-bottom: 0.35rem;">
              <span>Progres Sprint</span>
              <span id="spectrumProgressText">Memuat...</span>
            </div>
            <div style="width: 100%; height: 5px; background: #27272a; border-radius: 3px; overflow: hidden; display: flex;">
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
          <div id="spectrumCalendarList" style="display: flex; flex-direction: column; gap: 0.45rem; margin-top: 0.5rem; max-height: 235px; overflow-y: auto;">
            <div style="color: var(--gf-text-muted); font-size: 0.8rem;">Memuat agenda kalender...</div>
          </div>
        </div>

        <!-- ROW 2: TASK LIST & QA ACTION (col-12) -->
        <div class="panel col-12">
          <div class="panel-header" style="flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span>Daftar Task Sprint 6 (SPEctrum)</span>
              <span id="spectrumTaskCountBadge" class="hermes-badge badge-blue" style="font-size: 0.65rem;">0 Task</span>
            </div>
            <!-- FILTER TABS -->
            <div style="display: flex; gap: 0.35rem; flex-wrap: wrap;">
              <button class="spectrum-tab active" id="tabTaskAris" onclick="filterSpectrumTasks('my')"><span id="labelTabMyTask">Task Saya</span> <span id="countTabAris">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskDevDone" onclick="filterSpectrumTasks('devdone')">Ready QA / Dev Done <span id="countTabDevDone">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskProgress" onclick="filterSpectrumTasks('progress')">On Progress <span id="countTabProgress">(0)</span></button>
              <button class="spectrum-tab" id="tabTaskAll" onclick="filterSpectrumTasks('all')">Semua Task <span id="countTabAll">(0)</span></button>
            </div>
          </div>

          <div style="overflow-x: auto; max-height: 320px; margin-top: 0.5rem;">
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
        <div class="panel col-8" style="min-height: 480px; display: flex; flex-direction: column;">
          <div class="panel-header" style="margin-bottom: 0.65rem;">
            <div style="display: flex; align-items: center; gap: 0.6rem;">
              <!-- Hermes avatar monogram -->
              <div style="width:26px; height:26px; background:#18181b; border:1px solid #2d2d35; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:0.72rem; font-weight:700; color:#a1a1aa; flex-shrink:0;">H</div>
              <span style="color:#d4d4d8; font-weight:600;">Hermes</span>
              <span style="width:6px; height:6px; background:#10b981; border-radius:50%; display:inline-block;" title="Online"></span>
            </div>
            <button onclick="clearAiChatHistory()" style="background: transparent; color: #52525b; border: none; font-size: 0.72rem; cursor: pointer; padding: 0.2rem 0.4rem; border-radius: 4px;" title="Bersihkan chat">Bersihkan</button>
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

          <!-- QUICK PROMPT CHIPS -->
          <div style="display: flex; gap: 0.35rem; flex-wrap: wrap; margin-top: 0.65rem; margin-bottom: 0.35rem;">
            <button class="quick-chip" onclick="prepareDailyUpdatePrompt()">Draf Daily Standup</button>
            <button class="quick-chip" onclick="askHermesAboutTask('66-001', '[QA] create tc mobile', 'test_case')">TC VA PNRI (66-001)</button>
            <button class="quick-chip" onclick="askHermesAboutTask('66-002', '[QA] testing mobile biling dan notifikasi (PNRI, AHU)', 'test_case')">TC Billing & Notif (66-002)</button>
            <button class="quick-chip" onclick="applyQuickPrompt('Buatkan template Bug Report SPEctrum standar: severity, steps to reproduce, actual vs expected result, screenshot placeholder.')">Template Bug</button>
            <button class="quick-chip" onclick="applyQuickPrompt('Ada OTP atau email verifikasi baru yang masuk?')">Cek OTP Masuk</button>
            <button class="quick-chip" onclick="prepareEndOfDayPrompt()" style="background: rgba(16,185,129,0.1); border-color: rgba(16,185,129,0.3); color: #34d399;">Tutup Hari (Rekap)</button>
          </div>

          <!-- CHAT INPUT -->
          <div class="chat-box" style="margin-top: 0.25rem;">
            <input type="text" id="aiInputPrompt" class="chat-input" placeholder="Tanya Hermes..." onkeypress="if(event.key==='Enter') sendAiPrompt();">
            <button class="chat-btn" id="btnSendAiPrompt" onclick="sendAiPrompt()">Kirim</button>
          </div>
        </div>

        <!-- ROW 3: KONFIGURASI & STATUS (col-4) -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Konfigurasi</span>
            <span class="hermes-badge badge-ok" id="hermesOnlineBadge">Layanan Aktif</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.5rem;">
            <div class="stat-sub" style="border: none; padding: 0; flex-direction: column; align-items: flex-start; gap: 0.35rem;">
              <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
                <span>Model AI (Sumopod API):</span>
                <span id="modelChangeFeedback" style="color:#10b981; font-size:0.7rem; display:none; font-weight:600;">Tersimpan!</span>
              </div>
              <select id="hermesModelSelect" class="chat-input" style="width: 100%; box-sizing: border-box; padding: 0.4rem 0.5rem; font-size: 0.78rem; background: #14161b; color: #38bdf8; border: 1px solid #334155; border-radius: 4px; cursor: pointer;" onchange="onModelSelectChanged(this.value)">
                <option value="deepseek-v4-flash-0731:netra">DeepSeek V4 Flash (Netra 90% Off) - $0.05/1M</option>
              </select>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Target Solar Polling:</span>
              <b style="color: #10b981;">http://localhost:3005/api/state</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Baterai Rendah Threshold:</span>
              <b style="color: #f87171;" id="hermesLowThresh">20% SOC</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Baterai Surplus Threshold:</span>
              <b style="color: #34d399;" id="hermesHighThresh">95% SOC</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Interval Polling:</span>
              <b id="hermesPollSec">60 detik</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Status Terkoneksi Solar:</span>
              <b id="hermesConnStatus" style="color: #34d399;">TERHUBUNG</b>
            </div>
          </div>
        </div>

        <!-- ROW 4: EVENT LOG TIMELINE (col-12) -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Log Aktivitas Hermes</span>
            <button onclick="fetchHermesStatus()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">Refresh</button>
          </div>
          <div class="event-feed" id="hermesEventsFeed">
            <div style="color: var(--gf-text-muted); font-size: 0.85rem;">Memuat feed aktivitas...</div>
          </div>
        </div>
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
      <div class="grafana-grid">
        <!-- STATUS SERVER APPIUM -->
        <div class="panel col-4">
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
        <div class="panel col-8">
          <div class="panel-header">
            <span>Perangkat Android Terdeteksi</span>
            <span style="font-size:0.7rem; color:var(--gf-text-muted);">Pilih perangkat untuk mulai kontrol layar</span>
          </div>
          <div id="appiumDevicesList" style="display:flex; flex-direction:column; gap:0.65rem;">
            <div style="color:var(--gf-text-muted); font-size:0.8rem; padding:1rem; text-align:center;">Memuat daftar perangkat...</div>
          </div>
        </div>

        <!-- KONEKSI ADB OVER WI-FI & TAILSCALE -->
        <div class="panel col-12" style="background:#131317; border:1px solid #222228; padding:1rem;">
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
                <option value="low" selected>Low (Cepat)</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="original">Original (HD)</option>
              </select>
              <label style="display:flex; align-items:center; gap:0.35rem; font-size:0.75rem; color:#94a3b8; cursor:pointer;">
                <input type="checkbox" id="chkAutoStream" onchange="toggleScreenAutoStream()" style="cursor:pointer;" />
                <span>Auto-Stream (1.5s)</span>
              </label>
              <button onclick="refreshDeviceScreen()" style="background:#202026; color:#f4f4f5; border:1px solid #353540; border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;">Refresh</button>
              <button onclick="toggleAndroidFullscreen()" id="btnFullscreen" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.3rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Layar Penuh (Fullscreen Interactive Remote)">Layar Penuh</button>
            </div>
          </div>

          <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:1.25rem; margin-top:1rem; align-items:start;">
            
            <!-- PHONE FRAME & INTERACTIVE CANVAS -->
            <div style="display:flex; flex-direction:column; align-items:center;">
              <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:0.5rem; text-align:center;">
                Klik atau seret pada gambar layar untuk mengontrol perangkat
              </div>
              <div id="phoneBezel" style="position:relative; width:280px; max-width:100%; background:#18181b; border:3px solid #3f3f46; border-radius:24px; padding:10px 8px 14px 8px; box-shadow:0 10px 25px -5px rgba(0,0,0,0.6); user-select:none;">
                <!-- Notch/Speaker Pill -->
                <div style="width:60px; height:5px; background:#27272a; border-radius:3px; margin:0 auto 8px auto;"></div>
                
                <!-- Screen Canvas / Image Container -->
                <div id="screenContainer" style="position:relative; width:100%; background:#000; border-radius:14px; overflow:hidden; aspect-ratio:9/19.5; display:flex; align-items:center; justify-content:center; cursor:crosshair;">
                  <img id="androidScreenImg" src="" alt="Layar Android" style="width:100%; height:100%; object-fit:contain; display:none;" draggable="false" />
                  <div id="screenLoadingPlaceholder" style="color:#64748b; font-size:0.75rem; text-align:center; padding:1rem;">
                    Pilih perangkat untuk menampilkan layar...
                  </div>
                  <!-- Tap Ripple Element -->
                  <div id="tapRipple" style="position:absolute; width:24px; height:24px; border-radius:50%; background:rgba(56,189,248,0.7); border:2px solid #fff; pointer-events:none; transform:translate(-50%, -50%) scale(0); opacity:0; transition:transform 0.25s ease-out, opacity 0.25s ease-out;"></div>
                </div>

                <!-- Bottom Hardware Navigation Bar -->
                <div style="display:flex; justify-content:space-around; align-items:center; margin-top:10px; padding:0 0.5rem;">
                  <button onclick="triggerActiveDeviceAction('back')" style="background:#202026; color:#f1f5f9; border:1px solid #353540; border-radius:4px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Kembali">Back</button>
                  <button onclick="triggerActiveDeviceAction('home')" style="background:#202026; color:#f59e0b; border:1px solid #353540; border-radius:4px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Home">Home</button>
                  <button onclick="triggerActiveDeviceAction('recent')" style="background:#202026; color:#38bdf8; border:1px solid #353540; border-radius:4px; padding:0.35rem 0.75rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Recent Apps">Recent</button>
                  <button onclick="triggerActiveDeviceAction('wake')" style="background:#202026; color:#ef4444; border:1px solid #353540; border-radius:4px; padding:0.35rem 0.65rem; font-size:0.75rem; font-weight:600; cursor:pointer;" title="Power">Power</button>
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
        <!-- MEETING CONTROLS -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Perekaman & Notulen Rapat</span>
            <span class="hermes-badge badge-blue" id="meetingStatusBadge">SIAP</span>
          </div>
          <div style="display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; margin-top: 0.5rem;">
            <!-- SELECTOR JADWAL KALENDER SPECTRUM -->
            <select id="meetingScheduleSelect" class="chat-input" style="max-width: 280px; color: #38bdf8; font-weight: 500;" onchange="onSelectScheduledMeeting(this.value)">
              <option value="">-- Pilih dari Jadwal SPEctrum --</option>
            </select>
            <input type="text" id="meetingTitleInput" class="chat-input" style="max-width: 220px;" placeholder="Judul Meeting" value="Meeting Koordinasi">
            <select id="meetingAudioSourceSelect" class="chat-input" style="max-width: 260px; color: #38bdf8; font-weight: 500;" title="Pilih sumber audio: mikrofon, suara meeting laptop, atau keduanya">
              <option value="both" selected>🎙️+💻 Mic + Audio Laptop (Meet/Zoom)</option>
              <option value="mic">🎤 Mikrofon Saja (Tatap Muka)</option>
              <option value="system">💻 Audio Laptop/Tab Saja</option>
            </select>
            <select id="meetingLangSelect" class="chat-input" style="max-width: 140px;">
              <option value="id-ID" selected>Bahasa Indonesia</option>
              <option value="en-US">English</option>
            </select>
            <select id="meetingModelSelect" class="chat-input" style="max-width: 200px; font-size: 0.78rem; color: #38bdf8;">
              <option value="">Default (Sesuai Hermes)</option>
            </select>
            <div style="font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; font-weight: 700; color: #38bdf8;" id="meetingTimerDisplay">
              00:00:00
            </div>
            <div style="display: flex; gap: 0.5rem; margin-left: auto;">
              <button id="btnStartMeeting" class="chat-btn" style="background: #2563eb;" onclick="startMeetingRecording()">Mulai Merekam</button>
              <button id="btnStopMeeting" class="chat-btn" style="background: #ef4444; display: none;" onclick="stopMeetingRecording()">Selesai & Rangkum</button>
              <button class="chat-btn" style="background: #202026; color: #f4f4f5; border: 1px solid #353540;" onclick="clearMeetingData()">Reset</button>
            </div>
          </div>

          <!-- SPRINT & FILE CONTEXT BAR -->
          <div id="sprintContextBar" style="margin-top: 0.75rem; padding: 0.55rem 0.85rem; background: #14161b; border: 1px solid #27272a; border-radius: 6px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
              <span style="font-size: 0.7rem; color: #71717a; text-transform: uppercase; font-weight: 600;">Sprint Berjalan:</span>
              <span id="meetingSprintBadge" class="hermes-badge badge-blue" style="font-weight: 600;">Sprint 6 - 2026 (BNI YAP!)</span>
              <span id="meetingSprintGoal" style="font-size: 0.75rem; color: #94a3b8;">User PNRI melihat & membayar nomor VA di YAP!</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
              <button onclick="openSprintTestCasesModal()" class="chat-btn" style="padding: 0.2rem 0.55rem; font-size: 0.72rem; background: #1e293b; color: #38bdf8; border: 1px solid #38bdf8; display: inline-flex; align-items: center; gap: 0.35rem; cursor: pointer;" title="Lihat 71 Test Case berjalan di sprint ini">
                👁️ Lihat 71 Test Case Berjalan
              </button>
              <span style="font-size: 0.7rem; color: #71717a;">File Test Case:</span>
              <a href="https://docs.google.com/spreadsheets/d/1ZG0rdSEQnOY4Ect7VRB7Zi1fm94xHeXYlbyn9X9UNXs/edit" target="_blank" style="font-size: 0.72rem; color: #34d399; text-decoration: none; background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); border-radius: 4px; padding: 0.15rem 0.45rem; font-weight: 600;" title="Buka Spreadsheet di Google Sheets">
                📊 Google Sheet (71 TC)
              </a>
              <span style="font-size: 0.7rem; color: #a1a1aa; background: #202026; border: 1px solid #2d2d35; border-radius: 4px; padding: 0.15rem 0.45rem;">
                📄 Skenario Testing - Sprint PNRI.xlsx
              </span>
            </div>
          </div>
          <div id="speechEngineInfo" style="margin-top: 0.75rem; padding: 0.5rem 0.75rem; background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; font-size: 0.75rem; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
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
          <textarea id="meetingTranscriptText" class="chat-input" style="width: 100%; height: 350px; resize: vertical; line-height: 1.6; font-size: 0.85rem;" placeholder="Ketika tombol 'Mulai Merekam' ditekan, suara pembicaraan dari mikrofon akan otomatis diubah menjadi teks di sini. Anda juga dapat mengedit teks ini secara manual atau menekan tombol 'Koreksi & Rapikan (AI)'."></textarea>
          
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
          <div id="meetingSummaryOutput" style="background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; padding: 1.25rem; height: 350px; overflow-y: auto; line-height: 1.6; font-size: 0.875rem;">
            <div style="color: var(--gf-text-muted); text-align: center; margin-top: 5rem;">
              Tekan <b>"Selesai & Rangkum"</b> setelah rapat untuk menghasilkan notulen rapi yang merangkum poin diskusi, keputusan, dan tindak lanjut.
            </div>
          </div>
        </div>

        <!-- PANEL: EKSTRAKSI & APPROVAL TEST CASE KE SPREADSHEET (col-12) -->
        <div class="panel col-12" id="meetingTcPanel" style="margin-top: 0.5rem;">
          <div class="panel-header" style="flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span>📋 Rekomendasi Test Case dari Rapat (Pilih & Setujui)</span>
              <span id="meetingTcCountBadge" class="hermes-badge badge-blue" style="font-size: 0.65rem;">0 Skenario</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 0.4rem; background: #131418; border: 1px solid #2d3340; border-radius: 6px; padding: 0.2rem 0.5rem; font-size: 0.75rem;" title="Nama Tab Spreadsheet (Otomatis dipisah per Project &amp; Sprint)">
                <span style="color: #94a3b8; font-size: 0.7rem;">Tab Sheet:</span>
                <input type="text" id="targetSheetTabName" style="background: transparent; border: none; color: #38bdf8; font-size: 0.75rem; font-weight: 600; width: 145px; outline: none;" value="BNI YAP! - Sprint 6" placeholder="Project - Sprint">
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
        </div>

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
              <span style="font-weight: 700; font-size: 0.95rem; color: #f4f4f5;">Daftar Skenario Test Case Berjalan - Sprint 6 PNRI</span>
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
                <input type="text" id="cfgSpecProjectId" class="form-input" value="64881aa413013" style="width: 100%; background: #0b0c0e; border: 1px solid #334155; border-radius: 6px; padding: 0.55rem 0.85rem; color: #fff; font-size: 0.825rem; font-family: 'JetBrains Mono', monospace;" />
                <div style="font-size: 0.7rem; color: #71717a; margin-top: 0.25rem;">ID proyek default: 64881aa413013 (BNI YAP!)</div>
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
    const CURRENT_USER_EMAIL = "__USER_EMAIL__";
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
      hermes: 'Hermes AI Agent Orchestrator - Westpro Home Server',
      meeting: 'Notulen Meeting AI - Westpro Home Server',
      android: 'Android Remote & Appium - Westpro Home Server',
      otp: 'Kotak Masuk OTP - Westpro Home Server',
      webhook: 'Webhook & API Monitor - Westpro Home Server',
      profile: 'Profil Pengguna & SPEctrum - Westpro Home Server',
      'spectrum-config': 'Profil Pengguna & SPEctrum - Westpro Home Server',
      system: 'Sistem 24/7 & Resource Server - Westpro Home Server'
    };

    function navigateToPage(pageId, pushState = true) {
      if (!IS_ARES_OWNER && (pageId === 'solar' || pageId === 'history' || pageId === 'overview')) {
        pageId = 'hermes';
      }
      if (pageId === 'overview') pageId = IS_ARES_OWNER ? 'solar' : 'hermes';
      if (pageId === 'spectrum-config') pageId = 'profile';
      if (!PAGE_TITLES[pageId]) pageId = IS_ARES_OWNER ? 'solar' : 'hermes';

      if (pageId !== 'webhook' && typeof stopWebhookAutoStream === 'function') {
        stopWebhookAutoStream();
      }

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

      if (pageId === 'history') {
        loadPLTSAnalysisDashboard();
      } else if (pageId === 'hermes') {
        fetchHermesStatus();
        loadSpectrumWorkspaceData();
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
      } else if (pageId === 'profile' || pageId === 'spectrum-config') {
        loadUserSpectrumConfig();
      } else if (pageId === 'system') {
        fetchDeployStatus();
        fetchSystemResources(true);
      } else if (pageId === 'meeting') {
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
      const page = (e.state && e.state.page) || window.location.pathname.replace(/^\\//, '') || 'solar';
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

        // Update Energy Flow diagram nodes
        const flowPvWatt = document.getElementById('flowPvWatt');
        if (flowPvWatt) flowPvWatt.textContent = data.pv_power_watt + ' W';
        const flowLoadWatt = document.getElementById('flowLoadWatt');
        if (flowLoadWatt) flowLoadWatt.textContent = data.ac_output_watt + ' W';
        const flowBattSocText = document.getElementById('flowBattSocText');
        if (flowBattSocText) flowBattSocText.textContent = data.soc + '%';
        const flowGridVolt = document.getElementById('flowGridVolt');
        if (flowGridVolt) flowGridVolt.textContent = data.grid_voltage.toFixed(1) + ' V';

        const ovLoadBarMini = document.getElementById('ovLoadBarMini');
        if (ovLoadBarMini) {
          ovLoadBarMini.style.width = Math.min(100, Math.max(0, data.load_percent)) + '%';
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
        }

        // 2. 3-Day Forecast Strip
        if (Array.isArray(data.forecast)) {
          const gridEl = document.getElementById('heroForecastGrid');
          if (gridEl) {
            gridEl.innerHTML = data.forecast.map(f => `
              <div class="forecast-pill" style="padding: 0.4rem 0.35rem; text-align: center;">
                <div style="font-size: 0.68rem; font-weight: 700; color: #f4f4f5;">${escapeHtml(f.label)}</div>
                <div style="font-size: 0.62rem; color: #38bdf8; font-weight: 600; margin: 1px 0;">${escapeHtml(f.date_short || f.day_name || '')}</div>
                <div style="font-size: 1.15rem; margin: 2px 0;">${f.icon || '⛅'}</div>
                <div style="font-size: 0.72rem; font-weight: 700; color: #f4f4f5;">${f.temp_max}° / ${f.temp_min}°</div>
                <div style="font-size: 0.58rem; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(f.condition || '')}">${escapeHtml(f.condition || '')}</div>
              </div>
            `).join('');
          }
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
                borderColor: '#10b981',
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
      const myName = (window._myMemberName || '').toLowerCase().trim();
      const myEmail = (window._myEmail || '').toLowerCase().trim();
      const tAssignee = (t.assignee || '').toLowerCase();
      const tEmail = (t.assignee_email || '').toLowerCase();
      const tCreator = (t.created_by || '').toLowerCase();

      if (myName && (tAssignee.includes(myName) || tCreator.includes(myName))) return true;
      if (myEmail && (tEmail === myEmail || (tEmail && myEmail.includes(tEmail)))) return true;
      if (!myName && !myEmail) {
        return tAssignee.includes('aris') || tEmail.includes('aris');
      }
      return false;
    }

    async function loadSpectrumWorkspaceData() {
      try {
        const res = await fetch('/api/spectrum/data');
        if (!res.ok) return;
        const data = await res.json();
        window._spectrumData = data;

        const banner = document.getElementById('spectrumNeedsConfigBanner');
        if (data.needs_config) {
          if (banner) banner.style.display = 'flex';
          const titleEl = document.getElementById('spectrumSprintTitle');
          if (titleEl) titleEl.textContent = 'SPEctrum Belum Konek';
          const goalEl = document.getElementById('spectrumSprintGoal');
          if (goalEl) goalEl.textContent = data.message || 'Silakan hubungkan akun SPEctrum Anda di menu Config SPEctrum.';
          if (document.getElementById('metricTotalPoints')) document.getElementById('metricTotalPoints').textContent = '0';
          if (document.getElementById('metricTotalTasks')) document.getElementById('metricTotalTasks').textContent = '0';
          if (document.getElementById('metricDevDone')) document.getElementById('metricDevDone').textContent = '0';
          if (document.getElementById('metricMyTasks')) document.getElementById('metricMyTasks').textContent = '0';
          if (document.getElementById('countTabAris')) document.getElementById('countTabAris').textContent = '(0)';
          if (document.getElementById('countTabDevDone')) document.getElementById('countTabDevDone').textContent = '(0)';
          if (document.getElementById('countTabProgress')) document.getElementById('countTabProgress').textContent = '(0)';
          if (document.getElementById('countTabAll')) document.getElementById('countTabAll').textContent = '(0)';
          if (document.getElementById('spectrumTaskCountBadge')) document.getElementById('spectrumTaskCountBadge').textContent = '0 Task';
          renderSpectrumCalendar([]);
          renderSpectrumTasks();
          return;
        } else {
          if (banner) banner.style.display = 'none';
        }

        const userObj = data.user || {};
        window._myMemberName = (userObj.name || '').trim();
        window._myEmail = (userObj.email || '').trim().toLowerCase();

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
        if (document.getElementById('spectrumSprintTitle')) {
          document.getElementById('spectrumSprintTitle').textContent = sprint.name || 'Sprint 6 - 2026';
        }
        if (document.getElementById('spectrumSprintPeriod') && sprint.start_date && sprint.end_date) {
          document.getElementById('spectrumSprintPeriod').textContent = sprint.start_date + ' s/d ' + sprint.end_date;
        }
        if (document.getElementById('spectrumSprintGoal')) {
          document.getElementById('spectrumSprintGoal').textContent = sprint.description || sprint.target || 'User PNRI dapat melihat daftar tagihan PNRI, memilih, dan menyelesaikan pembayaran langsung dari aplikasi YAP!';
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

        // 3. Calendar Events
        renderSpectrumCalendar(data.calendar_events || []);

        // 4. Tasks Table
        renderSpectrumTasks();

        // 5. Daily Briefing — tampilkan sekali saat pertama kali buka
        showDailyBriefing(data, myTasks, devDoneCount);

        // 6. Check OTP inbox untuk suggestion strip
        checkOtpForSuggestion();

      } catch (err) {
        console.error('Gagal memuat data workspace SPEctrum:', err);
      }
    }

    function showDailyBriefing(data, myTasks, devDoneCount) {
      const card = document.getElementById('dailyBriefingCard');
      if (!card || sessionStorage.getItem('briefingDismissed')) return;

      const now = new Date();
      const hour = now.getHours();
      const greetWord = hour < 11 ? 'Selamat pagi' : hour < 15 ? 'Selamat siang' : 'Selamat sore';
      const dayName = now.toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'long' });

      const sprint = data.active_sprint || {};
      const events = data.calendar_events || [];
      const today = now.toISOString().split('T')[0];
      const todayEvents = events.filter(e => (e.execute_at || e.start || '').startsWith(today));
      const myDisplayName = window._myMemberName || 'Rekan';

      let bodyText = '';
      if (myTasks && myTasks.length > 0) {
        bodyText += myTasks.map(t => `<b>${t.code}</b> ${t.name}`).join(' &amp; ') + '. ';
      }
      if (todayEvents.length > 0) {
        const nextEvent = todayEvents[0];
        const sTime = nextEvent.start ? nextEvent.start.substring(11, 16) : '';
        bodyText += `Daily standup ${sTime ? 'jam ' + sTime : 'hari ini'} untuk <b>${nextEvent.name.replace('[Daily] ', '')}</b>. `;
      }
      if (devDoneCount > 0) {
        bodyText += `Ada <b>${devDoneCount} task</b> yang sudah dev done, siap untuk kamu QA.`;
      }
      if (!bodyText) bodyText = `Sprint ${sprint.name || ''} masih berjalan. Semangat ${myDisplayName}!`;

      const greetEl = document.getElementById('briefingGreeting');
      const bodyEl = document.getElementById('briefingBody');
      if (greetEl) greetEl.textContent = greetWord + ', ' + myDisplayName + ' — ' + dayName;
      if (bodyEl) bodyEl.innerHTML = bodyText;

      card.style.display = 'flex';
      const closeBtn = card.querySelector('button');
      if (closeBtn) closeBtn.onclick = () => {
        card.style.display = 'none';
        sessionStorage.setItem('briefingDismissed', '1');
      };
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

      container.innerHTML = events.map(ev => {
        let timeLabel = '';
        if (ev.start && ev.end) {
          const sTime = ev.start.substring(11, 16);
          const eTime = ev.end.substring(11, 16);
          const dStr = ev.start.substring(0, 10);
          timeLabel = dStr + ' &bull; ' + sTime + ' - ' + eTime + ' WIB';
        } else {
          timeLabel = ev.execute_at || 'Hari ini';
        }

        const isYAP = ev.name && ev.name.includes('YAP');
        const badgeBorder = isYAP ? 'rgba(56, 189, 248, 0.35)' : '#27272a';

        return `
          <div style="background: #18181b; border: 1px solid ${badgeBorder}; border-radius: 6px; padding: 0.5rem 0.65rem; display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;">
            <div style="overflow: hidden;">
              <div style="font-size: 0.78rem; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                ${escapeHtml(ev.name)}
              </div>
              <div style="font-size: 0.68rem; color: #a1a1aa; margin-top: 0.15rem;">
                ${timeLabel}
              </div>
            </div>
            <button onclick="prepareMeetingAgendaPrompt('${escapeHtml(ev.name)}', '${timeLabel}')" style="background: #27272a; color: #38bdf8; border: 1px solid #3f3f46; border-radius: 4px; padding: 0.2rem 0.5rem; font-size: 0.68rem; cursor: pointer; white-space: nowrap; flex-shrink: 0;" title="Siapkan bahan laporan untuk meeting ini">
              Bahan
            </button>
          </div>
        `;
      }).join('');
    }

    function filterSpectrumTasks(filterType) {
      window._spectrumFilter = filterType;
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
        if (data && data.needs_config) {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #f59e0b; padding: 1.5rem 0;">⚠️ Akun SPEctrum Anda belum dihubungkan. Silakan atur email dan password di <a href="javascript:void(0)" onclick="switchTab(\\'spectrum-config\\')" style="color:#38bdf8; text-decoration:underline; font-weight:600;">Config SPEctrum</a>.</td></tr>';
        } else {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">Belum ada data task sprint. Silakan klik "Perbarui Data".</td></tr>';
        }
        return;
      }

      let filtered = data.tasks;
      if (window._spectrumFilter === 'my' || window._spectrumFilter === 'aris') {
        filtered = data.tasks.filter(t => isMyTask(t));
      } else if (window._spectrumFilter === 'devdone') {
        filtered = data.tasks.filter(t => t.status === 'Done-Development' || t.status === 'Task-Finished');
      } else if (window._spectrumFilter === 'progress') {
        filtered = data.tasks.filter(t => t.status === 'On-Progress');
      }

      if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--gf-text-muted); padding: 1.5rem 0;">Tidak ada task pada filter ini.</td></tr>';
        return;
      }

      tbody.innerHTML = filtered.map(t => {
        let badgeStyle = 'background: #27272a; color: #a1a1aa;';
        if (t.status === 'Done-Development') badgeStyle = 'background: rgba(52, 211, 153, 0.15); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3);';
        else if (t.status === 'Task-Finished') badgeStyle = 'background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4);';
        else if (t.status === 'On-Progress') badgeStyle = 'background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);';

        const isMine = isMyTask(t);
        const assigneeBadge = isMine 
          ? `<span style="color:#f59e0b; font-weight:600;">${escapeHtml(t.assignee)}</span> <span style="font-size:0.65rem; background:rgba(245,158,11,0.15); color:#f59e0b; padding:0.05rem 0.3rem; border-radius:3px;">QA Anda</span>`
          : `<span style="color:#cbd5e1;">${escapeHtml(t.assignee || 'Unassigned')}</span>`;

        return `
          <tr>
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
          body: JSON.stringify({ test_cases: sampleTc, author: 'Aris' })
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

    function askHermesAboutTask(code, name, actionType) {
      let prompt = '';
      if (actionType === 'test_case') {
        prompt = `Sebagai QA Engineer untuk proyek BNI YAP! Sprint 6 - 2026, buatkan test case detail dan komprehensif untuk task [${code}] "${name}".\n\nSertakan:\n1. Positive Scenarios (Happy path)\n2. Negative & Validation Scenarios\n3. Edge Cases / Boundary Tests (misal VA kadaluarsa, nominal 0, timeout Biller)\n4. Kriteria Keberhasilan (Acceptance Criteria)\n5. Contoh Data Uji (Test Data Dummy)`;
      } else {
        prompt = `Untuk task [${code}] "${name}" di proyek BNI YAP! Sprint 6, berikan analisis QA:\n1. Titik kritis pengujian & potensi bug\n2. Dependensi sistem/API backend yang perlu dipastikan\n3. Checklist verifikasi sebelum sign-off QA`;
      }
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareDailyUpdatePrompt() {
      const prompt = `Buatkan draf update Daily Standup untuk saya (Aris - QA Tribe Ursa Major) untuk Sprint 6 - 2026 proyek BNI YAP!.\n\nKondisi saat ini:\n- Task QA aktif: [66-001] create tc mobile & [66-002] testing mobile biling dan notifikasi (PNRI, AHU)\n- Total Sprint: 49 story points, 30 tasks, sasaran integrasi pembayaran nomor VA PNRI pada YAP!\n\nFormatkan dengan format standar Agile Standup:\n1. Yesterday / Yang sudah dikerjakan: (analisis dokumen FSD/API VA PNRI & penyusunan skenario test case mobile)\n2. Today / Yang akan dikerjakan: (eksekusi pengujian billing & notifikasi PNRI/AHU di aplikasi mobile YAP)\n3. Blockers / Kendala: (menunggu deployment APK build terbaru & ketersediaan service dummy PNRI)`;
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareMeetingAgendaPrompt(meetingName, timeStr) {
      const prompt = `Tolong siapkan poin-poin penting dan bahan laporan QA yang perlu saya sampaikan untuk agenda rapat "${meetingName}" (${timeStr}) terkait status pengujian Sprint 6 proyek BNI YAP!.`;
      const input = document.getElementById('aiInputPrompt');
      if (input) {
        input.value = prompt;
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      sendAiPrompt();
    }

    function prepareEndOfDayPrompt() {
      const prompt = `Tolong buatkan Rekap Harian (End-of-day Summary) untuk saya (Aris - QA Tribe Ursa Major) hari ini terkait Sprint 6 - 2026 proyek BNI YAP!.\n\nRangkum:\n1. Aktivitas QA & pengujian yang telah selesai dilakukan hari ini (termasuk verifikasi flow billing/VA PNRI).\n2. Isu atau bug yang ditemukan hari ini (jika ada) dan statusnya.\n3. Rencana fokus pengujian untuk besok pagi.\nFormat rapi dan ringkas agar mudah dibaca tim.`;
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
          if (emailInputSpec) emailInputSpec.value = cfg.spectrum_email || '';

          const passInput = document.getElementById('cfgSpecPassword');
          const passNote = document.getElementById('cfgPassStatusNote');
          if (cfg.has_password) {
            if (passInput) passInput.placeholder = 'Password tersimpan (' + cfg.spectrum_password_masked + ') - Kosongkan jika tidak diubah';
            if (passNote) passNote.innerHTML = '<span style="color:#10b981;">✓ Password tersimpan</span>';
          } else {
            if (passInput) passInput.placeholder = 'Masukkan password SPEctrum Anda';
            if (passNote) passNote.innerHTML = '<span style="color:#f59e0b;">Belum diatur</span>';
          }

          const projInput = document.getElementById('cfgSpecProjectId');
          if (projInput) projInput.value = cfg.project_id || '64881aa413013';

          const memberInput = document.getElementById('cfgSpecMemberName');
          if (memberInput) memberInput.value = cfg.member_name || '';

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
      const project_id = document.getElementById('cfgSpecProjectId').value.trim() || '64881aa413013';
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
      fetchTailscaleStatus();
      try {
        const res = await fetch('/api/appium/devices');
        if (!res.ok) return;
        const data = await res.json();
        window._androidDevices = data.devices || [];
        
        const badge = document.getElementById('appiumBadge');
        if (data.appium && data.appium.ready) {
          badge.className = 'hermes-badge badge-ok';
          badge.textContent = `ONLINE (v${data.appium.version || '3.8.0'})`;
        } else {
          badge.className = 'hermes-badge badge-warn';
          badge.textContent = 'OFFLINE';
        }

        const devList = document.getElementById('appiumDevicesList');
        if (!data.devices || !data.devices.length) {
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
      const srcUrl = '/api/appium/screenshot?serial=' + encodeURIComponent(window._activeDeviceSerial) + '&quality=' + quality + '&t=' + Date.now();

      const newImg = new Image();
      newImg.onload = function() {
        if (img) {
          img.src = newImg.src;
          img.style.display = 'block';
        }
        if (fsImg) {
          fsImg.src = newImg.src;
          fsImg.style.display = 'block';
        }
        if (placeholder) placeholder.style.display = 'none';
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
        clearInterval(window._screenStreamTimer);
        window._screenStreamTimer = null;
      }
      if (chk && chk.checked) {
        refreshDeviceScreen();
        window._screenStreamTimer = setInterval(refreshDeviceScreen, 1500);
      }
    }

    function initScreenInteractiveListeners() {
      const container = document.getElementById('screenContainer');
      if (!container || container._hasTouchListeners) return;
      container._hasTouchListeners = true;

      // Mouse drag / click
      container.addEventListener('mousedown', function(e) {
        window._swipeStartPos = getScreenRelCoords(e, container);
      });

      container.addEventListener('mouseup', function(e) {
        if (!window._swipeStartPos) return;
        const endPos = getScreenRelCoords(e, container);
        const startPos = window._swipeStartPos;
        window._swipeStartPos = null;

        const dx = endPos.x - startPos.x;
        const dy = endPos.y - startPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 25) {
          // Swipe detected
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          // Tap detected
          showTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      });

      // Mobile Touch events
      container.addEventListener('touchstart', function(e) {
        if (e.touches.length > 0) {
          window._swipeStartPos = getScreenRelCoords(e.touches[0], container);
        }
      }, { passive: true });

      container.addEventListener('touchend', function(e) {
        if (!window._swipeStartPos || e.changedTouches.length === 0) return;
        const endPos = getScreenRelCoords(e.changedTouches[0], container);
        const startPos = window._swipeStartPos;
        window._swipeStartPos = null;

        const dx = endPos.x - startPos.x;
        const dy = endPos.y - startPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 25) {
          executeDeviceSwipe(startPos.devX, startPos.devY, endPos.devX, endPos.devY);
        } else {
          showTapRipple(startPos.elemX, startPos.elemY);
          executeDeviceTap(startPos.devX, startPos.devY);
        }
      }, { passive: true });
    }

    function getScreenRelCoords(e, container) {
      const rect = container.getBoundingClientRect();
      const elemX = e.clientX - rect.left;
      const elemY = e.clientY - rect.top;

      const normX = Math.max(0, Math.min(1, elemX / rect.width));
      const normY = Math.max(0, Math.min(1, elemY / rect.height));

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
        setTimeout(refreshDeviceScreen, 350);
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
          body: JSON.stringify({ serial: window._activeDeviceSerial, action: 'swipe', x1: x1, y1: y1, x2: x2, y2: y2, duration: 280 })
        });
        setTimeout(refreshDeviceScreen, 400);
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

    function initSpeechRecognition() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        return null;
      }
      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = document.getElementById('meetingLangSelect').value || 'id-ID';

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
          }, 300);
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

      if (!shouldSummarize) return;

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
          const lang = document.getElementById('meetingLangSelect').value || 'id-ID';
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

      const outBox = document.getElementById('meetingSummaryOutput');
      outBox.innerHTML = `
        <div style="text-align: center; margin-top: 5rem; color: #94a3b8;">
          <div style="font-weight: 600;">Menganalisis transkrip & menyusun notulen...</div>
          <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.25rem;">Memproses ringkasan ringkas, poin keputusan & rencana tindak lanjut...</div>
        </div>
      `;

      try {
        const selectedModel = document.getElementById('meetingModelSelect')?.value || undefined;
        const res = await fetch('/api/meetings/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, transcript, duration: elapsed, model: selectedModel })
        });
        const data = await res.json();
        currentMeetingSummaryMarkdown = data.summary;
        
        if (window.marked) {
          outBox.innerHTML = marked.parse(data.summary);
        } else {
          outBox.textContent = data.summary;
        }
        loadPastMeetings();
      } catch (err) {
        outBox.innerHTML = `<div style="color: #f87171;">Gagal merangkum meeting: ${err.message}</div>`;
      }
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
      }

      // 1. Initial page navigation
      try {
        let initial = '__INITIAL_PAGE__';
        if (!initial || initial.startsWith('__')) {
          const path = window.location.pathname.replace(/^\\//, '');
          initial = path || (IS_ARES_OWNER ? 'solar' : 'hermes');
        }
        if (initial === 'spectrum-config') initial = 'profile';
        if (!IS_ARES_OWNER && (initial === 'solar' || initial === 'history')) {
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
      try { fetchAppiumDevices(); } catch (e) {}
      try { loadMailConfig(); } catch (e) {}
      try { loadPastMeetings(); } catch (e) {}
      try { loadMeetingSchedulesAndSprint(); } catch (e) {}
      try { checkSpeechEngine(); } catch (e) {}
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

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initDashboard);
    } else {
      initDashboard();
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

    def get_current_user(self) -> dict | None:
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
        if clean_path in ("/", "/index.html", "/solar", "/history", "/hermes", "/meeting", "/android", "/otp", "/webhook", "/spectrum-config", "/profile", "/system"):
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
                if page_param in ("solar", "history", "hermes", "meeting", "android", "otp", "webhook", "spectrum-config", "profile", "system"):
                    initial_page = "profile" if page_param in ("spectrum-config", "profile") else page_param
                else:
                    initial_page = "solar" if is_ares else "hermes"
            elif clean_path in ("/spectrum-config", "/profile"):
                initial_page = "profile"
            else:
                initial_page = clean_path.lstrip("/")

            # Protection: solar and history are strictly for areskretindo@gmail.com
            if initial_page in ("solar", "history") and not is_ares:
                initial_page = "hermes"

            user_name = user.get("name") or user.get("email", "Ares").split("@")[0].capitalize()
            html = HTML_DASHBOARD_TEMPLATE.replace("__INITIAL_PAGE__", initial_page)
            html = html.replace("__USER_EMAIL__", user.get("email", "areskretindo@gmail.com"))
            html = html.replace("__USER_NAME__", user_name)
            html = html.replace("__USER_ROLE__", user.get("role", "owner").upper())

            # If initial page is not solar, activate the correct tab and section in the static HTML
            if initial_page != "solar":
                html = html.replace('class="tab-btn active" id="nav-solar"', 'class="tab-btn" id="nav-solar"')
                html = html.replace('id="page-solar" class="view-section active"', 'id="page-solar" class="view-section"')
                html = html.replace(f'class="tab-btn" id="nav-{initial_page}"', f'class="tab-btn active" id="nav-{initial_page}"')
                html = html.replace(f'id="page-{initial_page}" class="view-section"', f'id="page-{initial_page}" class="view-section active"')

            # If not Ares, hide solar live and history tabs from the DOM
            if not is_ares:
                html = html.replace('id="nav-solar"', 'id="nav-solar" style="display:none;"')
                html = html.replace('id="nav-history"', 'id="nav-history" style="display:none;"')
            _send(html, "text/html; charset=utf-8", extra_headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
            return

        # 4. Auth Session Check API
        if clean_path == "/api/auth/me":
            payload = json.dumps({"authenticated": True, "user": user}) if user else '{"authenticated": false}'
            _send(payload)
            return

        # 5. Telemetry Live API (Khusus areskretindo@gmail.com dan internal local services)
        elif self.path in ("/api/state", "/data"):
            is_local_client = self.client_address[0] in ("127.0.0.1", "::1", "localhost")
            if not is_local_client and (not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com"):
                _send(json.dumps({"error": "Akses dibatasi. Solar Live khusus untuk areskretindo@gmail.com"}), status=403)
                return
            data = self.inverter.last_state if self.inverter else {}
            _send(json.dumps(data, indent=2))

        # 5b. Weather & Solar Advisory API (Khusus areskretindo@gmail.com)
        elif self.path.startswith("/api/weather/solar"):
            if not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com":
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
            if not user or user.get("email", "").strip().lower() != "areskretindo@gmail.com":
                _send(json.dumps({"error": "Akses dibatasi. Analisis energi khusus untuk areskretindo@gmail.com"}), status=403)
                return
            try:
                analytics = self.history_db.get_energy_analytics() if self.history_db else {}
                _send(json.dumps(analytics))
            except Exception as e:
                _send(json.dumps({"error": str(e)}), status=500)

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
            global_fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spectrum_sprint_data.json")

            cfg = self.history_db.get_user_spectrum_config(user_id)
            spec_email = (cfg.get("spectrum_email") or "").strip()
            spec_pass = (cfg.get("spectrum_password") or "").strip()
            member_name = (cfg.get("member_name") or "").strip() or user.get("name") or ""
            proj_id = (cfg.get("project_id") or "64881aa413013").strip()

            # 1. Jika belum mengisi email atau password SPEctrum
            if not spec_email or not spec_pass:
                # Fallback jika file sprint eksisting ada
                if os.path.exists(global_fallback_path):
                    try:
                        with open(global_fallback_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            data["user"] = {
                                "name": member_name or user.get("name", "User"),
                                "email": spec_email or user.get("email", ""),
                                "role": user.get("role", "user")
                            }
                            data["needs_config"] = True
                            data["message"] = "Akun SPEctrum Anda belum dihubungkan. Silakan atur email dan kata sandi SPEctrum Anda di menu 'Profil & SPEctrum' untuk mengisolasi task Anda."
                            _send(json.dumps(data))
                            return
                    except Exception:
                        pass

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

            # Fallback jika cache spesifik belum tersimpan tapi global ada
            if os.path.exists(global_fallback_path):
                try:
                    with open(global_fallback_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data["user"] = {
                            "name": member_name or user.get("name", "User"),
                            "email": spec_email or user.get("email", ""),
                            "role": user.get("role", "user")
                        }
                        data["needs_config"] = False
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
                req = urllib.request.Request("http://localhost:8000/api/devices")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"appium": {"ready": False, "error": str(e)}, "devices": []}))

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
                # Proxy screenshot ke hermes-agent /api/devices/screenshot
                clean = self.path.replace("/api/appium/screenshot", "/api/devices/screenshot")
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

        elif clean_path == "/api/meetings":
            user_id = user["id"] if user else 1
            meetings = self.history_db.get_meetings(user_id=user_id) if self.history_db else []
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
                spectrum_project_id = data.get("spectrum_project_id", "64881aa413013").strip() or "64881aa413013"

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
                project_id = data.get("project_id", "64881aa413013").strip() or "64881aa413013"

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

                if id_token:
                    self.history_db.update_spectrum_sync_status(user["id"], "Koneksi Berhasil")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "message": f"Koneksi berhasil! Akun {spec_email} terverifikasi ke lifeinspectrum.com"}).encode("utf-8"))
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
                proj_id = (cfg.get("project_id") or "64881aa413013").strip()
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

        # Hermes AI Chat Proxy
        if self.path == "/api/hermes/chat":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
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
                user_id = user["id"] if user else None
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
                user_id = user["id"] if user else 1
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
                user_id = user["id"] if user else None
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

        elif clean_path == "/api/monitor/requests":
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
                user_id = user["id"] if user else None
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
