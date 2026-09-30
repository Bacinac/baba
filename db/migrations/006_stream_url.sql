-- Rename rtsp_url → stream_url. Cameras aren't always RTSP — we also accept
-- HTTP-FLV (older Reolink models), RTMP, ONVIF, and anything else go2rtc /
-- ffmpeg can open. The column was misleadingly named.

ALTER TABLE cameras RENAME COLUMN rtsp_url TO stream_url;
