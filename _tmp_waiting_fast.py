import json
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path('data') / 'claudetodo.db'
TODAY = '2026-03-27'
QUERY_TIMEOUT = 45
MAX_WORKERS = 3
WORKIQ_CMD = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'

LOAD_SQL = """
    SELECT id, title, description, key_people, source_type, source_id, created_at, status, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'waiting'
       OR (status = 'snoozed'
           AND waiting_activity LIKE '%out_of_office%'
           AND (json_extract(waiting_activity, '$.checked_at') IS NULL
                OR json_extract(waiting_activity, '$.checked_at') < datetime('now', '-20 hours')))
    ORDER BY id
"""


def parse_people(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return [{'name': str(raw), 'email': '', 'alternatives': []}]
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append({
                    'name': item.get('name', ''),
                    'email': item.get('email', '') or '',
                    'alternatives': item.get('alternatives', []) or [],
                })
            else:
                out.append({'name': str(item), 'email': '', 'alternatives': []})
        return out
    if isinstance(data, dict):
        return [{'name': data.get('name', ''), 'email': data.get('email', '') or '', 'alternatives': data.get('alternatives', []) or []}]
    return [{'name': str(data), 'email': '', 'alternatives': []}]


def compact(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def aliases_for_person(person):
    aliases = set()
    name = person.get('name', '') or ''
    email = person.get('email', '') or ''
    if name:
        aliases.add(name.lower())
        aliases.add(compact(name))
        for token in re.split(r'\s+', name.lower().strip()):
            if token:
                aliases.add(token)
    if email:
        aliases.add(email.lower())
        aliases.add(compact(email))
        local = email.split('@', 1)[0].lower()
        aliases.add(local)
        aliases.add(compact(local))
    for alt in person.get('alternatives', []) or []:
        if isinstance(alt, dict):
            alt_name = alt.get('name', '') or ''
            alt_email = alt.get('email', '') or ''
            if alt_name:
                aliases.add(alt_name.lower())
                aliases.add(compact(alt_name))
                for token in re.split(r'\s+', alt_name.lower().strip()):
                    if token:
                        aliases.add(token)
            if alt_email:
                aliases.add(alt_email.lower())
                aliases.add(compact(alt_email))
                local = alt_email.split('@', 1)[0].lower()
                aliases.add(local)
                aliases.add(compact(local))
    return {a for a in aliases if a}


def choose_target_person(source_type, source_id, people):
    if not people:
        return None
    if source_type in {'email', 'chat', 'meeting'} and source_id and '::' in source_id:
        parts = source_id.split('::')
        if len(parts) >= 2:
            origin = (parts[1] or '').strip().lower()
            origin_compact = compact(origin)
            best = None
            best_score = -1
            for person in people:
                score = 0
                person_name = (person.get('name', '') or '').lower()
                person_email = (person.get('email', '') or '').lower()
                if origin and person_email == origin:
                    score = 100
                elif origin and person_name == origin:
                    score = 95
                for alias in aliases_for_person(person):
                    alias_compact = compact(alias)
                    if alias == origin or alias_compact == origin_compact:
                        score = max(score, 90)
                    elif alias in origin or origin in alias:
                        score = max(score, 70)
                    elif alias_compact and (alias_compact in origin_compact or origin_compact in alias_compact):
                        score = max(score, 65)
                if score > best_score:
                    best = person
                    best_score = score
            if best is not None and best_score > 0:
                return best.get('name') or best.get('email') or origin
            return origin
    first = people[0]
    return first.get('name') or first.get('email') or None


def parse_created_at(value):
    if not value:
        return None
    value = value.strip()
    if '%' in value:
        return None
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def compute_start_date(created_at, source_type):
    dt = parse_created_at(created_at)
    if dt is None:
        dt = datetime.now(timezone.utc) - (timedelta(days=2) if source_type == 'manual' else timedelta(days=7))
    if source_type == 'manual':
        dt = dt - timedelta(days=2)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def extract_unanswered_questions(notes):
    if not notes:
        return []
    lines = notes.splitlines()
    questions = []
    for idx, line in enumerate(lines):
        if '@workiq' not in line.lower():
            continue
        next_line = lines[idx + 1] if idx + 1 < len(lines) else ''
        if not next_line.startswith('  →'):
            questions.append(line)
    return questions


def extract_json_object(text):
    text = (text or '').strip()
    start = text.find('{')
    if start == -1:
        raise ValueError('No JSON object found')
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    raise ValueError('Unterminated JSON object')


def run_workiq(prompt):
    proc = subprocess.run(
        [WORKIQ_CMD, 'ask', '-q', prompt],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=QUERY_TIMEOUT,
    )
    output = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
    lowered = output.lower()
    if proc.returncode != 0:
        raise RuntimeError(output.strip() or f'workiq exit {proc.returncode}')
    if 'permission denied and could not request permission from user' in lowered:
        raise RuntimeError(output.strip())
    if 'please sign in' in lowered or 'authentication required' in lowered:
        raise RuntimeError(output.strip())
    return output.strip()


def build_prompt(task, person, people, start_date, unanswered_questions):
    identity = person
    for p in people:
        display = p.get('name') or p.get('email')
        if display == person and p.get('email'):
            identity = f"{display} ({p['email']})"
            break
    question_block = ''
    if unanswered_questions:
        rendered = ' '.join(f"{idx + 1}) {q.replace('@WorkIQ', '').replace('@workiq', '').strip()}" for idx, q in enumerate(unanswered_questions))
        question_block = f' Additionally, answer these questions from the user notes: {rendered}'
    return (
        f'You are checking a waiting task in Microsoft 365. Today is {TODAY}. '
        f'Task title: {task["title"]}. '
        f'Task description: {task.get("description") or ""}. '
        f'Target person: {identity}. '
        f'Check whether this person is currently out of office in Teams or Outlook, has automatic replies, or recently sent an OOO email. '
        f'Then check my most recent emails, Teams messages, and chats with this person since {start_date}. '
        f'Determine whether any communication clearly resolves this waiting task. '
        f'{question_block} '
        'Return ONLY a minified JSON object with this exact schema: '
        '{"out_of_office":true,"ooo_summary":"string","return_date":"YYYY-MM-DD or null","interactions_found":true,'
        '"activity_summary":"string","resolution_signal":"clear or possible or none","resolution_summary":"string",'
        '"answers":[{"question_line":"string","answer":"string"}]}. '
        'If not out of office, set out_of_office false and return_date null. '
        'If no interactions are found, set interactions_found false. '
        'Use resolution_signal clear only for obvious resolution, possible for maybe related, else none.'
    )


def truncate(text, limit=180):
    text = re.sub(r'\s+', ' ', (text or '').strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def classify(target, start_date, data):
    if data.get('out_of_office'):
        return 'out_of_office', truncate(data.get('ooo_summary') or f'{target} is out of office'), data.get('return_date')
    if not data.get('interactions_found'):
        return 'no_activity', truncate(f'No response from {target} since {(start_date or "")[:10]}'), None
    signal = (data.get('resolution_signal') or 'none').strip().lower()
    activity_summary = truncate(data.get('activity_summary') or f'Recent activity found with {target}')
    resolution_summary = truncate(data.get('resolution_summary') or activity_summary)
    if signal == 'clear':
        return 'may_be_resolved', resolution_summary, None
    if signal == 'possible':
        if 'may be related' not in activity_summary.lower() and 'might be related' not in activity_summary.lower():
            activity_summary = truncate(f'{activity_summary} May be related.')
        return 'activity_detected', activity_summary, None
    return 'activity_detected', activity_summary, None


def update_user_notes(existing_notes, qa_pairs):
    if not existing_notes or not qa_pairs:
        return existing_notes
    qa_map = {q.strip(): a.strip() for q, a in qa_pairs if q and a}
    lines = existing_notes.split('\n')
    new_lines = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        new_lines.append(line)
        key = line.strip()
        if key in qa_map:
            next_line = lines[idx + 1] if idx + 1 < len(lines) else ''
            if next_line.startswith('  →'):
                idx += 1
                new_lines.append(next_line)
            else:
                new_lines.append('  → ' + qa_map[key])
        idx += 1
    return '\n'.join(new_lines)


def process_task(task):
    people = parse_people(task.get('key_people'))
    target = choose_target_person(task.get('source_type') or 'manual', task.get('source_id') or '', people)
    start_date = compute_start_date(task.get('created_at') or '', task.get('source_type') or 'manual')
    unanswered_questions = extract_unanswered_questions(task.get('user_notes') or '')
    if not target:
        return {
            'id': task['id'],
            'title': task['title'],
            'orig_status': task['status'],
            'classification': 'no_activity',
            'summary': 'No key people to check',
            'return_date': None,
            'qa_pairs': [],
        }
    raw = run_workiq(build_prompt(task, target, people, start_date, unanswered_questions))
    data = json.loads(extract_json_object(raw))
    classification, summary, return_date = classify(target, start_date, data)
    qa_pairs = []
    for item in data.get('answers') or []:
        if isinstance(item, dict):
            q = item.get('question_line', '')
            a = item.get('answer', '')
            if q and a:
                qa_pairs.append((q, truncate(a, 240)))
    return {
        'id': task['id'],
        'title': task['title'],
        'orig_status': task['status'],
        'classification': classification,
        'summary': summary,
        'return_date': return_date,
        'qa_pairs': qa_pairs,
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tasks = [dict(r) for r in conn.execute(LOAD_SQL).fetchall()]
    if not tasks:
        print('<<<SKILL_OUTPUT>>>')
        print(f'Waiting Activity Check — {TODAY}')
        print('Checked 0 tasks')
        print()
        print('<<<END_SKILL_OUTPUT>>>')
        conn.close()
        return 0

    results = []
    skipped = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(process_task, task): task for task in tasks}
        for future in as_completed(future_map):
            task = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                skipped.append((task['id'], task['title'], str(exc)))
                print(f'Skipping #{task["id"]}: {exc}', file=sys.stderr)

    results.sort(key=lambda item: item['id'])
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for item in results:
        activity = {'status': item['classification'], 'summary': item['summary'], 'checked_at': now}
        if item['return_date']:
            activity['return_date'] = item['return_date']
        val = json.dumps(activity)
        if item['orig_status'] == 'snoozed' and item['classification'] != 'out_of_office':
            conn.execute(
                'UPDATE tasks SET waiting_activity = ?, status = ?, snoozed_until = NULL, updated_at = ? WHERE id = ?',
                (val, 'waiting', now, item['id']),
            )
        else:
            conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (val, now, item['id']))
        if item['qa_pairs']:
            row = conn.execute('SELECT user_notes FROM tasks WHERE id = ?', (item['id'],)).fetchone()
            existing_notes = row[0] if row else ''
            new_notes = update_user_notes(existing_notes or '', item['qa_pairs'])
            if new_notes != (existing_notes or ''):
                conn.execute('UPDATE tasks SET user_notes = ?, updated_at = ? WHERE id = ?', (new_notes, now, item['id']))
    conn.commit()
    conn.close()

    print('<<<SKILL_OUTPUT>>>')
    print(f'Waiting Activity Check — {TODAY}')
    print(f'Checked {len(results)} tasks')
    print()
    for item in results:
        print(f'#{item["id"]} {item["title"]} — {item["classification"]}: {item["summary"]}')
    print('<<<END_SKILL_OUTPUT>>>')
    if skipped:
        ids = ', '.join(str(task_id) for task_id, _, _ in skipped)
        print(f'Skipped due to WorkIQ errors: {ids}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
