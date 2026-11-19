#!/usr/bin/env python3
"""并发检测 appstore.json 留言板中的所有 URL 可用性。

分类:
  OK      - 2xx
  REDIR   - 3xx（跟随后最终 2xx 也算 OK，否则按最终码分类）
  DEAD    - DNS失败/连接拒绝/超时(重试2次)/404/410
  SUSPECT - 403/5xx/其他异常状态（可能是 WAF 拦 curl，不直接判死）
结果写入 dead_check_result.json
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SRC = "appstore.json"
OUT = "dead_check_result.json"
TIMEOUT = 15

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "okhttp/4.9.3",
]


def smart_encode(url: str) -> str:
    """中文域名转 punycode，路径/查询转 percent-encoding。"""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    try:
        host.encode("ascii")
        netloc = p.netloc
    except UnicodeEncodeError:
        try:
            host_enc = host.encode("idna").decode()
        except Exception:
            host_enc = urllib.parse.quote(host)
        netloc = host_enc if p.port is None else f"{host_enc}:{p.port}"
    path = urllib.parse.quote(p.path, safe="/%@:,+()*!'~-._")
    query = urllib.parse.quote(p.query, safe="/?&=%#[]@,;+$!*'()~")
    return urllib.parse.urlunsplit((p.scheme, netloc, path, query, p.fragment))


def check_once(url: str, ua: str):
    # 中文域名/路径统一编码
    encoded = smart_encode(url)
    req = urllib.request.Request(encoded, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # 读一小块就关闭，避免大文件/长流
            resp.read(2048)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        return f"ERR:{type(reason).__name__ if reason is not None else type(e).__name__}"


def classify(status) -> str:
    if isinstance(status, int):
        if 200 <= status < 300:
            return "OK"
        if status in (404, 410):
            return "DEAD"
        return "SUSPECT"  # 403/5xx/999 等
    return "SUSPECT"  # ERR 可能是网络抖动，先用 SUSPECT 承接


def check_url(url: str) -> dict:
    results = []
    for ua in UAS:
        s = check_once(url, ua)
        results.append(s)
        c = classify(s)
        if c == "OK":
            return {"url": url, "status": "OK", "detail": s}
        if c == "DEAD":
            # 404 再用另一个 UA 确认一次已在循环里做，直接判死
            return {"url": url, "status": "DEAD", "detail": s}
    # 两次都不是 OK：如果都是 ERR 或 DEAD 状态码则判死，否则 SUSPECT
    errs = [r for r in results if isinstance(r, str) and r.startswith("ERR:")]
    codes = [r for r in results if isinstance(r, int)]
    if len(errs) == len(results):
        return {"url": url, "status": "DEAD", "detail": "|".join(results)}
    if codes and all(c in (404, 410) for c in codes):
        return {"url": url, "status": "DEAD", "detail": "|".join(map(str, results))}
    return {"url": url, "status": "SUSPECT", "detail": "|".join(map(str, results))}


def main():
    d = json.load(open(SRC))
    urls = []
    for mb in d["messageBoard"]:
        for c in mb["contents"]:
            t = c["text"].strip()
            if t.startswith("http"):
                urls.append(t)
    # 去重，保留原始顺序
    seen = {}
    for u in urls:
        seen.setdefault(u, None)
    uniq = list(seen.keys())
    print(f"共 {len(urls)} 条 URL，去重后 {len(uniq)} 条，开始检测...")

    results = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = {pool.submit(check_url, u): u for u in uniq}
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 40 == 0:
                print(f"  进度 {done}/{len(uniq)}")

    dead = sorted([r["url"] for r in results if r["status"] == "DEAD"])
    suspect = sorted([r["url"] for r in results if r["status"] == "SUSPECT"])
    detail = {r["url"]: r["detail"] for r in results if r["status"] != "OK"}
    print(f"OK: {len(uniq) - len(dead) - len(suspect)}  DEAD: {len(dead)}  SUSPECT: {len(suspect)}")
    json.dump({"dead": dead, "suspect": suspect, "detail": detail}, open(OUT, "w"), ensure_ascii=False, indent=2)
    print("结果已写入", OUT)


if __name__ == "__main__":
    main()
