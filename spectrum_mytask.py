"""
SPEctrum MyTask Manager & Audit Engine
Mengelola pengisian jam kerja harian, rekomendasi AI proporsional (target >= 8 jam),
dan audit masa lalu (hingga 3 bulan / 90 hari) untuk lifeinspectrum.com.
"""

import urllib.request
import urllib.error
import json
import datetime
from concurrent.futures import ThreadPoolExecutor

API_BASE = 'https://api.lifeinspectrum.com/v1'
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Origin': 'https://lifeinspectrum.com',
    'Referer': 'https://lifeinspectrum.com/',
    'Accept': 'application/json, text/plain, */*'
}

class SpectrumMyTask:
    def __init__(self, email='aris.aris@spesolution.com', password='jetak1998@SPE3'):
        self.email = email
        self.password = password
        self.token = None
        self.user_name = None

    def authenticate(self):
        """Login ke basic-auth/sign dan dapatkan JWT token."""
        url = f"{API_BASE}/basic-auth/sign"
        payload = json.dumps({'email': self.email, 'password': self.password}).encode('utf-8')
        headers = dict(DEFAULT_HEADERS)
        headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                self.token = data.get('id_token')
                self.user_name = data.get('name')
                return self.token
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f"Gagal login ke SPEctrum ({e.code}): {err_body}")

    def _ensure_token(self):
        if not self.token:
            self.authenticate()
        return self.token

    def _make_auth_headers(self):
        token = self._ensure_token()
        headers = dict(DEFAULT_HEADERS)
        headers['Authorization'] = f'Bearer {token}'
        return headers

    def get_day_data(self, date_str):
        """Mengambil data MyTask untuk tanggal YYYY-MM-DD."""
        url = f"{API_BASE}/mytask?offset=0&limit=0&date={date_str}"
        headers = self._make_auth_headers()
        req = urllib.request.Request(url, headers=headers, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token expired, coba login ulang sekali
                self.authenticate()
                headers = self._make_auth_headers()
                req = urllib.request.Request(url, headers=headers, method='GET')
                with urllib.request.urlopen(req, timeout=12) as resp2:
                    return json.loads(resp2.read().decode('utf-8'))
            raise

    def analyze_day(self, date_str=None):
        """
        Menganalisa rincian jam kerja untuk tanggal tertentu (default: hari ini).
        Memisahkan Teams meeting (read-only) dan task backlog (editable).
        Menghitung proporsi AI agar total durasi harian >= 8 jam (480 menit).
        """
        if not date_str:
            date_str = datetime.date.today().strftime('%Y-%m-%d')

        data = self.get_day_data(date_str)
        if not data:
            return {"date": date_str, "error": "Tidak ada data dari SPEctrum"}

        total_hour = data.get('TotalDuration', {}).get('Hour', 0)
        total_minute = data.get('TotalDuration', {}).get('Minute', 0)
        current_total_minutes = total_hour * 60 + total_minute

        meetings = []
        backlog_tasks = []
        meeting_minutes = 0

        projects = data.get('Projects', [])
        for p in projects:
            p_name = p.get('ProjectName', '')
            for t in p.get('Tasks', []):
                dur = t.get('duration') or {}
                h = dur.get('hour', 0)
                m = dur.get('minute', 0)
                t_minutes = h * 60 + m
                p_id = t.get('project_id', '')
                t_type = t.get('task_type', '')

                if p_id == 'other' or t_type == 'meeting' or 'meet' in t_type.lower() or 'past event' in str(t.get('status','')).lower():
                    meeting_minutes += t_minutes
                    meetings.append({
                        'id': t.get('_id'),
                        'description': t.get('description', ''),
                        'hours': h,
                        'minutes': m,
                        'total_minutes': t_minutes,
                        'status': t.get('status', ''),
                        'read_only': True
                    })
                else:
                    backlog_tasks.append({
                        'id': t.get('_id'),
                        'project_id': p_id,
                        'project_name': p_name,
                        'description': t.get('description', ''),
                        'weight': t.get('weight', 1) or 1,
                        'status': t.get('status', 'TODO'),
                        'hours': h,
                        'minutes': m,
                        'total_minutes': t_minutes,
                        'read_only': False
                    })

        target_total_minutes = 480  # 8 jam
        remaining_needed = max(0, target_total_minutes - current_total_minutes)
        allocatable_backlog_minutes = max(0, target_total_minutes - meeting_minutes)

        today_date = datetime.date.today()
        is_past_date = False
        try:
            req_d = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            is_past_date = (req_d < today_date)
        except Exception:
            pass

        total_weight = sum(t['weight'] for t in backlog_tasks)
        recommendations = []
        for t in backlog_tasks:
            share = (t['weight'] / total_weight) if total_weight > 0 else (1 / len(backlog_tasks) if backlog_tasks else 0)
            target_t_min = round(allocatable_backlog_minutes * share)
            prop_h = target_t_min // 60
            prop_m = target_t_min % 60

            # Cerdas menentukan rekomendasi status (To Do List, On Progress, Task Finished)
            cur_stat = (t['status'] or '').strip()
            if cur_stat.lower() in ('task finished', 'finished', 'done'):
                proposed_status = 'Task Finished'
                status_rationale = 'Status saat ini sudah selesai'
            elif target_t_min == 0:
                proposed_status = 'To Do List'
                status_rationale = 'Tidak ada alokasi jam kerja'
            elif is_past_date:
                # Untuk tanggal masa lalu yang diisi jamnya:
                # Jika task durasi dominan (>= 3 jam) atau task tunggal -> Task Finished
                if target_t_min >= 180 or len(backlog_tasks) == 1:
                    proposed_status = 'Task Finished'
                    status_rationale = f'Selesai pada tanggal {date_str} ({prop_h}h {prop_m}m)'
                else:
                    proposed_status = 'On Progress'
                    status_rationale = f'Pekerjaan berjalan pada {date_str}'
            else:
                # Untuk hari ini:
                # Jika kata kunci mengindikasikan pembuatan/review tuntas dan durasi besar -> Task Finished
                desc_lower = t['description'].lower()
                if any(w in desc_lower for w in ['create tc', 'buat tc', 'selesai', 'finalize', 'submit']) and prop_h >= 3:
                    proposed_status = 'Task Finished'
                    status_rationale = 'Penyelesaian task target hari ini'
                else:
                    proposed_status = 'On Progress'
                    status_rationale = 'Sedang dikerjakan hari ini'

            recommendations.append({
                'id': t['id'],
                'project_name': t['project_name'],
                'description': t['description'],
                'weight': t['weight'],
                'current_hours': t['hours'],
                'current_minutes': t['minutes'],
                'current_formatted': f"{t['hours']}h {t['minutes']}m",
                'proposed_hours': prop_h,
                'proposed_minutes': prop_m,
                'proposed_formatted': f"{prop_h}h {prop_m}m",
                'current_status': t['status'],
                'proposed_status': proposed_status,
                'status_rationale': status_rationale
            })

        return {
            'date': date_str,
            'current_hours': total_hour,
            'current_minutes': total_minute,
            'current_total_minutes': current_total_minutes,
            'meets_8_hours': current_total_minutes >= target_total_minutes,
            'deficit_minutes': max(0, target_total_minutes - current_total_minutes),
            'meeting_count': len(meetings),
            'total_meeting_minutes': meeting_minutes,
            'total_meeting_formatted': f"{meeting_minutes // 60}h {meeting_minutes % 60}m",
            'meetings': meetings,
            'backlog_count': len(backlog_tasks),
            'recommendations': recommendations
        }

    def update_task_duration(self, task_id, description, status, hour, minute, weight=1):
        """PATCH /v1/mytask/update/{task_id} untuk mengubah durasi dan bobot."""
        url = f"{API_BASE}/mytask/update/{task_id}"
        payload = json.dumps({
            'description': description,
            'status': status,
            'duration': {'hour': int(hour), 'minute': int(minute)},
            'weight': int(weight)
        }).encode('utf-8')
        headers = self._make_auth_headers()
        headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=payload, headers=headers, method='PATCH')
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError as e:
            if e.code in (200, 204):
                return True
            raise

    def audit_past_days(self, days_back=90, max_workers=5):
        """
        Audit hari kerja (Senin - Jumat) ke belakang hingga `days_back` hari (default 90 hari / 3 bulan).
        Mengembalikan daftar hari yang kosong atau < 8 jam.
        """
        self._ensure_token()
        today = datetime.date.today()
        dates_to_check = []
        for i in range(1, days_back + 1):
            d = today - datetime.timedelta(days=i)
            # 0=Monday, 4=Friday
            if d.weekday() < 5:
                dates_to_check.append(d.strftime('%Y-%m-%d'))

        day_names = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

        def check_single_date(d_str):
            try:
                data = self.get_day_data(d_str)
                if not data:
                    return {'date': d_str, 'status': 'EMPTY', 'hours': 0, 'minutes': 0, 'total_minutes': 0, 'task_count': 0}
                tot = data.get('TotalDuration', {})
                h = tot.get('Hour', 0)
                m = tot.get('Minute', 0)
                tot_min = h * 60 + m
                p_count = sum(len(p.get('Tasks', [])) for p in data.get('Projects', []))
                
                dt = datetime.datetime.strptime(d_str, '%Y-%m-%d').date()
                day_name = day_names[dt.weekday()]

                if tot_min == 0:
                    status = 'EMPTY'
                elif tot_min < 480:
                    status = 'DEFICIENT'
                else:
                    status = 'OK'

                return {
                    'date': d_str,
                    'day_name': day_name,
                    'hours': h,
                    'minutes': m,
                    'total_minutes': tot_min,
                    'formatted': f"{h}h {m}m",
                    'deficit_minutes': max(0, 480 - tot_min),
                    'deficit_formatted': f"{max(0, 480 - tot_min)//60}h {max(0, 480 - tot_min)%60}m",
                    'status': status,
                    'task_count': p_count
                }
            except Exception as e:
                return {'date': d_str, 'day_name': '', 'status': 'ERROR', 'error': str(e), 'total_minutes': 0}

        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for r in executor.map(check_single_date, dates_to_check):
                results.append(r)

        # Urutkan berdasarkan tanggal terbaru ke terlama
        results.sort(key=lambda x: x['date'], reverse=True)

        ok_days = [r for r in results if r['status'] == 'OK']
        deficient_days = [r for r in results if r['status'] == 'DEFICIENT']
        empty_days = [r for r in results if r['status'] == 'EMPTY']

        return {
            'total_weekdays_checked': len(results),
            'ok_count': len(ok_days),
            'deficient_count': len(deficient_days),
            'empty_count': len(empty_days),
            'attention_count': len(deficient_days) + len(empty_days),
            'attention_list': deficient_days + empty_days,
            'all_days': results
        }

if __name__ == '__main__':
    print("Testing SpectrumMyTask...")
    sm = SpectrumMyTask()
    print("Logging in...")
    sm.authenticate()
    print(f"Logged in as: {sm.user_name}")

    print("\nAnalyzing today...")
    today_analysis = sm.analyze_day('2026-10-15')
    print(f"Total Logged: {today_analysis['current_hours']}h {today_analysis['current_minutes']}m (Meets 8h: {today_analysis['meets_8_hours']})")
    print(f"Meetings: {today_analysis['meeting_count']} ({today_analysis['total_meeting_formatted']})")
    print(f"Backlogs: {today_analysis['backlog_count']}")
    for r in today_analysis['recommendations']:
        print(f" - [{r['project_name']}] {r['description']}: current {r['current_formatted']} -> AI proposal: {r['proposed_formatted']}")

    print("\nAuditing past 90 days (3 months)...")
    t0 = datetime.datetime.now()
    audit = sm.audit_past_days(days_back=90, max_workers=8)
    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f"Audit completed in {dt:.2f}s!")
    print(f"Checked: {audit['total_weekdays_checked']} days | OK: {audit['ok_count']} | Deficient: {audit['deficient_count']} | Empty: {audit['empty_count']}")
    if audit['attention_list']:
        print("Sample Deficient Days:")
        for d in audit['attention_list'][:5]:
            print(f" - {d['date']} ({d['day_name']}): {d['formatted']} (Kurang {d['deficit_formatted']}) | {d['task_count']} task")
