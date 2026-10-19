#!/usr/bin/env python3
"""
solar_weather.py - Open-Meteo Weather & Solar Irradiance Integration with Smart Advisory
Provides live weather conditions, 3-day forecast, peak solar hours, and intelligent energy recommendations.
"""

import time
import urllib.request
import json
import calendar
from datetime import datetime, timedelta

_weather_cache = {"ts": 0, "data": None}
_climate_cache = {"ts": 0, "key": None, "data": None}

INDONESIAN_DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
INDONESIAN_MONTHS = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
INDONESIAN_MONTHS_SHORT = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

CODE_MAP = {
    0: ("Cerah", "☀️"),
    1: ("Sebagian Cerah", "🌤️"),
    2: ("Cerah Berawan", "⛅"),
    3: ("Mendung Berawan", "☁️"),
    45: ("Berkabut", "🌫️"),
    48: ("Kabut Tebal", "🌫️"),
    51: ("Gerimis Ringan", "🌦️"),
    53: ("Gerimis Sedang", "🌦️"),
    61: ("Hujan Ringan", "🌧️"),
    63: ("Hujan Sedang", "🌧️"),
    65: ("Hujan Deras", "⛈️"),
    80: ("Hujan Lokal", "🌦️"),
    81: ("Hujan Lebat", "🌧️"),
    95: ("Hujan Petir", "⛈️")
}

BMKG_EMOJI_MAP = {
    "cerah": "☀️",
    "cerah berawan": "⛅",
    "berawan": "☁️",
    "berawan tebal": "☁️",
    "kabut": "🌫️",
    "asap": "🌫️",
    "hujan ringan": "🌧️",
    "hujan sedang": "🌧️",
    "hujan lebat": "⛈️",
    "hujan lokal": "🌦️",
    "hujan petir": "⛈️"
}

def fetch_bmkg_weather_data(adm4: str = "34.01.06.2006") -> dict:
    """
    Mengambil data prakiraan cuaca resmi dari API BMKG Republik Indonesia
    untuk adm4: 34.01.06.2006 (Desa Kaliagung, Sentolo, Kulon Progo, D.I. Yogyakarta).
    """
    url = f"https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4={adm4}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"})
    with urllib.request.urlopen(req, timeout=4.5) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _season_label(rain_mm: float) -> str:
    """Fase musim kasar untuk Jawa bagian selatan berdasarkan curah hujan bulanan rata-rata."""
    if rain_mm >= 200:
        return "Musim Hujan (Puncak)"
    if rain_mm >= 100:
        return "Musim Hujan / Pancaroba Basah"
    if rain_mm >= 50:
        return "Pancaroba (Transisi)"
    return "Musim Kemarau"


def _planting_hint(rain_mm: float, tmax: float) -> str:
    """Saran umum (indikatif) berdasarkan hujan bulanan; bukan pengganti kalender tanam resmi."""
    if rain_mm >= 200:
        hint = "Padi sawah & tanaman butuh air banyak cocok; waspada genangan, jamur & penyakit daun"
    elif rain_mm >= 100:
        hint = "Awal tanam padi/jagung/palawija; tanah cukup lembap, bagus untuk persemaian"
    elif rain_mm >= 50:
        hint = "Cocok jagung, kedelai, cabai, sayuran; siapkan irigasi cadangan"
    else:
        hint = "Kering: pilih palawija tahan kering (kacang hijau, jagung) atau sayuran dengan irigasi/mulsa"
    if tmax >= 33:
        hint += "; siang sangat panas, beri naungan/mulsa"
    return hint


def get_climate_outlook(lat: float, lon: float, months_ahead: int = 6, years: int = 5) -> list:
    """
    Klimatologi bulanan nyata (rata-rata beberapa tahun terakhir) dari arsip Open-Meteo untuk
    bulan ini dan beberapa bulan ke depan: suhu, curah hujan, hari hujan, radiasi (jam puncak surya).
    Dicache 24 jam. Mengembalikan [] bila arsip tidak bisa dijangkau.
    """
    global _climate_cache
    now = time.time()
    now_dt = datetime.now()
    key = (round(lat, 3), round(lon, 3), now_dt.year, now_dt.month, months_ahead)
    if _climate_cache["data"] is not None and _climate_cache["key"] == key and (now - _climate_cache["ts"]) < 86400:
        return _climate_cache["data"]

    start_year = now_dt.year - years
    url = (
        f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
        f"&start_date={start_year}-01-01&end_date={now_dt.year - 1}-12-31"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,shortwave_radiation_sum"
        f"&timezone=Asia%2FJakarta"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WestproSolar/1.0"})
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        daily = raw.get("daily", {})
        times = daily.get("time", [])
        tmax_l = daily.get("temperature_2m_max", [])
        tmin_l = daily.get("temperature_2m_min", [])
        rain_l = daily.get("precipitation_sum", [])
        rad_l = daily.get("shortwave_radiation_sum", [])
    except Exception:
        return _climate_cache["data"] or []

    # akumulasi per (tahun, bulan)
    acc = {}
    for i, t in enumerate(times):
        try:
            y, m = int(t[:4]), int(t[5:7])
        except Exception:
            continue
        b = acc.setdefault((y, m), {"tmax": [], "tmin": [], "rain": 0.0, "rainy": 0, "rad": []})
        if i < len(tmax_l) and tmax_l[i] is not None:
            b["tmax"].append(tmax_l[i])
        if i < len(tmin_l) and tmin_l[i] is not None:
            b["tmin"].append(tmin_l[i])
        if i < len(rain_l) and rain_l[i] is not None:
            b["rain"] += rain_l[i]
            if rain_l[i] >= 1.0:
                b["rainy"] += 1
        if i < len(rad_l) and rad_l[i] is not None:
            b["rad"].append(rad_l[i])

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    outlook = []
    for k in range(months_ahead):
        mi = (now_dt.month - 1 + k) % 12 + 1
        yr = now_dt.year + (now_dt.month - 1 + k) // 12
        buckets = [b for (y, m), b in acc.items() if m == mi and b["tmax"]]
        if not buckets:
            continue
        n = len(buckets)
        tmax = round(sum(avg(b["tmax"]) for b in buckets) / n, 1)
        tmin = round(sum(avg(b["tmin"]) for b in buckets) / n, 1)
        rain = round(sum(b["rain"] for b in buckets) / n)
        rainy = round(sum(b["rainy"] for b in buckets) / n)
        psh = round(sum(avg(b["rad"]) for b in buckets) / n / 3.6, 1)  # MJ/m2/hari -> kWh/m2/hari
        outlook.append({
            "month": mi,
            "year": yr,
            "month_name": f"{INDONESIAN_MONTHS[mi]} {yr}",
            "is_current": k == 0,
            "days_in_month": calendar.monthrange(yr, mi)[1],
            "avg_temp_max": tmax,
            "avg_temp_min": tmin,
            "rain_mm": rain,
            "rainy_days": rainy,
            "peak_sun_hours": psh,
            "season": _season_label(rain),
            "planting_hint": _planting_hint(rain, tmax),
            "years_sampled": n,
        })
    _climate_cache = {"ts": now, "key": key, "data": outlook}
    return outlook


def get_solar_weather_data(lat: float = -7.8342, lon: float = 110.2189, force: bool = False) -> dict:
    """
    Mengambil data cuaca resmi dari BMKG (Desa Kaliagung Sentolo) dipadukan
    dengan data radiasi fotovoltaik Open-Meteo (Direct Normal Irradiance W/m2).
    Dicache selama 15 menit, mendukung force refresh instan.
    """
    global _weather_cache
    now = time.time()
    if not force and _weather_cache["data"] and (now - _weather_cache["ts"]) < 900:
        return _weather_cache["data"]

    now_dt = datetime.now()
    cur_day_idx = now_dt.weekday()
    cur_day_name = INDONESIAN_DAYS[cur_day_idx]
    cur_month_name = INDONESIAN_MONTHS[now_dt.month]
    cur_week_num = now_dt.isocalendar()[1]
    cur_full_date = f"{cur_day_name}, {now_dt.day} {cur_month_name} {now_dt.year}"
    cur_date_info = {
        "day_name": cur_day_name,
        "day": now_dt.day,
        "month_name": cur_month_name,
        "month": now_dt.month,
        "year": now_dt.year,
        "week_number": cur_week_num,
        "week_str": f"Minggu ke-{cur_week_num}",
        "full_date": cur_full_date,
        "full_display": f"{cur_full_date} • Minggu ke-{cur_week_num}"
    }

    is_day = 5 <= now_dt.hour < 18
    provider = "BMKG Indonesia (Desa Kaliagung, Sentolo)"
    provider_badge = "BMKG RESMI"

    # 1. Fetch BMKG Data
    bmkg_raw = None
    try:
        bmkg_raw = fetch_bmkg_weather_data("34.01.06.2006")
    except Exception as e:
        pass

    # 2. Fetch Open-Meteo Data (16 days for 7-day, next week 14-day, and monthly solar stats)
    om_raw = None
    try:
        om_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,is_day,weather_code,cloud_cover"
            f"&hourly=direct_normal_irradiance"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_sum,precipitation_probability_max,sunshine_duration"
            f"&timezone=Asia%2FJakarta&forecast_days=16"
        )
        req_om = urllib.request.Request(om_url, headers={"User-Agent": "WestproSolar/1.0"})
        with urllib.request.urlopen(req_om, timeout=5.5) as resp:
            om_raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    # Extract Irradiance & Sun times from Open-Meteo
    est_kwh = 5.2
    sunrise_str = "05:17"
    sunset_str = "17:33"
    if om_raw:
        hourly = om_raw.get("hourly", {})
        irradiance_vals = hourly.get("direct_normal_irradiance", [])
        if irradiance_vals:
            est_kwh = round(min(8.5, max(3.0, sum(irradiance_vals[:24]) * 0.0012)), 1)
        daily_om = om_raw.get("daily", {})
        if daily_om.get("sunrise"):
            sunrise_str = daily_om["sunrise"][0][-5:]
        if daily_om.get("sunset"):
            sunset_str = daily_om["sunset"][0][-5:]

    # Parse BMKG if available
    cur_temp = 24.0
    cur_humidity = 85
    cur_cloud = 50
    cur_cond = "Cerah Berawan"
    cur_icon = "⛅" if is_day else "🌙"
    forecast_3d = []

    if bmkg_raw and bmkg_raw.get("data"):
        try:
            cuaca_days = bmkg_raw["data"][0].get("cuaca", [])
            # Find closest slot to now
            closest_slot = None
            min_diff = 9999999
            for day_list in cuaca_days:
                for slot in day_list:
                    try:
                        slot_dt = datetime.strptime(slot["local_datetime"], "%Y-%m-%d %H:%M:%S")
                        diff = abs((slot_dt - now_dt).total_seconds())
                        if diff < min_diff:
                            min_diff = diff
                            closest_slot = slot
                    except Exception:
                        pass

            cur_wind_speed = 8.5
            cur_wind_dir = "Barat Daya (SW)"
            cur_visibility = "> 10 km"
            hourly_forecast = []

            if closest_slot:
                cur_temp = float(closest_slot.get("t", 24))
                cur_humidity = int(closest_slot.get("hu", 85))
                cur_cloud = int(closest_slot.get("tcc", 50))
                if closest_slot.get("ws"):
                    try:
                        cur_wind_speed = float(closest_slot["ws"])
                    except Exception:
                        pass
                if closest_slot.get("wd"):
                    cur_wind_dir = closest_slot["wd"]
                if closest_slot.get("vs_text"):
                    cur_visibility = closest_slot["vs_text"]
                desc = closest_slot.get("weather_desc", "Cerah Berawan")
                cur_cond = desc
                cur_icon = BMKG_EMOJI_MAP.get(desc.lower(), "⛅")
                if not is_day and "cerah" in desc.lower():
                    cur_icon = "🌙"
                    cur_cond = f"Malam {desc}"

            # Hourly Forecast from BMKG slots
            all_bmkg_slots = []
            for d_list in cuaca_days:
                all_bmkg_slots.extend(d_list)
            
            for s in all_bmkg_slots:
                try:
                    s_dt = datetime.strptime(s["local_datetime"], "%Y-%m-%d %H:%M:%S")
                    if s_dt >= now_dt - timedelta(hours=2.5) and len(hourly_forecast) < 7:
                        s_is_day = 5 <= s_dt.hour < 18
                        s_desc = s.get("weather_desc", "Cerah Berawan")
                        s_icon = BMKG_EMOJI_MAP.get(s_desc.lower(), "⛅")
                        if not s_is_day and "cerah" in s_desc.lower():
                            s_icon = "🌙"
                        hourly_forecast.append({
                            "time": s_dt.strftime("%H:%M"),
                            "temp": round(float(s.get("t", cur_temp))),
                            "condition": s_desc,
                            "icon": s_icon,
                            "humidity": int(s.get("hu", cur_humidity)),
                            "cloud": int(s.get("tcc", cur_cloud)),
                            "pop": 10 if "cerah" in s_desc.lower() else (45 if "hujan" in s_desc.lower() else 20),
                            "is_now": abs((s_dt - now_dt).total_seconds()) < 5400
                        })
                except Exception:
                    pass

            # 3-Day Forecast from BMKG
            days_labels = ["Hari Ini", "Besok", "Lusa"]
            for idx in range(min(3, len(cuaca_days))):
                day_list = cuaca_days[idx]
                temps = [s["t"] for s in day_list if "t" in s]
                t_min = min(temps) if temps else 22
                t_max = max(temps) if temps else 32
                descs = [s.get("weather_desc", "Cerah Berawan") for s in day_list if "weather_desc" in s]
                f_desc = descs[len(descs)//2] if descs else "Cerah Berawan"
                f_icon = BMKG_EMOJI_MAP.get(f_desc.lower(), "⛅")
                
                date_str = day_list[0]["local_datetime"][:10] if day_list else now_dt.strftime("%Y-%m-%d")
                try:
                    f_dt = datetime.strptime(date_str, "%Y-%m-%d")
                    f_day_name = INDONESIAN_DAYS[f_dt.weekday()]
                    f_short = f"{f_day_name[:3]}, {f_dt.day} {INDONESIAN_MONTHS_SHORT[f_dt.month]}"
                    f_full = f"{f_day_name}, {f_dt.day} {INDONESIAN_MONTHS[f_dt.month]} {f_dt.year}"
                except Exception:
                    f_day_name = days_labels[idx]
                    f_short = date_str
                    f_full = date_str

                forecast_3d.append({
                    "label": days_labels[idx],
                    "day_name": f_day_name,
                    "date": date_str,
                    "date_short": f_short,
                    "full_date": f_full,
                    "temp_max": t_max,
                    "temp_min": t_min,
                    "condition": f_desc,
                    "icon": f_icon,
                    "precipitation_prob": 20 if idx == 0 else 35,
                    "precipitation_mm": 0.0,
                    "uv": 9.4
                })
        except Exception:
            bmkg_raw = None

    # Fallback to Open-Meteo for 3-day if BMKG is unavailable
    if not forecast_3d and om_raw:
        provider = "Google / Open-Meteo Satellite"
        provider_badge = "GLOBAL METEO"
        cur_code = om_raw.get("current", {}).get("weather_code", 0)
        cur_cond, cur_icon = CODE_MAP.get(cur_code, ("Berawan", "⛅"))
        if not is_day and cur_code in (0, 1):
            cur_icon = "🌙"
            cur_cond = "Malam Cerah"
        cur_temp = round(om_raw.get("current", {}).get("temperature_2m", 24.0), 1)
        cur_humidity = om_raw.get("current", {}).get("relative_humidity_2m", 78)
        cur_cloud = om_raw.get("current", {}).get("cloud_cover", 40)

        daily_om = om_raw.get("daily", {})
        time_list = daily_om.get("time", [])
        days_labels = ["Hari Ini", "Besok", "Lusa"]
        for i in range(min(3, len(time_list))):
            c = daily_om.get("weather_code", [0])[i] if i < len(daily_om.get("weather_code", [])) else 0
            cond, icon = CODE_MAP.get(c, ("Berawan", "⛅"))
            t_max = round(daily_om.get("temperature_2m_max", [33])[i])
            t_min = round(daily_om.get("temperature_2m_min", [22])[i])
            date_raw = time_list[i]
            try:
                f_dt = datetime.strptime(date_raw, "%Y-%m-%d")
                f_day_name = INDONESIAN_DAYS[f_dt.weekday()]
                f_short = f"{f_day_name[:3]}, {f_dt.day} {INDONESIAN_MONTHS_SHORT[f_dt.month]}"
                f_full = f"{f_day_name}, {f_dt.day} {INDONESIAN_MONTHS[f_dt.month]} {f_dt.year}"
            except Exception:
                f_day_name = days_labels[i]
                f_short = date_raw
                f_full = date_raw

            forecast_3d.append({
                "label": days_labels[i],
                "day_name": f_day_name,
                "date": date_raw,
                "date_short": f_short,
                "full_date": f_full,
                "temp_max": t_max,
                "temp_min": t_min,
                "condition": cond,
                "icon": icon,
                "precipitation_prob": 25,
                "precipitation_mm": 0.0,
                "uv": 9.4
            })

    # Build 7-day and 14-day forecasts (Next Week)
    forecast_7d = []
    forecast_14d = []
    if om_raw and om_raw.get("daily"):
        daily_om = om_raw["daily"]
        time_list = daily_om.get("time", [])
        precips = daily_om.get("precipitation_sum", [0] * len(time_list))
        probs = daily_om.get("precipitation_probability_max", [0] * len(time_list))
        codes = daily_om.get("weather_code", [0] * len(time_list))
        t_maxs = daily_om.get("temperature_2m_max", [33] * len(time_list))
        t_mins = daily_om.get("temperature_2m_min", [23] * len(time_list))
        uvs = daily_om.get("uv_index_max", [9.0] * len(time_list))

        days_prefixes = ["Hari Ini", "Besok", "Lusa"]
        for i in range(min(14, len(time_list))):
            date_raw = time_list[i]
            try:
                f_dt = datetime.strptime(date_raw, "%Y-%m-%d")
                f_day_name = INDONESIAN_DAYS[f_dt.weekday()]
                f_short = f"{f_day_name[:3]}, {f_dt.day} {INDONESIAN_MONTHS_SHORT[f_dt.month]}"
                f_full = f"{f_day_name}, {f_dt.day} {INDONESIAN_MONTHS[f_dt.month]} {f_dt.year}"
            except Exception:
                f_day_name = f"Hari +{i}"
                f_short = date_raw
                f_full = date_raw

            c = codes[i]
            cond, icon = CODE_MAP.get(c, ("Berawan", "⛅"))
            p_sum = round(precips[i], 1) if i < len(precips) else 0.0
            p_prob = int(round(probs[i])) if i < len(probs) else 20

            # Override first 3 days with BMKG if available
            if i < len(forecast_3d) and bmkg_raw:
                cond = forecast_3d[i].get("condition", cond)
                icon = forecast_3d[i].get("icon", icon)
                t_max = forecast_3d[i].get("temp_max", round(t_maxs[i]))
                t_min = forecast_3d[i].get("temp_min", round(t_mins[i]))
            else:
                t_max = round(t_maxs[i])
                t_min = round(t_mins[i])

            item = {
                "day_index": i,
                "label": days_prefixes[i] if i < 3 else (f"Mg Depan ({f_day_name[:3]})" if i >= 7 else f_day_name),
                "is_next_week": i >= 7,
                "day_name": f_day_name,
                "date": date_raw,
                "date_short": f_short,
                "full_date": f_full,
                "temp_max": t_max,
                "temp_min": t_min,
                "precipitation_mm": p_sum,
                "precipitation_prob": p_prob,
                "condition": cond,
                "icon": icon,
                "uv": round(uvs[i], 1) if i < len(uvs) else 9.0
            }
            if i < 7:
                forecast_7d.append(item)
            forecast_14d.append(item)

    # Lengkapi peluang hujan / curah hujan / UV 3 hari (sebelumnya angka tetap) dari Open-Meteo
    for i, f in enumerate(forecast_3d):
        if i < len(forecast_14d):
            for k in ("precipitation_prob", "precipitation_mm", "uv"):
                f[k] = forecast_14d[i][k]

    # Prospek iklim bulanan (klimatologi nyata) + dipadukan dengan prakiraan 16 hari
    cur_month_name = INDONESIAN_MONTHS[now_dt.month]
    cur_year = now_dt.year
    climate_outlook = get_climate_outlook(lat, lon)
    cur_clim = climate_outlook[0] if climate_outlook else None
    days_in_cur_month = calendar.monthrange(cur_year, now_dt.month)[1]
    if cur_clim:
        monthly_outlook = {
            "month_name": f"{cur_month_name} {cur_year}",
            "season_phase": cur_clim["season"],
            "avg_temp_day": f"{cur_clim['avg_temp_max']}°C",
            "avg_temp_night": f"{cur_clim['avg_temp_min']}°C",
            "avg_peak_sun_hours": f"{cur_clim['peak_sun_hours']} Jam Puncak / Hari",
            "monthly_solar_potential_kwh": f"{round(est_kwh * days_in_cur_month, 1)} kWh",
            "rainy_days_prob": f"~{cur_clim['rainy_days']} Hari hujan ({cur_clim['rain_mm']} mm / bulan)",
            "sunny_days_prob": f"~{max(0, days_in_cur_month - cur_clim['rainy_days'])} Hari tanpa hujan berarti",
            "recommendation": f"Rata-rata {cur_clim['years_sampled']} tahun terakhir untuk {cur_month_name}: {cur_clim['season']}. "
                              f"Saran tanam: {cur_clim['planting_hint']}.",
        }
    else:
        monthly_outlook = {
            "month_name": f"{cur_month_name} {cur_year}",
            "season_phase": "Data iklim belum tersedia",
            "avg_temp_day": "-", "avg_temp_night": "-", "avg_peak_sun_hours": "-",
            "monthly_solar_potential_kwh": f"{round(est_kwh * days_in_cur_month, 1)} kWh",
            "rainy_days_prob": "-", "sunny_days_prob": "-",
            "recommendation": "Arsip iklim Open-Meteo tidak bisa dijangkau saat ini. Coba lagi beberapa saat.",
        }

    # Ringkasan 14 hari ke depan untuk keputusan tanam
    planting_advice = None
    if forecast_14d:
        rain_14 = round(sum(d.get("precipitation_mm", 0) for d in forecast_14d), 1)
        wet_days = sum(1 for d in forecast_14d if d.get("precipitation_mm", 0) >= 1.0 or d.get("precipitation_prob", 0) >= 60)
        avg_max = round(sum(d["temp_max"] for d in forecast_14d) / len(forecast_14d), 1)
        avg_min = round(sum(d["temp_min"] for d in forecast_14d) / len(forecast_14d), 1)
        if rain_14 >= 70:
            verdict = "Basah: tanah cukup air, cocok mulai semai/tanam padi & palawija; pastikan drainase."
        elif rain_14 >= 25:
            verdict = "Lembap sedang: waktu baik tanam jagung, cabai, sayuran; tetap pantau hujan."
        else:
            verdict = "Kering: tunda tanam benih yang butuh air banyak atau siapkan irigasi/mulsa."
        planting_advice = {
            "rain_14d_mm": rain_14,
            "wet_days_14d": wet_days,
            "avg_temp_max_14d": avg_max,
            "avg_temp_min_14d": avg_min,
            "verdict": verdict,
        }

    cur_uv = forecast_3d[0].get("uv", 9.4) if forecast_3d else 9.4
    cur_precip = forecast_3d[0].get("precipitation_prob", 9) if forecast_3d else 9
    cur_feels_like = round(cur_temp + (cur_humidity / 100.0) * 3.5, 1)

    if not hourly_forecast:
        base_h = (now_dt.hour // 3) * 3
        for offset in range(6):
            target_h = (base_h + offset * 3) % 24
            slot_is_day = 5 <= target_h < 18
            hourly_forecast.append({
                "time": f"{target_h:02d}:00",
                "temp": round(cur_temp + (2 if slot_is_day and 10 <= target_h <= 14 else -3)),
                "condition": cur_cond,
                "icon": cur_icon if slot_is_day else "🌙",
                "humidity": cur_humidity,
                "cloud": cur_cloud,
                "pop": cur_precip,
                "is_now": offset == 0
            })

    res = {
        "location": "Jetak, Kaliagung, Sentolo, Kulon Progo",
        "provider": provider,
        "provider_badge": provider_badge,
        "updated_at": now_dt.strftime("%H:%M:%S WIB"),
        "auto_sync_interval": "15 menit (BMKG) • 1 menit (Frontend)",
        "current_date": cur_date_info,
        "current": {
            "temp": cur_temp,
            "humidity": cur_humidity,
            "cloud_cover": cur_cloud,
            "is_day": is_day,
            "condition": cur_cond,
            "icon": cur_icon,
            "wind_speed": cur_wind_speed,
            "wind_direction": cur_wind_dir,
            "visibility": cur_visibility,
            "uv_index": cur_uv,
            "uv_level": "Sangat Tinggi" if cur_uv >= 8 else ("Tinggi" if cur_uv >= 6 else ("Sedang" if cur_uv >= 3 else "Rendah")),
            "precipitation_prob": cur_precip,
            "feels_like": cur_feels_like,
            "pressure": "1011 hPa"
        },
        "hourly_forecast": hourly_forecast[:7],
        "forecast": forecast_3d,
        "forecast_3d": forecast_3d,
        "forecast_7d": forecast_7d,
        "forecast_14d": forecast_14d,
        "monthly_outlook": monthly_outlook,
        "climate_outlook": climate_outlook,
        "planting_advice": planting_advice,
        "sunrise": sunrise_str,
        "sunset": sunset_str,
        "peak_window": "09:30 - 14:00 WIB",
        "est_harvest_kwh": est_kwh,
        "sources_ok": {"bmkg": bool(bmkg_raw), "open_meteo": bool(om_raw), "climate": bool(climate_outlook)},
    }

    # Bila Open-Meteo gagal, jangan simpan hasil kosong 15 menit: coba lagi dalam ~1 menit
    _weather_cache = {"ts": now if om_raw else now - 840, "data": res}
    return res


def generate_smart_solar_advisory(weather: dict, state: dict) -> dict:
    """
    Menghasilkan rekomendasi cerdas penggunaan listrik rumah berdasarkan
    kondisi cuaca saat ini, jam waktu, daya baterai, dan beban rumah aktif.
    """
    soc = state.get("soc", 80)
    batt_v = state.get("battery_voltage", 48.0)
    load_w = state.get("ac_output_watt", 80)
    pv_w = state.get("pv_power_watt", 0)
    is_day = weather.get("current", {}).get("is_day", False)
    cloud = weather.get("current", {}).get("cloud_cover", 50)
    est_kwh = weather.get("est_harvest_kwh", 5.4)

    # 48V 100Ah = 4800Wh nominal, safe 80% usable DoD = 3840Wh
    usable_wh = (max(0, soc - 15) / 100.0) * 4800.0
    runtime_hours = round(usable_wh / max(25, load_w), 1) if load_w > 0 else 48.0

    # ESG Metrics & Savings
    co2_kg = round(est_kwh * 0.85, 2)
    trees = round(co2_kg / 20.0, 1)
    savings_rp = int(est_kwh * 1444.7) # Rp 1.444,70 / kWh (Tarif Golongan R-1)

    now_hour = datetime.now().hour

    if not is_day or now_hour >= 18 or now_hour < 6:
        status_tag = "🌙 Mode Malam (Baterai Aktif)"
        advice_title = "Cadangan Baterai Sangat Aman"
        advice_desc = (
            f"Baterai LiFePO4 pada {soc}% ({batt_v}V). Dengan beban rumah {load_w}W saat ini, "
            f"estimasi cadangan mampu bertahan hingga ~{runtime_hours} jam ke depan (aman melewati subuh tanpa PLN). "
            f"Hindari menyalakan pemanas air/oven untuk menjaga keawetan siklus baterai."
        )
        range_highlight = "05:35 WIB (Matahari Terbit Esok)"
    elif 6 <= now_hour < 10:
        status_tag = "🌅 Pagi Hari (Pemanasan PV)"
        advice_title = "Persiapan Jam Panen Surya"
        advice_desc = (
            f"Produksi surya mulai aktif ({pv_w}W). Puncak radiasi matahari hari ini diperkirakan "
            f"pukul 10:00 - 14:30 WIB (Est. potensi ~{est_kwh} kWh). Tunda penggunaan mesin cuci/pompa air "
            f"hingga pukul 10:00 agar disuplai 100% langsung dari matahari."
        )
        range_highlight = "10:00 - 14:30 WIB (Jam Terik Puncak)"
    elif 10 <= now_hour <= 14:
        if cloud > 70:
            status_tag = "⛅ Siang Berawan"
            advice_title = "Produksi PV Terhalang Awan"
            advice_desc = (
                f"Tutupan awan mencapai {cloud}%. Produksi PV berfluktuasi ({pv_w}W). Baterai berada di {soc}%. "
                f"Gunakan listrik secukupnya agar baterai tetap memiliki daya simpan optimal menjelang sore."
            )
            range_highlight = "Fluktuasi Terik s/d 14:30 WIB"
        else:
            status_tag = "☀️ Jam Terik Maksimal (Peak Solar)"
            advice_title = "Waktu Paling Hemat Menyalakan Beban"
            advice_desc = (
                f"Radiasi surya sedang di titik optimal! Produksi PV melimpah ({pv_w}W). Waktu terbaik untuk "
                f"menyalakan AC utama, mesin cuci, atau pompa air langsung dari tenaga surya tanpa menguras baterai!"
            )
            range_highlight = "10:00 - 14:30 WIB (Puncak Radiasi)"
    else: # 15 - 18
        status_tag = "🌇 Sore Hari (Penurunan Radiasi)"
        advice_title = "Persiapan Siklus Malam Hari"
        advice_desc = (
            f"Intensitas matahari mulai menurun ({pv_w}W). Pastikan baterai sudah terisi di atas 80% "
            f"(saat ini {soc}%). Kurangi beban berat agar cadangan baterai mencukupi kebutuhan malam hari."
        )
        range_highlight = "17:45 WIB (Matahari Terbenam)"

    return {
        "status_tag": status_tag,
        "advice_title": advice_title,
        "advice_desc": advice_desc,
        "range_highlight": range_highlight,
        "runtime_hours": runtime_hours,
        "co2_kg": co2_kg,
        "trees": trees,
        "savings_rp": savings_rp,
        "est_kwh": est_kwh
    }


if __name__ == "__main__":
    w = get_solar_weather_data()
    dummy_state = {"soc": 83, "battery_voltage": 48.5, "ac_output_watt": 80, "pv_power_watt": 0}
    a = generate_smart_solar_advisory(w, dummy_state)
    print("Weather:", json.dumps(w, indent=2))
    print("Advisory:", json.dumps(a, indent=2))
