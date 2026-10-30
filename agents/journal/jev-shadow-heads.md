# Jev shadow judge: recorded chain heads

Append-only; never edit or reorder a line. The shadow judge adds one line after every
write to `agents/data/paper/jev-shadow/decisions.jsonl`. Commit this file with each push:
the committed heads are what make a rewritten (re-chained) log detectable, because
`shadow-report` and every later shadow run check each recorded head against the log.

- 2026-10-30T02:32:51Z seq 0 rows 1 head 3f6e060ad789cc44aad3e558d062800e829a8463296c55d368fea3cfa0379580
- 2026-10-30T04:00:27Z seq 1 rows 2 head a79a5da2ddb6d526026f32e9be5ecbe29856e466b92b0be055b94d7d6fff1d7a
- 2026-10-30T08:00:26Z seq 2 rows 3 head cd88cf9a837b1753ef1ea8828558bdaf3545bcecc998bff07357d9f96eefafd8
- 2026-10-30T12:00:27Z seq 3 rows 4 head 02700e535dd83d74c798b1ecf768870e2cbc7ca367136ade44283af311472b7d
- 2026-10-30T16:00:27Z seq 4 rows 5 head e7bf1938d730458ceac4e17273a2300448898bcbf175e76c6a08d66dbfe15e61
