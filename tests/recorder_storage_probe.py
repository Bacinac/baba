import asyncio
import logging
import signal
import uuid
from pathlib import Path

import asyncpg
from baba_recorder.config import CameraSpec, RecorderConfig
from baba_recorder.supervisor import RecorderSupervisor
from baba_recorder.worker import CameraRecorder

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
real_exec = asyncio.create_subprocess_exec
writers = []

async def local_source(*args, **kwargs):
    if args[0] == 'ffmpeg' and '-rtsp_transport' in args:
        suffix = args[args.index('-c'):]
        args = ('ffmpeg', '-hide_banner', '-loglevel', 'warning', '-re', '-stream_loop', '-1', '-i', '/tmp/source.mp4', *suffix)
    proc = await real_exec(*args, **kwargs)
    if args[0] == 'ffmpeg' and '-re' in args:
        writers.append(proc)
    return proc

def cfg(root):
    return RecorderConfig(dsn='', media_path=Path(root), segment_seconds=2, rtsp_timeout_us=1_000_000,
                          poll_seconds=.2, retention_check_seconds=1, go2rtc_rtsp_base='rtsp://unused')

async def main():
    proc = await real_exec('ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                           'testsrc2=size=320x240:rate=25', '-t', '4', '-c:v', 'libx264', '-threads', '1',
                           '-preset', 'ultrafast', '-b:v', '2M', '-minrate', '2M', '-maxrate', '2M',
                           '-bufsize', '2M', '-g', '25', '/tmp/source.mp4')
    assert await proc.wait() == 0
    asyncio.create_subprocess_exec = local_source
    pool = await asyncpg.create_pool('postgresql://postgres:storage_fixture@postgres/storage')
    await pool.execute('''CREATE TABLE cameras(id uuid primary key, slug text, stream_width integer, stream_height integer,
            enabled bool default true, recording_enabled bool default true);
        CREATE TABLE recordings(id uuid primary key default gen_random_uuid(), camera_id uuid references cameras(id), started_at timestamptz,
            ended_at timestamptz, duration_s float, path text unique, size_bytes bigint, codec text, retain_until timestamptz);''')
    cam = uuid.uuid4()
    await pool.execute("INSERT INTO cameras(id,slug) VALUES($1,'fault')", cam)
    for root, kill in [('/ro',False),('/fault',False),('/recovery',True),('/recovery2',False),('/shutdown',False)]:
        worker = CameraRecorder(CameraSpec(id=str(cam), slug='fault', stream_url='rtsp://unused'), cfg(root), pool)
        out = Path(root)/'segments/fault'
        out.mkdir(parents=True, exist_ok=True)
        task = asyncio.ensure_future(worker._record_session(out))
        if root in ('/recovery','/recovery2','/shutdown'):
            await asyncio.sleep(4)
            if root == '/shutdown':
                writers[-1].send_signal(signal.SIGSTOP)
                await asyncio.sleep(.1)
                tail = sorted(out.glob('*.mp4'))[-1]
                with tail.open('r+b') as file:
                    file.truncate(16)
                worker._stop.set()
            elif kill:
                writers[-1].kill()
            else:
                worker._stop.set()
        try:
            await asyncio.wait_for(task, timeout=25)
            outcome = 'returned-success'
        except RuntimeError as exc:
            outcome = f'{type(exc).__name__}: {exc}'
        files = []
        for path in sorted(out.glob('*.mp4')):
            probe = await real_exec('ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout, _stderr = await probe.communicate()
            files.append((path.name,path.stat().st_size,probe.returncode,stdout.decode().strip()))
        rows = await pool.fetch('SELECT path,size_bytes,ended_at IS NOT NULL AS closed FROM recordings')
        if root in ('/recovery2','/shutdown'):
            assert outcome == 'returned-success'
        else:
            assert outcome.startswith('RuntimeError: recording writer exited with status'), outcome
        assert not any(row['size_bytes'] == 0 for row in rows), rows
        assert all((Path(root) / row['path']).is_file() for row in rows)
        probes = {name: (size, code) for name, size, code, _duration in files}
        assert all(probes[Path(row['path']).name] == (row['size_bytes'], 0) for row in rows)
        print('RESULT',root,outcome,'files=',len(files),'rows=',len(rows),flush=True)
        if root == '/shutdown':
            assert writers[-1].returncode == -signal.SIGKILL
            assert not tail.exists()
            print('RESULT forced-shutdown-discards-confirmed-unreadable-tail',flush=True)
        if root == '/ro':
            rid = uuid.uuid4()
            await pool.execute("INSERT INTO recordings(id,camera_id,started_at,ended_at,path) VALUES($1,$2,now()-interval '10 days',now()-interval '9 days','segments/fault/retention.mp4')",rid,cam)
            sup = RecorderSupervisor(cfg(root))
            async with pool.acquire() as conn:
                n = await sup._delete_segments(conn,'id=$1',rid)
            assert n == 0 and await pool.fetchval('SELECT count(*) FROM recordings WHERE id=$1',rid)==1
            print('RESULT readonly-retention preserved-row-and-file',flush=True)
        if root == '/fault':
            assert all(size > 0 for _,size,_code,_duration in files), files
            sup = RecorderSupervisor(cfg(root))
            async with pool.acquire() as conn:
                removed = await sup._disk_prune(conn,80,20)
            assert removed > 0 and await pool.fetchval('SELECT count(*) FROM recordings') == 0
            worker._stop.clear()
            resumed = asyncio.ensure_future(worker._record_session(out))
            await asyncio.sleep(.8)
            worker._stop.set()
            await asyncio.wait_for(resumed,timeout=12)
            rows = await pool.fetch('SELECT path,size_bytes FROM recordings')
            assert rows and all(row['size_bytes'] > 0 for row in rows), rows
            print('RESULT ENOSPC-retention-and-writer-recovery',flush=True)
        if root == '/recovery':
            prior_paths = {row['path'] for row in rows}
            resumed = asyncio.ensure_future(worker._record_session(out))
            await asyncio.sleep(.8)
            worker._stop.set()
            await asyncio.wait_for(resumed,timeout=12)
            after_paths = set(await pool.fetch('SELECT path FROM recordings'))
            assert prior_paths <= {row['path'] for row in after_paths}
            assert all((Path(root) / path).is_file() for path in prior_paths)
            print('RESULT SIGKILL-same-worker-recovery-preserves-valid-history',flush=True)
        await pool.execute('DELETE FROM recordings')
    denied = Path('/recovery/denied')
    await asyncio.to_thread(denied.mkdir)
    await asyncio.to_thread(denied.chmod, 0o500)
    worker = CameraRecorder(CameraSpec(id=str(cam), slug='fault', stream_url='rtsp://unused'), cfg(denied), pool)
    before = len(writers)
    worker.start()
    await asyncio.sleep(.3)
    assert len(writers) == before
    await asyncio.to_thread(denied.chmod, 0o700)
    await asyncio.sleep(1.5)
    await worker.stop()
    assert len(writers) > before
    assert await pool.fetchval('SELECT count(*) FROM recordings') > 0
    print('RESULT directory-permission-repair-automatic-recovery',flush=True)
    await pool.close()

asyncio.run(main())
