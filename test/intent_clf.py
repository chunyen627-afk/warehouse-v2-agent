"""
意圖分類器封裝 — server.py 用
載入 intent_clf.bin，提供 predict(text) → (intent, confidence)
"""
import re, pathlib, logging

log = logging.getLogger("demo")
_MODEL = None
_BIN   = pathlib.Path(__file__).parent / "intent_clf.bin"

# model 輸出的 function 與 clf 預測的 intent 不符時的處置門檻
# conf 夠高才信任 clf 的預測去做校正
CONF_THRESHOLD = 0.90

# fasttext label → function name 對照（少數有差異的）
LABEL_TO_FUNC = {
    "search_log": "search_log",
    "judge_cause_found": "search_log",
}


def _char_ngram(text: str) -> str:
    """jieba 分詞（與訓練時一致）"""
    try:
        import jieba
        jieba.setLogLevel(60)
        return " ".join(jieba.cut(text.strip()))
    except ImportError:
        # fallback: 字元切分
        tokens = []
        for ch in text:
            if re.match(r'[\w]', ch):
                if tokens and re.match(r'[\w]', tokens[-1]): tokens[-1] += ch
                else: tokens.append(ch)
            elif '一' <= ch <= '鿿':
                tokens.append(ch)
        return " ".join(tokens)


def reload():
    """強制重載（clf watchdog 自癒用：自檢失敗先試 reload 再認輸）。"""
    global _MODEL
    _MODEL = None
    load()


def load():
    global _MODEL
    if _MODEL is not None:
        return
    if not _BIN.exists():
        log.warning(f"[intent_clf] {_BIN} 不存在，跳過載入（請先執行 train_intent_clf.py）")
        return
    try:
        import fasttext
        _MODEL = fasttext.load_model(str(_BIN))
        log.info(f"[intent_clf] 載入完成：{_BIN.stat().st_size//1024} KB")
    except Exception as e:
        log.warning(f"[intent_clf] 載入失敗：{e}")


def predict(text: str) -> tuple[str, float]:
    """
    回傳 (intent_name, confidence)
    intent_name = "unclear" 表示信心不足，應觸發 clarify
    """
    if _MODEL is None:
        return "unknown", 1.0   # 沒載入 → 不干預，讓 270M 決定
    tok = _char_ngram(text.strip())
    if not tok:
        return "unclear", 0.0
    try:
        labels, probs = _MODEL.predict(tok, k=1)
        intent = labels[0].replace("__label__", "")
        conf   = float(list(probs)[0])
    except ValueError:
        # fasttext ≤0.9.3 的 predict() 末行 np.array(probs, copy=False) 在
        # numpy≥2 直接 ValueError → clf 整條靜默死亡、每句 fallback LLM。
        # RPI5 曾因此中招（2026-07-16 抓到：journalctl 全天 0 次 intent_clf
        # primary、C18 全滅，靠 LLM+校正層扛住全綠）。改走底層 binding 拿
        # (prob, label)，不經 numpy。
        try:
            preds = _MODEL.f.predict(tok + "\n", 1, 0.0, "strict")
            if not preds:
                return "unclear", 0.0
            conf, label = preds[0]
            return label.replace("__label__", ""), float(conf)
        except Exception:
            return "unknown", 1.0
    except Exception:
        return "unknown", 1.0
    return intent, conf


# ── 金絲雀自檢（2026-07-16 numpy2 事件後加）────────────────────────
# predict 的 fail-soft 設計會把內部崩潰吞成 ("unknown", 1.0)：主路由死掉時系統
# 照常運作（每句 fallback LLM），但毫秒級路由與 C18 保護靜默蒸發——曾在 RPI5
# 上死了多輪沒人發現（fasttext≤0.9.3 × numpy≥2）。金絲雀=固定句必須分對且高
# 信心，開機與週期各驗一次，死掉就大聲（log CRITICAL + /health 曝光）。
_CANARY = [
    ("欸幫我看哪些快缺貨", "list_low_stock"),
    ("不好意思幫我查一下藍牙耳機庫存", "query_inventory"),
    ("這個月熱銷排行", "list_hot_items"),
]


def self_check() -> tuple[bool, str]:
    """回 (ok, 說明)。ok=False 表示 clf 沒有真實分類能力（未載入/內部崩潰）。"""
    if _MODEL is None:
        return False, "model not loaded"
    for sent, want in _CANARY:
        intent, conf = predict(sent)
        if intent != want or conf < 0.8:
            return False, f"canary「{sent}」→ ({intent}, {conf:.2f})，應為 {want}"
    return True, "ok"


def check_mismatch(user_text: str, model_func: str) -> tuple[bool, str, float]:
    """
    主要對外介面：校驗 270M 輸出的 function 是否與 clf 預測吻合。
    回傳 (mismatch: bool, clf_intent: str, conf: float)
    mismatch=True + conf > CONF_THRESHOLD → 可考慮用 clf_intent 覆蓋 model_func
    """
    clf_intent, conf = predict(user_text)
    mapped = LABEL_TO_FUNC.get(clf_intent, clf_intent)
    mismatch = (mapped != model_func) and (conf >= CONF_THRESHOLD)
    return mismatch, clf_intent, conf
