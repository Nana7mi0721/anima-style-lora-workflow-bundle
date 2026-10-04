#!/usr/bin/env python
"""离线回归：Cloudflare 挑战识别 + curl 兜底参数 + cookie 域隔离 + 凭据优先级。

不带 requests 的解释器会直接 SKIP（exit 0）：

    E:/LoRA_Train/anima_lora/.venv/Scripts/python.exe test/net-offline.py

（也可以用 `npm run test:py`，它调 PATH 上的 `python`。）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


try:
    import requests

    from animasl import net
except Exception as exc:  # pragma: no cover - 只在缺依赖时走到
    print(f"[net-offline] SKIP 无法导入（{type(exc).__name__}: {exc}）")
    raise SystemExit(0)


CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    "<style>body{}</style></head><body>Checking your browser before accessing danbooru.donmai.us"
    "</body></html>"
)


def make_session(proxy="", auth=None, ua="", referer="", jar=None):
    class FakeSession:
        pass

    s = FakeSession()
    s._animasl_proxy = proxy
    s._animasl_curl_ua = ua
    s.auth = auth
    s.headers = {"Referer": referer} if referer else {}
    s.cookies = jar if jar is not None else requests.cookies.RequestsCookieJar()
    return s


print("[net-offline] Cloudflare 挑战识别")
check("403 + Just a moment 判定为挑战", net.looks_like_challenge(403, CHALLENGE) is True)
check("403 + JSON 错误体不误判", net.looks_like_challenge(403, '{"error":"bad tag"}') is False)
check("200 + HTML 不误判（状态码闸门）", net.looks_like_challenge(200, "<html>hi</html>") is False)
check("503 + HTML 判定为挑战", net.looks_like_challenge(503, "<html>checking your browser</html>") is True)
check("bytes 正文也能判定", net.looks_like_challenge(403, CHALLENGE.encode("utf-8")) is True)
check("其它 4xx 不管（404 不是挑战）", net.looks_like_challenge(404, "<html>not found</html>") is False)

print("[net-offline] curl 兜底参数")
plain = net._curl_args(make_session(), 60, "https://danbooru.donmai.us/posts.json")
check("默认用自报家门的 UA", f"User-Agent: {net.CURL_UA}" in plain, net.CURL_UA)
check("CURL_UA 里没有浏览器痕迹", "Chrome" not in net.CURL_UA and "Mozilla" not in net.CURL_UA, net.CURL_UA)
check(
    "浏览器 UA 不会泄漏到 curl（session 里带的是 curl 用的 UA）",
    all("Chrome" not in arg for arg in plain),
    " ".join(plain[:3]),
)

rich = net._curl_args(
    make_session(proxy="http://127.0.0.1:7897", auth=("ReisenCCB", "kqMy6Qu94T2MRfQZGnmHYFQr"),
                 ua="animasl-test/1", referer="https://danbooru.donmai.us/"),
    60,
    "https://danbooru.donmai.us/posts.json",
)
check("代理转成 -x", "-x" in rich and "http://127.0.0.1:7897" in rich)
check("API key 用 -u", "-u" in rich and "ReisenCCB:kqMy6Qu94T2MRfQZGnmHYFQr" in rich)
check("自定义 curl UA 生效", "User-Agent: animasl-test/1" in rich)
check("Referer 透传", "Referer: https://danbooru.donmai.us/" in rich)

print("[net-offline] cookie 域隔离（一个文件放多站）")
COOKIES = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        ".exhentai.org\tTRUE\t/\tFALSE\t1900000000\tipb_member_id\t6274327",
        "exhentai.org\tFALSE\t/\tFALSE\t1900000000\tigneous\tlx42i5nupka5ah1vo",
        ".pawchive.pw\tTRUE\t/\tFALSE\t1900000000\t__ddg1_\tEUvXCSxw5QzhN9x04Ix7",
        "pawchive.pw\tFALSE\t/\tFALSE\t1900000000\tthumbSize\t180",
        "",
    ]
)
with tempfile.TemporaryDirectory() as tmp:
    cookie_file = Path(tmp) / "cookies.txt"
    cookie_file.write_text(COOKIES, encoding="utf-8")
    jar = net.load_cookie_jar(cookie_file)
    domains = sorted(c.domain for c in jar)
    check("四行都读进来", len(jar) == 4, f"{len(jar)} 条")
    check("域级 cookie 补了前导点", ".exhentai.org" in domains and ".pawchive.pw" in domains, str(domains))
    check("Host-Only cookie 保持裸域", "exhentai.org" in domains and "pawchive.pw" in domains, str(domains))

    for host, wanted, unwanted in (
        ("https://exhentai.org/api.php", ("ipb_member_id", "igneous"), ("thumbSize", "__ddg1_")),
        ("https://pawchive.pw/api/v1/posts", ("thumbSize", "__ddg1_"), ("ipb_member_id", "igneous")),
    ):
        args = net._curl_args(make_session(jar=jar), 60, host)
        sent = args[args.index("-b") + 1] if "-b" in args else ""
        check(
            f"{host.split('/')[2]} 只带自己的 cookie",
            all(name in sent for name in wanted) and all(name not in sent for name in unwanted),
            sent or "（没带 cookie）",
        )

    check("无关主机完全不发 cookie", "-b" not in net._curl_args(make_session(jar=jar), 60, "https://cdn.donmai.us/x.jpg"))

    session = requests.Session()
    session.cookies.update(jar)
    prepared = session.prepare_request(requests.Request("GET", "https://exhentai.org/"))
    cookie_header = prepared.headers.get("Cookie", "")
    check("requests 侧也不串站", "ipb_member_id" in cookie_header and "thumbSize" not in cookie_header, cookie_header)
    sub = session.prepare_request(requests.Request("GET", "https://forums.exhentai.org/"))
    check("域级 cookie 会发给子域", "ipb_member_id" in sub.headers.get("Cookie", ""))
    # 注意：requests 的 create_cookie(domain=...) 一律 domain_specified=True，
    # 所以"Host-Only 不发子域"这条浏览器语义在 requests 侧不成立（同站子域仍会收到）。
    # 这里真正要保证的是**跨站隔离** —— 同站子域（比如 pawchive 的 n2 CDN）收到反而需要。
    cdn = session.prepare_request(requests.Request("GET", "https://n2.pawchive.pw/data/x.png"))
    cdn_header = cdn.headers.get("Cookie", "")
    check(
        "同站 CDN 子域仍拿得到自己的 cookie（不串到别的站）",
        "__ddg1_" in cdn_header and "ipb_member_id" not in cdn_header and "igneous" not in cdn_header,
        cdn_header,
    )

print("[net-offline] 凭据优先级（设置页 > 环境变量）")
saved = {key: os.environ.get(key) for key in ("DANBOORU_LOGIN", "DANBOORU_API_KEY")}


class FakeConfig:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)


try:
    os.environ["DANBOORU_LOGIN"] = "env-user"
    os.environ["DANBOORU_API_KEY"] = "env-key"
    login, key = net.danbooru_auth(FakeConfig({"danbooru_login": " page-user ", "danbooru_api_key": "page-key"}))
    check("设置页优先于环境变量", (login, key) == ("page-user", "page-key"), f"{login}/{key}")
    login, key = net.danbooru_auth(FakeConfig({}))
    check("设置页为空时回落到环境变量", (login, key) == ("env-user", "env-key"), f"{login}/{key}")
    login, key = net.danbooru_auth(FakeConfig({"danbooru_login": "   "}))
    check("空白字符串等于没配", (login, key) == ("env-user", "env-key"), f"{login}/{key}")
finally:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

print("[net-offline] 开关")
net._curl_ok = None
os.environ["ANIMASL_NO_CURL"] = "1"
check("ANIMASL_NO_CURL=1 关掉兜底", net.curl_available() is False)
os.environ.pop("ANIMASL_NO_CURL", None)
net._curl_ok = None
check("默认可用的机器上能探测到 curl", net.curl_available() is True)

print("")
if failures:
    print(f"[net-offline] {len(failures)} 个失败：")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("[net-offline] ALL OK")
