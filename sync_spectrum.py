import urllib.request
import json
import time
import os

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

def sync_spectrum():
    sign_url = 'https://api.lifeinspectrum.com/v1/basic-auth/sign'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://lifeinspectrum.com/',
        'Origin': 'https://lifeinspectrum.com'
    }
    email, password = _get_spectrum_credentials()
    data = json.dumps({'email': email, 'password': password}).encode('utf-8')
    req = urllib.request.Request(sign_url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        id_token = json.loads(resp.read().decode('utf-8'))['id_token']

    auth_headers = {
        'Authorization': f'Bearer {id_token}',
        'User-Agent': headers['User-Agent'],
        'Referer': headers['Referer'],
        'Origin': headers['Origin'],
        'Accept': 'application/json'
    }

    # 1. Get Sprint info
    r_sprint = urllib.request.Request('https://api.lifeinspectrum.com/v1/project/sprint?projectId=64881aa413013&limit=5', headers=auth_headers)
    with urllib.request.urlopen(r_sprint) as res:
        sprints = json.loads(res.read().decode('utf-8'))
    active_sprint = sprints[0]

    # 2. Get Calendar
    r_cal = urllib.request.Request('https://api.lifeinspectrum.com/v1/calendar?year-month=2026-10', headers=auth_headers)
    with urllib.request.urlopen(r_cal) as res:
        calendar_events = json.loads(res.read().decode('utf-8'))

    raw_issues = active_sprint.get('issue_id', [])
    issue_ids = []
    for x in raw_issues:
        if isinstance(x, dict) and '$oid' in x:
            issue_ids.append(x['$oid'])
        elif isinstance(x, str):
            issue_ids.append(x)

    print(f"Fetching {len(issue_ids)} issues in Sprint 6 - 2026...")
    sprint_tasks = []

    for i_id in issue_ids:
        try:
            url = f'https://api.lifeinspectrum.com/v1/project/issue/{i_id}'
            r = urllib.request.Request(url, headers=auth_headers)
            with urllib.request.urlopen(r) as res:
                issue_arr = json.loads(res.read().decode('utf-8'))
                if issue_arr:
                    issue = issue_arr[0]
                    emp_name = issue.get('employee', {}).get('name') if issue.get('employee') else (issue.get('created_by', {}).get('name') if issue.get('created_by') else 'Unassigned')
                    emp_email = issue.get('employee', {}).get('email') if issue.get('employee') else (issue.get('created_by', {}).get('email') if issue.get('created_by') else '')
                    emp_job = issue.get('employee', {}).get('job') if issue.get('employee') else 'QA / Dev'
                    sprint_tasks.append({
                        'id': issue.get('_id'),
                        'name': issue.get('name'),
                        'status': issue.get('status', 'To-do-list'),
                        'status_done': issue.get('status_done', False),
                        'type': issue.get('type', 'task'),
                        'code': issue.get('code', ''),
                        'created_by': issue.get('created_by', {}).get('name', ''),
                        'assignee': emp_name,
                        'assignee_email': emp_email,
                        'assignee_job': emp_job,
                        'weight': issue.get('weight', 0),
                        'acceptance_criteria': [ac.get('desc') for ac in issue.get('acceptanceCriteria', []) if ac.get('desc')],
                        'backlog_id': issue.get('backlog_id')
                    })
            time.sleep(0.04)
        except Exception as e:
            print(f'Err fetching {i_id}: {e}')

    full_spectrum_summary = {
        'last_synced': time.strftime('%Y-%m-%d %H:%M:%S'),
        'user': {
            'name': 'Aris',
            'email': 'aris.aris@spesolution.com',
            'role': 'Quality Assurance (QA)',
            'tribe': 'Ursa Major'
        },
        'active_project': {
            'id': '64881aa413013',
            'name': 'BNI YAP!',
            'code': 'BY',
            'tribe': 'Ursa Major'
        },
        'active_sprint': {
            'id': active_sprint.get('_id'),
            'name': active_sprint.get('name'),
            'description': active_sprint.get('description'),
            'start_date': active_sprint.get('start_date'),
            'end_date': active_sprint.get('end_date'),
            'status': active_sprint.get('status'),
            'total_tasks': len(sprint_tasks),
            'metrics': active_sprint.get('issue'),
            'summary': active_sprint.get('summary')
        },
        'calendar_events': calendar_events,
        'tasks': sprint_tasks
    }

    target_path = '/home/ares/Documents/myserver/spectrum_sprint_data.json'
    with open(target_path, 'w') as f:
        json.dump(full_spectrum_summary, f, indent=2)

    print(f"SUCCESS: Saved {len(sprint_tasks)} tasks & {len(calendar_events)} calendar events to {target_path}")

if __name__ == '__main__':
    sync_spectrum()
