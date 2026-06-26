"""
test_e2e.py — E2E 自動化測試（含 server.py 校正層 + finance.py 業務邏輯）

用法：
    # 1. 在 RPI5 或本機把 server 跑起來
    #    py -3.11 test/server.py

    # 2. 在工作站跑這支腳本（指向 server 位址）
    py -3.11 test_e2e.py --host http://192.168.4.1:8000
    py -3.11 test_e2e.py --host http://localhost:8000

驗證內容：
  - 63 條 case（與 test_model.py / test_gguf.py 共用 test_cases.py）
  - 同時連 /ws (訪客) + /ws/display (展示螢幕)
  - 訪客送 chat 後等 done，再從 display 的 parsed trace 拿實際 function + args
  - 比對 expected_tool / expected_args，輸出命中率

需要套件：
    pip install websockets
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_cases import CASES, E2E_EXTRA_CASES

ALL_CASES = CASES + E2E_EXTRA_CASES


def to_ws_url(http_url: str, path: str) -> str:
    u = urlparse(http_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}{path}"


async def reset_server(http_url: str):
    """打 /reset 把 finance state 拉回初始（避免上一輪殘留）"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{http_url}/reset", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[warn] /reset 失敗（不影響測試）：{e}")


async def run_one(visitor_ws, display_queue: asyncio.Queue, prompt: str, timeout: float = 60.0):
    """
    送一條 prompt，等到：
      - display 收到 parsed (拿 function + args)，或
      - 訪客收到 done (代表有可能是 guided/rejected/no_function)
    回傳 dict：{status, function, args, view, raw}
    """
    # 清空舊的 display 訊息
    while not display_queue.empty():
        try:
            display_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    await visitor_ws.send(json.dumps({"type": "chat", "text": prompt}))

    result = {
        "status": "unknown",
        "function": None,
        "args": {},
        "view": None,
        "raw": None,
    }
    done_received = False
    parsed_received = False

    async def wait_visitor():
        nonlocal done_received
        while True:
            msg = await visitor_ws.recv()
            try:
                obj = json.loads(msg)
            except Exception:
                continue
            t = obj.get("type")
            if t == "done":
                r = obj.get("result") or {}
                result["view"] = r.get("view")
                done_received = True
                return
            elif t == "error":
                result["status"] = "error"
                result["raw"] = obj.get("text")
                done_received = True
                return

    async def wait_display():
        nonlocal parsed_received
        while True:
            obj = await display_queue.get()
            stage = obj.get("stage")
            if stage == "llm_output":
                result["raw"] = obj.get("raw")
            elif stage == "parsed":
                result["function"] = obj.get("function")
                result["args"] = obj.get("args") or {}
                result["status"] = "parsed"
                parsed_received = True
            elif stage == "no_function":
                result["status"] = "no_function"
            elif stage == "rejected":
                result["status"] = "rejected"
            elif stage == "guided":
                result["status"] = "guided"
            elif stage == "result":
                return  # parsed + executed 都拿到了

    try:
        await asyncio.wait_for(
            asyncio.gather(wait_visitor(), wait_display()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        result["status"] = "timeout"

    return result


def args_match(actual: dict, expected: dict):
    diffs = []
    if not expected:           # expected_args None/{} → 不檢查參數(reject case 或不在意參數)
        return (True, diffs)
    if not actual:
        actual = {}
    for k, v in expected.items():
        a = actual.get(k, "<missing>")
        if str(a) != str(v):
            diffs.append(f"{k}: got={a!r} expected={v!r}")
    return (len(diffs) == 0, diffs)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:8000",
                    help="server 位址，例如 http://192.168.4.1:8000")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="單條 case 最長等待秒數")
    args = ap.parse_args()

    await reset_server(args.host)

    ws_visitor_url = to_ws_url(args.host, "/ws")
    ws_display_url = to_ws_url(args.host, "/ws/display")

    print(f"連線 visitor: {ws_visitor_url}")
    print(f"連線 display: {ws_display_url}")

    display_queue: asyncio.Queue = asyncio.Queue()

    async def display_listener(ws):
        try:
            async for msg in ws:
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                if obj.get("type") == "trace":
                    await display_queue.put(obj)
        except websockets.ConnectionClosed:
            pass

    async with websockets.connect(ws_display_url, max_size=2**22) as display_ws, \
               websockets.connect(ws_visitor_url, max_size=2**22) as visitor_ws:

        listener_task = asyncio.create_task(display_listener(display_ws))

        # 等一下確保 display socket 在 server 端註冊好
        await asyncio.sleep(0.3)

        print("=" * 78)
        print(f" E2E 自動化測試 — {len(ALL_CASES)} 條 case")
        print("=" * 78)

        n_total = len(ALL_CASES)
        n_format_ok = 0
        n_tool_ok = 0
        n_full_ok = 0
        results_by_tool: dict = {}

        for i, (prompt, expected_tool, expected_args) in enumerate(ALL_CASES, 1):
            r = await run_one(visitor_ws, display_queue, prompt, timeout=args.timeout)

            has_format = r["function"] is not None
            actual_tool = r["function"]
            tool_ok = (actual_tool == expected_tool)
            args_ok, diffs = args_match(r["args"], expected_args) if tool_ok else (False, [])
            full_ok = tool_ok and args_ok

            if has_format: n_format_ok += 1
            if tool_ok:    n_tool_ok += 1
            if full_ok:    n_full_ok += 1

            bucket = results_by_tool.setdefault(expected_tool, [0, 0])
            bucket[1] += 1
            if full_ok:
                bucket[0] += 1

            if not has_format:
                flag = f"[{r['status'].upper()}]"
            elif not tool_ok:
                flag = f"[TOOL: {actual_tool}]"
            elif not args_ok:
                flag = "[ARGS]"
            else:
                flag = "[OK]"

            print(f"  {i:2d}/{n_total} {flag:18s} expect={str(expected_tool):26s} prompt={prompt!r}")
            if has_format and not full_ok:
                print(f"        got function={actual_tool} args={r['args']}")
                for d in diffs:
                    print(f"        ! {d}")
            elif not has_format and r.get("raw"):
                print(f"        raw={str(r['raw'])[:120]}")

        listener_task.cancel()

        print()
        print("=" * 78)
        print(" E2E 結果總結（含 server.py 校正層 + finance.py）")
        print("=" * 78)
        print(f"  格式正確       : {n_format_ok}/{n_total}  ({n_format_ok/n_total*100:5.1f}%)")
        print(f"  工具名正確     : {n_tool_ok}/{n_total}  ({n_tool_ok/n_total*100:5.1f}%)")
        print(f"  工具名+參數全對: {n_full_ok}/{n_total}  ({n_full_ok/n_total*100:5.1f}%)  <-- E2E 命中率")
        print()
        print("  各 function 全對命中率:")
        for tool in sorted(results_by_tool.keys(), key=lambda x: str(x)):
            p, t = results_by_tool[tool]
            bar = "#" * p + "." * (t - p)
            print(f"    {str(tool):26s} {p}/{t}  {bar}")
        print("=" * 78)

        if n_full_ok < n_total:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
