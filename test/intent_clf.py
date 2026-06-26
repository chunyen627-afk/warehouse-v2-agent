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
    except Exception:
        return "unknown", 1.0
    return intent, conf


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
