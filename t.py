#$ PYTHONDONTWRITEBYTECODE=1

from core.api_client import OneCClient
from core.norms import load_doc_types
from core import db

base_url: str = "https://msk1.1cfresh.com/a/ea/450602"
client = OneCClient(base_url, "odata.user", "odatauser2026**")
start: str = "2026-01-01"
end: str = "2026-12-31"
registry = load_doc_types()
norm_titles = set(
    (n.get("title") or '').strip()
    for n in db.list_norms(active_only=False)
)
refs = client.fetch_journal_ref_types(start, end)

if not refs:
    print("NO_JOURNAL")
    raise SystemExit

all_docs = sorted([
    k
    for k, v in refs.items()
    if k.startswith("Document_")
])
reg_ent: set = {
    s.get("entity")
    for s in registry.values()
    if s.get("entity")
}
by_ent: dict = {}

for k, s in registry.items():
    if s.get("entity"):
        by_ent.setdefault(s["entity"], []).append((k, s))

found: int = 0
with_norm: int = 0
no_norm: int = 0
total_cnt: int = 0

for ent in all_docs:
    c = refs.get(ent, 0)
    total_cnt += c
    if ent in by_ent:
        found += 1
        specs = by_ent[ent]
        has = False
        for kk, sp in specs:
            if sp.get("title") in norm_titles:
                has = True
                break
        if has:
            with_norm += 1
        else:
            no_norm += 1
    else:
        found += 1
        if ent in norm_titles:
            with_norm += 1
        else:
            no_norm += 1

print("Bcero Document_* in the log:", len(all_docs))
print("Journal Total:", total_cnt)
print("The system will find (in the registry or synthetically):", found)
print("For which there is a norm:", with_norm)
print("No norm (will be from midnight):", no_norm)
print("\n-- without norm ---")

for ent in all_docs:
    if ent not in by_ent:
        if ent not in norm_titles:
            print(ent, refs.get(ent))
        continue
    
    specs = by_ent[ent]
    has = False
    for kk, sp in specs:
        if sp.get("title") in norm_titles:
            has = True
            break
    
    if not has:
        print(ent, refs.get(ent))
