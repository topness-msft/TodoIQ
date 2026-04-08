import subprocess

WORKIQ_CMD = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'

queries = {
    'ooo_manuela': "Check Manuela Pichler's current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?",
    'ooo_steve': "Check Steve Jeffery's current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?",
    'ooo_bill': "Check Bill Spencer's current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?",
    'ooo_adrian': "Check Adrian Maclean's current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?",
    'ctx_663': "Show me the recent email thread about CAPE guidance helper person with Manuela Pichler and Aamer Kaleem. Include the last 2-3 messages so I can see what was said.",
    'ctx_664': "What are my most recent emails and Teams messages with Steve Jeffery about CAB travel estimate? When was the last interaction?",
    'ctx_665': "What are my most recent emails and Teams messages with Bill Spencer about Rima Reyes performance feedback? When was the last interaction?",
    'ctx_666': "What are my most recent emails and Teams messages with Adrian Maclean about Irina Parsina or a guidance or mentoring role? When was the last interaction?",
}

results = {}
for key, query in queries.items():
    print(f"\n=== Querying: {key} ===")
    proc = subprocess.run(
        [WORKIQ_CMD, 'ask', '-q', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120
    )
    results[key] = proc.stdout
    print(proc.stdout[:2000] if proc.stdout else '(no output)')

with open('data/_parse_ooo_ctx.txt', 'w', encoding='utf-8') as f:
    for key, val in results.items():
        f.write(f"\n=== {key} ===\n{val}\n")
print("\nSaved to data/_parse_ooo_ctx.txt")
