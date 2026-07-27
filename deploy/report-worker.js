/**
 * deploy/report-worker.js — 周报中继（Cloudflare Worker）。
 *
 * 客户端 POST 结构化周报数据到这里，本 Worker 用只存在环境变量里的 Telegram token 转发。
 * 客户端一概不持有 token / chat_id，所以扒源码或反编译 exe 都拿不到它们。
 *
 * 环境变量（Cloudflare 控制台 Settings → Variables）：
 *   TG_TOKEN     Secret 类型。Telegram bot token。
 *   TG_CHAT_ID   接收周报的 chat id。
 *   CLIENT_KEY   与客户端 config.py 的 REPORT_KEY 相同。不是鉴权凭证，只挡随机扫描。
 *   ALLOW        允许的钱包地址，逗号分隔，小写。清空它即可全局止损（30 秒生效，不用发版）。
 *
 * 安全要点：周报文本在这里拼，客户端只传数字。凡是会原样进入消息的字符串都必须先过关卡 ——
 * 三个日期字段用正则校验（不匹配整条拒绝），label 来自使用者可编辑的钱包备注、是自由文本，
 * 必须剥控制字符并截断。少做任何一样，都等于把「让 bot 发任意内容」的能力还回去，而那正是
 * 2026-07-27 那次 token 泄露事故的形态。
 */

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) && Math.abs(n) < 1e9 ? n : 0;
}

function money(v) {
  const n = num(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

function label(v) {
  return String(v ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .slice(0, 20);
}

function buildText(p) {
  const lines = [
    `📊 做市周报 · ${p.week_start} ~ ${p.week_end}`,
    "",
    "【每日净利润】",
  ];
  for (const row of (p.daily_nets || []).slice(0, 7)) {
    if (!Array.isArray(row) || !DATE_RE.test(String(row[0]))) continue;
    lines.push(`${String(row[0]).slice(5)}  ${money(row[1])}`);
  }
  const t = p.week_totals || {};
  lines.push(
    "",
    "【本周汇总】",
    `做市奖励 ${money(t.reward)} · 返佣 ${money(t.rebate)} · 卖出盈利 ${money(
      t.sell_profit
    )}`,
    `亏损 ${money(-num(t.loss))} · 手续费 ${money(-num(t.fee))} · 净利润 ${money(
      t.net
    )}`,
    "",
    `【累计净利润】(自 ${p.since_date})  ${money(p.cumulative_net)}`
  );
  const pw = (p.per_wallet || []).slice(0, 50);
  if (pw.length) {
    lines.push("", "【各钱包本周净利润】");
    for (const w of pw) {
      lines.push(`${label(w && w.label)}  ${money(w && w.net)}`);
    }
  }
  return lines.join("\n");
}

export default {
  async fetch(req, env) {
    if (req.method !== "POST") return new Response("no", { status: 405 });
    if (req.headers.get("x-mm-key") !== env.CLIENT_KEY) {
      return new Response("no", { status: 403 });
    }

    let p;
    try {
      p = await req.json();
    } catch {
      return new Response("no", { status: 400 });
    }

    // 白名单:senders 里任意一个地址在 ALLOW 中即放行(使用者增删钱包不该让推送失效)。
    const allow = new Set(
      String(env.ALLOW || "")
        .split(",")
        .map((a) => a.trim().toLowerCase())
        .filter(Boolean)
    );
    const senders = Array.isArray(p.senders) ? p.senders : [];
    if (!senders.some((a) => allow.has(String(a).toLowerCase()))) {
      return new Response("no", { status: 403 });
    }

    // 三个会原样进消息的日期字段:不合格式整条拒绝(否则等于任意文本注入)。
    for (const d of [p.week_start, p.week_end, p.since_date]) {
      if (!DATE_RE.test(String(d))) return new Response("no", { status: 400 });
    }

    const resp = await fetch(
      `https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: env.TG_CHAT_ID, text: buildText(p) }),
      }
    );
    // 只回状态,不回显 Telegram 的响应体(可能含 token 相关描述)。
    return new Response(resp.ok ? "ok" : "no", { status: resp.ok ? 200 : 502 });
  },
};
