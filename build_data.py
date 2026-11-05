import akshare as ak
import pandas as pd
import json
from datetime import datetime, timedelta

CONFIG_FILE = "config.json"
OUTPUT_FILE = "premium_data.json"
LOOKBACK_DAYS = 365

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def get_hkd_cny_rate(start_date, end_date):
    """获取港元兑人民币历史中间价，返回 {日期: 汇率} 字典。
    获取失败或区间为空时抛出异常，不使用固定回退值。
    """
    fx = ak.currency_boc_safe()
    fx = fx[["日期", "港元"]].copy()
    fx.columns = ["date", "rate"]
    fx["date"] = pd.to_datetime(fx["date"]).dt.strftime("%Y-%m-%d")
    # 港元为直接标价法：100 港元折合人民币，除以 100 得到 1 港元对应的人民币
    fx["rate"] = pd.to_numeric(fx["rate"], errors="coerce") / 100
    fx = fx.dropna()
    fx = fx[(fx["date"] >= start_date) & (fx["date"] <= end_date)]
    if fx.empty:
        raise ValueError("汇率数据区间为空，无法计算溢价率")
    return dict(zip(fx["date"], fx["rate"]))

def fetch_price(code, market, start_date, end_date):
    """获取历史日线收盘价"""
    if market == "a":
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start_date, end_date=end_date, adjust="qfq")
    else:
        df = ak.stock_hk_hist(symbol=code, period="daily",
                               start_date=start_date, end_date=end_date, adjust="qfq")
    if df.empty:
        raise ValueError(f"{market.upper()}股 {code} 行情为空")
    df = df[["日期", "收盘"]].copy()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df

def calculate_premium(a_df, hk_df, fx_map):
    merged = pd.merge(a_df, hk_df, on="date", how="inner", suffixes=("_a", "_hk"))
    if merged.empty:
        raise ValueError("A/H 股无共同交易日")
    merged["rate"] = merged["date"].map(fx_map)
    # 仅对局部缺失做前向/后向填充；若仍有缺失则报错
    merged["rate"] = merged["rate"].ffill().bfill()
    if merged["rate"].isna().any():
        raise ValueError("部分交易日汇率缺失，无法计算溢价率")
    merged["premium"] = (merged["close_a"] / (merged["close_hk"] * merged["rate"]) - 1) * 100
    merged["premium"] = merged["premium"].round(2)
    return merged[["date", "premium"]]

def main():
    config = load_config()
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_date = start_dt.strftime("%Y%m%d")
    end_date = end_dt.strftime("%Y%m%d")
    start_date_fmt = start_dt.strftime("%Y-%m-%d")
    end_date_fmt = end_dt.strftime("%Y-%m-%d")

    print("拉取汇率…")
    try:
        fx_map = get_hkd_cny_rate(start_date_fmt, end_date_fmt)
        print(f"汇率获取成功，共 {len(fx_map)} 条")
    except Exception as e:
        print(f"[致命错误] 汇率获取失败：{e}")
        result = {"updated": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                  "error": f"汇率获取失败：{e}", "stocks": []}
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        raise

    result = {"updated": end_dt.strftime("%Y-%m-%d %H:%M:%S"), "stocks": []}

    for item in config:
        label = item["label"]
        a_code = str(item["a_code"]).zfill(6)
        hk_code = str(item["hk_code"]).zfill(5)
        try:
            print(f"处理 {label}: A={a_code} H={hk_code}")
            a_df = fetch_price(a_code, "a", start_date, end_date)
            hk_df = fetch_price(hk_code, "hk", start_date, end_date)
            premium_df = calculate_premium(a_df, hk_df, fx_map)

            result["stocks"].append({
                "label": label,
                "a_code": a_code,
                "a_name": item.get("a_name", ""),
                "hk_code": hk_code,
                "hk_name": item.get("hk_name", ""),
                "data": premium_df.to_dict(orient="records")
            })
        except Exception as e:
            print(f"[错误] {label}: {e}")
            result["stocks"].append({
                "label": label, "a_code": a_code, "hk_code": hk_code,
                "a_name": item.get("a_name", ""), "hk_name": item.get("hk_name", ""),
                "error": str(e), "data": []
            })

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已生成 {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
