from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FaceSelection:
    revision: int
    model_key: str
    detector_key: str


async def read_face_selection(pool) -> FaceSelection:
    row = await pool.fetchrow(
        "SELECT revision, model_key, detector_key FROM face_recognition_settings WHERE id = 1"
    )
    if row is None:
        raise RuntimeError("face_recognition_settings singleton missing")
    return FaceSelection(row["revision"], row["model_key"], row["detector_key"])


async def acknowledge_face_selection(pool, service: str, selection: FaceSelection,
                                     error: str | None = None) -> bool:
    statements = {
        "api": "UPDATE face_recognition_settings SET api_revision = $2, api_error = $3 "
               "WHERE id = 1 AND revision = $1 RETURNING revision, api_revision, embedder_revision",
        "embedder": "UPDATE face_recognition_settings SET embedder_revision = $2, embedder_error = $3 "
                    "WHERE id = 1 AND revision = $1 RETURNING revision, api_revision, embedder_revision",
    }
    if service not in statements:
        raise ValueError("unknown face activation participant")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            statements[service],
            selection.revision, selection.revision if error is None else None, error,
        )
        if row is None:
            return False
        if row["api_revision"] == row["revision"] == row["embedder_revision"]:
            await conn.execute(
                "UPDATE face_recognition_settings SET active_revision = revision, "
                "active_model_key = model_key, active_detector_key = detector_key, "
                "active_match_threshold = match_threshold "
                "WHERE id = 1 AND active_revision <> revision"
            )
        return True
