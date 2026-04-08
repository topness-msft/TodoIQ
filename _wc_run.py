"""Waiting-check WorkIQ queries - run sequentially."""
import subprocess, json, os, sys

WORKIQ = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'
OUT_DIR = 'data'

queries = [
    ('saurabh_pant', "Check Saurabh Pant (spant@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Saurabh Pant since 2026-02-25? List all interactions found."),
    ('ketaki_sakhardande', "Check Ketaki Sakhardande (kesakh@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Ketaki Sakhardande since 2026-02-23? List all interactions found."),
    ('rohini_chandrashekhar', "Check Rohini Chandrashekhar (Rohini.Chandrashekhar@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Rohini Chandrashekhar since 2026-03-08? List all interactions found."),
    ('manuela_pichler', "Check Manuela Pichler (Manuela.Pichler@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Manuela Pichler since 2026-03-10? List all interactions found."),
    ('greg_hurlman', "Check Greg Hurlman (grhurl@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Greg Hurlman since 2026-03-12? List all interactions found."),
    ('john_wheat', "Check John Wheat (jwheat@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with John Wheat since 2026-03-14? List all interactions found."),
    ('rodrigo_de_la_garza', "Check Rodrigo De la Garza (rodrigodg@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Rodrigo De la Garza since 2026-04-06? List all interactions found."),
    ('adrian_maclean', "Check Adrian Maclean (Adrian.Maclean@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Adrian Maclean since 2026-04-04? List all interactions found."),
    ('aamer_kaleem', "Check Aamer Kaleem (Aamer.Kaleem@microsoft.com) current presence and availability. Are they Out of Office in Teams or Outlook? Do they have an OOO automatic reply set? When returning if OOO? Also: what are my most recent emails, Teams messages, and chats with Aamer Kaleem since 2026-04-06? List all interactions found."),
]

target = sys.argv[1] if len(sys.argv) > 1 else None

for name, query in queries:
    if target and name != target:
        continue
    out_path = os.path.join(OUT_DIR, f'_wc_{name}.txt')
    print(f'[{name}] querying...')
    sys.stdout.flush()
    try:
        proc = subprocess.run(
            [WORKIQ, 'ask', '-q', query],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=180
        )
        output = proc.stdout or ''
        if not output and proc.stderr:
            output = proc.stderr
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'[{name}] done: {len(output)} chars')
    except subprocess.TimeoutExpired:
        print(f'[{name}] TIMEOUT')
    except Exception as e:
        print(f'[{name}] ERROR: {e}')
    sys.stdout.flush()

print('All done.')
