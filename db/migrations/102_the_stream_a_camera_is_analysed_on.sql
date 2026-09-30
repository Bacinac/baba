-- Which of a camera's streams the pipeline analyses.
--
-- A camera exposes its own stream, its low-resolution substream, or both. With
-- one there is nothing to choose; with both the operator picks the one that
-- detection, tracking and every body and face crop are cut from. Recording
-- always takes `stream_url`.
--
-- 'main' is the default because crops are cut at whatever size the subject
-- was, not squashed like the detector's 640² input. Measured 05.09 against
-- this property's own substreams and faces, whose median is 36-49 px against a
-- 60 px floor on naming anybody: backyard's 236 usable faces would have been
-- 6, south's 130 would have been 10, door's 240 would have been 71.
--
-- Until now a configured substream became the analysis stream implicitly; a
-- row that has one keeps what it had.
ALTER TABLE cameras ADD COLUMN analysis_stream text NOT NULL DEFAULT 'main';
UPDATE cameras SET analysis_stream = 'sub' WHERE substream_url IS NOT NULL;
ALTER TABLE cameras ADD CONSTRAINT cameras_analysis_stream
    CHECK (analysis_stream = 'main' OR (analysis_stream = 'sub' AND substream_url IS NOT NULL));

COMMENT ON COLUMN cameras.analysis_stream IS
    'main | sub — the stream detection, tracking and embedding read. sub '
    'requires substream_url.';

-- go2rtc no longer carries a camera's substream as a hidden second source
-- under the main stream's name, the fallback that recorded nine hours of
-- west's substream on 04.09. Each name has exactly one source, so a smaller
-- picture on it is the camera's own choice and is written down as such.
COMMENT ON COLUMN cameras.stream_width IS
    'Width of the video on stream_url, as the recorder last measured it from '
    'its own segments.';
COMMENT ON COLUMN cameras.stream_height IS
    'Height of the video on stream_url, as the recorder last measured it from '
    'its own segments.';
