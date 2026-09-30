"""Check every name that was minted from a face, against the curated evidence.

A name used to be attachable from the nearest PAST TRACK, and from a face of any
size at all. Both are closed now, but only going forward: 142 tracks in the 30
days to 31.08 still carry a name decided that way, on faces averaging 40 px — and
by the agreement curve that put the floor at 60, roughly one in five of them is
somebody else's name.

They cannot be left and they must not be bulk-deleted: four in five are right.
So each is asked again, with the face the recording holds rather than the one the
ring offered, and against the only evidence that can name anybody — the enrolled
photographs and the vector an operator attached to the identity. The rule is
imported, not restated: a verdict here that could differ from the pipeline's
would be worse than no audit.

    docker exec -i baba-event-manager python3 - < tools/audit_face_names.py
    docker exec -i baba-event-manager python3 - --apply < tools/audit_face_names.py

Read-only without `--apply`. With it, a track the evidence names differently is
reassigned, and one the evidence cannot name at all goes back to anonymous —
which is what it was owed in the first place.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg
from baba_core.dsn import dsn_from_env
from baba_event_manager.identity_rules import FACE_ID_MIN_PX, face_match_survives_rivals

# The same distance the pipeline accepts a curated face match at.
THRESHOLD = 0.75

_CURATED = """
    WITH active AS (SELECT model_key FROM face_recognition_settings),
    curated AS (
        SELECT global_id, face_embedding
          FROM identity_reference_photos
         WHERE face_embedding IS NOT NULL
           AND face_embedding_model = (SELECT model_key FROM active)
        UNION ALL
        SELECT global_id, face_embedding
          FROM identity_labels
         WHERE face_embedding IS NOT NULL
           AND face_embedding_model = (SELECT model_key FROM active)
    )
    SELECT c.global_id, NULL::uuid AS id, min(c.face_embedding <=> $1::vector) AS dist
    FROM curated c GROUP BY c.global_id ORDER BY 3 ASC LIMIT 16
"""


async def main(apply: bool) -> None:
    pool = await asyncpg.create_pool(dsn_from_env(), min_size=1, max_size=3)
    rows = await pool.fetch(
        """
        SELECT t.id, t.face_embedding, t.face_px, t.global_id, l.name,
               c.slug, t.started_at
        FROM tracks t
        JOIN cameras c ON c.id = t.camera_id
        JOIN identity_labels l ON l.global_id = t.global_id
        WHERE t.identity_source = 'face'
          AND t.face_embedding IS NOT NULL
          AND t.face_embedding_model = (SELECT model_key FROM face_recognition_settings)
          AND t.class_id = 0
        ORDER BY t.started_at DESC
        """
    )
    print(f"{len(rows)} track(s) named by face\n")
    agreed = reassigned = cleared = unusable = 0
    for r in rows:
        if (r["face_px"] or 0.0) < FACE_ID_MIN_PX:
            # The recording had no more of this face than the ring did, so
            # there is nothing better to ask with. Left exactly as found.
            unusable += 1
            continue
        cand = await pool.fetch(_CURATED, r["face_embedding"])
        chosen, _gap = face_match_survives_rivals(cand)
        verdict = (
            chosen
            if chosen is not None and float(chosen["dist"]) < THRESHOLD
            else None
        )
        when = r["started_at"].astimezone().strftime("%d.%m %H:%M")
        if verdict is not None and verdict["global_id"] == r["global_id"]:
            agreed += 1
            continue
        if verdict is not None:
            name = await pool.fetchval(
                "SELECT name FROM identity_labels WHERE global_id = $1",
                verdict["global_id"],
            )
            print(f"  {when} {r['slug']:<9} {r['name']:<8} → {name:<8} "
                  f"({float(verdict['dist']):.3f}, {r['face_px']:.0f} px)")
            reassigned += 1
            if apply:
                await pool.execute(
                    "UPDATE tracks SET global_id = $2, identity_source = 'face', "
                    "face_verified = true WHERE id = $1",
                    r["id"], verdict["global_id"],
                )
        else:
            best = float(cand[0]["dist"]) if cand else 9.9
            print(f"  {when} {r['slug']:<9} {r['name']:<8} → anonimno  "
                  f"(najbliža {best:.3f}, {r['face_px']:.0f} px)")
            cleared += 1
            if apply:
                await pool.execute(
                    "UPDATE tracks SET global_id = NULL, identity_source = NULL, "
                    "face_verified = false WHERE id = $1",
                    r["id"],
                )
    print(f"\npotvrđeno {agreed} · preimenovano {reassigned} · "
          f"vraćeno u anonimno {cleared} · bez upotrebljivog lica {unusable}")
    if not apply and (reassigned or cleared):
        print("(ništa nije mijenjano — pokreni s --apply)")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
