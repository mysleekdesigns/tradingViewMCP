# Jev shadow judge: recorded chain heads

Append-only; never edit or reorder a line. The shadow judge adds one line after every
write to `agents/data/paper/jev-shadow/decisions.jsonl`. Commit this file with each push:
the committed heads are what make a rewritten (re-chained) log detectable, because
`shadow-report` and every later shadow run check each recorded head against the log.

- 2026-10-30T02:32:51Z seq 0 rows 1 head 3f6e060ad789cc44aad3e558d062800e829a8463296c55d368fea3cfa0379580
