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
import urllib.error
import urllib.request

try:
    import serial
except ImportError:
    print("[ERROR] Modul 'pyserial' belum terpasang. Jalankan: pip install pyserial", file=sys.stderr)
    sys.exit(1)

from history_db import HistoryDatabase
from login_template import HTML_LOGIN_TEMPLATE

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
      --gf-bg: #0b0c0e;
      --gf-canvas: #111217;
      --gf-panel: #181b1f;
      --gf-panel-hover: #1f2329;
      --gf-border: #22252b;
      --gf-border-highlight: #343842;
      --gf-orange: #f59e0b;
      --gf-blue: #38bdf8;
      --gf-green: #10b981;
      --gf-purple: #a855f7;
      --gf-red: #ef4444;
      --gf-text: #f3f4f6;
      --gf-text-muted: #8b949e;
      --gf-text-dim: #586069;
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
      background: linear-gradient(135deg, #f59e0b, #ea580c);
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 15px rgba(245, 158, 11, 0.4);
    }
    .brand-title {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.3px;
      color: #fff;
    }
    .brand-sub {
      font-size: 0.75rem;
      color: var(--gf-text-muted);
      font-family: 'JetBrains Mono', monospace;
    }

    /* NAV TABS */
    .nav-tabs {
      display: flex;
      gap: 0.5rem;
      background: var(--gf-bg);
      padding: 0.25rem;
      border-radius: 8px;
      border: 1px solid var(--gf-border);
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--gf-text-muted);
      padding: 0.45rem 0.9rem;
      border-radius: 6px;
      font-size: 0.825rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      text-decoration: none;
      transition: all 0.2s;
    }
    .tab-btn:hover { color: #fff; background: rgba(255, 255, 255, 0.05); }
    .tab-btn.active {
      color: #fff;
      background: #272a31;
      border: 1px solid var(--gf-border-highlight);
      box-shadow: 0 2px 5px rgba(0,0,0,0.3);
    }

    .nav-right {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .status-pill {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
      padding: 0.3rem 0.7rem;
      border-radius: 4px;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34d399;
      font-weight: 600;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #10b981;
      box-shadow: 0 0 8px #10b981;
      animation: blink 1.8s infinite;
    }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

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
      border-radius: 6px;
      padding: 1rem;
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }
    .panel:hover { border-color: var(--gf-border-highlight); }
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
    .col-6 { grid-column: span 6; }
    .col-8 { grid-column: span 8; }
    .col-12 { grid-column: span 12; }

    @media (max-width: 1024px) {
      .col-3 { grid-column: span 6; }
      .col-4 { grid-column: span 6; }
      .col-6 { grid-column: span 12; }
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

    /* CHAT BOX */
    .chat-box {
      display: flex;
      gap: 0.5rem;
      margin-top: 1rem;
    }
    .chat-input {
      flex: 1;
      background: #0f1013;
      border: 1px solid var(--gf-border);
      color: #fff;
      padding: 0.65rem 1rem;
      border-radius: 6px;
      font-family: inherit;
      font-size: 0.875rem;
    }
    .chat-input:focus { outline: none; border-color: #38bdf8; }
    .chat-btn {
      background: #0284c7;
      color: #fff;
      border: none;
      padding: 0.65rem 1.25rem;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.85rem;
    }
    .chat-btn:hover { background: #0369a1; }

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

    <!-- SEPARATE PAGES NAVIGATION -->
    <div class="nav-tabs">
      <a class="tab-btn active" id="nav-solar" href="/solar" onclick="event.preventDefault(); navigateToPage('solar')">⚡ Solar Live</a>
      <a class="tab-btn" id="nav-history" href="/history" onclick="event.preventDefault(); navigateToPage('history')">📈 Riwayat & Grafik</a>
      <a class="tab-btn" id="nav-hermes" href="/hermes" onclick="event.preventDefault(); navigateToPage('hermes')">🤖 Hermes AI</a>
      <a class="tab-btn" id="nav-meeting" href="/meeting" onclick="event.preventDefault(); navigateToPage('meeting')">🎙️ Meeting AI</a>
      <a class="tab-btn" id="nav-system" href="/system" onclick="event.preventDefault(); navigateToPage('system')">🖥️ Sistem 24/7</a>
    </div>

    <div class="nav-right" style="display: flex; align-items: center; gap: 0.65rem;">
      <div class="status-pill">
        <div class="status-dot"></div>
        <span id="navModeText">ONLINE</span>
      </div>
      <button onclick="openPermissionModal()" id="navPermissionBtn" style="background: #141720; border: 1px solid var(--gf-border); border-radius: 20px; padding: 0.25rem 0.65rem; font-size: 0.75rem; color: #38bdf8; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;" title="Pengaturan Izin Mikrofon & Notifikasi">
        <span>🛡️</span>
        <span id="navPermissionSummary">Izin Akses</span>
      </button>
      <div id="userProfilePill" style="display: flex; align-items: center; gap: 0.4rem; background: #141720; border: 1px solid var(--gf-border); border-radius: 20px; padding: 0.25rem 0.6rem; font-size: 0.75rem;">
        <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #10b981;"></span>
        <span id="navUserEmail" style="color: #cbd5e1; font-weight: 500;">__USER_EMAIL__</span>
        <span id="navUserRole" class="hermes-badge badge-warn" style="font-size: 0.65rem; padding: 0.1rem 0.35rem;">__USER_ROLE__</span>
      </div>
      <button onclick="handleLogout()" style="background: rgba(239, 68, 68, 0.12); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 6px; padding: 0.35rem 0.65rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; transition: all 0.2s;" title="Keluar dari akun">
        Keluar
      </button>
    </div>
  </nav>

  <!-- ==================== STICKY PERMISSION WARNING BANNER ==================== -->
  <div id="stickyPermissionBanner" style="display: none; background: linear-gradient(90deg, #1e293b, #0f172a); border-bottom: 1px solid rgba(56, 189, 248, 0.35); padding: 0.6rem 1.5rem; align-items: center; justify-content: space-between; gap: 1rem; position: sticky; top: 56px; z-index: 1000; box-shadow: 0 4px 15px rgba(0, 0, 0, 0.4);">
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <div style="font-size: 1.25rem; animation: pulse 2s infinite;">🛡️</div>
      <div>
        <div style="font-weight: 700; font-size: 0.85rem; color: #f8fafc;">Izin Perangkat Diperlukan (Mikrofon & Notifikasi)</div>
        <div style="font-size: 0.75rem; color: #94a3b8;">Diperlukan untuk merekam notulen suara meeting & notifikasi peringatan daya surya.</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0;">
      <button onclick="requestAllPermissions()" style="background: linear-gradient(135deg, #10b981, #059669); color: white; border: none; border-radius: 6px; padding: 0.45rem 1rem; font-size: 0.8rem; font-weight: 700; cursor: pointer; box-shadow: 0 2px 10px rgba(16, 185, 129, 0.35);">
        ✨ Izinkan Sekarang (1-Klik)
      </button>
      <button onclick="openPermissionModal()" style="background: rgba(255, 255, 255, 0.08); color: #cbd5e1; border: 1px solid var(--gf-border); border-radius: 6px; padding: 0.45rem 0.75rem; font-size: 0.775rem; font-weight: 600; cursor: pointer;">
        Detail
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
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
          <span style="color: #f59e0b;">☀️</span> Telemetri Solar PV & Inverter Live
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Pemantauan daya real-time Inverter PowMr 6.2kW, baterai LiFePO4, dan status jaringan PLN.
        </div>
      </div>
      <div class="grafana-grid">
        <!-- SOLAR PV -->
        <div class="panel col-3">
          <div class="panel-header">
            <span>Solar PV Input</span>
            <span style="color: var(--gf-orange);">☀️ PV</span>
          </div>
          <div class="stat-val" id="ovPvWatt" style="color: var(--gf-orange);">-- W</div>
          <div class="stat-sub">
            <span>Volt: <b id="ovPvVolt" style="color: #fff;">-- V</b></span>
            <span>Arus: <b id="ovPvCurr" style="color: #fff;">-- A</b></span>
          </div>
        </div>

        <!-- BATTERY -->
        <div class="panel col-3">
          <div class="panel-header">
            <span>Baterai LiFePO4 (48V)</span>
            <span id="ovSocBadge" class="hermes-badge badge-ok">SOC --%</span>
          </div>
          <div class="stat-val" id="ovBattVolt" style="color: var(--gf-green);">-- V</div>
          <div class="stat-sub">
            <span>Charge: <b id="ovBattChg" style="color: #fff;">-- A</b></span>
            <span>Discharge: <b id="ovBattDis" style="color: #fff;">-- A</b></span>
          </div>
        </div>

        <!-- AC LOAD -->
        <div class="panel col-3">
          <div class="panel-header">
            <span>Beban AC Output</span>
            <span id="ovLoadPctBadge" style="color: var(--gf-blue);">Load --%</span>
          </div>
          <div class="stat-val" id="ovLoadWatt" style="color: var(--gf-blue);">-- W</div>
          <div class="stat-sub">
            <span>Tegangan: <b id="ovAcOutVolt" style="color: #fff;">-- V</b></span>
            <span>Apparent: <b id="ovAcOutVa" style="color: #fff;">-- VA</b></span>
          </div>
        </div>

        <!-- GRID PLN -->
        <div class="panel col-3">
          <div class="panel-header">
            <span>PLN / Grid Input</span>
            <span style="color: var(--gf-purple);">⚡ GRID</span>
          </div>
          <div class="stat-val" id="ovGridVolt" style="color: var(--gf-purple);">-- V</div>
          <div class="stat-sub">
            <span>Frekuensi: <b id="ovGridHz" style="color: #fff;">-- Hz</b></span>
            <span>Bus Volt: <b id="ovBusVolt" style="color: #fff;">-- V</b></span>
          </div>
        </div>

        <!-- REALTIME LIVE CHART PV VS LOAD -->
        <div class="panel col-8">
          <div class="panel-header">
            <span>Grafik Real-Time: Solar PV vs Beban AC (Watt)</span>
            <span style="color: var(--gf-text-muted); font-size: 0.7rem;">LIVE DARI DB9</span>
          </div>
          <div class="chart-container">
            <canvas id="livePowerChart"></canvas>
          </div>
        </div>

        <!-- INVERTER DIAGNOSTICS -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Status & Diagnostik Inverter</span>
            <span>⚙️ 6.2kW</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.25rem;">
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Mode Operasi:</span>
              <b id="ovInverterMode" style="color: #fbbf24;">Loading...</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Suhu Heatsink:</span>
              <b id="ovTemp" style="color: #f87171;">-- °C</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Frekuensi Output AC:</span>
              <b id="ovAcOutHz">-- Hz</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Net Baterai Power:</span>
              <b id="ovBattNetWatt">-- W</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Kapasitas Rated:</span>
              <b style="color: #38bdf8;">6200 W / 48V</b>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Update Terakhir:</span>
              <b id="ovLastUpdate" style="color: var(--gf-text-dim);">--</b>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 2: RIWAYAT & ANALITIK GRAFIK ==================== -->
    <div id="page-history" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
          <span style="color: #38bdf8;">📈</span> Riwayat & Analitik Grafik Telemetri
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Analisis tren daya surya, persentase baterai, beban harian, dan tabel data historis SQLite.
        </div>
      </div>
      <div class="grafana-grid">
        <!-- CHART 1: POWER (PV VS LOAD) -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Riwayat Daya: Solar PV vs Beban Output (Watt)</span>
            <span style="color: var(--gf-orange);">☀️ PV / ⚡ Load</span>
          </div>
          <div class="chart-container">
            <canvas id="histPowerChart"></canvas>
          </div>
        </div>

        <!-- CHART 2: BATTERY (VOLT & SOC) -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Riwayat Baterai: Tegangan (V) & Kapasitas (SOC %)</span>
            <span style="color: var(--gf-green);">🔋 48V LiFePO4</span>
          </div>
          <div class="chart-container">
            <canvas id="histBattChart"></canvas>
          </div>
        </div>

        <!-- CHART 3: VOLTAGE (GRID VS AC OUT) -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Tegangan AC: PLN / Grid Input vs Output Inverter (V)</span>
            <span style="color: var(--gf-purple);">⚡ 220V AC</span>
          </div>
          <div class="chart-container">
            <canvas id="histVoltChart"></canvas>
          </div>
        </div>

        <!-- CHART 4: TEMPERATURE -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Suhu Inverter & Heatsink (°C)</span>
            <span style="color: var(--gf-red);">🌡️ Termal</span>
          </div>
          <div class="chart-container">
            <canvas id="histTempChart"></canvas>
          </div>
        </div>

        <!-- DATA LOG TABLE -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Tabel Riwayat Log Telemetry (Database SQLite)</span>
            <div style="display: flex; gap: 0.5rem; align-items: center;">
              <button onclick="downloadHistoryCSV()" style="background: linear-gradient(135deg, #10b981, #059669); color: #fff; border: none; border-radius: 4px; padding: 0.25rem 0.65rem; cursor: pointer; font-size: 0.75rem; font-weight: 600; box-shadow: 0 2px 6px rgba(16,185,129,0.3);">📥 Unduh CSV</button>
              <button onclick="fetchHistoryData()" style="background: #272a31; color: #fff; border: 1px solid #444; border-radius: 4px; padding: 0.25rem 0.6rem; cursor: pointer; font-size: 0.75rem;">🔄 Refresh Riwayat</button>
            </div>
          </div>
          <div style="overflow-x: auto; max-height: 350px;">
            <table class="raw-table">
              <thead>
                <tr>
                  <th>Waktu</th>
                  <th>Mode</th>
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

    <!-- ==================== HALAMAN 3: HERMES AI ORCHESTRATOR ==================== -->
    <div id="page-hermes" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
          <span style="color: #a855f7;">🤖</span> Hermes Agent Orchestrator & AI Hub
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Manajemen LLM (Sumopod API), otomasi baterai surplus/low, ponsel Appium, dan sinkronisasi IMAP OTP.
        </div>
      </div>
      <div class="grafana-grid">
        <!-- STATUS HERMES -->
        <div class="panel col-4">
          <div class="panel-header">
            <span>Hermes Agent Orchestrator</span>
            <span class="hermes-badge badge-ok" id="hermesOnlineBadge">ONLINE (:8000)</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.5rem;">
            <div class="stat-sub" style="border: none; padding: 0; flex-direction: column; align-items: flex-start; gap: 0.35rem;">
              <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
                <span>Pilihan Model AI (Sumopod API):</span>
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

        <!-- INTERACTIVE AI CONSOLE -->
        <div class="panel col-8">
          <div class="panel-header">
            <span>Hermes AI Interactive Console</span>
            <span style="color: var(--gf-text-muted); font-size: 0.7rem;">KIRIM PERINTAH / PROMPT</span>
          </div>
          <div style="background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; padding: 1rem; min-height: 140px; font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; color: #e2e8f0; overflow-y: auto;" id="aiOutputBox">
            Selamat datang di Hermes Agent Controller. Anda dapat mengetik instruksi atau pertanyaan untuk AI di bawah ini.
          </div>
          <div class="chat-box">
            <input type="text" id="aiInputPrompt" class="chat-input" placeholder="Tulis instruksi atau tanya status solar/otomasi..." onkeypress="if(event.key==='Enter') sendAiPrompt();">
            <button class="chat-btn" onclick="sendAiPrompt()">Kirim ke Hermes</button>
          </div>
        </div>

        <!-- APPIUM & CONNECTED DEVICES PANEL -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>📱 Appium Automation & Perangkat USB</span>
            <button onclick="fetchAppiumDevices()" style="background:#272a31; color:#fff; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">🔄 Refresh Device</button>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-bottom: 0.75rem;">
            Server Appium melayani otomasi Android via USB menggunakan driver UiAutomator2 (Port 4723).
          </div>
          <div id="appiumStatusCard" style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem; margin-bottom:0.75rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;">
              <span style="font-weight:600; font-size:0.85rem;">Status Server Appium:</span>
              <span class="hermes-badge badge-ok" id="appiumBadge">ONLINE (v3.8.0)</span>
            </div>
            <div style="font-size:0.75rem; color:var(--gf-text-dim); font-family:'JetBrains Mono', monospace;" id="appiumUrlText">http://localhost:4723</div>
          </div>
          <div style="font-size:0.75rem; font-weight:600; color:var(--gf-text-muted); margin-bottom:0.4rem;">PERANGKAT ANDROID TERHUBUNG (ADB):</div>
          <div id="appiumDevicesList" style="display:flex; flex-direction:column; gap:0.5rem; max-height:220px; overflow-y:auto;">
            <div style="color:var(--gf-text-muted); font-size:0.8rem;">Memuat daftar perangkat...</div>
          </div>
        </div>

        <!-- GMAIL IMAP OTP HARVESTER PANEL -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>✉️ Gmail IMAP OTP Harvester</span>
            <span class="hermes-badge badge-blue" id="mailPollerBadge">IMAP SSL (Port 993)</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-bottom: 0.75rem;">
            Memantau email masuk dan mendeteksi kode OTP verifikasi secara otomatis untuk notifikasi Hermes & Telegram.
          </div>
          <div style="background:#0f1013; border:1px solid var(--gf-border); border-radius:6px; padding:0.85rem; display:flex; flex-direction:column; gap:0.6rem;">
            <div>
              <label style="font-size:0.75rem; color:var(--gf-text-muted); display:block; margin-bottom:0.25rem;">Alamat Email Gmail:</label>
              <input type="email" id="mailInputEmail" class="chat-input" style="width:100%; box-sizing:border-box;" placeholder="contoh@gmail.com">
            </div>
            <div>
              <label style="font-size:0.75rem; color:var(--gf-text-muted); display:block; margin-bottom:0.25rem;">Google App Password (16 Huruf):</label>
              <div style="display:flex; gap:0.35rem;">
                <input type="password" id="mailInputPassword" class="chat-input" style="flex:1;" placeholder="xxxx xxxx xxxx xxxx">
                <button type="button" onclick="toggleMailPasswordVisibility()" style="background:#272a31; color:#fff; border:1px solid #444; border-radius:4px; padding:0 0.6rem; cursor:pointer; font-size:0.75rem;" id="mailTogglePassBtn">👁️</button>
              </div>
              <div style="font-size:0.7rem; color:var(--gf-text-dim); margin-top:0.3rem;">
                Dibuat di: <i>Google Account &gt; Keamanan &gt; Verifikasi 2 Langkah &gt; Sandi Aplikasi</i>
              </div>
            </div>
            <div style="display:flex; gap:0.5rem; margin-top:0.25rem;">
              <button onclick="testMailConnection()" class="range-btn" style="background:#3b82f6; color:#fff; flex:1; padding:0.5rem; border-radius:4px; cursor:pointer;">🧪 Tes Login IMAP</button>
              <button onclick="saveMailConfig()" class="range-btn" style="background:#10b981; color:#fff; flex:1; padding:0.5rem; border-radius:4px; cursor:pointer;">💾 Simpan & Aktifkan</button>
            </div>
            <div id="mailTestResult" style="display:none; font-size:0.75rem; padding:0.5rem; border-radius:4px; margin-top:0.2rem;"></div>
          </div>
        </div>

        <!-- EVENT LOG TIMELINE -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Feed Aktivitas & Riwayat Otomasi Hermes Agent</span>
            <button onclick="fetchHermesStatus()" style="background:#272a31; color:#fff; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">🔄 Refresh Event</button>
          </div>
          <div class="event-feed" id="hermesEventsFeed">
            <div style="color: var(--gf-text-muted); font-size: 0.85rem;">Memuat feed aktivitas...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== HALAMAN 4: NOTULEN MEETING AI ==================== -->
    <div id="page-meeting" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
          <span style="color: #10b981;">🎙️</span> Notulen Meeting AI & Speech-to-Text
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Perekaman suara mikrofon real-time, transkripsi otomatis, dan pembuatan notulen rapat cerdas via LLM.
        </div>
      </div>
      <div class="grafana-grid">
        <!-- MEETING CONTROLS -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Hermes Meeting Listener & Notulen AI</span>
            <span class="hermes-badge badge-blue" id="meetingStatusBadge">⚪ SIAP MEREKAM</span>
          </div>
          <div style="display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-top: 0.5rem;">
            <input type="text" id="meetingTitleInput" class="chat-input" style="max-width: 300px;" placeholder="Judul Meeting (misal: Rapat Proyek Solar)" value="Meeting Koordinasi">
            <select id="meetingLangSelect" class="chat-input" style="max-width: 170px;">
              <option value="id-ID" selected>🇮🇩 Bahasa Indonesia</option>
              <option value="en-US">🇬🇧 English</option>
            </select>
            <select id="meetingModelSelect" class="chat-input" style="max-width: 260px; font-size: 0.78rem; color: #38bdf8;">
              <option value="">Default (Sesuai Hermes)</option>
            </select>
            <div style="font-family: 'JetBrains Mono', monospace; font-size: 1.1rem; font-weight: 700; color: #38bdf8;" id="meetingTimerDisplay">
              ⏱️ 00:00:00
            </div>
            <div style="display: flex; gap: 0.5rem; margin-left: auto;">
              <button id="btnStartMeeting" class="chat-btn" style="background: #10b981;" onclick="startMeetingRecording()">🎙️ Mulai Dengerin Meeting</button>
              <button id="btnStopMeeting" class="chat-btn" style="background: #ef4444; display: none;" onclick="stopMeetingRecording()">⏹️ Selesai & Rangkum (AI)</button>
              <button class="chat-btn" style="background: #374151;" onclick="clearMeetingData()">🗑️ Reset</button>
            </div>
          </div>
          <div id="speechEngineInfo" style="margin-top: 0.75rem; padding: 0.5rem 0.75rem; background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; font-size: 0.75rem; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="speechEngineBadge" class="hermes-badge badge-blue">🎙️ Mendeteksi Fitur Audio Browser...</span>
              <span id="speechEngineDetail" style="color: var(--gf-text-muted);">Memeriksa dukungan Web Speech API & perekaman mikrofon...</span>
            </div>
            <div id="audioWaveIndicator" style="display: none; align-items: center; gap: 0.35rem; color: #ef4444; font-weight: 600;">
              <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; animation: blink 1s infinite;"></span>
              <span>Audio Mikrofon Aktif</span>
            </div>
          </div>
        </div>

        <!-- TRANSCRIPT LIVE BOX -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Transkrip Suara Real-time (Mikrofon)</span>
            <span style="font-size: 0.7rem; color: var(--gf-text-muted);">AUTO SPEECH-TO-TEXT</span>
          </div>
          <textarea id="meetingTranscriptText" class="chat-input" style="width: 100%; height: 350px; resize: vertical; line-height: 1.6; font-size: 0.9rem;" placeholder="Ketika tombol 'Mulai Dengerin Meeting' ditekan, suara pembicaraan dari mikrofon akan otomatis diubah menjadi teks di sini secara langsung. Anda juga bisa mengetik atau mengedit teks transkrip ini secara manual."></textarea>
          <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.5rem;">
            💡 <i>Mendukung mikrofon laptop, HP, atau USB mic. Privasi aman diproses langsung di browser & server Hermes lokal.</i>
          </div>
        </div>

        <!-- SUMMARY RESULT BOX -->
        <div class="panel col-6">
          <div class="panel-header">
            <span>Dokumen Notulen & Rangkuman Hermes AI</span>
            <div style="display: flex; gap: 0.4rem; flex-wrap: wrap;">
              <button onclick="copyMeetingSummary()" style="background:#272a31; color:#fff; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">📋 Salin</button>
              <button onclick="downloadMeetingMarkdown()" style="background:#272a31; color:#38bdf8; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">💾 Unduh MD</button>
              <button onclick="downloadMeetingText()" style="background:#272a31; color:#34d399; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">📄 Unduh TXT</button>
            </div>
          </div>
          <div id="meetingSummaryOutput" style="background: #0f1013; border: 1px solid var(--gf-border); border-radius: 6px; padding: 1.25rem; height: 350px; overflow-y: auto; line-height: 1.6; font-size: 0.875rem;">
            <div style="color: var(--gf-text-muted); text-align: center; margin-top: 5rem;">
              Tekan <b>"Selesai & Rangkum"</b> setelah meeting selesai untuk menghasilkan notulen rapi yang merangkum poin diskusi, keputusan, dan action items lewat Hermes AI.
            </div>
          </div>
        </div>

        <!-- PAST MEETINGS ARCHIVE -->
        <div class="panel col-12">
          <div class="panel-header">
            <span>Arsip Riwayat Notulen Meeting (Tersimpan di SQLite)</span>
            <button onclick="loadPastMeetings()" style="background:#272a31; color:#fff; border:1px solid #444; border-radius:4px; padding:0.25rem 0.6rem; cursor:pointer; font-size:0.75rem;">🔄 Refresh Arsip</button>
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
    </div>

    <!-- ==================== HALAMAN 5: SISTEM 24/7 & AUTO-DEPLOY CI/CD ==================== -->
    <div id="page-system" class="view-section">
      <div class="page-header" style="margin-bottom: 1.25rem;">
        <h1 style="font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 0.5rem;">
          <span style="color: #06b6d4;">🖥️</span> Sistem Server 24/7 & Auto-Deploy CI/CD
        </h1>
        <div style="font-size: 0.8rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
          Spesifikasi ThinkCentre M90n Nano, status systemd services, dan webhook auto-deploy GitHub origin/main.
        </div>
      </div>
      <div class="grafana-grid">
        <div class="panel col-6">
          <div class="panel-header">
            <span>ThinkCentre M90n Nano Server Hardware</span>
            <span style="color: #38bdf8;">FANLESS PC</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.5rem;">
            <div class="stat-sub" style="border: none; padding: 0;"><span>Processor:</span><b>Intel Celeron 4205U @ 1.80GHz (2 Cores)</b></div>
            <div class="stat-sub" style="border: none; padding: 0;"><span>RAM:</span><b>3.7 GB (~4 GB Low Power DDR4)</b></div>
            <div class="stat-sub" style="border: none; padding: 0;"><span>Sistem Operasi:</span><b>Kali GNU/Linux Rolling x86_64</b></div>
            <div class="stat-sub" style="border: none; padding: 0;"><span>IP Lokal (wlan0):</span><b>172.16.5.107</b></div>
            <div class="stat-sub" style="border: none; padding: 0;"><span>Domain Cloudflare:</span><b>westpro.my.id (HTTPS)</b></div>
            <div class="stat-sub" style="border: none; padding: 0;"><span>Mode Server:</span><b style="color: #34d399;">24/7 Auto-Start On Boot (Linger=yes)</b></div>
          </div>
        </div>

        <div class="panel col-6">
          <div class="panel-header">
            <span>Status Service Systemd (Otomatis Nyala Saat Restart)</span>
            <span class="hermes-badge badge-ok">ENABLED</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.5rem;">
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>powmr.service (:3005):</span>
              <span class="hermes-badge badge-ok">ACTIVE (Serial RS232 + Web)</span>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>hermes-agent.service (:8000):</span>
              <span class="hermes-badge badge-ok">ACTIVE (LLM & Solar Watcher)</span>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>cloudflared.service:</span>
              <span class="hermes-badge badge-ok">ACTIVE (Tunnel westpro.my.id)</span>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>mail-collector.service:</span>
              <span class="hermes-badge badge-blue">ENABLED (IMAP Poller)</span>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Anti-Sleep / Suspend:</span>
              <span class="hermes-badge badge-ok">MASKED (Tidak Pernah Tidur)</span>
            </div>
            <div class="stat-sub" style="border: none; padding: 0;">
              <span>Auto-Deploy CI/CD (origin/main):</span>
              <span id="deployStatusBadge" class="hermes-badge badge-ok">AKTIF (Watcher 2m + Webhook)</span>
            </div>
          </div>
        </div>

        <!-- AUTO-DEPLOY CI/CD PANEL -->
        <div class="panel col-12" style="margin-top: 1rem;">
          <div class="panel-header">
            <span>Otomasi CI/CD & Auto-Deploy (GitHub origin/main)</span>
            <span class="hermes-badge badge-ok" id="deployBadgeHeader">🟢 AKTIF (Dual Mode)</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-top: 0.75rem;">
            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid var(--gf-border); border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">📡 GitHub Webhook (Instan &lt; 3 Detik)</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-bottom: 0.5rem;">
                Setiap kali ada push / merge PR ke branch <code>main</code>, GitHub otomatis mentrigger endpoint webhook ini:
              </div>
              <div style="display: flex; gap: 0.5rem; align-items: center;">
                <input type="text" readonly value="https://westpro.my.id/api/webhook/github" style="width: 100%; background: #14161b; color: #34d399; border: 1px solid #334155; padding: 0.35rem 0.5rem; font-family: monospace; font-size: 0.75rem; border-radius: 4px;" id="webhookUrlInput">
                <button onclick="copyWebhookUrl()" style="background: #0284c7; color: white; border: none; padding: 0.35rem 0.65rem; border-radius: 4px; cursor: pointer; font-size: 0.75rem; white-space: nowrap;">Salin URL</button>
              </div>
            </div>

            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid var(--gf-border); border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">⏱️ Background Watcher (Failsafe)</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-bottom: 0.5rem;">
                Service <code>deploy-watcher.timer</code> otomatis memeriksa commit baru di <code>origin/main</code> setiap <b>2 menit</b>. Tanpa webhook pun, server akan tetap auto-deploy!
              </div>
              <div style="font-size: 0.75rem; color: #34d399;">
                Status: <b>deploy-watcher.timer Running (Active)</b>
              </div>
            </div>

            <div style="background: #0f1013; padding: 0.85rem; border: 1px solid var(--gf-border); border-radius: 6px;">
              <div style="color: #38bdf8; font-weight: 600; font-size: 0.825rem; margin-bottom: 0.5rem;">🚀 Info Deploy Terakhir & Manual Action</div>
              <div style="font-size: 0.75rem; margin-bottom: 0.25rem;">Commit: <b id="deployCommitText" style="color: #fff;">--</b></div>
              <div style="font-size: 0.75rem; margin-bottom: 0.25rem;">Waktu: <b id="deployTimeText" style="color: var(--gf-text-muted);">--</b></div>
              <div style="font-size: 0.75rem; margin-bottom: 0.5rem;">Pesan: <i id="deployMsgText" style="color: #94a3b8;">--</i></div>
              <button id="btnManualDeploy" onclick="triggerManualDeploy()" style="background: #10b981; color: #fff; font-weight: 600; border: none; border-radius: 4px; padding: 0.4rem 0.75rem; cursor: pointer; font-size: 0.75rem;">
                🚀 Deploy Sekarang (Tarik origin/main)
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- ==================== POPUP MODAL IZIN-IZIN ==================== -->
  <div id="permissionModal" style="display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.82); backdrop-filter: blur(10px); z-index: 100000; align-items: center; justify-content: center; padding: 1rem;">
    <div style="background: #12141a; border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 16px; width: 100%; max-width: 490px; box-shadow: 0 0 50px rgba(56, 189, 248, 0.2), 0 25px 50px -12px rgba(0, 0, 0, 0.8); overflow: hidden;">
      <div style="padding: 1.5rem; border-bottom: 1px solid var(--gf-border); display: flex; align-items: center; justify-content: space-between;">
        <div style="display: flex; align-items: center; gap: 0.75rem;">
          <div style="width: 42px; height: 42px; border-radius: 10px; background: linear-gradient(135deg, #0284c7, #38bdf8); display: flex; align-items: center; justify-content: center; font-size: 1.35rem; box-shadow: 0 4px 12px rgba(2, 132, 199, 0.4);">
            🛡️
          </div>
          <div>
            <h3 style="font-size: 1.1rem; font-weight: 700; color: #fff;">Izin Perangkat & Browser</h3>
            <p style="font-size: 0.75rem; color: var(--gf-text-muted);">Aktifkan Mikrofon & Notifikasi untuk fitur pintar server.</p>
          </div>
        </div>
        <button onclick="closePermissionModal(true)" style="background: none; border: none; color: var(--gf-text-muted); font-size: 1.4rem; cursor: pointer; padding: 0.25rem;" title="Tutup">&times;</button>
      </div>

      <div style="padding: 1.5rem; display: flex; flex-direction: column; gap: 1rem;">
        <!-- NON-SECURE CONTEXT WARNING -->
        <div id="insecureContextAlert" style="display: none; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; padding: 0.75rem; font-size: 0.775rem; color: #f87171;">
          ⚠️ Browser memblokir izin mikrofon pada koneksi HTTP biasa. Silakan buka aplikasi melalui <a href="https://westpro.my.id" style="color: #38bdf8; text-decoration: underline;">https://westpro.my.id</a> agar popup izin dapat diaktifkan.
        </div>

        <!-- PERMISSION 1: MIKROFON -->
        <div style="background: #181b22; border: 1px solid var(--gf-border); border-radius: 10px; padding: 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
          <div style="display: flex; align-items: flex-start; gap: 0.75rem;">
            <div style="font-size: 1.5rem; line-height: 1;">🎙️</div>
            <div>
              <div style="font-weight: 600; font-size: 0.875rem; color: #fff;">Mikrofon (Audio Recorder & STT)</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
                Mencatat suara rapat meeting & transkripsi rangkuman otomatis Hermes AI.
              </div>
              <div style="margin-top: 0.4rem;">
                <span id="badgeMicStatus" class="hermes-badge badge-blue">⚪ Belum Diizinkan</span>
              </div>
            </div>
          </div>
          <div>
            <button id="btnGrantMic" onclick="requestMicrophonePermission()" style="background: #0284c7; color: white; border: none; border-radius: 6px; padding: 0.45rem 0.85rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.2s;">
              Izinkan Mic
            </button>
          </div>
        </div>

        <!-- PERMISSION 2: NOTIFIKASI -->
        <div style="background: #181b22; border: 1px solid var(--gf-border); border-radius: 10px; padding: 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
          <div style="display: flex; align-items: flex-start; gap: 0.75rem;">
            <div style="font-size: 1.5rem; line-height: 1;">🔔</div>
            <div>
              <div style="font-weight: 600; font-size: 0.875rem; color: #fff;">Notifikasi Browser</div>
              <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.2rem;">
                Peringatan baterai surya kritis/surplus dan pemberitahuan email OTP baru masuk.
              </div>
              <div style="margin-top: 0.4rem;">
                <span id="badgeNotifStatus" class="hermes-badge badge-blue">⚪ Belum Diizinkan</span>
              </div>
            </div>
          </div>
          <div>
            <button id="btnGrantNotif" onclick="requestNotificationPermission()" style="background: #0284c7; color: white; border: none; border-radius: 6px; padding: 0.45rem 0.85rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.2s;">
              Izinkan Notif
            </button>
          </div>
        </div>

        <!-- HELPER TEXT IF BLOCKED -->
        <div id="blockedHelperText" style="display: none; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 8px; padding: 0.75rem; font-size: 0.75rem; color: #fbbf24;">
          💡 <b>Izin pernah diblokir di browser?</b> Klik ikon gembok / slider di sebelah kiri URL address bar (di atas), lalu ubah opsi <i>Mikrofon</i> dan <i>Notifikasi</i> menjadi <b>"Izinkan" (Allow)</b> kemudian muat ulang halaman.
        </div>
      </div>

      <div style="padding: 1.25rem 1.5rem; background: #0c0e12; border-top: 1px solid var(--gf-border); display: flex; align-items: center; justify-content: flex-end; gap: 0.75rem;">
        <button onclick="closePermissionModal(true)" style="background: transparent; color: var(--gf-text-muted); border: 1px solid var(--gf-border); border-radius: 6px; padding: 0.5rem 1rem; font-size: 0.8rem; font-weight: 600; cursor: pointer;">
          Nanti Saja
        </button>
        <button id="btnGrantAll" onclick="requestAllPermissions()" style="background: linear-gradient(135deg, #10b981, #059669); color: white; border: none; border-radius: 6px; padding: 0.55rem 1.35rem; font-size: 0.825rem; font-weight: 700; cursor: pointer; box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35);">
          ✨ Izinkan Semua Sekarang (1-Klik)
        </button>
      </div>
    </div>
  </div>

  <div id="toastContainer" style="position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 100005; display: flex; flex-direction: column; gap: 0.5rem; pointer-events: none;"></div>

  <!-- JAVASCRIPT LOGIC -->
  <script>
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
      system: 'Sistem 24/7 & Auto-Deploy CI/CD - Westpro Home Server'
    };

    function navigateToPage(pageId, pushState = true) {
      if (pageId === 'overview') pageId = 'solar';
      if (!PAGE_TITLES[pageId]) pageId = 'solar';

      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-section').forEach(sec => sec.classList.remove('active'));
      
      const navBtn = document.getElementById('nav-' + pageId);
      if (navBtn) navBtn.classList.add('active');

      const targetSec = document.getElementById('page-' + pageId) || document.getElementById('view-' + pageId);
      if (targetSec) targetSec.classList.add('active');

      document.title = PAGE_TITLES[pageId] || 'Westpro Home Server';

      if (pushState) {
        try {
          window.history.pushState({ page: pageId }, '', '/' + pageId);
        } catch(e) {}
      }

      if (pageId === 'history') {
        fetchHistoryData();
      } else if (pageId === 'hermes') {
        fetchHermesStatus();
        fetchAppiumDevices();
        loadMailConfig();
      } else if (pageId === 'meeting') {
        checkSpeechEngine();
        loadPastMeetings();
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

      // 1. Notification
      const notifBadge = document.getElementById('badgeNotifStatus');
      const notifBtn = document.getElementById('btnGrantNotif');
      if (window.Notification) {
        if (Notification.permission === 'granted') {
          notifGranted = true;
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-ok'; notifBadge.textContent = '🟢 Diizinkan'; }
          if (notifBtn) { notifBtn.style.display = 'none'; }
        } else if (Notification.permission === 'denied') {
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-warn'; notifBadge.textContent = '🔴 Diblokir'; }
        } else {
          if (notifBadge) { notifBadge.className = 'hermes-badge badge-blue'; notifBadge.textContent = '⚪ Belum Diizinkan'; }
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
            if (micBadge) { micBadge.className = 'hermes-badge badge-ok'; micBadge.textContent = '🟢 Diizinkan'; }
            if (micBtn) { micBtn.style.display = 'none'; }
          } else if (micStatus.state === 'denied') {
            if (micBadge) { micBadge.className = 'hermes-badge badge-warn'; micBadge.textContent = '🔴 Diblokir'; }
          } else {
            if (micBadge) { micBadge.className = 'hermes-badge badge-blue'; micBadge.textContent = '⚪ Belum Diizinkan'; }
          }
          micStatus.onchange = () => checkAllPermissions();
        } catch (e) {}
      }

      // 3. Update Navbar Summary
      const navSummary = document.getElementById('navPermissionSummary');
      if (navSummary) {
        if (micGranted && notifGranted) {
          navSummary.innerHTML = '<span style="color:#10b981;">🟢 Izin Lengkap</span>';
        } else if (micGranted || notifGranted) {
          navSummary.innerHTML = '<span style="color:#38bdf8;">🟡 Izin Sebagian</span>';
        } else {
          navSummary.innerHTML = '<span style="color:#f59e0b;">🛡️ Belum Diizinkan</span>';
        }
      }

      // 4. Update Sticky Banner Visibility
      const banner = document.getElementById('stickyPermissionBanner');
      if (banner) {
        if (micGranted && notifGranted) {
          banner.style.display = 'none';
        } else {
          banner.style.display = 'flex';
        }
      }

      // 5. Show Blocked Helper if Denied
      const blockedHelper = document.getElementById('blockedHelperText');
      if (blockedHelper) {
        const isBlocked = (window.Notification && Notification.permission === 'denied');
        blockedHelper.style.display = isBlocked ? 'block' : 'none';
      }

      return { micGranted, notifGranted };
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
        showToast('🎙️ Mikrofon berhasil diizinkan!', 'success');
        await checkAllPermissions();
        if (window.checkSpeechEngine) checkSpeechEngine();
        return true;
      } catch (err) {
        console.warn('Izin mic gagal:', err);
        showToast('⚠️ Mikrofon belum diizinkan: ' + (err.message || 'Ditolak'), 'error');
        await checkAllPermissions();
        const blockedHelper = document.getElementById('blockedHelperText');
        if (blockedHelper) blockedHelper.style.display = 'block';
        return false;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Izinkan Mic'; }
      }
    }

    async function requestNotificationPermission() {
      if (!window.Notification) {
        showToast('⚠️ Browser ini tidak mendukung Notifikasi Desktop', 'error');
        return false;
      }
      const btn = document.getElementById('btnGrantNotif');
      if (btn) { btn.disabled = true; btn.textContent = 'Meminta...'; }
      try {
        const perm = await Notification.requestPermission();
        if (perm === 'granted') {
          showToast('🔔 Notifikasi berhasil diizinkan!', 'success');
          try {
            new Notification('Westpro Home Server', {
              body: 'Notifikasi aktif! Anda akan menerima update alert inverter & OTP.',
              icon: '/favicon.ico'
            });
          } catch(e) {}
        } else {
          showToast('⚠️ Notifikasi belum diizinkan (' + perm + ')', 'error');
        }
        await checkAllPermissions();
        return perm === 'granted';
      } catch (err) {
        console.warn('Izin notif error:', err);
        showToast('⚠️ Gagal meminta izin notifikasi', 'error');
        return false;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Izinkan Notif'; }
      }
    }

    async function requestAllPermissions() {
      const btn = document.getElementById('btnGrantAll');
      if (btn) { btn.disabled = true; btn.textContent = '⏳ Meminta Izin Browser...'; }

      // 1. Trigger Microphone prompt
      await requestMicrophonePermission();

      // 2. Trigger Notification prompt
      await requestNotificationPermission();

      if (btn) { btn.disabled = false; btn.textContent = '✨ Izinkan Semua Sekarang (1-Klik)'; }

      const { micGranted, notifGranted } = await checkAllPermissions();
      if (micGranted && notifGranted) {
        showToast('🎉 Semua izin berhasil diaktifkan!', 'success');
        setTimeout(() => closePermissionModal(false), 800);
      }
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
    async function updateLiveData() {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error('API down');
        const data = await res.json();

        document.getElementById('ovPvWatt').textContent = data.pv_power_watt + ' W';
        document.getElementById('ovPvVolt').textContent = data.pv_voltage.toFixed(1) + ' V';
        document.getElementById('ovPvCurr').textContent = data.pv_current.toFixed(1) + ' A';

        document.getElementById('ovBattVolt').textContent = data.battery_voltage.toFixed(2) + ' V';
        document.getElementById('ovSocBadge').textContent = 'SOC ' + data.soc + '%';
        document.getElementById('ovBattChg').textContent = data.battery_charging_current + ' A';
        document.getElementById('ovBattDis').textContent = data.battery_discharge_current + ' A';

        document.getElementById('ovLoadWatt').textContent = data.ac_output_watt + ' W';
        document.getElementById('ovLoadPctBadge').textContent = 'Load ' + data.load_percent + '%';
        document.getElementById('ovAcOutVolt').textContent = data.ac_output_voltage.toFixed(1) + ' V';
        document.getElementById('ovAcOutVa').textContent = data.ac_output_va + ' VA';

        document.getElementById('ovGridVolt').textContent = data.grid_voltage.toFixed(1) + ' V';
        document.getElementById('ovGridHz').textContent = data.grid_frequency.toFixed(1) + ' Hz';
        document.getElementById('ovBusVolt').textContent = data.bus_voltage + ' V';

        document.getElementById('ovInverterMode').textContent = data.mode;
        document.getElementById('ovTemp').textContent = data.temperature + ' °C';
        document.getElementById('ovAcOutHz').textContent = data.ac_output_frequency.toFixed(1) + ' Hz';
        document.getElementById('ovBattNetWatt').textContent = data.battery_power_watt + ' W';
        
        const now = new Date();
        const timeStr = now.toTimeString().split(' ')[0];
        document.getElementById('ovLastUpdate').textContent = timeStr;
        document.getElementById('navModeText').textContent = data.mode;

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
        document.getElementById('navModeText').textContent = 'OFFLINE / RECONNECTING';
      }
    }

    let currentHistoryRows = [];

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
      if (window.showToast) showToast('📥 Berkas CSV riwayat berhasil diunduh!', 'success');
    }

    function renderHistoryCharts(labels, pvW, loadW, battV, soc, gridV, acOutV, tempC) {
      const commonScales = {
        x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', maxTicksLimit: 8, font: { family: 'JetBrains Mono' } } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { family: 'JetBrains Mono' } } }
      };

      // 1. Hist Power Chart
      if (histPowerChart) histPowerChart.destroy();
      histPowerChart = new Chart(document.getElementById('histPowerChart').getContext('2d'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'PV Watt', data: pvW, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.1)', fill: true, borderWidth: 2 },
            { label: 'Load Watt', data: loadW, borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,0.1)', fill: true, borderWidth: 2 }
          ]
        },
        options: { responsive: true, maintainAspectRatio: false, scales: commonScales }
      });

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
          fb.textContent = '✅ Aktif: ' + newModel.split(':')[0];
          fb.style.color = '#10b981';
          setTimeout(() => { fb.style.display = 'none'; }, 3500);
        }
        fetchHermesStatus();
      } catch (e) {
        if (fb) {
          fb.textContent = '❌ Gagal simpan';
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
          feed.innerHTML = '<div style="color: var(--gf-text-muted);">Belum ada event log dari Hermes Agent.</div>';
        }
      } catch (e) {
        console.error('Gagal fetch status Hermes:', e);
      }
    }

    // Send AI prompt to Hermes
    async function sendAiPrompt() {
      const input = document.getElementById('aiInputPrompt');
      const prompt = input.value.trim();
      if (!prompt) return;

      const outBox = document.getElementById('aiOutputBox');
      outBox.innerHTML += `<div style="color: #38bdf8; margin-top: 0.5rem;"><b>Anda:</b> ${prompt}</div>`;
      outBox.innerHTML += `<div style="color: var(--gf-text-muted);" id="aiLoading"><i>Hermes Agent sedang berpikir...</i></div>`;
      input.value = '';
      outBox.scrollTop = outBox.scrollHeight;

      try {
        const selectedModel = document.getElementById('hermesModelSelect')?.value || undefined;
        const res = await fetch('/api/hermes/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, model: selectedModel })
        });
        const data = await res.json();
        document.getElementById('aiLoading')?.remove();
        outBox.innerHTML += `<div style="color: #34d399; margin-top: 0.35rem;"><b>Hermes:</b> ${data.reply}</div>`;
        outBox.scrollTop = outBox.scrollHeight;
        fetchHermesStatus();
      } catch (err) {
        document.getElementById('aiLoading')?.remove();
        outBox.innerHTML += `<div style="color: #f87171; margin-top: 0.35rem;"><b>Error:</b> Gagal menghubungi Hermes Agent.</div>`;
      }
    }

    // Appium & Connected Devices
    async function fetchAppiumDevices() {
      try {
        const res = await fetch('/api/appium/devices');
        if (!res.ok) return;
        const data = await res.json();
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
          devList.innerHTML = '<div style="color:var(--gf-text-muted); font-size:0.8rem; padding:0.5rem;">Tidak ada perangkat Android terdeteksi lewat USB.<br>Colok kabel USB dan pastikan USB Debugging aktif.</div>';
          return;
        }
        devList.innerHTML = data.devices.map(d => {
          const isDev = d.state === 'device';
          const bat = d.battery !== null ? `🔋 ${d.battery}%` : '🔋 --%';
          const stateBadge = isDev 
            ? `<span class="hermes-badge badge-ok" style="font-size:0.7rem;">TERHUBUNG</span>`
            : `<span class="hermes-badge badge-warn" style="font-size:0.7rem;">${d.state.toUpperCase()}</span>`;
          const actionButtons = isDev ? `
            <div style="display:flex; gap:0.35rem; margin-top:0.4rem; flex-wrap:wrap;">
              <button onclick="triggerDeviceAction('${d.serial}', 'wake')" style="background:#272a31; color:#38bdf8; border:1px solid #444; border-radius:3px; padding:0.2rem 0.5rem; font-size:0.7rem; cursor:pointer;" title="Toggle Power / Wake Layar">⚡ Power/Layar</button>
              <button onclick="triggerDeviceAction('${d.serial}', 'home')" style="background:#272a31; color:#fbbf24; border:1px solid #444; border-radius:3px; padding:0.2rem 0.5rem; font-size:0.7rem; cursor:pointer;" title="Tekan Tombol Home">🏠 Home</button>
              <button onclick="triggerDeviceAction('${d.serial}', 'battery')" style="background:#272a31; color:#34d399; border:1px solid #444; border-radius:3px; padding:0.2rem 0.5rem; font-size:0.7rem; cursor:pointer;" title="Info Diagnostik Baterai">🔋 Diagnostik</button>
            </div>
          ` : '';
          return `
            <div style="background:#14161b; border:1px solid #2d3139; border-radius:4px; padding:0.5rem 0.75rem; display:flex; justify-content:space-between; align-items:flex-start;">
              <div>
                <div style="font-weight:600; color:#fff; font-size:0.8rem;">📱 ${d.model} <span style="font-size:0.7rem; color:var(--gf-text-dim);">(${d.product || 'Android'})</span></div>
                <div style="font-size:0.7rem; color:var(--gf-text-dim); font-family:'JetBrains Mono', monospace; margin-top:0.2rem;">Serial: <b>${d.serial}</b> &bull; ${bat}</div>
                ${actionButtons}
              </div>
              <div>${stateBadge}</div>
            </div>
          `;
        }).join('');
      } catch (e) {
        console.error('Gagal fetch appium devices:', e);
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
            alert(`🔋 Diagnostik Baterai (${serial}):\n• Level: ${lvl}%\n• Suhu: ${temp}\n• Tegangan: ${volt}\n• Status: ${ret.battery_info.status || 'OK'}`);
          } else {
            alert(`✅ Perintah '${action}' berhasil dikirim ke ${serial}!`);
          }
          fetchAppiumDevices();
        } else {
          alert('❌ Gagal: ' + (ret.error || ret.message || 'Unknown error'));
        }
      } catch (e) {
        alert('Gagal kirim perintah: ' + e);
      }
    }

    // Gmail IMAP OTP Config
    async function loadMailConfig() {
      try {
        const res = await fetch('/api/mail/config');
        if (!res.ok) return;
        const data = await res.json();
        if (data.accounts && data.accounts.length) {
          const acc = data.accounts[0];
          document.getElementById('mailInputEmail').value = acc.email || '';
          if (acc.has_password) {
            document.getElementById('mailInputPassword').placeholder = '●●●●●●●● (Tersimpan)';
          }
        }
      } catch (e) {
        console.error('Gagal load mail config:', e);
      }
    }

    function toggleMailPasswordVisibility() {
      const inp = document.getElementById('mailInputPassword');
      const btn = document.getElementById('mailTogglePassBtn');
      if (inp.type === 'password') {
        inp.type = 'text';
        btn.textContent = '🔒';
      } else {
        inp.type = 'password';
        btn.textContent = '👁️';
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
          resultBox.textContent = '✅ ' + data.message;
        } else {
          resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
          resultBox.style.color = '#f87171';
          resultBox.textContent = '❌ Gagal Login IMAP: ' + data.error;
        }
      } catch (e) {
        resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
        resultBox.style.color = '#f87171';
        resultBox.textContent = '❌ Error: ' + e.message;
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

      try {
        const res = await fetch('/api/mail/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            accounts: [{
              account_name: 'Gmail_OTP',
              imap_server: 'imap.gmail.com',
              imap_port: 993,
              email: email,
              password: password
            }]
          })
        });
        const data = await res.json();
        if (data.status === 'saved') {
          resultBox.style.background = 'rgba(16, 185, 129, 0.15)';
          resultBox.style.color = '#34d399';
          resultBox.textContent = '✅ Konfigurasi tersimpan dan mail-collector aktif!';
          fetchHermesStatus();
        } else {
          resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
          resultBox.style.color = '#f87171';
          resultBox.textContent = '❌ Gagal menyimpan konfigurasi';
        }
      } catch (e) {
        resultBox.style.background = 'rgba(239, 68, 68, 0.15)';
        resultBox.style.color = '#f87171';
        resultBox.textContent = '❌ Error: ' + e.message;
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

    function checkSpeechEngine() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const badge = document.getElementById('speechEngineBadge');
      const detail = document.getElementById('speechEngineDetail');
      if (!badge || !detail) return;
      if (SpeechRecognition) {
        badge.className = 'hermes-badge badge-ok';
        badge.textContent = '🟢 Web Speech Streaming Aktif';
        detail.textContent = 'Browser mendukung transkripsi teks live mengalir saat berbicara (Chrome/Edge/Safari/HP).';
      } else {
        badge.className = 'hermes-badge badge-blue';
        badge.textContent = '🎙️ Mode Perekaman Mikrofon Universal';
        detail.innerHTML = 'Browser ini tidak memiliki Web Speech bawaan (misal Firefox/Brave). Audio direkam & otomatis ditranskripsi via server saat menekan tombol Selesai & Rangkum.';
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
      recognition.lang = document.getElementById('meetingLangSelect').value;

      recognition.onresult = (event) => {
        let finalTranscript = '';
        for (let i = event.resultIndex; i < event.results.length; ++i) {
          if (event.results[i].isFinal) {
            finalTranscript += event.results[i][0].transcript + ' ';
          }
        }
        const textarea = document.getElementById('meetingTranscriptText');
        if (finalTranscript) {
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
        if (isRecordingMeeting) {
          try { recognition.start(); } catch(e){}
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
      isRecordingMeeting = true;
      meetingStartTime = Date.now();
      document.getElementById('btnStartMeeting').style.display = 'none';
      document.getElementById('btnStopMeeting').style.display = 'inline-block';
      const badge = document.getElementById('meetingStatusBadge');
      badge.textContent = '🔴 MEREKAM & MENDENGARKAN...';
      badge.className = 'hermes-badge badge-warn';
      const wave = document.getElementById('audioWaveIndicator');
      if (wave) wave.style.display = 'inline-flex';

      // Start timer
      clearInterval(meetingTimerInterval);
      meetingTimerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - meetingStartTime) / 1000);
        const hrs = String(Math.floor(elapsed / 3600)).padStart(2, '0');
        const mins = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
        const secs = String(elapsed % 60).padStart(2, '0');
        document.getElementById('meetingTimerDisplay').textContent = `⏱️ ${hrs}:${mins}:${secs}`;
      }, 1000);

      // 1. Try Native Speech Recognition (Chrome/Edge/Safari)
      speechRecognition = initSpeechRecognition();
      if (speechRecognition) {
        try { speechRecognition.start(); } catch(e){}
      }

      // 2. Start Universal Audio Recording via getUserMedia (Firefox/Brave/Any Browser)
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        try {
          audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
          recordedAudioChunks = [];
          mediaRecorder = new MediaRecorder(audioStream);
          mediaRecorder.ondataavailable = (e) => {
            if (e.data && e.data.size > 0) recordedAudioChunks.push(e.data);
          };
          mediaRecorder.start();
        } catch (err) {
          console.warn('Microphone getUserMedia error:', err);
          stopMeetingRecording(false);
          openPermissionModal();
          return;
        }
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

      // Stop MediaRecorder & collect audio
      let recordedBlob = null;
      if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        const stopPromise = new Promise(resolve => {
          mediaRecorder.onstop = () => {
            recordedBlob = new Blob(recordedAudioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
            resolve();
          };
        });
        mediaRecorder.stop();
        if (audioStream) {
          audioStream.getTracks().forEach(t => t.stop());
        }
        await stopPromise;
      }

      document.getElementById('btnStartMeeting').style.display = 'inline-block';
      document.getElementById('btnStopMeeting').style.display = 'none';
      const badge = document.getElementById('meetingStatusBadge');
      badge.textContent = '⚪ SELESAI';
      badge.className = 'hermes-badge badge-ok';

      if (!shouldSummarize) return;

      let transcript = document.getElementById('meetingTranscriptText').value.trim();

      // If transcript is still empty and we have an audio blob, transcribe it via server!
      if (!transcript && recordedBlob && recordedBlob.size > 0) {
        const outBox = document.getElementById('meetingSummaryOutput');
        outBox.innerHTML = `
          <div style="text-align: center; margin-top: 5rem; color: #38bdf8;">
            <div style="font-size: 1.5rem; animation: pulse 1.5s infinite;">🎙️ ⚙️</div>
            <div style="font-weight: 600; margin-top: 0.75rem;">Mentranskripsikan audio mikrofon via server...</div>
            <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.25rem;">Mengonversi suara ke teks Bahasa Indonesia...</div>
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

      const outBox = document.getElementById('meetingSummaryOutput');
      outBox.innerHTML = `
        <div style="text-align: center; margin-top: 5rem; color: #38bdf8;">
          <div style="font-size: 1.5rem; animation: pulse 1.5s infinite;">🤖 🧠</div>
          <div style="font-weight: 600; margin-top: 0.75rem;">Hermes Agent sedang menganalisis transkrip & menyusun notulen...</div>
          <div style="font-size: 0.75rem; color: var(--gf-text-muted); margin-top: 0.25rem;">Menggunakan LLM via Sumopod API</div>
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
                <div style="display:flex; gap:0.35rem;">
                  <button onclick="viewPastMeeting(${idx})" style="background:#0284c7; color:#fff; border:none; border-radius:4px; padding:0.2rem 0.5rem; cursor:pointer; font-size:0.75rem;">Buka</button>
                  <button onclick="deletePastMeeting(${m.id})" style="background:#dc2626; color:#fff; border:none; border-radius:4px; padding:0.2rem 0.45rem; cursor:pointer; font-size:0.75rem;" title="Hapus Notulen">🗑️</button>
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
              badgeHeader.textContent = '🟢 AKTIF (Tersinkron)';
            }
          } else if (data.status === 'deploying') {
            badgeEl.className = 'hermes-badge badge-warn';
            badgeEl.textContent = '🚀 SEDANG DEPLOY...';
            if (badgeHeader) {
              badgeHeader.className = 'hermes-badge badge-warn';
              badgeHeader.textContent = '🚀 SEDANG DEPLOY...';
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
        btn.textContent = '🚀 Sedang Memulai Deploy...';
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
            btn.textContent = '🚀 Deploy Sekarang (Tarik origin/main)';
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

    // Startup
    window.addEventListener('DOMContentLoaded', () => {
      // 1. Initial page navigation
      try {
        let initial = '__INITIAL_PAGE__';
        if (!initial || initial.startsWith('__')) {
          const path = window.location.pathname.replace(/^\\//, '');
          initial = path || 'solar';
        }
        navigateToPage(initial, false);
      } catch (err) {
        console.warn('Navigation error:', err);
      }

      // 2. CHECK & PROMPT PERMISSIONS IMMEDIATELY!
      try {
        checkAllPermissions().then(({ micGranted, notifGranted }) => {
          const dismissed = sessionStorage.getItem('westpro_perm_dismissed');
          if ((!micGranted || !notifGranted) && !dismissed) {
            openPermissionModal();
          }
        }).catch(err => {
          console.warn('Initial permission check error:', err);
          openPermissionModal();
        });
      } catch (err) {
        openPermissionModal();
      }

      // 3. Init charts and telemetry safely
      try { initLiveChart(); } catch (e) { console.warn('initLiveChart error:', e); }
      try { updateLiveData(); } catch (e) { console.warn('updateLiveData error:', e); }
      setInterval(updateLiveData, 2000);

      try { fetchModels(); } catch (e) {}
      try { fetchHermesStatus(); } catch (e) {}
      try { fetchAppiumDevices(); } catch (e) {}
      try { loadMailConfig(); } catch (e) {}
      try { loadPastMeetings(); } catch (e) {}
      try { checkSpeechEngine(); } catch (e) {}
      try { fetchDeployStatus(); } catch (e) {}
      setInterval(fetchDeployStatus, 15000);
    });
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

    def do_HEAD(self):
        self.do_GET(is_head=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
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

        # 1. Web App Manifest
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
                self.send_response(302)
                self.send_header("Location", "/solar")
                self.end_headers()
                return
            _send(HTML_LOGIN_TEMPLATE, "text/html; charset=utf-8")
            return

        # 3. Dedicated Feature Pages (Protected by Session)
        if clean_path in ("/", "/index.html", "/solar", "/history", "/hermes", "/meeting", "/system"):
            if not user:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            initial_page = "solar" if clean_path in ("/", "/index.html", "/solar") else clean_path.lstrip("/")
            html = HTML_DASHBOARD_TEMPLATE.replace("__INITIAL_PAGE__", initial_page)
            html = html.replace("__USER_EMAIL__", user.get("email", "areskretindo@gmail.com"))
            html = html.replace("__USER_ROLE__", user.get("role", "owner").upper())
            _send(html, "text/html; charset=utf-8")
            return

        # 4. Auth Session Check API
        if clean_path == "/api/auth/me":
            payload = json.dumps({"authenticated": True, "user": user}) if user else '{"authenticated": false}'
            _send(payload)
            return

        # 5. Telemetry Live API
        elif self.path in ("/api/state", "/data"):
            data = self.inverter.last_state if self.inverter else {}
            _send(json.dumps(data, indent=2))

        # 6. Telemetry History API (SQLite)
        elif clean_path in ("/api/history/download", "/api/history.csv"):
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

        elif self.path == "/api/appium/devices":
            try:
                req = urllib.request.Request("http://localhost:8000/api/devices")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    _send(resp.read())
            except Exception as e:
                _send(json.dumps({"appium": {"ready": False, "error": str(e)}, "devices": []}))

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

        elif self.path == "/api/meetings":
            meetings = self.history_db.get_meetings() if self.history_db else []
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

        elif self.path == "/healthz":
            _send('{"status": "ok"}')

        else:
            _send("Not Found", "text/plain", 404)

    def do_POST(self):
        clean_path = self.path.split("?")[0].rstrip("/")

        # 0. Auth APIs
        if clean_path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                email = data.get("email", "").strip().lower()
                if not email or "@" not in email:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Email tidak valid"}')
                    return

                token = self.history_db.create_session(email) if self.history_db else "session_token"
                user = self.history_db.get_or_create_user(email) if self.history_db else {"email": email, "role": "owner"}

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
                if not email or "@" not in email:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Email tidak valid"}')
                    return

                user = self.history_db.get_or_create_user(email, name) if self.history_db else {"email": email, "role": "user"}
                token = self.history_db.create_session(email) if self.history_db else "session_token"

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
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path in ("/api/mail/config", "/api/mail/test", "/api/model/select"):
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
                with urllib.request.urlopen(req, timeout=20.0) as resp:
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
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "success": False}).encode("utf-8"))
        elif self.path == "/api/meetings/summarize":
            try:
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

                    # Simpan ke SQLite history_db
                    if self.history_db:
                        self.history_db.save_meeting(
                            title=req_data.get("title", "Meeting"),
                            duration_sec=req_data.get("duration", 0),
                            transcript=req_data.get("transcript", ""),
                            summary=resp_data.get("summary", "")
                        )

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
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
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req_data = json.loads(body.decode("utf-8"))
                meeting_id = int(req_data.get("id", 0))
                if self.history_db and meeting_id > 0:
                    self.history_db.delete_meeting(meeting_id)
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
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_DELETE(self):
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path.startswith("/api/meetings/"):
            try:
                meeting_id = int(clean_path.split("/")[-1])
                if self.history_db and meeting_id > 0:
                    self.history_db.delete_meeting(meeting_id)
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
