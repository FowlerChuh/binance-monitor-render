import os
import sys
import signal
import time
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
from datetime import datetime

# ----------------- 配置（全部来自环境变量） -----------------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
PORTFOLIO_ID = os.environ.get("PORTFOLIO_ID", "5075281354358777856").strip()
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "5"))
# 检测到新成交后进入「爆发模式」，用更短的间隔抓同一批连续下单
BURST_INTERVAL = int(os.environ.get("BURST_INTERVAL", "3"))
BURST_WINDOW = int(os.environ.get("BURST_WINDOW", "120"))
# 轮询间隔随机抖动比例，避免固定周期特征被风控识别
JITTER = float(os.environ.get("JITTER", "0.15"))
# MAX_RUNTIME=0 表示永久运行；GitHub Actions 等场景可设为 21000（约 5h50m）
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))
SEND_STARTUP = os.environ.get("SEND_STARTUP", "1").lower() not in ("0", "false", "no")
# ------------------------------------------------------------

if not TG_BOT_TOKEN or not TG_CHAT_ID:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
    logging.error("缺少 TG_BOT_TOKEN 或 TG_CHAT_ID 环境变量，拒绝启动。")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# /bapi/ 必须请求主站域名，不能用 api.binance.com，否则必出 403
ORDER_HISTORY_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history"

# 补全防封反爬专属请求头（避开 Cloudflare 403）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.binance.com",
    "Referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{PORTFOLIO_ID}",
    "clienttype": "web",
    "lang": "zh-CN",
}

pending_tg_messages = []

START_TIME = time.time()
shutdown_requested = False


def should_stop() -> bool:
    if shutdown_requested:
        return True
    if MAX_RUNTIME > 0 and (time.time() - START_TIME) >= MAX_RUNTIME:
        logging.info(f"已达到 MAX_RUNTIME={MAX_RUNTIME}s，准备正常退出。")
        return True
    return False


def handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    logging.info(f"收到退出信号 {signum}，正在收尾...")


def create_resilient_session():
    """创建稳定 Session 并配置浏览器行为"""
    s = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


session = create_resilient_session()
is_offline = False


def reset_network_session():
    global session
    try:
        session.close()
    except Exception:
        pass
    session = create_resilient_session()


def send_telegram_msg(text: str) -> bool:
    global pending_tg_messages
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_notification": False,
    }
    try:
        resp = session.post(url, json=payload, timeout=(5, 10))
        if resp.status_code == 200:
            return True
        else:
            logging.error(f"TG消息被拒绝: {resp.text}")
    except Exception:
        pass

    if text not in pending_tg_messages:
        pending_tg_messages.append(text)
    return False


def flush_pending_messages():
    global pending_tg_messages
    if not pending_tg_messages:
        return

    logging.info(f"正在补发断网期间积压的 {len(pending_tg_messages)} 条交易推送...")
    remains = []
    for msg in pending_tg_messages:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_notification": False,
        }
        try:
            resp = session.post(url, json=payload, timeout=(5, 10))
            if resp.status_code == 200:
                time.sleep(0.5)
                continue
        except Exception:
            pass
        remains.append(msg)

    pending_tg_messages = remains


def fetch_orders_with_reconnect():
    global is_offline
    payload = {
        "portfolioId": str(PORTFOLIO_ID),
        "pageNumber": 1,
        "pageSize": 5,
    }

    while True:
        if should_stop():
            return []

        try:
            resp = session.post(
                ORDER_HISTORY_URL, json=payload, headers=HEADERS, timeout=(5, 10)
            )

            # 状态码 200，说明成功
            if resp.status_code == 200:
                if is_offline:
                    logging.info("✅ 网络已重新连接恢复！恢复正常监控...")
                    is_offline = False
                    flush_pending_messages()

                data = resp.json()
                if data.get("success") and "data" in data and "list" in data["data"]:
                    return data["data"]["list"]
                return []

            # 如果遇到 403，打印出详细原因
            elif resp.status_code == 403:
                logging.warning("⚠️ 遭遇币安安全盾检测 (403)，等待 15 秒后重试...")
                time.sleep(15)
                return []

            # 如果被 429 频控
            elif resp.status_code == 429:
                logging.warning("触发访问频率限制 (429)，休眠 20 秒...")
                time.sleep(20)
                return []

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if not is_offline:
                logging.warning(f"⚠️ 检测到网络波动: {e.__class__.__name__}，正在等待恢复...")
                is_offline = True

            reset_network_session()
            time.sleep(5)
            continue
        except Exception as e:
            logging.error(f"意外异常: {e}")
            time.sleep(3)
            return []


def format_action(order):
    side = order.get("side")
    pos_side = order.get("positionSide")

    if pos_side == "LONG":
        return "🟢 【开多】" if side == "BUY" else "🔴 【平多止盈/止损】"
    elif pos_side == "SHORT":
        return "🔴 【开空】" if side == "SELL" else "🟢 【平空止盈/止损】"
    return f"{side} {pos_side}"


def get_quote_asset(symbol: str) -> str:
    if not symbol:
        return "USDT"
    if symbol.endswith("USDC"):
        return "USDC"
    return "USDT"


def calculate_order_value(order):
    symbol = order.get("symbol", "")
    unit = get_quote_asset(symbol)

    try:
        price = float(order.get("avgPrice", 0))
        qty = float(order.get("executedQty", 0))
    except (ValueError, TypeError):
        price, qty = 0.0, 0.0

    cum_quote = order.get("cumQuote")
    if cum_quote is not None and float(cum_quote) > 0:
        order_value = float(cum_quote)
    else:
        order_value = price * qty

    leverage = order.get("leverage")
    margin_val = None
    if leverage and float(leverage) > 0:
        margin_val = order_value / float(leverage)

    return price, qty, order_value, leverage, margin_val, unit


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logging.info("🦅 熬鹰资本实时交易流水监控系统已启动...")
    logging.info(
        f"配置: portfolio={PORTFOLIO_ID} interval={CHECK_INTERVAL}s "
        f"burst={BURST_INTERVAL}s/{BURST_WINDOW}s jitter=±{int(JITTER * 100)}% "
        f"max_runtime={MAX_RUNTIME or '∞'}s startup_msg={SEND_STARTUP}"
    )

    seen_order_ids = set()
    initial_orders = []

    while not initial_orders and not should_stop():
        initial_orders = fetch_orders_with_reconnect()
        if not initial_orders:
            time.sleep(2)

    if should_stop():
        logging.info("启动阶段收到退出指令，结束。")
        return

    for o in initial_orders:
        uid = f"{o.get('orderUpdateTime')}_{o.get('symbol')}_{o.get('executedQty')}"
        seen_order_ids.add(uid)

    first = initial_orders[0]
    f_price, f_qty, f_val, _, _, f_unit = calculate_order_value(first)
    latest_desc = f"{first.get('symbol')} {format_action(first)} @ {f_price} (金额: `{f_val:,.2f} {f_unit}`)"
    logging.info(f"历史订单已同步，最近一笔是: {latest_desc}")

    if SEND_STARTUP:
        send_telegram_msg(
            f"🚀 *【熬鹰资本】实时跟单雷达已启动！*\n\n"
            f"当前最新动态：{latest_desc}\n"
            f"监控状态：`7x24小时监听（带断网自愈能力）`"
        )

    burst_until = 0.0

    while not should_stop():
        orders = fetch_orders_with_reconnect()
        if orders:
            new_orders = []
            for o in orders:
                uid = f"{o.get('orderUpdateTime')}_{o.get('symbol')}_{o.get('executedQty')}"
                if uid not in seen_order_ids:
                    new_orders.append((uid, o))

            if new_orders:
                # 有人在下单，进入爆发模式，抓同一批连续成交
                burst_until = time.time() + BURST_WINDOW

            for uid, o in reversed(new_orders):
                seen_order_ids.add(uid)
                action = format_action(o)
                symbol = o.get("symbol", "")
                price, qty, order_value, leverage, margin_val, unit = calculate_order_value(o)
                pnl = float(o.get("totalPnl", 0))
                time_str = datetime.fromtimestamp(o.get("orderUpdateTime", 0) / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                msg = (
                    f"🦅 *熬鹰资本最新交易提醒！*\n\n"
                    f"动作：*{action}*\n"
                    f"标的：`{symbol}`\n"
                    f"成交均价：`{price}`\n"
                    f"成交数量：`{qty}`\n"
                    f"💵 *开单金额（货值）*：`{order_value:,.2f} {unit}`\n"
                )

                if margin_val is not None:
                    msg += f"杠杆倍数：`{leverage}x`\n"
                    msg += f"预估占用本金：`{margin_val:,.2f} {unit}`\n"

                if pnl != 0:
                    pnl_emoji = "💰" if pnl > 0 else "🩸"
                    msg += f"已实现盈亏：{pnl_emoji} `{pnl:+.2f} {unit}`\n"

                msg += f"成交时间：`{time_str}`"

                lag = max(0.0, time.time() - o.get("orderUpdateTime", 0) / 1000)
                logging.info(
                    f"检测到新交易: {symbol} {action} 金额: {order_value:,.2f} {unit} "
                    f"(成交延迟 {lag:.1f}s)"
                )
                send_telegram_msg(msg)

        base = BURST_INTERVAL if time.time() < burst_until else CHECK_INTERVAL
        time.sleep(max(1.0, base * random.uniform(1 - JITTER, 1 + JITTER)))

    logging.info("监控已停止，补发积压消息后退出。")
    flush_pending_messages()


if __name__ == "__main__":
    main()
