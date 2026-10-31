#!/usr/bin/env python3
"""发布当前快照并清理旧应用版本；默认仅显示计划。"""
import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
REPO = 'z0brk/netamade-releases'
TOOLS = {'speedtest': '5MB.img', 'android-fio-arm64': 'fio-android-arm64'}
OWNED = ['README.md', 'AGENTS.md', 'scripts/retain_current_releases.py']


def run(*args, env=None, input=None):
    p = subprocess.run(args, cwd=ROOT, env=env, input=input, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if p.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} 失败：{p.stderr.strip()}')
    return p.stdout.strip()


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
    for channel in ('stable', 'testing'):
        m = json.loads((ROOT / f'{channel}.json').read_text())
        if not m['releaseVersion'].endswith('-' + channel) or m['testing'] != (channel == 'testing'):
            raise RuntimeError(f'{channel}.json 渠道不一致')
        if len(m['apkMd5']) != 32 or not (ROOT / f'{channel}.md').read_text().strip():
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
        print('GitHub APK 附件状态、文件名、大小及 SHA-256 核验通过', flush=True)
    else:
        print('GitHub APK 附件状态、文件名及大小核验通过；API 未提供摘要，未校验远程内容摘要', flush=True)
    return bool(digest)


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
            'name': m['releaseVersion'], 'draft': True, 'prerelease': False,
            'body': (ROOT / f'{channel}.md').read_text(),
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
    print(f'上传 APK 到 Release {current["id"]}：{m["releaseFilename"]}', flush=True)
    run('gh', 'api', '--method', 'POST', upload_url,
        '--header', 'Content-Type: application/vnd.android.package-archive', '--input', str(apk))
    current = api('releases/' + str(current['id']))
    check_uploaded_apk(current, m['releaseFilename'], size, sha256)
    api('releases/' + str(current['id']), 'PATCH', {'draft': False, 'make_latest': 'true' if channel == 'stable' else 'false'})


def backup(directory, items, remote):
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    (directory / 'releases.json').write_text(json.dumps(items, ensure_ascii=False, indent=2))
    (directory / 'refs.json').write_text(json.dumps(remote, indent=2))
    (directory / 'worktree-status.txt').write_text(run('git', 'status', '--short'))
    (directory / 'worktree.patch').write_text(run('git', 'diff', '--binary', 'HEAD'))
    run('git', 'clone', '--mirror', run('git', 'remote', 'get-url', 'origin'), str(directory / 'repository.git'))
    for filename in ('stable.json', 'stable.md', 'testing.json', 'testing.md'):
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
            begin = baseline.index('    log = run(', baseline.index('def collect_releases()'))
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
    return commit, keep


def cleanup(items, keep, old_refs):
    obsolete = [r for r in items if r['tag_name'] not in keep]
    def delete_release(r):
        api('releases/' + str(r['id']), 'DELETE')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(delete_release, r): r for r in obsolete}
        for count, future in enumerate(concurrent.futures.as_completed(pending), 1):
            future.result()
            if count % 25 == 0 or count == len(obsolete):
                print(f'旧 Release 已删除 {count}/{len(obsolete)}', flush=True)
    tags = [ref for ref in old_refs if ref.startswith('refs/tags/') and ref[10:] not in keep]
    for offset in range(0, len(tags), 80):
        batch = tags[offset:offset + 80]
        run('git', 'push', '--atomic', 'origin',
            *[f'--force-with-lease={ref}:{old_refs[ref]}' for ref in batch],
            *[':' + ref for ref in batch])
        print(f'旧远程标签已删除 {min(offset + 80, len(tags))}/{len(tags)}', flush=True)
    local_tags = run('git', 'tag', '--list').splitlines()
    for tag in local_tags:
        if tag not in keep:
            run('git', 'tag', '-d', tag)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true', help='执行远程快照发布和旧版本清理')
    p.add_argument('--update-policy', action='store_true', help='将 README、AGENTS 和保留脚本的修改纳入唯一快照')
    p.add_argument('--backup-dir', type=Path, help='首次迁移必须指定仓库外的备份目录')
    p.add_argument('--publish-channel', choices=['stable', 'testing'])
    p.add_argument('--apk', type=Path)
    p.add_argument('--run-publisher', nargs=2, metavar=('SCRIPT', 'CHANNEL'), help=argparse.SUPPRESS)
    args = p.parse_args()
    gitdir = Path(run('git', 'rev-parse', '--absolute-git-dir'))
    lock_path = gitdir / 'retain-current-releases.lock'
    inherited = os.environ.get('NETAMADE_RELEASE_LOCK_FD')
    if inherited:
        lock_file = os.fdopen(os.dup(int(inherited)), 'w')
        if (os.fstat(lock_file.fileno()).st_dev, os.fstat(lock_file.fileno()).st_ino) != (lock_path.stat().st_dev, lock_path.stat().st_ino):
            raise RuntimeError('继承的发布锁不属于当前仓库')
    else:
        lock_file = lock_path.open('w')
    with lock_file as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一个发布或清理任务正在运行')
        if args.run_publisher:
            # 父进程持有同一把锁，覆盖 Shell 准备元数据和 Python 远程发布全过程。
            env = dict(os.environ, NETAMADE_RELEASE_LOCK_FD=str(lock.fileno()))
            result = subprocess.run(['bash', *args.run_publisher], cwd=ROOT, env=env,
                                    pass_fds=(lock.fileno(),))
            if result.returncode:
                raise RuntimeError('发布脚本执行失败')
            return
        remote = refs()
        meta = metadata()
        for channel in ('stable', 'testing'):
            if channel != args.publish_channel:
                committed = json.loads(run('git', 'show', f'HEAD:{channel}.json'))
                if meta[channel] != committed:
                    raise RuntimeError(f'{channel}.json 存在未发布修改，本次停止')
        items = releases()
        keep = set(TOOLS) | {m['releaseVersion'] for m in meta.values()}
        print(json.dumps({'keep_releases': sorted(keep), 'delete_releases': sum(r['tag_name'] not in keep for r in items),
                          'preserve_other_branches': {k: v for k, v in remote.items() if k.startswith('refs/heads/') and k != 'refs/heads/main'}}, ensure_ascii=False), flush=True)
        if not args.apply:
            return
        migration = int(run('git', 'rev-list', '--count', 'HEAD')) != 1
        if migration and not args.backup_dir:
            raise RuntimeError('首次清理有历史的 main 必须传入 --backup-dir')
        if args.backup_dir:
            directory = args.backup_dir.resolve()
            if directory == ROOT or ROOT in directory.parents:
                raise RuntimeError('恢复备份必须位于仓库外')
            backup(directory, items, remote)
        if args.publish_channel:
            if not args.apk:
                raise RuntimeError('发布必须指定 --apk')
            prepare_release(args.publish_channel, args.apk.resolve(), meta, items)
            items = releases()
            # gh 创建草稿可能新增当前版本标签，重新读取并用于有租约的更新。
            fresh = refs()
            for ref, sha in remote.items():
                if fresh.get(ref) != sha:
                    raise RuntimeError('准备 APK 时远程引用发生变化，请重新核对后重试')
            remote = fresh
        check_assets(items, meta)
        commit, keep = snapshot(meta, args.publish_channel, remote, migration, args.update_policy)
        print('唯一快照已推送：' + commit, flush=True)
        cleanup(items, keep, remote)
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
        print('完成：main 一次提交、两个应用 Release、两个工具 Release；其他分支未修改。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        print('失败：' + str(error), file=sys.stderr)
        sys.exit(1)
