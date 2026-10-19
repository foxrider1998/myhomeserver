import sqlite3
from datetime import datetime, timedelta

def get_battery_telemetry_stats(db_path='/home/ares/Documents/myserver/data/history.db', current_soc=85, current_batt_w=536):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Query last 3 days of telemetry to ensure today and yesterday are fully captured
    c.execute('''
        SELECT created_at, batt_v, batt_soc, batt_power_w, load_w, pv_w
        FROM telemetry_history
        WHERE created_at >= datetime('now', 'localtime', '-3 days')
        ORDER BY id ASC
    ''')
    rows = c.fetchall()

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    indonesian_days = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']

    days_data = {}
    prev_t = None
    prev_pw = 0.0

    for r in rows:
        try:
            t = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        d_str = t.strftime("%Y-%m-%d")
        if d_str not in days_data:
            days_data[d_str] = {
                "chg_wh": 0.0,
                "dis_wh": 0.0,
                "night_dis_wh": 0.0,
                "night_seconds": 0.0,
                "latest_soc": r["batt_soc"] or 80,
                "latest_v": r["batt_v"] or 48.0
            }
        
        d = days_data[d_str]
        pw = float(r["batt_power_w"] or 0)
        if r["batt_soc"]: d["latest_soc"] = int(r["batt_soc"])
        if r["batt_v"]: d["latest_v"] = float(r["batt_v"])

        if prev_t is not None and prev_t.date() == t.date():
            dt = (t - prev_t).total_seconds()
            if 0 < dt < 300:
                avg_pw = (pw + prev_pw) / 2.0
                if avg_pw > 0:
                    d["chg_wh"] += avg_pw * (dt / 3600.0)
                elif avg_pw < 0:
                    d["dis_wh"] += abs(avg_pw) * (dt / 3600.0)
                
                # Malam hari (17:00 - 06:00)
                if t.hour >= 17 or t.hour < 6:
                    if avg_pw < 0:
                        d["night_dis_wh"] += abs(avg_pw) * (dt / 3600.0)
                        d["night_seconds"] += dt
                    elif float(r["load_w"] or 0) > 0 and avg_pw >= 0:
                        # Beban AC malam jika baterai tidak mencatat negatif
                        d["night_dis_wh"] += float(r["load_w"]) * (dt / 3600.0)
                        d["night_seconds"] += dt

        prev_t = t
        prev_pw = pw

    today_data = days_data.get(today_str, {"chg_wh": 0.0, "dis_wh": 0.0, "latest_soc": current_soc, "latest_v": 48.0})
    yesterday_data = days_data.get(yesterday_str, {"chg_wh": 0.0, "dis_wh": 0.0, "night_dis_wh": 0.0, "night_seconds": 0.0})

    # Rata-rata daya pemakaian malam kemarin (Watt)
    if yesterday_data.get("night_seconds", 0) > 600:
        yesterday_avg_night_w = yesterday_data["night_dis_wh"] / (yesterday_data["night_seconds"] / 3600.0)
    elif yesterday_data.get("dis_wh", 0) > 0:
        # Fallback jika interval malam tidak penuh, estimasi dari total dis_wh / 6 jam malam
        yesterday_avg_night_w = yesterday_data["dis_wh"] / 6.0
    else:
        yesterday_avg_night_w = 180.0

    yesterday_avg_night_w = round(max(50.0, yesterday_avg_night_w), 1)

    # Kapasitas LiFePO4 48V 100Ah = 4800 Wh nominal. Usable safe cutoff 20% SOC (80% DoD)
    active_soc = current_soc if current_soc is not None else today_data["latest_soc"]
    safe_cutoff_soc = 20
    usable_soc_pct = max(0, active_soc - safe_cutoff_soc)
    nominal_total_wh = 4800.0
    usable_wh = usable_soc_pct * (nominal_total_wh / 100.0)

    # Estimasi jam support
    hours_remaining = round(usable_wh / yesterday_avg_night_w, 1)

    # Estimasi target jam (clock time)
    # 1. Dari jam saat ini (realtime backup bertahan sampai kapan)
    target_dt_now = now + timedelta(hours=hours_remaining)
    target_day_name = indonesian_days[target_dt_now.weekday()]
    
    if target_dt_now.date() == now.date():
        target_time_display = f"Hari Ini, {target_dt_now.strftime('%H:%M')} WIB"
    elif target_dt_now.date() == (now + timedelta(days=1)).date():
        target_time_display = f"Besok, {target_dt_now.strftime('%H:%M')} WIB"
    else:
        target_time_display = f"{target_day_name}, {target_dt_now.strftime('%H:%M')} WIB"

    # Status badge
    if hours_remaining >= 14:
        status_label = "🟢 Aman Melewati Subuh"
        status_badge_class = "badge-ok"
    elif hours_remaining >= 10:
        status_label = "🟡 Cukup Sampai Dini Hari"
        status_badge_class = "badge-warn"
    else:
        status_label = "🔴 Perlu Hemat Beban"
        status_badge_class = "badge-danger"

    result = {
        "today_charged_kwh": round(today_data["chg_wh"] / 1000.0, 2),
        "today_charged_wh": round(today_data["chg_wh"], 1),
        "today_discharged_kwh": round(today_data["dis_wh"] / 1000.0, 2),
        "today_discharged_wh": round(today_data["dis_wh"], 1),
        "yesterday_charged_kwh": round(yesterday_data["chg_wh"] / 1000.0, 2),
        "yesterday_discharged_kwh": round(yesterday_data["dis_wh"] / 1000.0, 2),
        "yesterday_discharged_dod_pct": round((yesterday_data["dis_wh"] / nominal_total_wh) * 100, 1),
        "yesterday_avg_night_w": yesterday_avg_night_w,
        "current_soc": active_soc,
        "usable_wh": round(usable_wh, 1),
        "usable_kwh": round(usable_wh / 1000.0, 2),
        "hours_remaining": hours_remaining,
        "target_time_display": target_time_display,
        "target_iso": target_dt_now.strftime("%Y-%m-%d %H:%M:%S"),
        "status_label": status_label,
        "status_badge_class": status_badge_class,
        "calculation_basis": f"Berdasarkan rata-rata pengosongan malam kemarin ({yesterday_avg_night_w} W) & batas aman SOC {safe_cutoff_soc}%"
    }
    return result

if __name__ == "__main__":
    res = get_battery_telemetry_stats()
    import pprint
    pprint.pprint(res)
