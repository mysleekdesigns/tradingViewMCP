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
- 2026-10-30T20:00:27Z seq 5 rows 6 head da0bbc987e9399488cc1946e56f6a69697e3935d1120edfd31ace2cddd22d7ad
- 2026-10-31T00:00:27Z seq 7 rows 8 head 81da75bebe727dea54e430a7d5150854409425fd44e4c8ad3949583e7604a79b
- 2026-10-31T04:00:27Z seq 8 rows 9 head 9cc9ae46a34ebe8707a07896acfe79efff5303f763d325de5706dd17f13cb053
- 2026-10-31T08:00:26Z seq 9 rows 10 head 70f1f5a32dabfcbf0ccfac80aac5b4cc5c1a335b93e08d8864a0b33095d46e78
- 2026-10-31T12:00:26Z seq 10 rows 11 head b6d97d96d6b9364fb95bc81fe4081d29ecde016403dcbc67a170a491eadfe1d2
- 2026-10-31T16:00:26Z seq 11 rows 12 head 19adb4aa18128107e63c7893541a16487b059ce2cd792b50f0bc02331f33dede
- 2026-10-31T20:00:26Z seq 12 rows 13 head 7688d5dc7ce3de0fd7d76ffa3965bf2981038c268ca17ff28ec66d37840b70bf
- 2026-11-01T00:00:26Z seq 13 rows 14 head dc4a57b7c0c5174654272b5cac760eb8e0b2bb4306161c00cb381e5564367766
- 2026-11-01T04:00:27Z seq 14 rows 15 head f2095f1fbc64f3d02f0ccca68117cd613e23e94c6540ee4533a70e1e0fc42ca9
- 2026-11-01T09:33:18Z seq 15 rows 16 head 5c8d8d825bc95f9d62212db5a6a7ec75c718c6aa7727a5b63c26a10ebf46838f
- 2026-11-01T12:00:26Z seq 16 rows 17 head dcd33e0efb5f2f57f6f43da73bf23c25f561d005c392de54c44e1aa8ab85cad2
