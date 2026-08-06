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

    # 🚨 只平移「原始 seed 範圍內」的日期（≤ 原始 snapshot_date）。
    #   執行期新寫入的資料（訪客操作、動態模擬）**已經是平移後的日期**
    #   （`commit_movement` 用的 `snap_date` 就是平移後的今天），
    #   再平移一次會變成未來 ⇒ 實測 08-03 的 222 件被推到 **2026-10-11**，
    #   而 anomaly 讀到「今天出 222 件 vs 日均 9」→ **60 筆假暴增警報**。
    #   判準：日期 > 原始 snapshot_date 的一律不動（那是執行期產生的）。
    try:
        _cutoff = _date.fromisoformat(snap[:10])
    except Exception:
        _cutoff = None

    def _shift_hist(ds: str) -> str:
        """只平移歷史（原始 seed）日期；執行期寫入的原樣保留。"""
        if not ds or _cutoff is None:
            return _shift_str(ds, off)
        try:
            if _date.fromisoformat(ds[:10]) > _cutoff:
                return ds                      # 執行期資料，已是正確日期
        except Exception:
            return ds
        return _shift_str(ds, off)

    bundle["snapshot_date"] = _shift_str(snap, off)

    for m in bundle.get("movements", []) or []:
        if m.get("date"):
            m["date"] = _shift_hist(m["date"])

    # 🚨 批次日期一律**無條件平移**（2026-08-06 user 回報「快過期警告是否固定」
    #   時追出來的實害）：`_shift_hist` 的判準「日期 > 原始快照日 = 執行期資料」
    #   對 movements 正確（歷史異動一定在快照日之前），但 **`expire_date`
    #   天生就在未來**——154 批有 138 批的到期日晚於原始快照日，全被誤判成
    #   執行期資料而留在原地。⇒ 庫存/歷史平移了 72 天、到期日沒動 = 被時間
    #   追過去：實測 28 批到期警示裡 **15 批顯示已過期**、最急的「剩 -69 天」
    #   （訪客看到負數天數會直接認定系統壞掉），而且**逐日惡化**。
    #   安全性：`expire_date`/`mfg_date` 沒有執行期寫入來源（訪客新增商品不建
    #   批次、模擬器只寫 movement），不存在重複平移風險；冪等性仍由原始 config
    #   的固定 snapshot_date（2026-05-26）保證。
    #   驗證：原始資料 vs 原始快照日 = 已過期 0 批/紅燈 3 批（設計本意），
    #        修好後 vs 今天 = 已過期 0 批/紅燈 3 批 ✅ 完全還原。
    for b in bundle.get("batches", []) or []:
        for k in ("mfg_date", "expire_date"):
            if b.get(k):
                b[k] = _shift_str(b[k], off)

    for o in bundle.get("orders", []) or []:
        if o.get("date"):
            o["date"] = _shift_hist(o["date"])
        for ln in o.get("lines", []) or []:
            for k in ("date", "eta", "received_date"):
                if ln.get(k):
                    ln[k] = _shift_hist(ln[k])

    cfg = bundle.get("_v2_config")
    if isinstance(cfg, dict) and cfg.get("snapshot_date"):
        cfg["snapshot_date"] = _shift_str(cfg["snapshot_date"], off)

    bundle["_date_offset_days"] = off
    return bundle
