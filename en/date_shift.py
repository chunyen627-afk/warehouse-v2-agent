"""date_shift.py — 把 demo 的時間軸平移到「今天」（2026-08-03）。

## 為什麼要有這個
Demo 資料的快照日寫死 `2026-05-26`，但展場是 **9/2-9/4**，而且機器**提前交客戶**
（8 月就會被打開）。訪客問「今天進了什麼」卻看到三個月前的資料，現場一戳就破。

而且動態模擬把新異動全寫在 `snap_date`（05-26），造成兩個症狀：
  ① 訪客看到的異動日期是三個月前
  ② 幾百筆全堆在同一天 → 那天出貨量是日均的 20 倍
     → **60 個商品同時觸發「出貨暴增」假警報**（實測 burst 60 筆）

## 做法：整條時間軸平移，不改原始檔
`offset = 今天 - snapshot_date`，把 movements / batches / orders / config
的日期全部 +offset。**只在記憶體裡做**，`warehouse_data/` 原檔不動
⇒ `reset_demo` 仍然回得到乾淨狀態，git 也不會有雜訊。

平移後：
  - 「今天」= 真實今天（客戶 8 月開機看到 8 月、展場 9/2 看到 9/2）
  - 三個月歷史跟著移（本週/上週/本月/昨天 全部仍可查）
  - 動態模擬寫入 `snap_date` 也就是今天 ⇒ 日期一致、暴增偵測回歸正常

## ⚠️ 為何不寫死 9/2
機器提前交客戶，寫死的話 8 月測試期顯示 9/2、展後也永遠卡在 9/2。
用「今天」則三個時期都對。
"""
from datetime import date as _date, timedelta as _td


def _shift_str(ds: str, days: int) -> str:
    """'2026-05-26' → 平移後的日期字串。非日期格式原樣回傳。"""
    if not ds or len(ds) < 10:
        return ds
    try:
        return str(_date.fromisoformat(ds[:10]) + _td(days=days)) + ds[10:]
    except Exception:
        return ds


def _effective_today() -> _date:
    """今天是哪天 —— 帶「只進不退」防線。

    ⚠️ **RPI5 的 RTC 沒外接電池**（user 2026-08-03 確認）⇒ 斷電後靠
    `fake-hwclock` 回復（關機時把時間寫檔、開機讀回），所以最壞是
    **時間停在上次關機時刻**（慢幾小時到一天），不會歸零到 1970。
    但展場若沒網路、又跨日開機，時間可能**倒退**；而時間一倒退，
    整條資料時間軸就往回跳（今天的異動變成「未來」，撐天/效期全亂）。

    ⇒ 記住看過的最大日期（`.last_demo_date`），**只往前不往後**。
    真的要往回調（例如測試）就刪掉那個檔。
    """
    today = _date.today()
    try:
        from pathlib import Path
        f = Path(__file__).parent / ".last_demo_date"
        if f.exists():
            seen = _date.fromisoformat(f.read_text(encoding="utf-8").strip()[:10])
            if seen > today:
                return seen                    # 時鐘倒退 → 沿用上次的日期
        f.write_text(str(today), encoding="utf-8")
    except Exception:
        pass                                    # 記錄失敗不影響主流程
    return today


def compute_offset(snapshot_date: str, target: _date | None = None) -> int:
    """要平移幾天。snapshot_date 空或壞 → 0（不動）。"""
    if not snapshot_date:
        return 0
    try:
        base = _date.fromisoformat(snapshot_date[:10])
    except Exception:
        return 0
    return ((target or _effective_today()) - base).days


def shift_bundle(bundle: dict, target: _date | None = None) -> dict:
    """把 loader 產出的整包資料平移到 target（預設今天）。就地修改並回傳。

    平移對象：
      snapshot_date / movements[].date / batches[].mfg_date,expire_date
      / orders[].date / _v2_config.snapshot_date
    """
    snap = bundle.get("snapshot_date", "")
    off = compute_offset(snap, target)
    if off == 0:
        return bundle
    # ⚠️ 冪等性：offset 一律從**原始檔的 snapshot_date**（永遠是 2026-05-26）
    #   重算，不是拿上次平移後的結果再平移 ⇒ 載入幾次都得到同一個結果。
    #   （實測連載 3 次，movements 範圍完全一致。）

    bundle["snapshot_date"] = _shift_str(snap, off)

    for m in bundle.get("movements", []) or []:
        if m.get("date"):
            m["date"] = _shift_str(m["date"], off)

    for b in bundle.get("batches", []) or []:
        for k in ("mfg_date", "expire_date"):
            if b.get(k):
                b[k] = _shift_str(b[k], off)

    for o in bundle.get("orders", []) or []:
        if o.get("date"):
            o["date"] = _shift_str(o["date"], off)
        for ln in o.get("lines", []) or []:
            for k in ("date", "eta", "received_date"):
                if ln.get(k):
                    ln[k] = _shift_str(ln[k], off)

    cfg = bundle.get("_v2_config")
    if isinstance(cfg, dict) and cfg.get("snapshot_date"):
        cfg["snapshot_date"] = _shift_str(cfg["snapshot_date"], off)

    bundle["_date_offset_days"] = off
    return bundle
