import yfinance as yf
import json
import requests
from datetime import datetime
import os
import pandas as pd

# 觀察名單
WATCHLIST = [
    {"code": "2330.TW", "name": "台積電", "sector": "科技"},
    {"code": "2059.TW", "name": "川湖科技", "sector": "科技"},
    {"code": "2891.TW", "name": "中信金", "sector": "金融"},
    {"code": "2886.TW", "name": "兆豐金", "sector": "金融"},
    {"code": "2912.TW", "name": "統一超商", "sector": "消費"},
    {"code": "1476.TW", "name": "儒鴻企業", "sector": "消費"},
]

# 買進觸發條件
ALERT_RULES = {
    "yield_above":        4.0,   # 殖利率門檻 %
    "roe_above":         15.0,   # ROE 門檻 %
    "payout_max":        90.0,   # 發放率上限（超過代表借錢配息）
    "div_years_min":      5,     # 最少連續配息年數
}

def normalize_yield(raw_yield):
    """修正 yfinance 台股殖利率單位不一致的問題"""
    if raw_yield is None:
        return 0.0
    if raw_yield > 0.3:
        return round(raw_yield, 2)
    else:
        return round(raw_yield * 100, 2)

def get_div_consecutive_years(ticker):
    """計算連續配息年數（從最近一年往回算，中斷即停）"""
    try:
        divs = ticker.dividends
        if divs.empty:
            return 0
        # 取每年是否有配息
        div_years = sorted(set(divs.index.year), reverse=True)
        if not div_years:
            return 0
        # 從最近年份往回數，中斷就停
        consecutive = 1
        for i in range(1, len(div_years)):
            if div_years[i-1] - div_years[i] == 1:
                consecutive += 1
            else:
                break
        return consecutive
    except:
        return None

def get_roe_history(ticker):
    """
    計算過去 3-4 年的 ROE 歷史
    ROE = 淨利 / 股東權益
    回傳 list，例如 [36.2, 32.1, 28.5, 25.3]（由近到遠）
    """
    try:
        financials = ticker.financials      # 損益表，含淨利
        balance    = ticker.balance_sheet   # 資產負債表，含股東權益

        net_income = financials.loc["Net Income"] if "Net Income" in financials.index else None
        equity_row = None
        for label in ["Stockholders Equity", "Total Stockholders Equity",
                      "Common Stock Equity", "Total Equity Gross Minority Interest"]:
            if label in balance.index:
                equity_row = balance.loc[label]
                break

        if net_income is None or equity_row is None:
            return None

        # 對齊欄位（年份）
        common_cols = [c for c in net_income.index if c in equity_row.index]
        if not common_cols:
            return None

        roe_list = []
        for col in common_cols:
            ni = net_income[col]
            eq = equity_row[col]
            if eq and eq != 0:
                roe_list.append(round(float(ni) / float(eq) * 100, 1))

        return roe_list if roe_list else None
    except:
        return None

def get_payout_ratio(info):
    """
    計算發放率 = 每股股利 / EPS
    yfinance 有時直接提供 payoutRatio，沒有的話自己算
    """
    try:
        # 優先用 yfinance 提供的
        pr = info.get("payoutRatio")
        if pr and pr > 0:
            return round(pr * 100, 1)
        # 自己算
        div_rate = info.get("dividendRate") or 0
        eps = info.get("trailingEps") or 0
        if eps and eps > 0 and div_rate > 0:
            return round(div_rate / eps * 100, 1)
        return None
    except:
        return None

def score_stock(info, div_years, roe_history, payout_ratio):
    """四層指標評分，回傳 0–4 分"""
    score = 0
    reasons = []

    # 第一層：體質（負債比）
    debt_ratio = info.get("debtToEquity", 999)
    if debt_ratio and debt_ratio < 100:
        score += 1
        reasons.append("負債比合理")

    # 第二層：獲利（ROE）
    # 優先看歷史，否則看當期
    if roe_history and len(roe_history) >= 2:
        roe_ok = all(r >= ALERT_RULES["roe_above"] for r in roe_history)
        roe_display = roe_history[0]
        if roe_ok:
            score += 1
            reasons.append(f"ROE 連續達標（近期 {roe_display:.1f}%）")
        else:
            # 只有當期達標也給分，但標註
            if roe_display >= ALERT_RULES["roe_above"]:
                score += 1
                reasons.append(f"ROE {roe_display:.1f}%（歷史有波動）")
    else:
        roe = (info.get("returnOnEquity") or 0) * 100
        if roe >= ALERT_RULES["roe_above"]:
            score += 1
            reasons.append(f"ROE {roe:.1f}%")

    # 第三層：股利品質
    raw_yield = info.get("dividendYield") or 0
    div_yield_pct = normalize_yield(raw_yield)
    payout_ok = payout_ratio is None or payout_ratio <= ALERT_RULES["payout_max"]
    div_years_ok = div_years and div_years >= ALERT_RULES["div_years_min"]

    if div_yield_pct >= ALERT_RULES["yield_above"] and payout_ok and div_years_ok:
        score += 1
        reasons.append(f"殖利率 {div_yield_pct:.1f}%（配息 {div_years} 年）")
    elif div_yield_pct >= ALERT_RULES["yield_above"]:
        # 殖利率達標但其他條件不足，也給分但標註
        score += 1
        note = []
        if not payout_ok:
            note.append(f"發放率偏高 {payout_ratio}%")
        if not div_years_ok:
            note.append(f"配息年數不足（{div_years} 年）")
        reasons.append(f"殖利率 {div_yield_pct:.1f}%" + (f"⚠️ {', '.join(note)}" if note else ""))

    # 第四層：估值
    pe = info.get("trailingPE", 999)
    fwd_pe = info.get("forwardPE", 999)
    if pe and fwd_pe and fwd_pe < pe:
        score += 1
        reasons.append("遠期P/E優於現在")

    return score, reasons

def check_alert(info, score, div_years, payout_ratio):
    """判斷買進訊號與警示"""
    alerts = []
    warnings = []

    raw_yield = info.get("dividendYield") or 0
    div_yield_pct = normalize_yield(raw_yield)

    if score >= 3:
        alerts.append(f"四層指標通過 {score}/4 層")
    if div_yield_pct >= ALERT_RULES["yield_above"]:
        alerts.append(f"殖利率 {div_yield_pct:.1f}% 達標")

    # 警示條件
    if payout_ratio and payout_ratio > 90:
        warnings.append(f"⚠️ 發放率 {payout_ratio}%，注意借錢配息風險")
    if div_years is not None and div_years < ALERT_RULES["div_years_min"]:
        warnings.append(f"⚠️ 配息年數僅 {div_years} 年，歷史不足")

    return alerts, warnings

def fetch_all():
    results = []

    for stock in WATCHLIST:
        print(f"抓取 {stock['name']}...")
        try:
            ticker = yf.Ticker(stock["code"])
            info = ticker.info

            price    = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            pe       = info.get("trailingPE")
            pb       = info.get("priceToBook")
            roe      = (info.get("returnOnEquity") or 0) * 100
            raw_yield = info.get("dividendYield") or 0
            div_yield = normalize_yield(raw_yield)
            eps      = info.get("trailingEps")

            # 新增：配息年數、ROE歷史、發放率
            div_years    = get_div_consecutive_years(ticker)
            roe_history  = get_roe_history(ticker)
            payout_ratio = get_payout_ratio(info)

            score, reasons       = score_stock(info, div_years, roe_history, payout_ratio)
            alerts, warnings     = check_alert(info, score, div_years, payout_ratio)

            results.append({
                "code":         stock["code"].replace(".TW", ""),
                "name":         stock["name"],
                "sector":       stock["sector"],
                "price":        round(price, 1) if price else None,
                "pe":           round(pe, 1) if pe else None,
                "pb":           round(pb, 2) if pb else None,
                "roe":          round(roe, 1) if roe else None,
                "roe_history":  roe_history,
                "div_yield":    div_yield,
                "div_years":    div_years,
                "payout_ratio": payout_ratio,
                "eps":          round(eps, 2) if eps else None,
                "score":        score,
                "reasons":      reasons,
                "alerts":       alerts,
                "warnings":     warnings,
                "has_alert":    len(alerts) > 0,
            })

        except Exception as e:
            print(f"  ⚠️ 抓取 {stock['name']} 失敗：{e}")
            results.append({
                "code":      stock["code"].replace(".TW", ""),
                "name":      stock["name"],
                "sector":    stock["sector"],
                "error":     str(e),
                "score":     0,
                "alerts":    [],
                "warnings":  [],
                "has_alert": False,
            })

    return results

def send_line_notify(message):
    token = os.environ.get("LINE_TOKEN")
    if not token:
        print("沒有設定 LINE_TOKEN，跳過通知")
        return
    requests.post(
        "https://notify-api.line.me/api/notify",
        headers={"Authorization": f"Bearer {token}"},
        data={"message": message}
    )

def main():
    print("開始抓取股票資料...")
    stocks = fetch_all()

    alert_stocks = [s for s in stocks if s.get("has_alert")]

    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks":       stocks,
        "alert_count":  len(alert_stocks),
    }

    with open("data/stocks.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完成，共 {len(stocks)} 檔，{len(alert_stocks)} 檔有訊號")

    if alert_stocks:
        msg = "\n📊 股票監控每日更新\n"
        for s in alert_stocks:
            msg += f"\n✅ {s['name']}（{s['code']}）\n"
            for a in s["alerts"]:
                msg += f"   · {a}\n"
            for w in s.get("warnings", []):
                msg += f"   {w}\n"
        send_line_notify(msg)
    else:
        send_line_notify("\n📊 股票監控：今日無買進訊號")

if __name__ == "__main__":
    main()
