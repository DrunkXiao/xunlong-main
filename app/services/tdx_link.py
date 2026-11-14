"""通达信联动（单向外发）：把当前标的代码 GET 给本机桥接服务。

口径：

- 只发一次 `GET {base_url}/tdx?code=600156`，**不读响应内容、不写库、不改行情**；
- 桥接只认 6 位代码，带后缀（`.SH`）它不一定认，所以统一剥掉；
- 成功就一条「已发送」，失败只报原因（未启动 / 超时 / HTTP 错误），**不重试**——
  连点重试会把请求打成风暴；
- 只用标准库（`urllib`），不因为一个按钮给项目加新依赖；
- **显式绕开系统代理**：`HTTP_PROXY` 会让 `127.0.0.1` 的请求先转到代理，
  代理回一个 200 就成了「假成功」；本机回环地址必须直连；
- 跑在调用方的后台线程里，界面不会卡在这 2 秒上。
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:3000"
DEFAULT_TIMEOUT = 2.0

# 空 ProxyHandler = 不走任何代理，也不会去读 HTTP_PROXY/HTTPS_PROXY
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def endpoint(base_url: str = DEFAULT_BASE) -> str:
    return (base_url or DEFAULT_BASE).rstrip("/") + "/tdx"


def digits_of(symbol: str) -> str:
    """`600156.SH` → `600156`。"""
    raw = str(symbol or "").strip().upper()
    return raw.split(".")[0] if raw else ""


def build_url(code: str, base_url: str = DEFAULT_BASE) -> str:
    return f"{endpoint(base_url)}?{urllib.parse.urlencode({'code': code})}"


def send(symbol: str, base_url: str = DEFAULT_BASE,
         timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """发一次联动请求，返回 (是否成功, 给人看的文案)。"""
    code = digits_of(symbol)
    if len(code) != 6 or not code.isdigit():
        return False, f"联动失败：{symbol!r} 不是 6 位代码，未发送"

    url = build_url(code, base_url)
    request = urllib.request.Request(url, method="GET")
    try:
        # 只确认请求被接住；桥接返回什么内容与本程序无关
        with _OPENER.open(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        return False, f"联动失败：桥接返回 HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return False, f"联动失败：{timeout:g} 秒内没有响应（桥接服务没启动？）"
        if isinstance(reason, ConnectionRefusedError):
            return False, f"联动失败：服务连不上，桥接服务没启动？"
        return False, f"联动失败：服务无法访问（{reason}）"
    except (socket.timeout, TimeoutError):
        return False, f"联动失败：{timeout:g} 秒内没有响应（桥接服务没启动？）"
    except OSError as exc:
        return False, f"联动失败：{type(exc).__name__} {exc}"
    return True, f"已联动通达信 → {code}"
