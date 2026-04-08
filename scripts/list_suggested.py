import sys, json

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    wa = d["waiting_activity"][:40] if d["waiting_activity"] else ""
    kp_raw = d["key_people"]
    try:
        kp_list = json.loads(kp_raw) if kp_raw else []
        kp = ", ".join(p["name"] for p in kp_list[:2]) if kp_list else ""
    except Exception:
        kp = kp_raw[:40]
    print(f"#{d['id']} | {d['title'][:60]} | people={kp} | src={d['source_type']} | {d['created_at'][:10]} | wa={wa}")
