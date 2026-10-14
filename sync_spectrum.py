import urllib.request
import json
import time
import os
import re

def _get_spectrum_credentials():
    email = os.environ.get('SPECTRUM_EMAIL')
    password = os.environ.get('SPECTRUM_PASSWORD')
    if not email or not password:
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        if os.path.exists(env_file):
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('SPECTRUM_EMAIL='):
                        email = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('SPECTRUM_PASSWORD='):
                        password = line.split('=', 1)[1].strip().strip('"').strip("'")
    return email or 'aris.aris@spesolution.com', password or ''

API_BASE = 'https://api.lifeinspectrum.com/v1'
# Kandidat endpoint daftar proyek milik akun (dicoba berurutan sampai ada yang mengembalikan data).
PROJECT_LIST_ENDPOINTS = ('/project', '/project/my', '/project/mine', '/project/list')


def _oid(v):
    if isinstance(v, dict):
        return v.get('$oid') or v.get('_id') or v.get('id') or ''
    return v if isinstance(v, str) else ''


def parse_project_ids(raw) -> list:
    """'id1, id2;id3' -> ['id1','id2','id3'] (tanpa duplikat, urutan terjaga)."""
    out = []
    for x in re.split(r'[,;\s]+', raw or ''):
        if x and x not in out:
            out.append(x)
    return out


def discover_projects(auth_headers: dict) -> list:
    """Ambil proyek yang diikuti akun login. Mengembalikan [{'id','name','code'}]."""
    for ep in PROJECT_LIST_ENDPOINTS:
        try:
            req = urllib.request.Request(API_BASE + ep, headers=auth_headers)
            with urllib.request.urlopen(req, timeout=12.0) as res:
                data = json.loads(res.read().decode('utf-8'))
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get('data') or data.get('projects') or data.get('items') or []
        found = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            pid = _oid(item.get('_id') or item.get('id'))
            if pid:
                found.append({'id': pid, 'name': item.get('name') or '', 'code': item.get('code') or ''})
        if found:
            return found
    return []


def sync_spectrum(email: str = None, password: str = None, project_id: str = None, member_name: str = None, output_path: str = None):
    sign_url = 'https://api.lifeinspectrum.com/v1/basic-auth/sign'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://lifeinspectrum.com/',
        'Origin': 'https://lifeinspectrum.com'
    }
    if not email or not password:
        def_email, def_password = _get_spectrum_credentials()
        email = email or def_email
        password = password or def_password

    data = json.dumps({'email': email, 'password': password}).encode('utf-8')
    req = urllib.request.Request(sign_url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=12.0) as resp:
        id_token = json.loads(resp.read().decode('utf-8'))['id_token']

    auth_headers = {
        'Authorization': f'Bearer {id_token}',
        'User-Agent': headers['User-Agent'],
        'Referer': headers['Referer'],
        'Origin': headers['Origin'],
        'Accept': 'application/json'
    }

    # 0. Tentukan proyek: isian manual (boleh banyak, pisah koma) atau otomatis dari akun SPEctrum
    projects = [{'id': pid, 'name': '', 'code': ''} for pid in parse_project_ids(project_id)]
    if not projects:
        projects = discover_projects(auth_headers)
    if not projects:
        raise RuntimeError('Proyek SPEctrum tidak terdeteksi otomatis. Isi Project ID secara manual '
                           '(boleh lebih dari satu, pisahkan dengan koma).')

    # Kalender bulan berjalan
    r_cal = urllib.request.Request(f"{API_BASE}/calendar?year-month={time.strftime('%Y-%m')}", headers=auth_headers)
    with urllib.request.urlopen(r_cal, timeout=12.0) as res:
        calendar_events = json.loads(res.read().decode('utf-8'))

    sprint_tasks = []
    project_summaries = []
    errors = []
    first_sprint = None
    for proj in projects:
        pid = proj['id']
        try:
            r_sprint = urllib.request.Request(f'{API_BASE}/project/sprint?projectId={pid}&limit=5', headers=auth_headers)
            with urllib.request.urlopen(r_sprint, timeout=12.0) as res:
                sprints = json.loads(res.read().decode('utf-8'))
            if not sprints:
                continue
            active_sprint = next((sp for sp in sprints if str(sp.get('status', '')).lower() in ('active', 'running', 'berjalan')), sprints[0])
        except Exception as e:
            errors.append(f'{pid}: {e}')
            continue
        if first_sprint is None:
            first_sprint = (proj, active_sprint)

        issue_ids = [i for i in (_oid(x) for x in active_sprint.get('issue_id', [])) if i]
        print(f"Fetching {len(issue_ids)} issues in {active_sprint.get('name')} (project {pid})...")
        count = 0
        for i_id in issue_ids:
            try:
                r = urllib.request.Request(f'{API_BASE}/project/issue/{i_id}', headers=auth_headers)
                with urllib.request.urlopen(r, timeout=12.0) as res:
                    issue_arr = json.loads(res.read().decode('utf-8'))
                if issue_arr:
                    issue = issue_arr[0]
                    emp = issue.get('employee') or {}
                    cre = issue.get('created_by') or {}
                    sprint_tasks.append({
                        'id': issue.get('_id'),
                        'name': issue.get('name'),
                        'status': issue.get('status', 'To-do-list'),
                        'status_done': issue.get('status_done', False),
                        'type': issue.get('type', 'task'),
                        'code': issue.get('code', ''),
                        'created_by': cre.get('name', ''),
                        'assignee': emp.get('name') or cre.get('name') or 'Unassigned',
                        'assignee_email': emp.get('email') or cre.get('email') or '',
                        'assignee_job': emp.get('job') if emp else 'QA / Dev',
                        'weight': issue.get('weight', 0),
                        'acceptance_criteria': [ac.get('desc') for ac in issue.get('acceptanceCriteria', []) if ac.get('desc')],
                        'backlog_id': issue.get('backlog_id'),
                        'project_id': pid,
                        'project_name': proj['name'] or pid,
                        'sprint_name': active_sprint.get('name'),
                    })
                    count += 1
                time.sleep(0.04)
            except Exception as e:
                print(f'Err fetching {i_id}: {e}')
        project_summaries.append({
            'id': pid, 'name': proj['name'] or pid, 'code': proj['code'],
            'sprint': active_sprint.get('name'), 'start_date': active_sprint.get('start_date'),
            'end_date': active_sprint.get('end_date'), 'status': active_sprint.get('status'),
            'task_count': count,
        })

    if first_sprint is None:
        raise RuntimeError('Tidak ada sprint yang bisa diambil. ' + ('; '.join(errors) if errors else 'Proyek belum punya sprint.'))
    proj_main, active_sprint = first_sprint
    proj_id = proj_main['id']

    user_name = member_name or (email.split('@')[0].split('.')[0].capitalize() if email else 'User')
    full_spectrum_summary = {
        'last_synced': time.strftime('%Y-%m-%d %H:%M:%S'),
        'user': {
            'name': user_name,
            'email': email or 'aris.aris@spesolution.com',
            'role': 'Quality Assurance (QA)' if 'aris' in (email or '').lower() else 'Member',
            'tribe': 'Ursa Major'
        },
        'active_project': {
            'id': proj_id,
            'name': proj_main['name'] or proj_id,
            'code': proj_main['code'],
            'tribe': 'Ursa Major'
        },
        'projects': project_summaries,
        'active_sprint': {
            'id': active_sprint.get('_id'),
            'name': active_sprint.get('name'),
            'description': active_sprint.get('description'),
            'start_date': active_sprint.get('start_date'),
            'end_date': active_sprint.get('end_date'),
            'status': active_sprint.get('status'),
            'total_tasks': len([t for t in sprint_tasks if t['project_id'] == proj_id]),
            'metrics': active_sprint.get('issue'),
            'summary': active_sprint.get('summary')
        },
        'calendar_events': calendar_events,
        'tasks': sprint_tasks
    }

    target_path = output_path or '/home/ares/Documents/myserver/spectrum_sprint_data.json'
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    with open(target_path, 'w', encoding='utf-8') as f:
        json.dump(full_spectrum_summary, f, indent=2)

    # Keep default global file updated for owner
    global_path = '/home/ares/Documents/myserver/spectrum_sprint_data.json'
    if target_path != global_path and ('spectrum_user_1.json' in target_path or (email and 'aris' in email.lower())):
        try:
            with open(global_path, 'w', encoding='utf-8') as f:
                json.dump(full_spectrum_summary, f, indent=2)
        except Exception:
            pass

    print(f"SUCCESS: Saved {len(sprint_tasks)} tasks & {len(calendar_events)} calendar events to {target_path}")
    return full_spectrum_summary

if __name__ == '__main__':
    sync_spectrum()
