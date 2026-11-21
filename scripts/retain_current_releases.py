#!/usr/bin/env python3
"""发布当前快照并清理旧应用版本；默认仅显示计划。"""
import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
from urllib.parse import urlencode
import urllib.request

try:
    import fcntl
except ImportError:  # Windows 没有 fcntl，发布锁由目录锁实现（见 main）
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
REPO = 'z0brk/netamade-releases'
TOOLS = {'speedtest': '5MB.img', 'android-fio-arm64': 'fio-android-arm64'}
CHANNELS = {'stable': 'REL', 'testing': 'TEST', 'beta': 'BETA'}
OWNED = ['README.md', 'AGENTS.md', 'scripts/retain_current_releases.py']

REMOTE_TIMEOUT_SECONDS = 180
REMOTE_MAX_ATTEMPTS = 6
REMOTE_RETRY_DELAY_SECONDS = 3


def stage(title):
    sep = "=" * 68
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)


def ok(message):
    print(f"[ 完成 ] {message}", flush=True)


def finish_box(m):
    cols = 64
    def line(label, val):
        text = f"{label}{val}"
        w = sum(2 if ord(c) > 127 else 1 for c in text)
        sp = " " * max(0, cols - w)
        print(f"| {text}{sp} |", flush=True)

    print("\n+==================================================================+", flush=True)
    print("|                         NETAMADE 发布完成                        |", flush=True)
    print("+==================================================================+", flush=True)
    channel = m.get('channel') or ('testing' if m.get('testing') else 'stable')
    line("渠道：", channel)
    line("版本：", m.get('releaseVersion', ''))
    line("标签：", f"refs/tags/{m.get('releaseVersion', '')}")
    line("产物：", m.get('releaseFilename', ''))
    line("MD5 ：", m.get('apkMd5', ''))
    print("+==================================================================+\n", flush=True)


def is_remote_command(args):
    if not args:
        return False
    cmd = args[0]
    if cmd == 'git' and len(args) > 1:
        return args[1] in ('ls-remote', 'push', 'fetch', 'pull', 'clone')
    if cmd == 'gh' and len(args) > 1:
        return args[1] in ('api', 'release', 'repo')
    if cmd == 'curl':
        return True
    return False


def retry_call(description, fn, *args, max_attempts=REMOTE_MAX_ATTEMPTS, delay=REMOTE_RETRY_DELAY_SECONDS, **kwargs):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            if attempt >= max_attempts:
                print(f"失败：{description}，已尝试 {max_attempts} 次", file=sys.stderr, flush=True)
                raise
            err_msg = str(error).strip()
            lines = [l.strip() for l in err_msg.splitlines() if l.strip()]
            err_summary = lines[0] if lines else "未知错误"
            if isinstance(error, subprocess.TimeoutExpired):
                print(f"警告：{description} 超过 {REMOTE_TIMEOUT_SECONDS} 秒未完成，{delay} 秒后重试（第 {attempt}/{max_attempts} 次）", file=sys.stderr, flush=True)
            else:
                print(f"警告：{description} 失败（{err_summary}），{delay} 秒后重试（第 {attempt}/{max_attempts} 次）", file=sys.stderr, flush=True)
            time.sleep(delay)


def _run_once(*args, cwd=ROOT, env=None, input=None, timeout=REMOTE_TIMEOUT_SECONDS):
    # 显式 UTF-8：Windows 文本模式默认本地编码（GBK），git 输出含中文文件名/提交信息会解码失败。
    p = subprocess.run(args, cwd=cwd, env=env, input=input, text=True,
                       encoding='utf-8', errors='replace',
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        err = p.stderr.strip() or p.stdout.strip()
        cmd_desc = f"{args[0]} {args[1]}" if len(args) > 1 else args[0]
        raise RuntimeError(err or f"{cmd_desc} 返回非零退出码 {p.returncode}")
    return p.stdout.strip()


def run(*args, cwd=ROOT, env=None, input=None, timeout=None, retry=None):
    cmd_timeout = timeout or REMOTE_TIMEOUT_SECONDS
    cmd_desc = f"{args[0]} {args[1]}" if len(args) > 1 else args[0]
    should_retry = retry if retry is not None else is_remote_command(args)
    if should_retry:
        return retry_call(cmd_desc, _run_once, *args, cwd=cwd, env=env, input=input, timeout=cmd_timeout)
    return _run_once(*args, cwd=cwd, env=env, input=input, timeout=cmd_timeout)


def api(path, method='GET', data=None):
    args = ['gh', 'api', f'repos/{REPO}/{path}', '--method', method]
    if data is not None:
        args += ['--input', '-']
    for attempt in range(5):
        try:
            value = run(*args, input=json.dumps(data) if data is not None else None)
            return json.loads(value) if value else None
        except RuntimeError as error:
            message = str(error)
            if not any(s in message for s in ('HTTP 429', 'HTTP 502', 'HTTP 503', 'secondary rate limit')) or attempt == 4:
                raise RuntimeError(f'GitHub API {method} {path} 失败：{message}') from error
            delay = min(60, 5 * 2 ** attempt)
            print(f'GitHub 暂时限流或不可用，{delay} 秒后重试', flush=True)
            time.sleep(delay)


def releases():
    pages = run('gh', 'api', '--paginate', '--slurp', f'repos/{REPO}/releases?per_page=100')
    return [item for page in json.loads(pages) for item in page]


def refs():
    return {ref: sha for sha, ref in (line.split() for line in run('git', 'ls-remote', '--refs', 'origin').splitlines())}


def metadata():
    result = {}
    for channel, marker in CHANNELS.items():
        path = ROOT / f'{channel}.json'
        if channel == 'beta' and not path.exists():
            if run('git', 'ls-tree', '--name-only', 'HEAD', '--', 'beta.json'):
                raise RuntimeError('已启用的 beta.json 缺失，本次停止，禁止删除当前内测版')
            continue
        m = json.loads(path.read_text(encoding='utf-8'))
        declared_channel = m.get('channel', 'testing' if m.get('testing') else 'stable')
        if (declared_channel != channel or m['testing'] is not (channel != 'stable')
                or m['versionName'].partition('-')[2].partition('-')[0] != marker
                or m['releaseVersion'] != f'v{m["versionName"]}-{channel}'):
            raise RuntimeError(f'{channel}.json 渠道不一致')
        if (type(m['versionCode']) is not int or m['versionCode'] <= 0
                or m['releaseFilename'] != f'netamade-{channel}-{m["versionName"]}-{m["versionCode"]}.apk'):
            raise RuntimeError(f'{channel}.json 版本或附件名称不一致')
        if not re.fullmatch(r'[a-fA-F0-9]{32}', m['apkMd5']) or not (ROOT / f'{channel}.md').read_text(encoding='utf-8').strip():
            raise RuntimeError(f'{channel} 缺少摘要或发布说明')
        result[channel] = m
    return result


def release_asset(release, filename):
    assets = [a for a in release['assets'] if a['name'] == filename]
    if len(assets) != 1 or assets[0]['state'] != 'uploaded' or assets[0]['size'] <= 0:
        raise RuntimeError(f'Release 附件缺失或未上传完成：{release["tag_name"]}/{filename}')
    return assets[0]


def check_assets(items, meta):
    by_tag = {r['tag_name']: r for r in items}
    for tag, filename in {**TOOLS, **{m['releaseVersion']: m['releaseFilename'] for m in meta.values()}}.items():
        release = by_tag.get(tag)
        if not release or release['draft']:
            raise RuntimeError(f'应保留的 Release 尚未发布：{tag}')
        release_asset(release, filename)


def check_uploaded_apk(release, filename, size, sha256):
    asset = release_asset(release, filename)
    if asset['size'] != size:
        raise RuntimeError('GitHub APK 附件大小与本地文件不一致')
    digest = asset.get('digest')
    if digest:
        if digest != 'sha256:' + sha256:
            raise RuntimeError('GitHub APK SHA-256 与本地文件不一致')
        print('  [ 完成 ] 远程附件状态、大小及 SHA-256 核验通过', flush=True)
    else:
        print('  [ 完成 ] 远程附件状态与大小核验通过（API 未下发摘要）', flush=True)
    return bool(digest)


class UploadProgressFile:
    def __init__(self, path, desc="上传 APK"):
        self.path = Path(path)
        self.file = open(self.path, 'rb')
        self.total = self.path.stat().st_size
        self.uploaded = 0
        self.start = time.time()
        self.last_render = 0.0
        self.desc = desc

    def read(self, size=-1):
        chunk = self.file.read(size if size > 0 else 65536)
        if chunk:
            self.uploaded += len(chunk)
            self._render()
        return chunk

    def _render(self, force=False):
        now = time.time()
        if not force and now - self.last_render < 0.1 and self.uploaded < self.total:
            return
        self.last_render = now
        elapsed = now - self.start
        speed = self.uploaded / elapsed if elapsed > 0 else 0
        percent = (self.uploaded / self.total * 100.0) if self.total > 0 else 100.0

        uploaded_mb = self.uploaded / (1024 * 1024)
        total_mb = self.total / (1024 * 1024)
        speed_mb = speed / (1024 * 1024)

        if speed > 0 and self.uploaded < self.total:
            eta = int((self.total - self.uploaded) / speed)
            eta_str = f"剩余 {eta // 60:02d}:{eta % 60:02d}"
        else:
            eta_str = "计算中..."

        bar_len = 28
        filled = int(percent / 100.0 * bar_len)
        if filled > 0 and filled < bar_len:
            bar = '=' * (filled - 1) + '>' + ' ' * (bar_len - filled)
        elif filled >= bar_len:
            bar = '=' * bar_len
        else:
            bar = ' ' * bar_len

        msg = f"\r  上传中：[{bar}] {percent:5.1f}%  {uploaded_mb:.1f}MB/{total_mb:.1f}MB  {speed_mb:.2f}MB/s  {eta_str}"
        sys.stderr.write(msg)
        sys.stderr.flush()

    def finish(self):
        self._render(force=True)
        elapsed = max(1, int(time.time() - self.start))
        total_mb = self.total / (1024 * 1024)
        avg_speed = total_mb / elapsed if elapsed > 0 else 0
        sys.stderr.write(f"\r  [ 完成 ] 附件上传完成 ({total_mb:.1f}MB, 平均 {avg_speed:.2f}MB/s, 用时 {elapsed}秒)\n")
        sys.stderr.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __len__(self):
        return self.total

    def readable(self):
        return True

    def seekable(self):
        return False

    def fileno(self):
        return self.file.fileno()


def _upload_stream(upload_url, apk_path, token):
    with UploadProgressFile(apk_path) as pfile:
        req = urllib.request.Request(upload_url, data=pfile, method='POST')
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/vnd.android.package-archive')
        req.add_header('Content-Length', str(pfile.total))
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_unredirected_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_SECONDS) as resp:
                resp.read()
        except urllib.error.HTTPError as error:
            err_body = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"HTTP {error.code}: {err_body}") from error
        pfile.finish()


def _upload_curl(upload_url, apk_path, token):
    cmd = [
        'curl', '--fail', '--show-error',
        '-H', f'Authorization: Bearer {token}',
        '-H', 'Accept: application/vnd.github+json',
        '-H', 'Content-Type: application/vnd.android.package-archive',
        '--data-binary', f'@{apk_path}',
        upload_url,
        '-o', '/dev/null',
    ]
    if sys.stderr.isatty():
        cmd.insert(1, '--progress-bar')
    else:
        cmd.insert(1, '-s')
    p = subprocess.run(cmd, timeout=REMOTE_TIMEOUT_SECONDS)
    if p.returncode != 0:
        raise RuntimeError(f"curl 上传失败，退出码 {p.returncode}")


def upload_apk_with_progress(upload_url, apk_path, release_id, filename):
    token = run('gh', 'auth', 'token')

    def _do_upload():
        try:
            rel = api('releases/' + str(release_id))
            stale = next((a for a in rel.get('assets', []) if a['name'] == filename), None)
            if stale is not None:
                api('releases/assets/' + str(stale['id']), 'DELETE')
        except Exception:
            pass
        try:
            _upload_stream(upload_url, apk_path, token)
        except Exception as e:
            sys.stderr.write(f"\n原生流式上传遇到异常（{e}），回退到 curl 上传...\n")
            sys.stderr.flush()
            _upload_curl(upload_url, apk_path, token)

    retry_call("上传 APK", _do_upload)


def prepare_release(channel, apk, meta, items):
    m = meta[channel]
    content = apk.read_bytes()
    if hashlib.md5(content).hexdigest() != m['apkMd5']:
        raise RuntimeError('本地 APK MD5 与发布元数据不一致')
    size = len(content)
    sha256 = hashlib.sha256(content).hexdigest()
    del content
    current = next((r for r in items if r['tag_name'] == m['releaseVersion']), None)
    if current is None:
        # 草稿尚未创建公开标签，按标签查询会返回 404；直接使用创建响应中的 ID。
        current = api('releases', 'POST', {
            'tag_name': m['releaseVersion'], 'target_commitish': 'main',
            'name': m['releaseVersion'], 'draft': True, 'prerelease': channel == 'beta',
            'body': (ROOT / f'{channel}.md').read_text(encoding='utf-8'),
        })
    # 同名公开包不覆盖；无远程摘要时无法证明内容相同，要求使用新版本标识。
    if not current['draft']:
        if not check_uploaded_apk(current, m['releaseFilename'], size, sha256):
            raise RuntimeError('同名公开 APK 缺少远程摘要，无法确认内容相同，请使用新版本标识')
        return
    asset = next((a for a in current['assets'] if a['name'] == m['releaseFilename']), None)
    if asset is not None:
        # 草稿失败后重试可替换附件，旧的公开 Release 不受影响。
        api('releases/assets/' + str(asset['id']), 'DELETE')
    # 按 Release ID 的上传地址操作，避免 CLI 再次按尚不存在的公开标签定位草稿。
    upload_url = current['upload_url'].split('{', 1)[0] + '?' + urlencode({'name': m['releaseFilename']})
    print(f"目标 Release：{m['releaseVersion']} (Release ID: {current['id']})", flush=True)
    upload_apk_with_progress(upload_url, apk, current['id'], m['releaseFilename'])
    current = api('releases/' + str(current['id']))
    check_uploaded_apk(current, m['releaseFilename'], size, sha256)
    api('releases/' + str(current['id']), 'PATCH', {'draft': False, 'make_latest': 'true' if channel == 'stable' else 'false'})


def backup(directory, items, remote, meta):
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    (directory / 'releases.json').write_text(json.dumps(items, ensure_ascii=False, indent=2))
    (directory / 'refs.json').write_text(json.dumps(remote, indent=2))
    (directory / 'worktree-status.txt').write_text(run('git', 'status', '--short'))
    (directory / 'worktree.patch').write_text(run('git', 'diff', '--binary', 'HEAD'))
    run('git', 'clone', '--mirror', run('git', 'remote', 'get-url', 'origin'), str(directory / 'repository.git'))
    for channel in meta:
        for suffix in ('json', 'md'):
            filename = f'{channel}.{suffix}'
            (directory / filename).write_bytes((ROOT / filename).read_bytes())
    print(f'恢复备份已保存：{directory}（Git 全部引用、Release 元数据和本地差异；不下载全部旧 APK）', flush=True)


def snapshot(meta, channel, remote, migration, update_policy=False):
    head = run('git', 'rev-parse', 'HEAD')
    if head != remote['refs/heads/main']:
        raise RuntimeError('本地 HEAD 与远程 main 不一致，请先核对，禁止覆盖远程更新')
    if run('git', 'symbolic-ref', '--short', 'HEAD') != 'main':
        raise RuntimeError('必须在 main 分支操作')
    if run('git', 'diff', '--cached', '--name-only'):
        raise RuntimeError('暂存区非空，请先保留暂存工作，本次停止')
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        run('git', 'read-tree', head, env=env)
        files = (OWNED if migration or update_policy else []) + ([f'{channel}.json', f'{channel}.md'] if channel else [])
        # 迁移时仅提交本任务的文档、脚本和导出器修改，不带入用户已有导出器修改。
        if files:
            run('git', 'add', '--', *files, env=env)
        if migration:
            baseline = run('git', 'show', 'HEAD:scripts/export_stable_updates.py') + '\n'
            # 快照后的脚本维护也会增加提交数；压缩历史不代表导出器仍需迁移。
            begin = baseline.find('    log = run(', baseline.index('def collect_releases()'))
            if begin != -1:
                end = baseline.index('    # Allow exporting', begin)
                baseline = baseline[:begin] + '    releases: dict[str, ReleaseNote] = {}\n' + baseline[end:]
                begin = baseline.index('    missing_versions = sorted(')
                end = baseline.index('    return releases', begin)
                baseline = baseline[:begin] + baseline[end:]
                baseline = baseline.replace('Incrementally export every stable release note from Git history.', 'Export the current stable release note only.')
                blob = run('git', 'hash-object', '-w', '--stdin', input=baseline)
                run('git', 'update-index', '--cacheinfo', '100755,' + blob + ',scripts/export_stable_updates.py', env=env)
        tree = run('git', 'write-tree', env=env)
        commit = run('git', 'commit-tree', tree, input='chore: 更新当前发布快照\n')
    keep = set(TOOLS) | {m['releaseVersion'] for m in meta.values()}
    targets = {'refs/heads/main': commit, **{'refs/tags/' + tag: commit for tag in keep}}
    # 工具标签也改指快照；附件和下载地址不变，避免保留旧 main 历史。
    push = ['git', 'push', '--atomic', 'origin']
    for ref in targets:
        push.append(f'--force-with-lease={ref}:{remote.get(ref, "")}')
    push += [f'{value}:{ref}' for ref, value in targets.items()]
    run(*push)
    # 原子推送成功后才更新本地引用，不使用 reset --hard，保留无关工作区改动。
    run('git', 'update-ref', 'refs/heads/main', commit, head)
    run('git', 'read-tree', commit)
    run('git', 'update-ref', 'refs/remotes/origin/main', commit)
    for tag in keep:
        run('git', 'update-ref', 'refs/tags/' + tag, commit)
    print(f"  快照提交：{commit[:8]} (chore: 更新当前发布快照)", flush=True)
    return commit, keep


def cleanup(items, keep, old_refs):
    obsolete = [r for r in items if r['tag_name'] not in keep]
    def delete_release(r):
        api('releases/' + str(r['id']), 'DELETE')
    if obsolete:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(delete_release, r): r for r in obsolete}
            for count, future in enumerate(concurrent.futures.as_completed(pending), 1):
                future.result()
                if count % 25 == 0 or count == len(obsolete):
                    print(f'  旧 Release 清理：已删除 {count}/{len(obsolete)} 个历史版本', flush=True)
    else:
        print('  旧 Release 清理：无需删除', flush=True)
    tags = [ref for ref in old_refs if ref.startswith('refs/tags/') and ref[10:] not in keep]
    if tags:
        for offset in range(0, len(tags), 80):
            batch = tags[offset:offset + 80]
            run('git', 'push', '--atomic', 'origin',
                *[f'--force-with-lease={ref}:{old_refs[ref]}' for ref in batch],
                *[':' + ref for ref in batch])
            print(f'  旧远程标签清理：已删除 {min(offset + 80, len(tags))}/{len(tags)} 个历史标签', flush=True)
    else:
        print('  旧远程标签清理：无需删除', flush=True)
    local_tags = run('git', 'tag', '--list').splitlines()
    for tag in local_tags:
        if tag not in keep:
            run('git', 'tag', '-d', tag)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true', help='执行远程快照发布和旧版本清理')
    p.add_argument('--update-policy', action='store_true', help='将 README、AGENTS 和保留脚本的修改纳入唯一快照')
    p.add_argument('--backup-dir', type=Path, help='首次迁移必须指定仓库外的备份目录')
    p.add_argument('--publish-channel', choices=list(CHANNELS))
    p.add_argument('--apk', type=Path)
    p.add_argument('--run-publisher', nargs=2, metavar=('SCRIPT', 'CHANNEL'), help=argparse.SUPPRESS)
    args = p.parse_args()
    gitdir = Path(run('git', 'rev-parse', '--absolute-git-dir'))
    inherited = os.environ.get('NETAMADE_RELEASE_LOCK_FD')
    if os.name == 'nt':
        # Windows 没有 flock，子进程也无法继承锁 fd（pass_fds 不支持）；发布锁改用
        # 目录锁，子进程凭 NETAMADE_RELEASE_LOCK_FD（此处为锁目录路径）识别已持锁状态。
        lock_dir = gitdir / 'retain-current-releases.lock.d'
        if inherited:
            if not Path(inherited).is_dir():
                raise RuntimeError('继承的发布锁目录不存在')
            acquired = False
        else:
            try:
                lock_dir.mkdir()
            except FileExistsError:
                raise RuntimeError('另一个发布或清理任务正在运行；若确认无发布进行，可删除 ' + str(lock_dir))
            acquired = True

        @contextlib.contextmanager
        def release_lock_scope():
            try:
                yield None
            finally:
                if acquired:
                    lock_dir.rmdir()
        lock_scope = release_lock_scope()
    else:
        lock_path = gitdir / 'retain-current-releases.lock'
        if inherited:
            lock_file = os.fdopen(os.dup(int(inherited)), 'w')
            if (os.fstat(lock_file.fileno()).st_dev, os.fstat(lock_file.fileno()).st_ino) != (lock_path.stat().st_dev, lock_path.stat().st_ino):
                raise RuntimeError('继承的发布锁不属于当前仓库')
        else:
            lock_file = lock_path.open('w')
        if not inherited:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('另一个发布或清理任务正在运行')
        lock_scope = lock_file
    with lock_scope as lock:
        if args.run_publisher:
            # 父进程持有同一把锁，覆盖发布器准备元数据和 Python 远程发布全过程。
            if os.name == 'nt':
                env = dict(os.environ, NETAMADE_RELEASE_LOCK_FD=str(lock_dir))
                result = subprocess.run([sys.executable, *args.run_publisher], cwd=ROOT, env=env)
            else:
                env = dict(os.environ, NETAMADE_RELEASE_LOCK_FD=str(lock.fileno()))
                result = subprocess.run([sys.executable, *args.run_publisher], cwd=ROOT, env=env,
                                        pass_fds=(lock.fileno(),))
            if result.returncode:
                raise RuntimeError('发布脚本执行失败')
            return

        total_steps = 5 if args.publish_channel else 4
        current_step = 1

        if args.apply:
            stage(f"步骤 {current_step}/{total_steps}：准备发布元数据与核验远程状态")
        current_step += 1

        remote = refs()
        meta = metadata()
        if args.publish_channel and args.publish_channel not in meta:
            raise RuntimeError(f'缺少 {args.publish_channel}.json，请先准备发布元数据')
        for channel in meta:
            if channel != args.publish_channel:
                committed = json.loads(run('git', 'show', f'HEAD:{channel}.json'))
                if meta[channel] != committed:
                    raise RuntimeError(f'{channel}.json 存在未发布修改，本次停止')
        items = releases()
        if 'beta' not in meta and (any(r['tag_name'].endswith('-beta') for r in items)
                                   or any(ref.endswith('-beta') for ref in remote if ref.startswith('refs/tags/'))):
            raise RuntimeError('远程存在内测版，但本地缺少 beta.json，本次停止，禁止误删')
        keep = set(TOOLS) | {m['releaseVersion'] for m in meta.values()}
        if not args.apply:
            print(json.dumps({'keep_releases': sorted(keep), 'delete_releases': sum(r['tag_name'] not in keep for r in items),
                              'preserve_other_branches': {k: v for k, v in remote.items() if k.startswith('refs/heads/') and k != 'refs/heads/main'}}, ensure_ascii=False), flush=True)
            return

        ok("远程状态与发布元数据核验通过")

        migration = int(run('git', 'rev-list', '--count', 'HEAD')) != 1
        if args.backup_dir:
            directory = args.backup_dir.resolve()
            if directory == ROOT or ROOT in directory.parents:
                raise RuntimeError('恢复备份必须位于仓库外')
            backup(directory, items, remote, meta)

        if args.publish_channel:
            if not args.apk:
                raise RuntimeError('发布必须指定 --apk')
            stage(f"步骤 {current_step}/{total_steps}：准备 GitHub Release 并上传 APK")
            current_step += 1
            prepare_release(args.publish_channel, args.apk.resolve(), meta, items)
            items = releases()
            # gh 创建草稿可能新增当前版本标签，重新读取并用于有租约的更新。
            fresh = refs()
            for ref, sha in remote.items():
                if fresh.get(ref) != sha:
                    raise RuntimeError('准备 APK 时远程引用发生变化，请重新核对后重试')
            remote = fresh
            ok("GitHub Release 已公开发布")

        stage(f"步骤 {current_step}/{total_steps}：生成并推送唯一快照")
        current_step += 1
        check_assets(items, meta)
        commit, keep = snapshot(meta, args.publish_channel, remote, migration, args.update_policy)
        ok("唯一快照已原子推送到 main 并同步保留标签")

        stage(f"步骤 {current_step}/{total_steps}：清理旧 Release 与标签")
        current_step += 1
        cleanup(items, keep, remote)
        ok("旧版本与旧标签清理完成")

        stage(f"步骤 {current_step}/{total_steps}：核验最终发布状态")
        final = refs()
        final_items = releases()
        if {r['tag_name'] for r in final_items} != keep:
            raise RuntimeError('最终 Release 集合与保留清单不一致')
        if {r for r in final if r.startswith('refs/tags/')} != {'refs/tags/' + tag for tag in keep}:
            raise RuntimeError('远程标签清理未完成')
        if final['refs/heads/main'] != commit or run('git', 'rev-list', '--count', 'HEAD') != '1':
            raise RuntimeError('main 未保持唯一提交')
        for ref, sha in remote.items():
            if ref.startswith('refs/heads/') and ref != 'refs/heads/main' and final.get(ref) != sha:
                raise RuntimeError('其他分支在执行期间发生变化：' + ref)
        check_assets(final_items, meta)
        ok("核验通过：main 保持单次快照提交，渠道与工具版本符合保留规则")

        if args.publish_channel:
            finish_box(meta[args.publish_channel])


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as error:
        print('失败：' + str(error), file=sys.stderr)
        sys.exit(1)
