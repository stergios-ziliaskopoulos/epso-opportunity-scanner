import json

with open('data/vacancies.js', 'r', encoding='utf-8') as f:
    text = f.read()
vacs = json.loads(text.split('window.VACANCIES = ', 1)[1].rsplit(';', 1)[0])

fits = [v for v in vacs if v.get('fit')]
non_fits = [v for v in vacs if not v.get('fit')]

print(f"=== TOTAL VACANCIES: {len(vacs)} (FITS: {len(fits)} | NON-FITS: {len(non_fits)}) ===\n")

print("--- FIT VACANCIES ---")
for v in fits:
    print(f"\n[{v['id']}] {v['title']}")
    print(f"  DG/Org: {v.get('organization') or v.get('dg')} | FG: {v.get('fg')} | Art: {v.get('article')} | Deadline: {v.get('deadline')}")
    print(f"  Score: {v.get('score')} ({v.get('scoreLabel')}) | NetPay: {v.get('netPay')} EUR")
    print(f"  Gaps: {v.get('gaps')}")
    note = v.get('note', '') or ''
    print(f"  Notes: {note}")

print("\n\n--- NON-FIT VACANCIES (WHY EXCLUDED) ---")
for v in non_fits:
    print(f"\n[{v['id']}] {v['title']}")
    print(f"  DG/Org: {v.get('organization') or v.get('dg')} | FG: {v.get('fg')} | Art: {v.get('article')} | Deadline: {v.get('deadline')}")
    print(f"  Gaps/Reason: {v.get('gaps')}")
    note = v.get('note', '') or ''
    print(f"  Notes: {note}")
