import akshare as ak
import pandas as pd
import json
import re
from datetime import datetime, timedelta

CONFIG_FILE = "config.json"
OUTPUT_FILE = "premium_data.json"
LOOKBACK_DAYS = 365

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def get_a_stock_list():
    """全量 A 股代码-名称对照表"""
    df = ak.stock_info_a_code_name()
    df.columns = ["code", "name"]
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df

def get_hk_stock_list():
    """全量港股代码-名称列表"""
    df = ak.stock_hk_spot_em()
    df = df[["代码", "名称"]].rename(columns={"代码": "code", "名称": "name"})
    df["code"] = df["code"].astype(str).str.zfill(5)
    return df

def resolve_a_input(raw, a_list):
    """解析 A 股输入：纯数字视为代码，否则按名称精确匹配"""
    raw = raw.strip()
    if re.fullmatch(r"\d{6}", raw):
        row = a_list[a_list["code"] == raw]
        if row.empty:
            raise ValueError(f"A股代码 {raw} 不存在")
        return raw, row.iloc[0]["name"]
    # 名称匹配
    row = a_list[a_list["name"] == raw]
    if row.empty:
        raise ValueError(f"A股名称“{raw}”未找到，请改用 6 位代码")
    return row.iloc[0]["code"], raw

def resolve_hk_input(raw, hk_list):
    """解析 H 股输入：纯数字补零视为代码，否则按名称精确匹配"""
    raw = raw.strip()
    if re.fullmatch(r"\d{1,5}", raw):
        code = raw.zfill(5)
        row = hk_list[hk_list["code"] == code]
        if row.empty:
            raise ValueError(f"H股代码 {code} 不存在")
        return code, row.iloc[0]["name"]
    row = hk_list[hk_list["name"] == raw]
    if row.empty:
        raise ValueError(f"H股名称“{raw}”未找到，请改用 5 位代码")
    return row.iloc[0]["code"], raw

def get_hkd_cny_rate(start_date, end_date):
    """获取港元兑人民币历史中间价，返回 {日期: 汇率} 字典"""
    try:
        fx = ak.currency_boc_safe()
        fx = fx[["日期", "港元"]].copy()
        fx.columns = ["date", "rate"]
        fx["date"] = pd.to_datetime(fx["date"]).dt.strftime("%Y-%m-%d")
        # 中间价通常以 100 外币为单位，港元需除以 100
        fx["rate"] = pd.to_numeric(fx["rate"], errors="coerce") / 100
        fx = fx.dropna()
        fx = fx[(fx["date"] >= start_date) & (fx["date"] <= end_date)]
        return dict(zip(fx["date"], fx["rate"]))
    except Exception as e:
        print(f"[警告] 汇率获取失败，使用固定值 0.92：{e}")
        return {}

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
    # 汇率缺失时用最近可用值前向填充，仍缺失则用 0.92
    merged["rate"] = merged["rate"].ffill().bfill().fillna(0.92)
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

    print("拉取股票列表…")
    a_list = get_a_stock_list()
    hk_list = get_hk_stock_list()

    print("拉取汇率…")
    fx_map = get_hkd_cny_rate(start_date_fmt, end_date_fmt)

    result = {"updated": end_dt.strftime("%Y-%m-%d %H:%M:%S"), "stocks": []}

    for item in config:
        label = item["label"]
        try:
            a_code, a_name = resolve_a_input(item["a_input"], a_list)
            hk_code, hk_name = resolve_hk_input(item["hk_input"], hk_list)
            print(f"处理 {label}: A={a_code}({a_name}) H={hk_code}({hk_name})")

            a_df = fetch_price(a_code, "a", start_date, end_date)
            hk_df = fetch_price(hk_code, "hk", start_date, end_date)
            premium_df = calculate_premium(a_df, hk_df, fx_map)

            result["stocks"].append({
                "label": label,
                "a_code": a_code,
                "a_name": a_name,
                "hk_code": hk_code,
                "hk_name": hk_name,
                "data": premium_df.rename(columns={"date": "date", "premium": "value"}).to_dict(orient="records")
            })
        except Exception as e:
            print(f"[错误] {label}: {e}")
            result["stocks"].append({"label": label, "error": str(e), "data": []})

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已生成 {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
