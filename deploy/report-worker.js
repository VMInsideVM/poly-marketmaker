/**
 * deploy/report-worker.js — 周报中继（Cloudflare Worker）。
 *
 * 客户端 POST 结构化周报数据到这里，本 Worker 用只存在环境变量里的 Telegram token 转发。
 * 客户端一概不持有 token / chat_id，所以扒源码或反编译 exe 都拿不到它们。
 *
 * 环境变量（Cloudflare 控制台 Settings → Variables）。前三个必填，后两个平时不用配：
 *   TG_TOKEN     Secret 类型。Telegram bot token。
 *   TG_CHAT_ID   接收周报的 chat id。
 *   CLIENT_KEY   与客户端 config.py 的 REPORT_KEY 相同。不是鉴权凭证，只挡随机扫描。
 *   ENABLED      止损开关。设成 "0" 立即全局停止转发；不设或非 "0" 即为开启。
 *   ALLOW        钱包地址白名单，逗号分隔、小写。**可选，默认留空 = 不检查**。
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
    // 止损总开关,放在一切处理之前:出事时把 ENABLED 设成 "0",转发全停(Cloudflare 变量有
    // 几十秒传播延迟,不是瞬时的)。部署时不必配这个变量,未设即为开启。
    // 用 String().trim() 而不是 === "0" 直比:填成 "0 "(多一个空格,手机上很容易)会让急停
    // **静默失效**,而按下的人以为已经停了——这种失败模式的代价太高,宁可判宽一点。
    if (String(env.ENABLED ?? "").trim() === "0") {
      return new Response("no", { status: 503 });
    }
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
    if (!p || typeof p !== "object") return new Response("no", { status: 400 });

    // 白名单**可选**:留空(默认)= 不做地址检查,只认 CLIENT_KEY。作者并不知道使用者有哪些
    // 钱包地址,而且他们会随时导入/删除,要求逐个登记等于派一个维护不起的活,还会在朋友加了
    // 新钱包时让周报无声无息地断掉。填了才逐个校验(senders 里任意一个命中即放行),留给
    // 「出事之后想收紧」时用——那时从正常收到的周报里就能看出合法地址长什么样。
    const allowRaw = String(env.ALLOW || "").trim();
    if (allowRaw) {
      const allow = new Set(
        allowRaw
          .split(",")
          .map((a) => a.trim().toLowerCase())
          .filter(Boolean)
      );
      const senders = Array.isArray(p.senders) ? p.senders : [];
      if (!senders.some((a) => allow.has(String(a).toLowerCase()))) {
        return new Response("no", { status: 403 });
      }
    }

    // 三个会原样进消息的日期字段:不合格式整条拒绝(否则等于任意文本注入)。
    for (const d of [p.week_start, p.week_end, p.since_date]) {
      if (!DATE_RE.test(String(d))) return new Response("no", { status: 400 });
    }

    const text = buildText(p);
    let resp;
    try {
      resp = await fetch(
        `https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ chat_id: env.TG_CHAT_ID, text }),
        }
      );
    } catch {
      // 网络层失败(DNS/连接):按转发失败处理,绝不把异常抛给 Cloudflare 默认处理。
      return new Response("no", { status: 502 });
    }
    // 只回状态,不回显 Telegram 的响应体(可能含 token 相关描述)。
    return new Response(resp.ok ? "ok" : "no", { status: resp.ok ? 200 : 502 });
  },
};
