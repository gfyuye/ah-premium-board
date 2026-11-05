import akshare as ak
import pandas as pd
import json
import os
from datetime import datetime, timedelta

CONFIG_FILE = "config.json"
DATA_DIR = "data"
LOOKBACK_DAYS = 365

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def stock_file_path(a_code):
    return os.path.join(DATA_DIR, f"{a_code}.json")

def load_existing(a_code):
    path = stock_file_path(a_code)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        old = json.load(f)
    return old.get("data", [])

def save_stock(a_code, payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(stock_file_path(a_code), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def save_manifest(config):
    manifest = []
    for item in config:
        manifest.append({
            "label": item["label"],
            "a_code": str(item["a_code"]).zfill(6),
            "hk_code": str(item["hk_code"]).zfill(5),
            "a_name": item.get("a_name", ""),
            "hk_name": item.get("hk_name", "")
        })
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def get_hkd_cny_rate(start_date, end_date):
    """港元兑人民币历史中间价，失败时抛异常，不使用固定回退值。"""
    fx = ak.currency_boc_safe()
    fx = fx[["日期", "港元"]].copy()
    fx.columns = ["date", "rate"]
    fx["date"] = pd.to_datetime(fx["date"]).dt.strftime("%Y-%m-%d")
    fx["rate"] = pd.to_numeric(fx["rate"], errors="coerce") / 100
    fx = fx.dropna()
    fx = fx[(fx["date"] >= start_date) & (fx["date"] <= end_date)]
    if fx.empty:
        raise ValueError("汇率数据区间为空")
    return dict(zip(fx["date"], fx["rate"]))

def fetch_a_price(code, start_date, end_date):
    """A 股：东财主源，腾讯备源。腾讯需 sh/sz 前缀，返回列 date/close。"""
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start_date, end_date=end_date, adjust="qfq")
        df = df[["日期", "收盘"]].copy()
        df.columns = ["date", "close"]
    except Exception as e:
        print(f"  [A股] 东财失败，切换腾讯源：{e}")
        prefix = "sh" if code.startswith("6") else "sz"
        df = ak.stock_zh_a_hist_tx(symbol=prefix + code,
                                    start_date=start_date, end_date=end_date, adjust="qfq")
        # 腾讯源返回列名以 date/close 为准 [citation:6]
        df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df

def fetch_hk_price(code, start_date, end_date):
    """港股：东财主源，新浪备源。新浪 stock_hk_daily 返回 date/close。"""
    try:
        df = ak.stock_hk_hist(symbol=code, period="daily",
                               start_date=start_date, end_date=end_date, adjust="qfq")
        df = df[["日期", "收盘"]].copy()
        df.columns = ["date", "close"]
    except Exception as e:
        print(f"  [港股] 东财失败，切换新浪源：{e}")
        df = ak.stock_hk_daily(symbol=code, adjust="qfq")
        # 新浪源列名以实际返回为准，常见为 date/close
        df = df[["date", "close"]].copy()
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df

def calculate_premium(a_df, hk_df, fx_map):
    merged = pd.merge(a_df, hk_df, on="date", how="inner", suffixes=("_a", "_hk"))
    if merged.empty:
        raise ValueError("A/H 股无共同交易日")
    merged["rate"] = merged["date"].map(fx_map)
    merged["rate"] = merged["rate"].ffill().bfill()
    if merged["rate"].isna().any():
        raise ValueError("部分交易日汇率缺失")
    merged["premium"] = (merged["close_a"] / (merged["close_hk"] * merged["rate"]) - 1) * 100
    merged["premium"] = merged["premium"].round(2)
    return merged[["date", "premium"]].rename(columns={"premium": "value"})

def merge_records(old_records, new_records):
    """合并新旧记录，按日期去重（新覆盖旧），升序排列。"""
    seen = {}
    for r in old_records + new_records:
        seen[r["date"]] = r
    return [seen[d] for d in sorted(seen.keys())]

def main():
    config = load_config()
    save_manifest(config)

    end_dt = datetime.now()
    end_date = end_dt.strftime("%Y%m%d")
    end_date_fmt = end_dt.strftime("%Y-%m-%d")

    print("拉取汇率…")
    try:
        start_dt_rate = end_dt - timedelta(days=LOOKBACK_DAYS + 30)
        fx_map = get_hkd_cny_rate(start_dt_rate.strftime("%Y-%m-%d"), end_date_fmt)
        print(f"汇率获取成功，共 {len(fx_map)} 条")
    except Exception as e:
        print(f"[致命错误] 汇率获取失败：{e}")
        return

    for item in config:
        label = item["label"]
        a_code = str(item["a_code"]).zfill(6)
        hk_code = str(item["hk_code"]).zfill(5)

        old_records = load_existing(a_code)

        if old_records:
            last_date = old_records[-1]["date"]
            start_dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
        else:
            start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)

        start_date = start_dt.strftime("%Y%m%d")

        if start_date > end_date:
            print(f"{label} 已是最新，跳过")
            continue

        try:
            print(f"处理 {label}: 增量 {start_date} ~ {end_date}")
            a_df = fetch_a_price(a_code, start_date, end_date)
            hk_df = fetch_hk_price(hk_code, start_date, end_date)
            new_premium = calculate_premium(a_df, hk_df, fx_map)

            merged = merge_records(old_records, new_premium.to_dict(orient="records"))

            save_stock(a_code, {
                "label": label,
                "a_code": a_code,
                "hk_code": hk_code,
                "a_name": item.get("a_name", ""),
                "hk_name": item.get("hk_name", ""),
                "updated": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "data": merged
            })
            print(f"  {label} 成功，共 {len(merged)} 条")
        except Exception as e:
            print(f"[错误] {label}: {e}，保留旧数据")
            save_stock(a_code, {
                "label": label,
                "a_code": a_code,
                "hk_code": hk_code,
                "a_name": item.get("a_name", ""),
                "hk_name": item.get("hk_name", ""),
                "updated": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "error": str(e),
                "data": old_records
            })

    print("完成")

if __name__ == "__main__":
    main()
