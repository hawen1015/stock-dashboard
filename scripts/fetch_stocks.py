import yfinance as yf
import json
import requests
from datetime import datetime
import os

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
    "pe_below_avg":   True,
    "yield_above":    4.0,    # 殖利率超過這個%數
    "roe_above":      15.0,   # ROE 超過這個%數
}

def normalize_yield(raw_yield):
    """
    yfinance 台股殖利率有時回傳小數（0.018 = 1.8%），
    有時回傳已是百分比（1.06 = 1.06%）。
    正常殖利率不會超過 30%，超過 0.3 代表已是百分比格式。
    """
    if raw_yield is None:
        return 0.0
    if raw_yield > 0.3:
        return round(raw_yield, 2)      # 已經是 % 格式，直接用
    else:
        return round(raw_yield * 100, 2)  # 小數格式，乘 100

def score_stock(info):
    """四層指標評分，回傳 0–4 分"""
    score = 0
    reasons = []

    # 第一層：體質
    debt_ratio = info.get("debtToEquity", 999)
    if debt_ratio < 100:
        score += 1
        reasons.append("負債比合理")

    # 第二層：獲利
    roe = (info.get("returnOnEquity") or 0) * 100
    if roe >= ALERT_RULES["roe_above"]:
        score += 1
        reasons.append(f"ROE {roe:.1f}%")

    # 第三層：股利
    raw_yield = info.get("dividendYield") or 0
    div_yield_pct = normalize_yield(raw_yield)
    if div_yield_pct >= ALERT_RULES["yield_above"]:
        score += 1
        reasons.append(f"殖利率 {div_yield_pct:.1f}%")

    # 第四層：估值
    pe = info.get("trailingPE", 999)
    fwd_pe = info.get("forwardPE", 999)
    if pe and fwd_pe and fwd_pe < pe:
        score += 1
        reasons.append("遠期P/E優於現在")

    return score, reasons

def check_alert(info, score):
    """判斷是否觸發買進通知"""
    alerts = []
    raw_yield = info.get("dividendYield") or 0
    div_yield_pct = normalize_yield(raw_yield)

    if score >= 3:
        alerts.append(f"四層指標通過 {score}/4 層")
    if div_yield_pct >= ALERT_RULES["yield_above"]:
        alerts.append(f"殖利率 {div_yield_pct:.1f}% 達標")

    return alerts

def fetch_all():
    results = []

    for stock in WATCHLIST:
        print(f"抓取 {stock['name']}...")
        try:
            ticker = yf.Ticker(stock["code"])
            info = ticker.info

            price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            pe = info.get("trailingPE")
            pb = info.get("priceToBook")
            roe = (info.get("returnOnEquity") or 0) * 100
            raw_yield = info.get("dividendYield") or 0
            div_yield = normalize_yield(raw_yield)
            eps = info.get("trailingEps")

            score, reasons = score_stock(info)
            alerts = check_alert(info, score)

            results.append({
                "code":      stock["code"].replace(".TW", ""),
                "name":      stock["name"],
                "sector":    stock["sector"],
                "price":     round(price, 1) if price else None,
                "pe":        round(pe, 1) if pe else None,
                "pb":        round(pb, 2) if pb else None,
                "roe":       round(roe, 1) if roe else None,
                "div_yield": div_yield,
                "eps":       round(eps, 2) if eps else None,
                "score":     score,
                "reasons":   reasons,
                "alerts":    alerts,
                "has_alert": len(alerts) > 0,
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
                "has_alert": False,
            })

    return results

def send_line_notify(message):
    """發送 LINE 通知（需要在 GitHub Secrets 設定 LINE_TOKEN）"""
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
        "stocks": stocks,
        "alert_count": len(alert_stocks),
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
        send_line_notify(msg)
    else:
        send_line_notify("\n📊 股票監控：今日無買進訊號")

if __name__ == "__main__":
    main()
